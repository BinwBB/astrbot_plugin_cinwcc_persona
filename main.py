"""
角色状态管理插件
维护好感度、淫乱度、恶堕值、情绪等跨轮持久化数值。
通过 LLM 请求/响应钩子实现状态注入和更新。

设计原则：
  - SKILL.md / SUPPLEMENT.md 是人设默认值的唯一来源
  - 配置项默认值 = SKILL.md 默认值，用户修改即覆盖
  - 启动时验证 SKILL.md 文件存在性，输出同步状态日志
"""

import asyncio
import json
import os
import sqlite3
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api import logger, AstrBotConfig

# 状态文件实际写入 data/plugin_data/lili_state/（按 umo 区分）
STATE_DIR = Path("data/plugin_data/lili_state")
SKILL_DIR = Path("data/skills/lili_persona")
PLUGIN_SKILL_DIR = Path("data/plugins/astrbot_plugin_lili_state/lili_persona")


def _ensure_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _state_path(umo: str) -> Path:
    safe = umo.replace(":", "_").replace("/", "_").replace("\\", "_")
    return STATE_DIR / f"{safe}.json"


def _default_state(config: dict = None) -> dict:
    cfg = config or {}
    return {
        "affection": cfg.get("initial_affection", 65),
        "lewdness": cfg.get("initial_lewdness", 20),
        "depravity": cfg.get("initial_depravity", 0),
        "emotion": 60,
        "social_need": 20,
        "last_interaction_time": 0.0,
        "stutter_done": False,
        "conversation_log": [],
        "_last_date": "",
    }


# ── 内存状态缓存 ──
# 设计要点（延迟红线）：
#   1. 请求路径只读 _STATE_CACHE，不做任何文件 IO
#   2. 启动时 prewarm_states() 一次性把磁盘状态读进内存
#   3. 写盘走后台协程 + 防抖合并，实际落盘丢到线程里，不占事件循环
_STATE_CACHE: dict = {}      # 状态文件名 stem -> 状态 dict
_DIRTY: set = set()          # 待落盘的 key
_SKIP_LOG: set = set()       # 落盘时不写 conversation_log 的 key
_FLUSH_WORKER = None         # 后台落盘协程
_FLUSH_EVENT = None          # 通知后台协程有新改动
FLUSH_DELAY_SECS = 1.5       # 防抖窗口：这段时间内的多次改动合并成一次写盘
STATE_SCHEMA_VERSION = 2     # 状态结构版本，升级字段时 +1，靠它触发就地补齐


def _cache_key(umo: str) -> str:
    return umo.replace(":", "_").replace("/", "_").replace("\\", "_")


def _path_by_key(key: str) -> Path:
    return STATE_DIR / f"{key}.json"


def _normalize_state(state: dict, config: dict = None) -> dict:
    """纯内存补齐字段 + 数值校验，不写盘。"""
    defaults = _default_state(config)
    for k, v in defaults.items():
        state.setdefault(k, v)
    state.setdefault("last_seen", {})
    for k in ("affection", "lewdness", "depravity", "emotion", "social_need"):
        v = state.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            state[k] = defaults[k]
    v = state.get("last_interaction_time", 0.0)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        state["last_interaction_time"] = 0.0
    if not isinstance(state.get("conversation_log"), list):
        state["conversation_log"] = []
    if not isinstance(state.get("last_seen"), dict):
        state["last_seen"] = {}
    state["_schema_version"] = STATE_SCHEMA_VERSION
    return state


def prewarm_states(config: dict = None) -> int:
    """启动时预热：把持久化状态读进内存。只在初始化调用。

    sqlite 后端读快照表；json 后端读 .json，读不到再看 .json.bak
    （迁移后原文件会改名成 .bak，切回 json 后端时靠它兜底）。
    """
    _ensure_dir()
    loaded = 0

    if _BACKEND == "sqlite":
        for key, st in _load_snapshots_db().items():
            if key in _STATE_CACHE:
                continue
            _STATE_CACHE[key] = _normalize_state(st, config)
            loaded += 1
        for key, st in _STATE_CACHE.items():
            _LAST_PERSISTED[key] = {d: int(st.get(d, 0) or 0) for d in EVENT_DIMS}
        return loaded

    for pattern in ("*.json", "*.json.bak*"):
        try:
            files = sorted(STATE_DIR.glob(pattern))
        except Exception as e:
            logger.warning(f"扫描状态目录失败({pattern}): {e}")
            continue
        for path in files:
            key = _key_from_name(path.name)
            if key in _STATE_CACHE:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                if not isinstance(st, dict):
                    st = {}
            except Exception as e:
                logger.warning(f"读取状态文件失败 {path.name}: {e}")
                continue
            _STATE_CACHE[key] = _normalize_state(st, config)
            loaded += 1
    for key, st in _STATE_CACHE.items():
        _LAST_PERSISTED[key] = {d: int(st.get(d, 0) or 0) for d in EVENT_DIMS}
    return loaded


def get_state(umo: str, config: dict = None) -> dict:
    """请求路径专用：只查内存。没命中就当作新会话，绝不读盘。

    命中后仍检查结构版本：版本不符就地补齐字段（纯内存），
    这样热重载/结构升级时不会出现"缓存里是旧结构、新字段永远补不上"。
    """
    key = _cache_key(umo)
    st = _STATE_CACHE.get(key)
    if st is not None and st.get("_schema_version") == STATE_SCHEMA_VERSION:
        return st
    if st is None:
        st = _default_state(config)
        st["_schema_version"] = STATE_SCHEMA_VERSION
        _STATE_CACHE[key] = st
    else:
        _normalize_state(st, config)   # 旧结构就地补齐，几微秒
    mark_dirty(key)
    return st


def _snapshot(state: dict, key: str) -> dict:
    """浅拷贝快照，供写盘线程使用（避免写盘期间状态被改）。"""
    snap = {}
    for k, v in state.items():
        if k == "conversation_log" and key in _SKIP_LOG:
            continue
        if isinstance(v, dict):
            snap[k] = dict(v)
        elif isinstance(v, list):
            snap[k] = [dict(i) if isinstance(i, dict) else i for i in v]
        else:
            snap[k] = v
    return snap


def _ensure_flush_worker():
    global _FLUSH_WORKER, _FLUSH_EVENT
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if (_FLUSH_WORKER is not None and not _FLUSH_WORKER.done()
            and _FLUSH_WORKER.get_loop() is loop):
        return
    _FLUSH_EVENT = asyncio.Event()
    _FLUSH_WORKER = loop.create_task(_flush_worker())
    logger.debug("状态落盘后台协程已启动")


async def _flush_worker():
    ev = _FLUSH_EVENT
    while True:
        await ev.wait()
        await asyncio.sleep(FLUSH_DELAY_SECS)
        ev.clear()
        keys = list(_DIRTY)
        _DIRTY.clear()
        try:
            await asyncio.to_thread(_persist, keys)
        except Exception as e:
            logger.warning(f"状态落盘失败: {e}")


def mark_dirty(key: str, source: str = "chat"):
    """标记某状态待落盘，交给后台协程。请求路径上只是一次 set.add。"""
    _DIRTY.add(key)
    _DIRTY_SOURCE[key] = source
    _ensure_flush_worker()
    if _FLUSH_EVENT is not None:
        _FLUSH_EVENT.set()


def flush_all_sync():
    """进程退出/插件卸载前同步刷盘。非请求路径。"""
    keys = list(_DIRTY)
    _DIRTY.clear()
    _persist(keys)


def load_state(umo: str, config: dict = None) -> dict:
    """直接读盘（保留给非请求路径使用；请求路径请用 get_state）。"""
    _ensure_dir()
    path = _state_path(umo)
    if not path.exists():
        state = _default_state(config)
        _write(path, state)
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    # 旧状态文件可能缺新字段：用默认值补齐，避免下游 KeyError 被吞后注入静默失效
    defaults = _default_state(config)
    changed = False
    for k, v in defaults.items():
        if k not in state:
            state[k] = v
            changed = True
    if "last_seen" not in state:
        state["last_seen"] = {}
        changed = True
    # 数值字段类型校验：异常值回退默认
    for k in ("affection", "lewdness", "depravity", "emotion"):
        v = state.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            state[k] = defaults[k]
            changed = True
    if changed:
        _write(path, state)
    return state


def save_state(umo: str, state: dict):
    """请求路径上只更新内存并打脏标记，不写盘。

    兼容旧调用：如果传进来的 dict 被裁剪掉了 conversation_log，
    这里把缓存里的日志补回去（日志留在内存），只在写盘时排除。
    """
    key = _cache_key(umo)
    if "conversation_log" not in state:
        cached = _STATE_CACHE.get(key)
        if cached is not None and "conversation_log" in cached:
            merged = dict(state)
            merged["conversation_log"] = cached["conversation_log"]
            state = merged
        _SKIP_LOG.add(key)
    else:
        _SKIP_LOG.discard(key)
    _STATE_CACHE[key] = state
    mark_dirty(key)


def _write(path: Path, state: dict):
    """原子写：先写 .tmp 再替换，避免被强杀时留下半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── 存储后端：SQLite（标准库，无第三方依赖）──
# 两张业务表 + 一张元信息表：
#   persona_state_snapshot  每个会话一行快照
#   state_events            数值变化的事件流水
#   state_meta              结构版本 / 迁移标记
_BACKEND = "json"            # 当前后端：sqlite / json
_DB_FILE: Path = None        # 当前数据库路径
_PERSONA_ID = "default"      # 人格标识，先固定，为以后一人格一份状态留位
_LAST_PERSISTED: dict = {}   # key -> {维度: 上次落盘数值}，供算事件增量
_DIRTY_SOURCE: dict = {}     # key -> 最近一次改动来源（chat / decay / ...）
EVENT_DIMS = ("affection", "lewdness", "depravity", "emotion", "social_need")
META_VERSION = "1"

_DDL = """
CREATE TABLE IF NOT EXISTS state_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS persona_state_snapshot (
  umo        TEXT NOT NULL,
  persona_id TEXT NOT NULL DEFAULT 'default',
  state_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (umo, persona_id)
);
CREATE TABLE IF NOT EXISTS state_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  umo        TEXT NOT NULL,
  persona_id TEXT NOT NULL DEFAULT 'default',
  ts         REAL NOT NULL,
  source     TEXT NOT NULL,
  dimension  TEXT NOT NULL,
  delta      REAL,
  detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_umo_ts
  ON state_events(umo, persona_id, ts DESC);
"""


def _db_path(config: dict = None) -> Path:
    raw = ""
    if config:
        raw = str(config.get("db_path", "") or "").strip()
    return Path(raw) if raw else Path("data/plugin_data/lili_state.db")


def _db_connect(path: Path) -> sqlite3.Connection:
    """每次在线程里新开连接（sqlite 连接不能跨线程复用）。WAL 让读不挡写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db(config: dict = None) -> None:
    global _DB_FILE
    _DB_FILE = _db_path(config)
    conn = _db_connect(_DB_FILE)
    try:
        conn.executescript(_DDL)
        conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES('schema_version', ?)",
                     (META_VERSION,))
        conn.commit()
    finally:
        conn.close()


def _load_snapshots_db() -> dict:
    out = {}
    if _DB_FILE is None or not _DB_FILE.exists():
        return out
    conn = _db_connect(_DB_FILE)
    try:
        for umo, raw in conn.execute("SELECT umo, state_json FROM persona_state_snapshot"):
            try:
                st = json.loads(raw)
            except Exception:
                continue
            if isinstance(st, dict):
                out[umo] = st
    finally:
        conn.close()
    return out


def _backup_state_dir() -> Path:
    """迁移前把整个状态目录复制一份（不动原文件）。"""
    import shutil
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = STATE_DIR.parent / f"{STATE_DIR.name}_backup_{stamp}"
    try:
        shutil.copytree(STATE_DIR, dst)
        logger.info(f"旧状态目录已备份: {dst}")
    except Exception as e:
        logger.warning(f"备份旧状态目录失败（继续迁移）: {e}")
    return dst


def migrate_json_to_db(config: dict = None) -> int:
    """把旧 state.json 一次性导入 SQLite。

    幂等设计：
      1. 已有 migration_json_done 标记 → 整段跳过
      2. 快照用 INSERT OR IGNORE → 重跑不会覆盖运行期数据
      3. .bak 文件名冲突时自动加序号 → 重跑不会互相覆盖
      4. 任何异常都不写 done 标记 → 下次启动自动重试
    """
    if _DB_FILE is None:
        return 0
    conn = _db_connect(_DB_FILE)
    try:
        if conn.execute("SELECT 1 FROM state_meta WHERE key='migration_json_done'").fetchone():
            return 0
        files = sorted(STATE_DIR.glob("*.json"))
        if not files:
            conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES('migration_json_done', ?)",
                         (time.strftime("%Y-%m-%dT%H:%M:%S"),))
            conn.commit()
            return 0

        backup_dir = _backup_state_dir()
        now = time.time()
        migrated = 0
        for path in files:
            key = path.stem
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"迁移跳过 {path.name}: {e}")
                continue
            if not isinstance(st, dict):
                continue
            _normalize_state(st, config)
            cur = conn.execute(
                "INSERT OR IGNORE INTO persona_state_snapshot"
                "(umo, persona_id, state_json, updated_at) VALUES(?, ?, ?, ?)",
                (key, _PERSONA_ID, json.dumps(st, ensure_ascii=False), now))
            if cur.rowcount:
                migrated += 1
            conn.execute(
                "INSERT INTO state_events"
                "(umo, persona_id, ts, source, dimension, delta, detail) VALUES(?,?,?,?,?,?,?)",
                (key, _PERSONA_ID, now, "migrate", "state", 0, "import from state.json"))
        conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES('migration_json_done', ?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        conn.commit()

        # 提交后再改名备份，原文件不删除
        for path in files:
            target = path.with_name(path.name + ".bak")
            i = 1
            while target.exists():
                target = path.with_name(f"{path.name}.bak.{i}")
                i += 1
            try:
                path.rename(target)
            except Exception as e:
                logger.warning(f"备份改名失败 {path.name}: {e}")
        logger.info(f"JSON → SQLite 迁移完成: {migrated} 个会话（备份目录 {backup_dir.name}）")
        return migrated
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"JSON 迁移失败，下次启动重试: {e}")
        return 0
    finally:
        conn.close()


def _persist_db(keys: list) -> None:
    """线程内执行：一次事务写快照 + 事件增量。"""
    if _DB_FILE is None or not keys:
        return
    conn = _db_connect(_DB_FILE)
    try:
        now = time.time()
        rows = []
        events = []
        new_vals = {}
        for key in keys:
            st = _STATE_CACHE.get(key)
            if st is None:
                continue
            snap = _snapshot(st, key)
            rows.append((key, _PERSONA_ID, json.dumps(snap, ensure_ascii=False), now))
            prev = _LAST_PERSISTED.get(key) or {}
            src = _DIRTY_SOURCE.get(key, "chat")
            vals = {}
            for dim in EVENT_DIMS:
                try:
                    v = int(snap.get(dim, 0) or 0)
                except (TypeError, ValueError):
                    v = 0
                vals[dim] = v
                if dim in prev and v != prev[dim]:
                    events.append((key, _PERSONA_ID, now, src, dim, v - prev[dim], None))
            new_vals[key] = vals
        conn.executemany(
            "INSERT INTO persona_state_snapshot(umo, persona_id, state_json, updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(umo, persona_id)"
            " DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            rows)
        if events:
            conn.executemany(
                "INSERT INTO state_events"
                "(umo, persona_id, ts, source, dimension, delta, detail) VALUES(?,?,?,?,?,?,?)",
                events)
        conn.commit()
        _LAST_PERSISTED.update(new_vals)
        if events:
            logger.debug(f"[persist] 落盘 {len(rows)} 个会话，新增 {len(events)} 条事件")
    finally:
        conn.close()


def _persist(keys: list) -> None:
    """按当前后端落盘。线程内调用，不占事件循环。"""
    if not keys:
        return
    if _BACKEND == "sqlite":
        _persist_db(keys)
        return
    for key in keys:
        st = _STATE_CACHE.get(key)
        if st is None:
            continue
        _write(_path_by_key(key), _snapshot(st, key))


def _key_from_name(name: str) -> str:
    """从状态文件名还原缓存 key（兼容 .json 和 .json.bak / .json.bak.N）。"""
    if ".json.bak" in name:
        return name.split(".json.bak")[0]
    if name.endswith(".json"):
        return name[:-5]
    return name


# ── 工具函数 ──

def _get_bot_name(config: dict = None) -> str:
    cfg = config or {}
    return cfg.get("bot_name", "").strip() or "莉莉"


# 配置项默认值回退（与 _conf_schema.json 中的 default 保持一致）
# 当插件加载时旧的配置文件中没有新 key 时使用此回退



# 哨兵值，用于判断 config 中 key 是否存在
# 全局缓存 schema 默认值，避免每次 _get_cfg 都读文件
_SCHEMA_DEFAULTS = None

def _load_schema_defaults() -> dict:
    """从 _conf_schema.json 加载默认值。缓存在全局变量中。"""
    global _SCHEMA_DEFAULTS
    if _SCHEMA_DEFAULTS is not None:
        return _SCHEMA_DEFAULTS
    schema_path = Path(__file__).parent / "_conf_schema.json"
    if not schema_path.exists():
        _SCHEMA_DEFAULTS = {}
        return _SCHEMA_DEFAULTS
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        _SCHEMA_DEFAULTS = {
            key: spec["default"]
            for key, spec in schema.items()
            if "default" in spec
        }
    except Exception:
        _SCHEMA_DEFAULTS = {}
    return _SCHEMA_DEFAULTS


def _get_cfg(cfg: dict, keys: list, default=""):
    """依次尝试多个key，取第一个非空值。兼容 str 和 list 类型。

    安全原则：
    - 从 cfg 读取（用户自定义值优先）
    - 如果 cfg 中所有 key 都为空，回退到 schema 默认值
    - 此回退是读时回退（read-time fallback），不写回 config 文件
    - 用户在面板修改并保存后，cfg 中就有值了，回退不触发
    """
    for key in keys:
        val = cfg.get(key, "")
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (list, tuple)) and val:
            return ",".join(str(v).strip() for v in val if str(v).strip())
    # 所有 key 都为空 → 读时回退到 schema 默认值
    schema_def = _load_schema_defaults()
    for key in keys:
        if key in schema_def:
            return schema_def[key]
    return default


def _parse_list_cfg(cfg: dict, *keys):
    """从多个key（兼容新 old）读取列表。支持 str(逗号分隔) 和 list 类型。"""
    for key in keys:
        raw = cfg.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            return [str(u).strip() for u in raw if str(u).strip()]
        if isinstance(raw, str) and raw.strip():
            return [u.strip() for u in raw.replace("，", ",").split(",") if u.strip()]
    return []


# ── 标注构建 ──

def period_label() -> str:
    h = datetime.now().hour
    if 0 <= h < 5:   return "凌晨"
    if 5 <= h < 8:   return "清晨"
    if 8 <= h < 12:  return "上午"
    if 12 <= h < 14: return "中午"
    if 14 <= h < 18: return "下午"
    if 18 <= h < 20: return "傍晚"
    return "晚上"


def emotion_label(val: int) -> str:
    if val >= 70: return "开心"
    if val >= 40: return "平静"
    if val >= 20: return "烦躁"
    return "低落"


# ── 色情检测（分级关键词） ──
# 设计：
# - 已移除 嗯/啊/摸/热/紧/插 等高频单字：
#   正常长回复里几乎必中一个（"插"更是"插件"二字的一部分，技术聊天必然命中），
#   曾导致普通聊天持续误涨淫乱度
# - 命中 1 个 = 疑似，仅小幅加分
# - 命中 2 个及以上共现 = 确认，正常加分
# - 不再依赖回复长度门槛（短回复里的明确词同样算数）
LEWD_KW = ["舔", "骚", "呻吟", "喘", "湿", "紧", "舒服", "想要", "身体", "色色", "欲望"]

# 单字词出现在常见中性词组里时不算命中（如"湿度""紧张"）
_NEUTRAL_COMPOUNDS = {
    "湿": ["湿度", "潮湿"],
    "紧": ["紧张", "紧急", "紧凑", "紧跟", "赶紧"],
}

# 好感度/情绪词表的中性词组豁免：关键词命中的词组若属于常见中性/亲昵语境则不算触发。
# 注意：key 必须与词表里的关键词完全一致（单字关键词按单字豁免，多字词按整词豁免）
_AFFECTION_NEUTRAL = {
    "棒": ["棒球", "棒棒糖"],
    "傻蛋": ["小傻蛋", "大傻蛋"],
}

def _kw_hit(text: str, kw: str, neutral_map: dict = None) -> bool:
    if not text or not kw or kw not in text:
        return False
    for compound in (neutral_map or _NEUTRAL_COMPOUNDS).get(kw, []):
        text = text.replace(compound, "")
    return kw in text

def lewd_score(text: str) -> int:
    """回复命中的色情关键词个数（按词去重，排除中性词组误命中）。"""
    if not text:
        return 0
    return sum(1 for kw in LEWD_KW if _kw_hit(text, kw))

def is_lewd_explicit(text: str) -> bool:
    """判断文本是否构成色情语境：2个及以上关键词共现，或含强信号词。"""
    if not text:
        return False
    if lewd_score(text) >= 2:
        return True
    return any(kw in text for kw in ("舔", "骚", "色色", "欲望", "呻吟"))


def lewdness_label(val: int) -> str:
    if val >= 100: return "满"
    if val >= 67:  return "高"
    if val >= 34:  return "中"
    return "低"


def _user_entries(log: list) -> list:
    return [e for e in log if e.get("role") == "user"]


def _core_match(a: str, b: str) -> bool:
    strip_chars = "~！@#￥%…&*（）—+、，。；：？！.,;:… \t\n\r"
    a_clean = a.translate(str.maketrans("", "", strip_chars))
    b_clean = b.translate(str.maketrans("", "", strip_chars))
    return len(a_clean) > 2 and len(b_clean) > 2 and a_clean == b_clean


def todays_duplicate_count(state: dict, user_msg: str, user_id: str = "") -> int:
    """统计当前用户当天发送的重复消息次数（持久化在 state 的 repeat 结构里）。

    相比旧版（扫会话日志）的修正：
    - 按 user_id 区分：群聊里别人发的相同消息不再计入当前用户的次数
    - 真正的"当天"：计数随 state 文件持久化，日期切换时整体清零，
      不再受会话日志 30条/600秒 窗口裁剪影响
    """
    if not user_msg or not user_msg.strip():
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    key = user_msg.strip()[:100]  # 截断防状态文件膨胀
    repeat = state.get("repeat") or {}
    if repeat.get("date") != today:
        # 日期切换：整体清零（跨天不累计）
        repeat = {"date": today, "users": {}}
    users = repeat.setdefault("users", {})
    uid = str(user_id or "")
    msgs = users.get(uid)
    if msgs is None:
        msgs = users[uid] = {}
    hit_key = None
    for k in msgs:
        if k == key or _core_match(k, key):
            hit_key = k
            break
    if hit_key is None:
        if len(msgs) >= 20:
            msgs.pop(next(iter(msgs)))  # FIFO 淘汰最早的区分消息，防无限增长
        msgs[key] = 1
    else:
        msgs[hit_key] = msgs[hit_key] + 1
    state["repeat"] = repeat
    return msgs.get(hit_key if hit_key is not None else key, 1)


def groom_history(state: dict, max_count: int, timeout_secs: int):
    if max_count <= 0:
        return
    log = state.get("conversation_log", [])
    if not log:
        return
    now = int(time.time())
    cutoff = now - timeout_secs
    recent = [e for e in log if e["time"] >= cutoff]
    expired = [e for e in log if e["time"] < cutoff]
    keep = list(recent)
    R = len(recent)
    budget = max(0, max_count - R)
    if expired and budget > 0:
        expired.sort(key=lambda e: e["time"])
        keep = expired[-budget:] + keep
    seen = set()
    deduped = []
    for e in keep:
        sig = (e.get("role", ""), e.get("content", e.get("lili_thought", "")), e.get("time", 0))
        if sig not in seen:
            seen.add(sig)
            deduped.append(e)
    deduped.sort(key=lambda e: e["time"])
    state["conversation_log"] = deduped


def _lili_thought_summary(reply_text: str, bot_name: str = "莉莉") -> str:
    if not reply_text:
        return f"{bot_name}没说话"
    t = reply_text.strip()
    tech_kw = ["报错", "错误", "bug", "修复", "代码", "配置", "日志",
               "插件", "框架", "语法", "文件", "重启", "覆盖", "备份"]
    chat_kw = ["哈哈", "笑死", "可爱", "喜欢", "早啊", "晚安", "天气",
               "嗯嗯", "好哦", "行吧", "emm", "诶"]
    close_kw = ["抱抱", "贴贴", "想你了", "亲", "爱你", "摸摸"]
    if any(k in t for k in tech_kw):
        return f"{bot_name}觉得又在折腾代码了"
    if any(k in t for k in close_kw):
        return f"{bot_name}想亲近对方"
    if is_lewd_explicit(t):
        return f"{bot_name}有点发情了"
    if any(k in t for k in chat_kw):
        return f"{bot_name}聊得挺开心"
    short = t[:10].replace("\n", " ")
    if len(t) > 10:
        short += "…"
    return f"{bot_name}回了句「{short}」"


def _user_intent_summary(user_msg: str) -> str:
    if not user_msg:
        return "用户发了空消息"
    m = user_msg.strip()
    tech_kw = ["报错", "错误", "bug", "修复", "改", "加", "删",
               "代码", "配置", "插件", "框架", "文件", "重启",
               "为什么", "怎么", "如何", "不行", "没触发", "没保存",
               "覆盖", "丢失", "写", "读", "改一下", "short"]
    chat_kw = ["哈哈", "笑", "早", "晚", "在吗", "好", "嗯", "哦"]
    complain_kw = ["烦", "累", "困", "无聊", "无语", "算了"]
    if any(k in m for k in tech_kw):
        if any(k in m for k in ["报错", "错误", "bug", "不行", "没触发"]):
            return "用户想让我看报错"
        if any(k in m for k in ["改", "加", "删", "改一下"]):
            return "用户想让我改代码"
        if any(k in m for k in ["为什么", "怎么", "如何"]):
            return "用户想问我技术问题"
        return "用户想讨论技术问题"
    if is_lewd_explicit(m):
        return "用户想色色"
    if any(k in m for k in complain_kw):
        return "用户想吐槽"
    if any(k in m for k in chat_kw):
        return "用户想闲聊"
    short = m[:15].replace("\n", " ")
    if len(m) > 15:
        short += "…"
    return f"用户说「{short}」"


def build_bot_thought(state: dict, user_id: str, config: dict = None) -> str:
    aff = state["affection"]
    em_label = emotion_label(state["emotion"])
    lew_label = lewdness_label(state["lewdness"])
    em_thoughts = {
        "开心": "心情挺好的",
        "平静": "没什么特别的感觉",
        "烦躁": "有点烦",
        "低落": "不太想说话",
    }
    thought = em_thoughts.get(em_label, "还行")
    if aff >= 90:
        thought += "，觉得对方人很好"
    elif aff >= 80:
        thought += "，聊得挺舒服"
    elif aff <= 30:
        thought += "，不太想理这个人"
    if lew_label == "满" or lew_label == "高":
        thought += "，有点想做坏事"
    elif lew_label == "中":
        thought += "，脑子里偶尔飘过色色的念头"
    return thought


def build_state_snapshot(state: dict, user_id: str = "") -> str:
    aff = state.get("affection", 65)
    emo = state.get("emotion", 60)
    lew = state.get("lewdness", 20)
    dep = state.get("depravity", 0)
    em = emotion_label(emo)
    lew_label = lewdness_label(lew)
    aff_desc = "非常高" if aff >= 90 else "高" if aff >= 80 else "中" if aff >= 50 else "低"
    return f"好感:{aff}({aff_desc}) 情绪:{em}({emo}) 淫乱:{lew_label}({lew}) 恶堕:{dep}"


def _fmt_time_ago(ts: int) -> str:
    dt = datetime.fromtimestamp(ts)
    elapsed = int(time.time() - ts)
    if elapsed < 120:
        return "刚刚"
    if elapsed < 3600:
        return f"{elapsed // 60}分前"
    if elapsed < 7200:
        dt2 = datetime.fromtimestamp(time.time())
        if dt.strftime("%H:%M") == dt2.strftime("%H:%M"):
            return "刚刚"
    return dt.strftime("%H:%M")


def _summarize_msg(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"[总结] {text[:max_chars // 2]}..."


def build_conversation_context(state: dict, current_user_id: str = "",
                                max_entries: int = 20, user_msg_max_chars: int = 200,
                                thought_mode: str = "内心想法", config: dict = None) -> str:
    bot_name = _get_bot_name(config)
    log = state.get("conversation_log", [])
    if not log or max_entries <= 0:
        return ""
    recent = log[-max_entries:]
    if recent and current_user_id:
        last = recent[-1]
        if last.get("role") == "user" and last.get("user_id", "") == current_user_id:
            recent = recent[:-1]
    lines = []
    for entry in recent:
        role = entry.get("role", "user")
        ts = entry.get("time", 0)
        time_tag = _fmt_time_ago(ts) if ts else "?"
        if role == "assistant":
            thought = entry.get("lili_thought", entry.get("state_snapshot", ""))
            if thought:
                lines.append(f"[{time_tag}] {bot_name}心想: {thought}")
            else:
                lines.append(f"[{time_tag}] {bot_name}: ...")
        else:
            content = _summarize_msg(entry.get("content", ""), user_msg_max_chars)
            uid = entry.get("user_id", "")
            if uid and uid == current_user_id:
                lines.append(f"[{time_tag}] 当前用户: {content}")
            else:
                label = f"用户({uid})" if uid else "用户"
                lines.append(f"[{time_tag}] {label}: {content}")
    return "【近期对话历史（按时间排序）】\n" + "\n".join(lines) + "\n"


def msg_similarity_label(state: dict, user_msg: str) -> str:
    users = _user_entries(state.get("conversation_log", []))
    if not users or not user_msg.strip():
        return "不相似"
    last = users[-1].get("content", "")
    if last == user_msg or last.strip() == user_msg.strip():
        return "完全相同"
    if _core_match(last, user_msg):
        return "高度相似"
    return "不相似"


def minutes_since_last(state: dict, user_id: str = "") -> str | None:
    """距当前用户自己上一条消息的时间（用于"上条消息就在X前发的"提示）。

    旧版直接取 log[-2]（整个日志的倒数第二条），群聊里可能是别人的消息；
    现在倒扫日志找当前用户自己的上一条 user 条目（末条是当前这条，排除）。
    日志里没有时（save_conversation_log=False 日志未持久化、或已被窗口裁剪），
    回退到 state["last_seen"]（每条用户消息后都会持久化到状态文件）。
    完全找不到上一条时返回 None，调用方不再输出"刚发完上一条"这类无依据的提示。
    """
    if not user_id:
        return None
    log = state.get("conversation_log", [])
    prev_time = None
    for entry in reversed(log[:-1]):
        if entry.get("role") != "user":
            continue
        if str(entry.get("user_id", "")) != str(user_id):
            continue
        if entry.get("time"):
            prev_time = entry["time"]
            break
    if prev_time is None:
        prev_time = (state.get("last_seen") or {}).get(str(user_id))
    if not prev_time:
        return None
    seconds = max(0.0, time.time() - float(prev_time))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    return f"{int(seconds // 86400)}天前"


# ── 行为提示 ──

def _affection_feel(aff: int) -> str:
    if aff >= 90: return "超喜欢的，有点依赖感"
    if aff >= 80: return "挺喜欢的，想多聊聊"
    if aff >= 50: return "印象还行吧"
    if aff >= 30: return "就那样，一般般"
    return "不太想搭理...挺烦的"


def _lewdness_feel(val: int) -> str:
    lbl = lewdness_label(val)
    if lbl == "满" or lbl == "高": return "身体有点燥热，想做坏事"
    if lbl == "中": return "脑子里偶尔飘过色色的念头"
    return "身体没什么特别的感觉"


def _depravity_feel(val: int) -> str:
    if val >= 70: return "堕落感很重"
    if val >= 30: return "有点堕落感"
    if val > 0:   return "轻微堕落感"
    return ""


# ── 角色人设注入 ──

def _load_skill_content() -> dict:
    """读取 SKILL.md / SUPPLEMENT.md。"""
    result = {"skill_md": "", "supplement_md": ""}
    for base_dir in [SKILL_DIR, PLUGIN_SKILL_DIR]:
        skill_path = base_dir / "SKILL.md"
        supp_path = base_dir / "SUPPLEMENT.md"
        if base_dir.exists():
            try:
                if skill_path.exists():
                    with open(skill_path, "r", encoding="utf-8") as f:
                        result["skill_md"] = f.read()
                if supp_path.exists():
                    with open(supp_path, "r", encoding="utf-8") as f:
                        result["supplement_md"] = f.read()
                if result["skill_md"]:
                    break
            except Exception:
                continue
    return result




# ── 离线推演（纯内存、零 IO、O(1)）──
MIN_DECAY_GAP_SECS = 1800      # 间隔小于 30 分钟直接跳过：连续聊天时零影响
EMOTION_BASELINE = 60          # 心情基线＝平静
SOCIAL_GAIN_FACTOR = 40        # 社交需求每小时增量 = decay_rate × 40
HINT_EMOTION_DELTA = 12        # 心情位移 ≥12 分才算"显著"
HINT_SOCIAL_DELTA = 25         # 社交需求涨幅 ≥25 点才算"显著"


def apply_time_decay(state: dict, now: float = None,
                     config: dict = None) -> tuple:
    """根据离线时长做一次静默推演，返回 (state, hint)。

    hint 是"仅本轮"的提示句，空字符串表示不提示（不写进 state，避免被持久化）。
    全程纯内存：一次减法 + 一次阈值比较；只有命中推演才做两次幂运算。
    """
    if now is None:
        now = time.time()

    # 关注：last_interaction_time 的更新不在这里做，由调用方统一更新
    last = state.get("last_interaction_time", 0.0) or 0.0
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        # 老状态没有基线：退回用 last_seen 里最近的一次交互时间做基线
        ls = state.get("last_seen") or {}
        if isinstance(ls, dict) and ls:
            try:
                last = max(float(v) for v in ls.values()
                           if isinstance(v, (int, float)) and not isinstance(v, bool))
            except (TypeError, ValueError):
                last = 0.0
    if last <= 0:
        # 连 last_seen 都没有：本轮只记录时间，不推演，避免首次对话被当成离线了几十年
        return state, ""

    gap = now - last
    if gap < MIN_DECAY_GAP_SECS:
        # 快速连聊走这里：一个字段都不改，注入文本逐字节不变
        return state, ""

    if config is not None and not config.get("enable_time_decay", True):
        return state, ""

    rate = 0.15
    max_hours = 12.0
    if config is not None:
        try:
            rate = float(config.get("decay_rate", 0.15))
        except (TypeError, ValueError):
            rate = 0.15
        try:
            max_hours = float(config.get("max_offline_hours", 12))
        except (TypeError, ValueError):
            max_hours = 12.0
    rate = min(max(rate, 0.0), 1.0)
    if max_hours <= 0:
        max_hours = 12.0
    if rate == 0.0:
        return state, ""

    hours = min(gap / 3600.0, max_hours)   # 封顶，防止久别之后数值爆炸

    em_old = state.get("emotion", EMOTION_BASELINE)
    sn_old = state.get("social_need", 20)
    if not isinstance(em_old, (int, float)):
        em_old = EMOTION_BASELINE
    if not isinstance(sn_old, (int, float)):
        sn_old = 20

    # 维度一：心情向基线回归。指数衰减是闭式解，离线 8 小时和 800 小时的计算量一样
    em_new = EMOTION_BASELINE + (em_old - EMOTION_BASELINE) * ((1.0 - rate) ** hours)
    # 维度二：社交需求随离线时长上升
    sn_new = sn_old + rate * SOCIAL_GAIN_FACTOR * hours

    em_new = int(round(min(max(em_new, 0), 100)))
    sn_new = int(round(min(max(sn_new, 0), 100)))

    d_em = em_new - int(em_old)
    d_sn = sn_new - int(sn_old)
    state["emotion"] = em_new
    state["social_need"] = sn_new

    hint = ""
    if d_sn >= HINT_SOCIAL_DELTA:
        hint = "（好久没人说话，有点想找人聊）"
    elif abs(d_em) >= HINT_EMOTION_DELTA:
        hint = "（心情慢慢平下来了）"
    logger.debug(f"[decay] 离线 {hours:.2f}h，心情 {int(em_old)}→{em_new}，"
                 f"社交 {int(sn_old)}→{sn_new}")
    return state, hint


def build_inject_text(state: dict, user_id: str, user_msg: str,
                       config: dict = None,
                       context_entries: int = 20, msg_max_chars: int = 200,
                       thought_mode: str = "内心想法",
                       decay_hint: str = "") -> str:
    bot_name = _get_bot_name(config)
    dup_count = todays_duplicate_count(state, user_msg, user_id)
    minutes = minutes_since_last(state, user_id)
    stutter = False
    if not state["stutter_done"]:
        prob = config.get("stutter_probability", 0.3) if config else 0.3
        if random.random() < prob:
            state["stutter_done"] = True
            stutter = True
    inject_ctx = config.get("inject_conversation_context", False) if config else False
    context = build_conversation_context(
        state, current_user_id=user_id,
        max_entries=context_entries,
        user_msg_max_chars=msg_max_chars,
        thought_mode=thought_mode,
        config=config,
    ) if inject_ctx else ""
    enable_aff = config.get("enable_affection", True) if config else True
    enable_lew = config.get("enable_lewdness", True) if config else True
    allow_ero = config.get("allow_erotic_content", True) if config else True
    aff_feel = _affection_feel(state["affection"]) if enable_aff else ""
    lewd_feel = _lewdness_feel(state["lewdness"]) if (enable_lew and allow_ero) else "身体没什么特别的感觉"
    dep_feel = _depravity_feel(state["depravity"]) if enable_lew else ""
    dep_suffix = f"，{dep_feel}" if dep_feel else ""
    em_label = emotion_label(state["emotion"])

    # 内容限制覆盖（不受制约用户豁免）
    restriction = ""
    if config and not config.get("allow_erotic_content", True):
        unrestricted_raw = config.get("unrestricted_list", "")
        if isinstance(unrestricted_raw, list):
            restricted_users = [str(u).strip() for u in unrestricted_raw if str(u).strip()]
        elif isinstance(unrestricted_raw, str) and unrestricted_raw.strip():
            restricted_users = [u.strip() for u in unrestricted_raw.replace("，", ",").split(",") if u.strip()]
        else:
            restricted_users = []
        if user_id not in restricted_users:
            restriction = "\n【内容限制】\n禁止回复任何色情内容。\n"
    # 预先组装条件文本（避免 f-string 表达式内出现 \n）
    stutter_line = "说话有点结巴。\n\n" if stutter else "\n"
    # 社交需求：只在高位时用文字体现，不报数字
    social_line = "想找人说话。" if state.get("social_need", 0) >= 70 else ""
    hint_line = f"{decay_hint}\n" if decay_hint else ""
    obj_line = f"【对象】{user_id}，你对ta{aff_feel}。\n" if enable_aff else f"【对象】{user_id}。\n"
    ctx_line = f"{context}" if inject_ctx else ""
    # 说明：dup_count 仍要每轮统计（有累加副作用），但不再写进注入文本，
    # 改由 get_relationship_detail 工具按需拉取；上条间隔同理。
    _ = dup_count
    body = (
        f"【状态】现在是{period_label()}，心情{em_label}{dep_suffix}。{lewd_feel}。{social_line}\n"
        f"{stutter_line}"
        f"{hint_line}"
        f"{obj_line}"
        f"{restriction}"
        "【规则】按人设说话。\n"
        "\n"
        f"{ctx_line}"
    )
    # 固定标记包裹，方便下游解析（配置项 inject_wrapper 可关）
    if config and config.get("inject_wrapper", True):
        return "〖persona_state〗\n" + body.rstrip("\n") + "\n〖/persona_state〗\n"
    return body


def update_state(state: dict, llm_response: str, user_msg: str, config: dict = None):
    em = state.get("emotion", 60)
    lewd = state.get("lewdness", 20)
    # 正向/负向词表：单字高频词（好/爱/牛/骂等）要么改成明确的双字形式，
    # 要么走中性词组豁免（见 _AFFECTION_NEUTRAL），避免"你好""牛奶""骂人"这类普通对话误触发
    pos_kw = [
        "喜欢", "可爱", "厉害", "棒", "夸", "贴贴", "抱抱", "想你了",
        "牛逼", "好棒", "真好", "好看", "好听", "好好", "好厉害", "好喜欢", "好可爱", "好想你",
        "爱你", "爱死", "超爱", "好爱", "爱慕",
    ]
    neg_kw = [
        "傻逼", "傻X", "傻叉", "傻蛋",
        "蠢蛋", "蠢货", "蠢猪",
        "滚蛋", "滚开", "滚犊子", "你滚", "给我滚",
        "好烦", "真烦", "别烦", "烦人", "烦死", "别烦我",
        "讨厌", "恶心", "垃圾", "废物",
        "骂你", "骂我", "你骂", "骂死",
    ]
    pos_hit = any(_kw_hit(user_msg, kw, _AFFECTION_NEUTRAL) for kw in pos_kw)
    neg_hit = any(_kw_hit(user_msg, kw, _AFFECTION_NEUTRAL) for kw in neg_kw)
    if pos_hit:
        state["emotion"] = min(100, em + 8)
    if neg_hit:
        state["emotion"] = max(0, em - 10)
    enable_aff = config.get("enable_affection", True) if config else True
    enable_lew = config.get("enable_lewdness", True) if config else True
    if enable_aff:
        if pos_hit:
            state["affection"] = min(100, state["affection"] + 3)
        if neg_hit:
            state["affection"] = max(0, state["affection"] - 3)
    if enable_lew:
        # 色情检测：命中2个及以上关键词共现才算确认，命中1个只算疑似小幅加分
        # 不再要求回复超过100字，也不再因“开心”强制抬到30（那会让满100清零形同虚设）
        score = lewd_score(llm_response)
        if score >= 2:
            if em >= 40:
                state["lewdness"] = min(100, lewd + random.randint(10, 15))
                state["depravity"] = min(100, state["depravity"] + random.randint(10, 15))
        elif score == 1:
            if em >= 40:
                state["lewdness"] = min(100, lewd + 2)
        if state["lewdness"] >= 100:
            state["lewdness"] = 0
            state["depravity"] = 0


# ── 能力边界：启动期探测同类插件，避免互相覆盖 ──
# 本机看不到 AstrBot 框架源码（它跑在容器里），所以只做目录扫描，
# 读 metadata.yaml 的 name/desc 做关键词匹配。只在启动时跑一次，不在请求路径上。
MEMORY_KEYWORDS = ("memory", "mem0", "longterm", "long-term", "long_term", "recall", "记忆")
PERSONA_KEYWORDS = ("persona", "personality", "人格", "人设", "character")


def _iter_plugin_meta(base: Path):
    """极简 yaml 读取：只取 name 和 desc 两行，不引入 yaml 依赖。"""
    for meta in base.glob("*/metadata.yaml"):
        try:
            text = meta.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        name = desc = ""
        for line in text.splitlines():
            ls = line.strip()
            if ls.startswith("name:"):
                name = ls[5:].strip().strip("\"'")
            elif ls.startswith("desc:"):
                desc = ls[5:].strip().strip("\"'")
        yield meta.parent.name, name, desc


def detect_conflicting_plugins(self_marker: str = "cinwcc") -> dict:
    """扫描 data/plugins，返回 {"memory": [插件目录名], "persona": [...]}。"""
    found = {"memory": [], "persona": []}
    base = Path("data/plugins")
    if not base.exists():
        return found
    for dirname, name, desc in _iter_plugin_meta(base):
        blob = f"{dirname} {name} {desc}".lower()
        if self_marker and self_marker in blob:
            continue
        if any(k in blob for k in MEMORY_KEYWORDS):
            found["memory"].append(dirname)
        if any(k in blob for k in PERSONA_KEYWORDS):
            found["persona"].append(dirname)
    return found


# ── LLM 工具：把"不是每轮都必须"的细节改成按需拉取 ──
try:
    from astrbot.core.agent.tool import FunctionTool
    from pydantic.dataclasses import dataclass as _pyd_dataclass
    from typing import Any as _Any
    _HAS_TOOL = True
except Exception:      # 框架版本不一致时不拖垮插件加载
    FunctionTool = None
    _HAS_TOOL = False


if _HAS_TOOL:
    @_pyd_dataclass
    class _LiliStateTool(FunctionTool):
        """插件自带的工具，避免依赖框架的一次性 handler 绑定。"""

        plugin: _Any = None
        method_name: str = ""

        async def call(self, context, **kwargs):
            event = getattr(getattr(context, "context", None), "event", None)
            method = getattr(self.plugin, self.method_name)
            return await method(event, **kwargs)


# ── 插件主体 ──

@register("astrbot_plugin_lili_state", "mcxxiu", "角色状态管理插件", "1.5.2")
class LiliStatePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._patch_config_defaults()
        self._ensure_skills()
        self._log_cache = {}  # umo -> conversation_log list（save_conversation_log=False 时用）

        # 存储后端：sqlite（默认）或 json，可随时切回
        global _BACKEND
        backend = str(self.config.get("storage_backend", "sqlite") or "sqlite").strip().lower()
        _BACKEND = "sqlite" if backend == "sqlite" else "json"
        if _BACKEND == "sqlite":
            try:
                init_db(self.config)
                moved = migrate_json_to_db(self.config)
                if moved:
                    logger.info(f"启动迁移: {moved} 个会话已导入 SQLite")
            except Exception as e:
                _BACKEND = "json"
                logger.warning(f"SQLite 初始化失败，回退 JSON 存储: {e}")
        logger.info(f"状态存储后端: {_BACKEND}")

        # 启动预热：一次性把持久化状态读进内存，之后请求路径只读内存
        loaded = prewarm_states(self.config)
        logger.info(f"状态缓存预热完成: {loaded} 个会话已载入内存")

        self._register_state_tools()

        # 能力边界检测：同类插件存在时优雅降级，不互相覆盖
        self._memory_delegate = ""
        try:
            found = detect_conflicting_plugins("cinwcc")
            forced = str(self.config.get("delegate_target", "") or "").strip()
            if forced:
                self._memory_delegate = forced
            elif self.config.get("auto_delegate_memory", True) and found["memory"]:
                self._memory_delegate = found["memory"][0]
            if found["persona"]:
                logger.warning("检测到其他人格类插件: " + ", ".join(found["persona"])
                               + "；同时注入可能打架，建议只保留一个")
            if self._memory_delegate:
                logger.info(f"记忆能力已委托给 {self._memory_delegate}，本插件记忆工具降级")
            else:
                logger.info("未检测到记忆类插件，记忆能力由本插件独立提供")
        except Exception as e:
            logger.warning(f"能力边界检测失败，按独立运行处理: {e}")

        bot_name = _get_bot_name(self.config)
        skill = _load_skill_content()

        if skill["skill_md"]:
            logger.info(f"{bot_name}状态: SKILL.md OK ({len(skill['skill_md'])} chars)"
                        + (f" + SUPPLEMENT.md ({len(skill['supplement_md'])} chars)"
                           if skill["supplement_md"] else ""))
        else:
            logger.warning(f"{bot_name}状态: SKILL.md 未找到")

        logger.info(f"{bot_name}状态管理插件已加载")



    def _patch_config_defaults(self):
        """补全 config 中空值项为 schema 默认值。

        行为说明：
        - 如果配置项的值为空字符串（""）、空列表（[]）或 None，
          自动填充为 _conf_schema.json 中的 default 值
        - 已有内容的自定义项不受影响
        - 清空某字段 → 重启插件 → 恢复默认值
        - 面板提示用户：「清空此字段并重启插件将恢复默认值」
        """
        schema_path = Path(__file__).parent / "_conf_schema.json"
        if not schema_path.exists():
            logger.warning("_conf_schema.json 未找到，跳过配置补全")
            return
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            patched = 0
            for key, spec in schema.items():
                if "default" not in spec:
                    continue
                current = self.config.get(key)
                if current in (None, "", [], {}):
                    self.config[key] = spec["default"]
                    patched += 1
            if patched > 0:
                logger.info(f"启动自检: 补全 {patched} 个空值配置项为默认值（清空字段=恢复默认）")
        except Exception as e:
            logger.warning(f"读取 schema 默认值失败: {e}")

    def _ensure_skills(self):
        import shutil
        logger.info("--- 技能模板初始化 ---")
        # 首次启动时复制模板目录（SUPPLEMENT.md 等）
        if not SKILL_DIR.exists():
            if not PLUGIN_SKILL_DIR.exists():
                return
            try:
                shutil.copytree(PLUGIN_SKILL_DIR, SKILL_DIR)
                logger.info(f"已复制 lili_persona skill 到 {SKILL_DIR}")
            except Exception as e:
                logger.warning(f"复制 skill 目录失败: {e}")
                return
        # 每次启动：从模板填充配置值，生成最终 SKILL.md
        self._fill_skill_template()



    def _fill_skill_template(self):
        """从 SKILL_TEMPLATE.md 填充配置值，生成 SKILL.md。

        每次插件加载时执行，确保配置修改后 SKILL.md 同步更新。
        空值字段使用 schema 默认值填充。
        """
        import shutil
        schema_path = Path(__file__).parent / "_conf_schema.json"
        src_path = SKILL_DIR / "SKILL_TEMPLATE.md"
        dst_path = SKILL_DIR / "SKILL.md"

        if not src_path.exists():
            # 回退：直接复制模板目录下的备用模板
            fallback = PLUGIN_SKILL_DIR / "SKILL_TEMPLATE.md"
            if fallback.exists():
                shutil.copy2(fallback, src_path)
            else:
                logger.warning("SKILL_TEMPLATE.md 未找到，跳过模板填充")
                return
        else:
            # 已有模板，与插件包模板比对内容，不同则同步
            plugin_src = PLUGIN_SKILL_DIR / "SKILL_TEMPLATE.md"
            if plugin_src.exists():
                local_content = src_path.read_text(encoding="utf-8")
                plugin_content = plugin_src.read_text(encoding="utf-8")
                if local_content != plugin_content:
                    shutil.copy2(plugin_src, src_path)
                    logger.info("SKILL_TEMPLATE.md 已从插件包同步内容（内容不一致）")

        # 加载 schema 默认值
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            logger.warning(f"读取 schema 失败: {e}")
            schema = {}

        # 读取模板
        with open(src_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 替代值字典：从 config 取值，空值回退 schema 默认值
        replacements = {}
        # bot_name 单独处理：config 取值，空值回退 schema 默认值
        bot_name = self.config.get("bot_name", "").strip()
        if not bot_name:
            bot_name = schema.get("bot_name", {}).get("default", "莉莉")
        replacements["bot_name"] = bot_name

        for key in ["persona_core", "persona_personality", "persona_interests",
                     "persona_background", "persona_oral_habits", "persona_taboos",
                     "persona_emotion_rules", "persona_time_rules",
                     "persona_interaction_styles",
                     "reply_rules"]:
            val = self.config.get(key, "").strip()
            if not val:
                # 尝试 schema 默认值
                if key in schema and "default" in schema[key]:
                    val = schema[key]["default"]
            replacements[key] = val if val else "（未配置）"

        # persona_style_extra：额外风格说明，允许空值留空
        extra_style = self.config.get("persona_style_extra", "").strip()
        if not extra_style:
            if "persona_style_extra" in schema and "default" in schema["persona_style_extra"]:
                extra_style = schema["persona_style_extra"]["default"]
        replacements["persona_style_extra"] = extra_style


        # 关系列表：格式化为 JSON 数组字符串
        def fmt_list(key):
            raw = self.config.get(key)
            if isinstance(raw, list):
                items = [str(u).strip() for u in raw if str(u).strip()]
            elif isinstance(raw, str) and raw.strip():
                items = [u.strip() for u in raw.replace("，", ",").split(",") if u.strip()]
            else:
                items = []
            return "[" + ", ".join(f'"{u}"' for u in items) + "]"

        for key in ["friend_list", "neighbor_classmate_list", "enemy_list",
                     "nemesis_list", "unrestricted_list"]:
            replacements[key] = fmt_list(key)

        # 填充替换
        for key, val in replacements.items():
            content = content.replace("{{" + key + "}}", val)

        # 如果已有 SKILL.md 且内容相同，跳过写入避免触发技能系统重载
        if dst_path.exists():
            existing = dst_path.read_text(encoding="utf-8")
            if existing == content:
                logger.info(f"SKILL.md 与配置一致，跳过写入（配置未变化）")
                return

        # 写入 SKILL.md
        logger.info(f"SKILL.md 与配置不一致，写入更新（{sum(len(v) for v in replacements.values())} chars）")
        with open(dst_path, "w", encoding="utf-8") as f:
            f.write(content)




    @filter.on_llm_request(priority=90)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        try:
            umo = event.unified_msg_origin
            uid = event.message_obj.sender.user_id
            msg = event.message_str or ""
            state = get_state(umo, self.config)  # 只读内存，不碰磁盘

            # save_conversation_log=False 时从内存缓存恢复日志
            if not self.config.get("save_conversation_log", True) and umo in self._log_cache:
                state["conversation_log"] = self._log_cache[umo]

            today_str = datetime.now().strftime("%Y-%m-%d")
            if state.get("_last_date", "") != today_str:
                state["conversation_log"] = []
                state["stutter_done"] = False
                state["_last_date"] = today_str

            summary_mode = self.config.get("user_msg_store_mode", "原文")
            max_chars = self.config.get("user_msg_max_chars", 200)
            if summary_mode == "总结":
                stored_content = _user_intent_summary(msg)
            else:
                stored_content = msg[:max_chars] if max_chars > 0 else msg
            state.setdefault("conversation_log", []).append({
                "role": "user",
                "user_id": uid,
                "content": stored_content,
                "time": int(time.time()),
            })

            max_count = self.config.get("max_history_count", 30)
            timeout_secs = self.config.get("history_timeout_seconds", 600)
            groom_history(state, max_count, timeout_secs)

            # 离线推演：纯内存计算，未到阈值直接返回（<10us）
            em_before = state.get("emotion")
            sn_before = state.get("social_need")
            state, decay_hint = apply_time_decay(state, config=self.config)
            if state.get("emotion") != em_before or state.get("social_need") != sn_before:
                mark_dirty(_cache_key(umo), source="decay")   # 数值变了才安排异步落盘

            ctx_entries = self.config.get("conversation_context_entries", 20)
            msg_max_chars = self.config.get("user_msg_max_chars", 200)
            thought_mode = self.config.get("bot_thought_mode", "内心想法")
            inject = build_inject_text(state, uid, msg,
                                       config=self.config,
                                       context_entries=ctx_entries,
                                       msg_max_chars=msg_max_chars,
                                       thought_mode=thought_mode,
                                       decay_hint=decay_hint)
            logger.debug(f"[inject] key={_cache_key(umo)} len={len(inject)} "
                         f"hint={bool(decay_hint)}")

            # 记录该用户最后被看到的时间戳（随状态文件持久化，与 save_conversation_log 无关；
            # minutes_since_last 在日志里找不到该用户上一条时用它回退）
            now_ts = time.time()
            state.setdefault("last_seen", {})[uid] = now_ts
            # 推演基线：每次对话都刷新，保证下一轮的离线时长从这次算起
            state["last_interaction_time"] = now_ts

            # 注入到当前用户消息（req.prompt），而非历史上下文（req.contexts）
            # 原因: Runner 构建消息时:
            #   messages = bind_checkpoint_messages(request.contexts)  ← 历史消息
            #   messages.append(assemble_context(request))            ← 当前消息来自 req.prompt
            # 所以 inject 挂到 req.prompt 头部，才会出现在当前消息最前面
            if req.prompt:
                req.prompt = f"{inject}\n\n{req.prompt}"
            else:
                # 红线：绝不动 system_prompt（会作废前缀缓存）。prompt 为空时放弃本轮注入
                logger.debug("req.prompt 为空，跳过状态注入以保护前缀缓存")

            event.set_extra("_lili_state", state)
            event.set_extra("_lili_user_msg", msg)
            event.set_extra("_lili_umo", umo)
            event.set_extra("_lili_uid", uid)

        except Exception as e:
            bot_name = _get_bot_name(self.config)
            logger.warning(f"{bot_name}状态注入失败: {type(e).__name__}: {e}", exc_info=True)

    @filter.on_llm_response(priority=90)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.config.get("enabled", True):
            return
        try:
            state = event.get_extra("_lili_state")
            user_msg = event.get_extra("_lili_user_msg", "")
            umo = event.get_extra("_lili_umo")
            uid = event.get_extra("_lili_uid", "")
            if not state or not umo:
                return

            response_text = resp.completion_text or ""
            update_state(state, response_text, user_msg, self.config)

            if self.config.get("save_bot_state_to_history", True):
                bot_name = _get_bot_name(self.config)
                thought_mode = self.config.get("bot_thought_mode", "内心想法")
                state_snapshot = build_state_snapshot(state, uid)
                if thought_mode == "内心想法":
                    bot_thought = build_bot_thought(state, uid, self.config)
                    no_content = True
                elif thought_mode == "简短":
                    bot_thought = f"{_lili_thought_summary(response_text, bot_name)} | {state_snapshot}"
                    no_content = True
                elif thought_mode == "具体":
                    content_text = response_text[:200] if len(response_text) > 200 else response_text
                    bot_thought = f"回复:{content_text} | {state_snapshot}"
                    no_content = True
                else:
                    bot_thought = state_snapshot
                    no_content = False
                entry = {"role": "assistant", "time": int(time.time()), "lili_thought": bot_thought}
                if not no_content:
                    entry["content"] = response_text[:200]
                state["conversation_log"].append(entry)
                max_count = self.config.get("max_history_count", 30)
                timeout_secs = self.config.get("history_timeout_seconds", 600)
                groom_history(state, max_count, timeout_secs)

            # save_conversation_log=False：日志存内存缓存，不写磁盘
            if not self.config.get("save_conversation_log", True):
                self._log_cache[umo] = list(state["conversation_log"])
                state_no_log = {k: v for k, v in state.items() if k != "conversation_log"}
                save_state(umo, state_no_log)
            else:
                save_state(umo, state)

        except Exception as e:
            bot_name = _get_bot_name(self.config)
            logger.warning(f"{bot_name}状态更新失败: {e}")

    def _register_state_tools(self):
        """注册按需拉取工具。失败不影响主流程。"""
        if not _HAS_TOOL:
            logger.warning("当前 AstrBot 版本不支持 FunctionTool，状态工具未注册")
            return
        try:
            tools = []
            if self.config.get("enable_relationship_tool", True):
                tools.append(_LiliStateTool(
                    name="get_relationship_detail",
                    description=(
                        "查询当前聊天对象与本角色的关系细节：好感/心情/堕落等数值档位、"
                        "对方今天重复发了几次同样的话、距对方上次发言多久。"
                        "仅当回复需要判断关系深浅、决定要不要更亲近或更冷淡、"
                        "或者要吐槽对方重复刷屏时调用；日常寒暄不要调用。"
                    ),
                    parameters={"type": "object", "properties": {}, "required": []},
                    handler=None, plugin=self,
                    method_name="_tool_relationship_detail"))
            if self.config.get("enable_memory_tool", True):
                tools.append(_LiliStateTool(
                    name="get_memory_summary",
                    description=(
                        "读取本会话最近若干条对话原文，用于回忆之前聊过什么。"
                        "仅当对方提到过去发生的事、或需要接上前面的话题时调用；"
                        "普通聊天、情绪表达、寒暄都不要调用。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "返回条数，默认 6，最多 12。",
                                "default": 6,
                            }
                        },
                        "required": [],
                    },
                    handler=None, plugin=self,
                    method_name="_tool_memory_summary"))
            if not tools:
                logger.info("状态工具全部关闭（配置项 enable_*_tool）")
                return
            self.context.add_llm_tools(*tools)
            logger.info("状态工具已注册: " + ", ".join(t.name for t in tools))
        except Exception as e:
            logger.warning(f"状态工具注册失败（不影响主流程）: {e}")

    async def _tool_relationship_detail(self, event, **kwargs):
        """只读内存，不查库不做 IO。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        uid = ""
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = str(getattr(event, "sender_id", "") or "")
        st = get_state(umo, self.config)
        logger.debug(f"[tool] get_relationship_detail umo={umo} uid={uid}")

        aff = int(st.get("affection", 0) or 0)
        em = int(st.get("emotion", 60) or 0)
        parts = [f"聊天对象 {uid or '未知'}",
                 f"好感度 {aff}（{_affection_feel(aff)}）",
                 f"心情 {em}"]
        if self.config.get("enable_lewdness", True):
            lew = int(st.get("lewdness", 0) or 0)
            dep = int(st.get("depravity", 0) or 0)
            parts.append(f"色情倾向 {lew}（{_lewdness_feel(lew)}）")
            if dep:
                parts.append(f"堕落 {dep}（{_depravity_feel(dep)}）")
        repeat = st.get("repeat") or {}
        if repeat.get("date") == datetime.now().strftime("%Y-%m-%d"):
            msgs = ((repeat.get("users") or {}).get(str(uid)) or {})
            top = max(msgs.items(), key=lambda kv: kv[1]) if msgs else None
            if top and top[1] > 1:
                parts.append(f"对方今天把同一句话重复了 {top[1]} 次")
        mins = minutes_since_last(st, uid)
        if mins:
            parts.append("距对方上一条消息: " + ("刚刚" if mins == "刚刚" else str(mins)))
        return "；".join(parts)[:400]

    async def _tool_memory_summary(self, event, limit: int = 6, **kwargs):
        """只读内存里的对话日志，按需返回最近若干条。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        if getattr(self, "_memory_delegate", ""):
            return (f"记忆由 {self._memory_delegate} 插件负责，本插件不提供记忆摘要。"
                    f"需要回忆过去时请以对方当前话题为准。")
        st = get_state(umo, self.config)
        try:
            n = max(1, min(int(limit), 12))
        except (TypeError, ValueError):
            n = 6
        log = st.get("conversation_log") or []
        logger.debug(f"[tool] get_memory_summary umo={umo} n={n} have={len(log)}")
        if not log:
            return "本会话还没有可回忆的对话记录。"
        lines = []
        for item in log[-n:]:
            if not isinstance(item, dict):
                continue
            role = "对方" if item.get("role") == "user" else "我"
            content = str(item.get("content", "")).replace("\n", " ").strip()[:80]
            lines.append(f"{role}: {content}")
        return "最近对话（旧到新）:\n" + "\n".join(lines)

    async def _recent_events(self, key: str, limit: int = 5) -> list:
        """最近事件：sqlite 后端查表，json 后端没有流水，返回空。命令路径，不在热路径上。"""
        if _BACKEND != "sqlite" or _DB_FILE is None:
            return []

        def _q():
            conn = _db_connect(_DB_FILE)
            try:
                return conn.execute(
                    "SELECT ts, source, dimension, delta FROM state_events"
                    " WHERE umo = ? ORDER BY ts DESC, id DESC LIMIT ?",
                    (key, int(limit))).fetchall()
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_q)
        except Exception as e:
            logger.warning(f"读取事件失败: {e}")
            return []

    async def _build_state_report(self, umo: str, limit: int = 5) -> str:
        key = _cache_key(umo)
        st = get_state(umo, self.config)
        tag = umo.rsplit("!", 1)[-1] if "!" in umo else umo.rsplit(":", 1)[-1]
        tag = tag[:8]
        lines = [f"状态 · {tag}"]
        lines.append(
            f"心情 {int(st.get('emotion', 0) or 0)}，社交 {int(st.get('social_need', 0) or 0)}，"
            f"好感 {int(st.get('affection', 0) or 0)}，色情 {int(st.get('lewdness', 0) or 0)}，"
            f"堕落 {int(st.get('depravity', 0) or 0)}")
        lit = st.get("last_interaction_time") or 0
        if lit:
            lines.append("上次交互 " + time.strftime("%m-%d %H:%M", time.localtime(lit)))
        lines.append(f"存储后端 {_BACKEND}")
        evs = await self._recent_events(key, limit)
        if evs:
            lines.append(f"最近 {len(evs)} 条事件（新到旧）")
            for ts, source, dim, delta in evs:
                d = int(delta or 0)
                lines.append(f"{time.strftime('%m-%d %H:%M', time.localtime(ts))} "
                             f"{source} {dim} {'+' if d > 0 else ''}{d}")
        else:
            lines.append("暂无事件记录")
        return "\n".join(lines)[:600]

    @filter.command("persona_state")
    async def cmd_persona_state(self, event: AstrMessageEvent):
        """查看当前会话的状态快照和最近事件。/persona_state 10 可指定条数。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        limit = 5
        raw = (getattr(event, "message_str", "") or "").strip()
        for token in raw.split()[1:]:
            if token.isdigit():
                limit = max(1, min(int(token), 20))
                break
        text = await self._build_state_report(umo, limit)
        logger.debug(f"[cmd] persona_state umo={umo} limit={limit}")
        if hasattr(event, "plain_result"):
            yield event.plain_result(text)
            return
        await event.send(text)

    async def terminate(self):
        bot_name = _get_bot_name(self.config)
        # 退出前把还没落盘的状态同步写掉
        flush_all_sync()
        global _FLUSH_WORKER
        if _FLUSH_WORKER is not None and not _FLUSH_WORKER.done():
            _FLUSH_WORKER.cancel()
        logger.info(f"{bot_name}状态管理插件已卸载")
