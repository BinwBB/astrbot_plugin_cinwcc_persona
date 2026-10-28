"""
角色状态管理插件
维护好感度、淫乱度、恶堕值、情绪等跨轮持久化数值。
通过 LLM 请求/响应钩子实现状态注入和更新。

设计原则：
  - SKILL.md / SUPPLEMENT.md 是人设默认值的唯一来源
  - 配置项默认值 = SKILL.md 默认值，用户修改即覆盖
  - 启动时验证 SKILL.md 文件存在性，输出同步状态日志
"""

import json
import os
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
        "stutter_done": False,
        "conversation_log": [],
        "_last_date": "",
    }


def load_state(umo: str, config: dict = None) -> dict:
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
    _ensure_dir()
    _write(_state_path(umo), state)


def _write(path: Path, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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




def build_inject_text(state: dict, user_id: str, user_msg: str,
                       config: dict = None,
                       context_entries: int = 20, msg_max_chars: int = 200,
                       thought_mode: str = "内心想法") -> str:
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
    aff_line = f"你对ta{aff_feel}。\n" if enable_aff else "\n"
    dup_line = f"TA刚发了条跟之前一模一样的消息，今天已经第{dup_count}次了。\n" if dup_count > 1 else ""
    if minutes is None:
        time_line = ""
    elif minutes == "刚刚":
        time_line = "TA刚发完上一条。\n"
    else:
        time_line = f"上条消息就在{minutes}发的。\n"
    ctx_line = f"{context}" if inject_ctx else ""
    return (
        f"【{bot_name}当前感受】\n"
        f"现在是{period_label()}了。心情{em_label}{dep_suffix}。{lewd_feel}。\n"
        f"{stutter_line}"
        f"【关于聊天对象】\n"
        f"你在跟{user_id}聊天。{aff_line}"
        f"{dup_line}"
        f"{time_line}"
        f"{restriction}"
        "【行为参考】行为规则见上文SKILL.md中情绪/时段/好感度部分\n"
        "\n"
        f"{ctx_line}"
    )


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


# ── 插件主体 ──

@register("astrbot_plugin_lili_state", "mcxxiu", "角色状态管理插件", "1.5.2")
class LiliStatePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._patch_config_defaults()
        self._ensure_skills()
        self._log_cache = {}  # umo -> conversation_log list（save_conversation_log=False 时用）

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
            state = load_state(umo, self.config)

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

            ctx_entries = self.config.get("conversation_context_entries", 20)
            msg_max_chars = self.config.get("user_msg_max_chars", 200)
            thought_mode = self.config.get("bot_thought_mode", "内心想法")
            inject = build_inject_text(state, uid, msg,
                                       config=self.config,
                                       context_entries=ctx_entries,
                                       msg_max_chars=msg_max_chars,
                                       thought_mode=thought_mode)

            # 记录该用户最后被看到的时间戳（随状态文件持久化，与 save_conversation_log 无关；
            # minutes_since_last 在日志里找不到该用户上一条时用它回退）
            state.setdefault("last_seen", {})[uid] = time.time()

            # 注入到当前用户消息（req.prompt），而非历史上下文（req.contexts）
            # 原因: Runner 构建消息时:
            #   messages = bind_checkpoint_messages(request.contexts)  ← 历史消息
            #   messages.append(assemble_context(request))            ← 当前消息来自 req.prompt
            # 所以 inject 挂到 req.prompt 头部，才会出现在当前消息最前面
            _injected = False
            if req.prompt:
                req.prompt = f"{inject}\n\n{req.prompt}"
                _injected = True
            elif req.system_prompt:
                req.system_prompt += f"\n\n{inject}\n"
                _injected = True
            else:
                req.system_prompt = inject

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

    async def terminate(self):
        bot_name = _get_bot_name(self.config)
        logger.info(f"{bot_name}状态管理插件已卸载")
