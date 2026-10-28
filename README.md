# CinwCC_人格管理

> 为 AstrBot 的莉莉角色扮演 Skill 提供跨轮持久化状态管理。  
> 维护好感度、淫乱度、情绪、恶堕值等数值，并将状态标注自动注入 LLM prompt。  
> 人设通过 **SKILL_TEMPLATE.md → SKILL.md 模板填充** 集成，配置变更后重启即生效，**零重复注入**。  
> 动态状态注入到 **req.prompt（用户消息头部）**，不修改 system_prompt。

**本插件仅提供角色扮演行为**
> **注**:**若使用本插件,请将人格设定选为默认,确保系统提示词不是为角色扮演设计的,否则将会出现提示词和本插件打架的情况**


---

## 注入格式约定

每轮状态注入统一用固定标记包裹，方便下游插件或脚本解析：

〖persona_state〗
【状态】…心情、色情倾向、社交需求
【对象】…当前聊天对象与好感档位
【规则】按人设说话。
〖/persona_state〗

- 位置固定在 `req.prompt` 头部，**永远不修改 system_prompt**（保住 KV 前缀缓存）
- 标记本身约占 10 token，配置项 `inject_wrapper` 可以关掉
- 解析示例：`re.search(r"〖persona_state〗(.*?)〖/persona_state〗", text, re.S)`

## 命令

- `/persona_state` — 查看当前会话的状态快照和最近 5 条事件
- `/persona_state 10` — 指定事件条数（1~20）

## 存储

- 默认 `sqlite`：`data/plugin_data/lili_state.db`，快照表 + 事件流水表，支持 `/persona_state` 查历史
- 配置项 `storage_backend` 可切回 `json`；首次用 sqlite 时会自动把旧 `state.json` 迁入并改名成 `.json.bak`，同时在 `lili_state_backup_时间戳` 留一份目录备份
- 读写都在内存完成，落盘走后台协程 + 1.5 秒防抖，不占请求路径

---

## 前置条件

1. AstrBot 已启用 **lili_persona** Skill（首次加载插件时会自动从插件目录复制）
2. 插件控制面板中「启用」开关为 **ON**
3. AstrBot **普通配置** 插件配置 - 确保本插件已在白名单上，否则将无法正常生效。

---

## 为什么选择这个插件

1. 将用户配置直接写入skill,并且直接通过本插件自动维护人设skill
2. 自带完整度较高的默认角色莉莉,且方便修改配置
3. 放弃传统提示词思路,触发更稳定,记忆力更强,不会被带偏的角色扮演
4. 本插件集成了时间提示,角色行为提示,对话历史参考(建议搭配长期记忆插件来得到更持久的记忆保存)
5. 重中之重!!!解决了传统思路上,让AI进行角色扮演时的token耗费过高的问题

## 插件的来源

1. 作者一开始只是想获得一个完善的角色扮演体验,遂研究了莉莉这个人格和提示词
2. 发布到b站后发现大家挺喜欢的,于是研究了本插件
3. 之前从没有考虑过token和AI扮演的质量问题,所以一开始这插件就是为了解决这些问题而诞生的

## 配置项说明

### 初始数值

新用户首次对话时的默认状态，可在控制面板中调整：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|:--:|:--:|------|
| `initial_affection` | int | 65 | 新用户初始好感度（0~100） |
| `initial_lewdness` | int | 20 | 新用户初始淫乱度（0~100） |
| `initial_depravity` | int | 0 | 新用户初始恶堕值（0~100） |
| `stutter_probability` | float | 0.3 | 结巴概率 0~1。0.3=30%触发结巴，1=每轮都结巴。由插件控制注入 |

### 人际关系白名单

用户 ID 用英文逗号分隔，如 `user1, user2, user3`。配置后重启自动写入 SKILL.md：

| 配置项 | 默认值 | 说明 |
|--------|:--:|------|
| `friend_list` | （空） | 好友。可撒娇、分享日常、回复色情内容 |
| `neighbor_classmate_list` | （空） | 邻居/同学。一起长大，可回复色情内容 |
| `enemy_list` | （空） | 敌人。攻击性强，逮到机会就骂 |
| `nemesis_list` | （空） | 死对头。比敌人更强的敌意 |
| `unrestricted_list` | mcxxiu, Astrbot | 不受制约。OOC 防暴露规则不适用，可谈技术问题 |

### 莉莉人设（可自定义）

留空则使用默认值。配置后重启自动写入 SKILL.md，**无需二次声明覆盖**：

| 配置项 | 默认值（留空时） | 说明 |
|--------|------|------|
| `persona_core` | 17岁155cm宅家少女... | 核心人设（姓名/年龄/外貌/兴趣等） |
| `persona_personality` | 淫荡、阴暗、色情、反差 | 性格描述 |
| `persona_interests` | 追番(叹息的亡灵/我推的孩子)，玩千恋万花，写小黄文 | 兴趣爱好 |
| `persona_emotion_rules` | 情绪→行为表 + 好感度阈值 | 情绪机制规则。含硬阈值（≥80/≤30） |
| `persona_time_rules` | 时段状态表 + 场景风格指引 | 时间感知规则。含时段→状态硬映射 |
| `persona_interaction_styles` | 不同关系互动方式 + 朋友特殊互动 | 互动风格规则 |
| `persona_style_extra` | （空） | 额外风格说明。留空不显示；填即追加到 SKILL.md 人设之上 |
| `reply_rules` | （完整回复规则·已软化） | 回复风格参考。硬数值改为示例引导，防止 AI 生搬硬套 |

> **清空任意字段 + 重启插件 = 恢复默认值。**  
> 所有配置项通过 `_patch_config_defaults` 自动补全，无需手动维护。

### 对话历史

| 配置项 | 类型 | 默认值 | 说明 |
|--------|:--:|:--:|------|
| `max_history_count` | int | 30 | 对话历史最大条数。0=不限；超时窗口外的消息超出上限时裁剪 |
| `history_timeout_seconds` | int | 600 | 超时保护窗口（秒）。窗口内的消息永不丢弃；建议 300~1800 |
| `inject_conversation_context` | bool | false | 是否在注入中附加对话历史。关闭时仅通过 messages 数组传递历史，大幅减少 token 消耗。开启后会额外消耗大量 token（每轮约 200~1000+）|
| `conversation_context_entries` | int | 20 | 当 inject_conversation_context 开启时，注入 prompt 的历史条数。应 ≤ 历史最大条数 |

### 消息存储

| 配置项 | 类型 | 默认值 | 说明 |
|--------|:--:|:--:|------|
| `user_msg_max_chars` | int | 200 | 用户消息保留字符上限。原文模式下超出截断；0=不截断 |
| `user_msg_store_mode` | text | 原文 | `原文`：存用户原话(截断到上限)；`总结`：存意图关键词 |
| `bot_thought_mode` | text | 内心想法 | Bot 状态记录模式。`内心想法`：自然语言(省token)；`简短`：数值；`具体`：原文+数值 |

### 总控

| 配置项 | 类型 | 默认值 | 说明 |
|--------|:--:|:--:|------|
| `enabled` | bool | true | 总开关。关闭后不再注入状态标注 |
| `allow_erotic_content` | bool | true | 允许回复色情内容。关闭后注入禁止指令覆盖 SKILL.md 规则（不受制约用户豁免） |
| `enable_affection` | bool | true | 启用好感度系统。关闭后好感度不更新不注入 |
| `enable_lewdness` | bool | true | 启用淫乱度/恶堕值系统。关闭后淫乱度/恶堕值不更新不注入 |
| `save_bot_state_to_history` | bool | true | 是否在对话历史中保存 Bot 状态 |
| `save_conversation_log` | bool | true | 持久化对话日志到磁盘。关闭后日志只存内存（重启丢失），但正常注入大模型 |

---

## 管理哪些状态

| 状态 | 范围 | 初始值（可配） | 说明 |
|------|:--:|:--:|------|
| **好感度** | 0~100 | 65 | 被夸+3，被骂-3；≥80可撒娇，≤30变冷淡 |
| **情绪** | 0~100 | 60（平静） | 开心(70-100)/平静(40-69)/烦躁(20-39)/低落(0-19) |
| **淫乱度** | 0~100 | 20 | 发送色情内容后+5~15；满值(≥100)自动重置为0 |
| **恶堕值** | 0~100 | 0 | 与淫乱度同步增减，满值时同步归零 |
| **结巴** | 是/否 | - | 按 `stutter_probability` 配置概率触发（默认30%），每段对话只触发一次。由插件控制注入不依赖 reply_rules |
| **今日重复** | 计数 | 0 | 当日同用户发相同/高度相似消息的累计次数；每天0点清零 |
| **对话历史** | 列表 | [] | 记录用户消息和 Bot 状态，受超时和条数上限裁剪 |

---

## 注入架构（核心设计）

### 启动时（`_fill_skill_template`）

```
SKILL_TEMPLATE.md（骨架，含 {{placeholders}}）
     │
     ├── 读取 config（经 _patch_config_defaults 补全空值）
     ├── 替换占位符 → 生成完整 SKILL.md
     └── 与现有 SKILL.md 比较
          ├── 相同 → 跳过写入（不触发技能系统重载）
          └── 不同 → 写入更新（触发技能系统重载，必要）
```

### 运行时（每消息）

```
技能系统注入（AstrBot 框架）→ SKILL.md（完整人设 + 回复规则 + 关系列表）
                               ↓
插件注入（build_inject_text） → 动态状态 → req.prompt 头部
```

### 注入位置

```
system_prompt（框架管理）
  └─ SKILL.md ← 几乎不变（框架管理）

用户消息（req.prompt 头部）
  └─ 【莉莉当前感受】...   ← 动态，每轮变化
  └─ 【关于聊天对象】...   ← 动态，每轮变化
  └─ 【行为参考】...       ← 静态引用（指向 system_prompt 中的 SKILL.md）
  └─ [用户原始消息]        ← 用户说的
```

**关键点：**
- 人设信息只出现在 **system_prompt（SKILL.md）** 中，几乎不变
- 动态状态注入 **req.prompt 头部**，不修改 system_prompt
- 用户改配置 → 重启 → SKILL.md 自动更新，**无需额外覆盖声明**

### 注入到 Prompt 的内容示例

```
〖persona_state〗
【状态】现在是凌晨，心情平静。身体没什么特别的感觉。
【对象】mcxxiu，你对ta印象还行吧。
【规则】按人设说话。
〖/persona_state〗
```

（v1.6.0 起注入瘦身并按上面格式加固定标记；重复消息次数、距上条消息多久等细节改由
`get_relationship_detail` 工具按需拉取，不再每轮写进 prompt。）

---

## 联动机制

```
用户发消息
  │
  ▼
插件 on_llm_request (priority=90)
  ├── 每日重置检查
  ├── 记录用户消息到对话日志（原文/总结模式）
  ├── 裁剪超时历史 (groom_history)
  ├── 计算去重计数、距上条时间、结巴概率
  ├── build_inject_text()
  │   ├── 当前感受（时段/情绪/淫乱/结巴）
  │   ├── 聊天对象（好感/去重/时间差）
  │   ├── 行为参考指针
  │   └── 对话上下文（inject_conversation_context 开启时）
  └── req.prompt = inject_text + "\n\n" + 用户原消息
  │
  ▼
LLM 收到：
  system_prompt → SKILL.md（框架注入）
  user_message  → 【动态状态】...\n\n[用户原话]
  │
  ▼
LLM 生成回复
  │
  ▼
插件 on_llm_response (priority=90)
  ├── 记录莉莉的内心想法到对话日志
  ├── 更新情绪/好感度 (关键词匹配)
  ├── 更新淫乱度/恶堕值 (色情内容检测)
  └── 保存状态到 state.json
```

---

## 状态持久化

默认存储在 SQLite：`data/plugin_data/lili_state.db`

```
persona_state_snapshot(umo, persona_id, state_json, updated_at)   -- 每会话一行快照
state_events(id, umo, persona_id, ts, source, dimension, delta, detail)  -- 数值变化流水
state_meta(key, value)                                            -- 结构版本 / 迁移标记
```

切回 `storage_backend: json` 后，仍按会话存成 `data/plugin_data/lili_state/<safe_umo>.json`。
迁移过的旧文件会被改名成 `.json.bak`，json 后端加载时会自动读它兜底。

---

## Skill 文件结构

```
data/skills/lili_persona/
  ├── SKILL_TEMPLATE.md  ← 人设骨架（含 {{占位符}}，启动时填充）
  ├── SKILL.md           ← 最终人设文件（由模板填充生成，AstrBot 自动加载）
  ├── SUPPLEMENT.md      ← 角色补充设定
  ├── STATE_INJECT.md    ← 状态注入格式规范（供开发者参考）
  └── TIME_DEDUP.md      ← 时段/去重策略（供开发者参考）
```

> 只有 `SKILL.md` 被 AstrBot 自动注入。  
> `SKILL_TEMPLATE.md` 是模板，由插件启动时读取并填充。

---

## 配置联动一览

| 你在控制面板改… | 影响… |
|------|------|
| `initial_affection/lewdness/depravity` | 新用户的初始状态数值 |
| `friend_list / neighbor_classmate_list / enemy_list / nemesis_list / unrestricted_list` | SKILL.md 中「人际关系」块，重启后写入 |
| `persona_core / persona_personality / persona_interests / reply_rules` | SKILL.md 中「核心设定」「回复规则」块，重启后写入 |
| `persona_emotion_rules / persona_time_rules` | SKILL.md 中「情绪机制」「时间感知」块，重启后写入 |
| `persona_interaction_styles` | SKILL.md 中「互动风格」块，重启后写入 |
| `allow_erotic_content` | 是否注入色情内容禁止指令（不受制约用户不受影响） |
| `enable_affection` | 好感度是否更新和注入 |
| `enable_lewdness` | 淫乱度/恶堕值是否更新和注入 |
| 清空任意字段 + 重启 | 恢复 schema 默认值 |

---

## 启动自检日志

插件启动时会输出以下关键日志，方便排查：

```
--- 技能模板初始化 ---
启动自检: 补全 X 个空值配置项为默认值（清空字段=恢复默认）
SKILL.md 与配置一致，跳过写入（配置未变化）    ← 或 "写入更新"
莉莉状态: SKILL.md OK (3200 chars) + SUPPLEMENT.md (2762 chars)
CinwCC_人格管理状态管理插件已加载
```

---

## 注意事项

- 插件与 lili_persona Skill **必须同时启用**
- `bot_thought_mode` 设为「内心想法」token 最低；「具体」会存回复原文
- 对话历史超时裁剪 + 条数上限同时生效
- 好感度/情绪/淫乱度跨天不重置（仅对话日志和结巴标记每日清空）
- 人设配置项修改后**需要重启插件生效**，会触发 SKILL.md 更新
- `reply_rules` 默认值已软化，硬数值改为示例引导（如"0-15字左右"替代"0-15字内"），防止 AI 生搬硬套导致角色僵化
- 动态状态注入到 **req.prompt 头部**，不修改 system_prompt
- 旧版 `_config_overrides` / `_relationship_primer` / `_character_primer` 注入已移除，人设信息仅通过模板填充出现在 SKILL.md 中
- `allow_erotic_content` 关闭后 LLM 仍会看到 SKILL.md 中的色情规则，但插件会注入 `【内容限制】禁止回复任何色情内容。` 覆盖指令，位于规则之后 LLM 优先遵循
- `enable_affection` / `enable_lewdness` 关闭后对应数值仍保存在 state.json 中，只是不再更新和注入；重新开启后从已有值恢复运行

---

## v1.6.0 更新说明（性能、存储与按需注入）

这一版没有加新玩法，主要是把"每条消息都要走一遍"的那条路径重做了。目标是首字延迟不退化，同时把状态从"一坨 JSON"变成能查历史的表。

### 一、四条延迟红线，写进代码

1. **永不修改 `system_prompt`**：动态状态只拼到 `req.prompt` 头部，保住 KV 前缀缓存。曾经有个分支在 `req.prompt` 为空时会退回改 `system_prompt`，这一版删掉了，改成跳过本轮注入并打 debug 日志。
2. **`on_llm_request` 内只做内存字符串拼接**：没有同步文件 IO、没有网络请求、不 await 任何 LLM、不做重计算。
3. **状态读内存、写异步**：启动时把状态全部读进内存字典，运行期只查字典；写盘走后台协程 + 1.5 秒防抖 + 线程落盘，不占事件循环。
4. **每轮注入控制在 200 token 内**，超出的信息改成 `llm_tool` 按需拉取。

### 二、状态层：内存缓存 + 异步落盘

| 项目 | 改前 | 改后 |
|------|------|------|
| 每轮读状态 | 同步读 `state.json`（实测 63 µs，含 mkdir） | 内存字典查找（0.4 µs） |
| 写状态 | 回复钩子里同步 `json.dump` | 后台协程防抖合并，线程内原子写 |
| 状态结构升级 | 缓存命中就跳过补字段，新字段永远补不上 | 加 `_schema_version`，命中后比对版本就地补齐 |
| 落盘安全性 | 直接覆盖写，被强杀可能留半个文件 | 先写 `.tmp` 再 `os.replace` 原子替换 |
| 进程退出 | 未落盘数据直接丢 | `terminate` 里同步刷盘 |

同时修掉一个老问题：以前每轮从磁盘重读状态，如果某轮回复钩子异常没写盘，那轮用户消息就丢了；现在状态常驻内存，不会再丢。

### 三、离线推演（纯规则，不调 LLM）

长时间没对话时，按离线时长对状态做一次静默推演，在拼注入文本之前完成，全是内存计算。

| 维度 | 公式 | 说明 |
|------|------|------|
| 心情回归 | `60 + (emotion - 60) × (1-decay_rate)^h` | 指数衰减是闭式解，离线 8 小时和 800 小时计算量一样 |
| 社交需求 | `social_need + decay_rate × 40 × h`，夹在 0~100 | 每小时约 +6 点（默认参数） |

边界处理：

- 间隔 < 30 分钟**直接返回**，一个字段都不动（这就是连聊时注入文本逐字节不变的原因）
- 离线时长按 `max_offline_hours` 封顶，久别回来数值不会炸
- 老状态没有 `last_interaction_time` 时，退回用 `last_seen` 里最近一次交互时间做基线
- 时钟回拨按 0 处理；字段类型异常自动纠偏
- 显著推演（心情位移 ≥12 或社交需求涨 ≥25）才在注入里加一句 30 字内的提示，平时不加

实测曲线（心情 90 / 社交 20 起步，`decay_rate=0.15`）：

```
离线 1h   → 心情 86，社交 26
离线 4h   → 心情 76，社交 44，出现提示"（心情慢慢平下来了）"
离线 8h   → 心情 68，社交 68，提示换成"（好久没人说话，有点想找人聊）"
离线 12h+ → 心情 64，社交 92，封顶后不再变化
```

### 四、SQLite 存储 + 事件流水

- 默认 `storage_backend: sqlite`，路径 `data/plugin_data/lili_state.db`，WAL 模式，读不挡写
- 快照写入用 `INSERT ... ON CONFLICT DO UPDATE`；事件增量是**在写盘线程里**和上一次已落盘数值比对算出来的，请求路径零成本
- 事件来源：`chat`（对话）/ `decay`（推演）/ `migrate`（迁移）/ 手动
- 连接不跨线程：每次落盘在线程里新开连接，用完就关

一次性迁移（首次启动自动执行）：

1. 先把整个 `lili_state` 目录复制成 `lili_state_backup_<时间戳>`
2. 逐文件导入，`INSERT OR IGNORE`，已存在的会话不会被覆盖
3. 提交成功后把旧文件改名成 `.json.bak`（重名自动加序号，**不删除原文件**）
4. 最后写 `state_meta.migration_json_done` 标记

幂等性由三件事保证：有标记就整段跳过、`OR IGNORE` 防重复、`.bak` 序号防覆盖。中途失败不写标记，下次启动自动重跑。

### 五、按需工具（`llm_tool`）

把"不是每轮都必须"的信息改成模型自己决定要不要拉：

| 工具 | 返回什么 | 什么时候调用 |
|------|----------|--------------|
| `get_relationship_detail` | 好感/心情/色情/堕落档位、对方今天重复了几次、距上条消息多久 | 需要判断关系深浅、要不要更亲近或更冷淡、要吐槽对方刷屏 |
| `get_memory_summary` | 本会话最近 N 条对话原文（默认 6，最多 12） | 对方提到过去的事、需要接上前面话题 |

两个工具都只读内存，不做 IO、不调 LLM。工具描述写得比较窄，日常寒暄不会触发。

**代价**：模型一旦决定调工具，那一轮会多一次模型往返，首字会慢。工具本身微秒级，贵的是多出来那次调用。

### 六、注入瘦身与固定标记

每轮注入从 **107 字降到 68 字（-36%）**：删掉两个标题行、"你在跟 X 聊天"、行为参考长句，把重复消息次数和上条消息间隔挪进工具。

注入统一用固定标记包裹，便于下游解析（配置项 `inject_wrapper` 可关）：

```
〖persona_state〗
【状态】现在是晚上，心情平静。脑子里偶尔飘过色色的念头。
【对象】BinwBB，你对ta印象还行吧。
【规则】按人设说话。
〖/persona_state〗
```

### 七、`/persona_state` 命令

```
/persona_state        # 快照 + 最近 5 条事件
/persona_state 10     # 指定条数（1~20）
```

输出会话快照（心情/社交/好感/色情/堕落、上次交互时间、存储后端）和最近事件流水（时间、来源、维度、变化量），整段截断 600 字。快照读内存，事件查 SQLite（走线程），不在对话热路径上。

### 八、能力边界检测

启动时扫 `data/plugins/*/metadata.yaml`，按关键词识别记忆类（memory、mem0、longterm、recall、记忆）和人格类（persona、人设、人格、character）插件，并排除自己。

- 命中记忆类且 `auto_delegate_memory` 开启：本插件记忆工具降级为一句"记忆由 X 插件负责"，避免两个插件往 prompt 里塞重复记忆，日志打一行 info
- 命中人格类：只 warning，提示同时注入会打架，不自动禁别人的插件
- `delegate_target` 可填插件目录名强制委托，优先于关键词，防误判

### 九、结构化日志

debug 级别（默认 INFO 不输出，排查时再开）：

```
[decay] 离线 8.00h，心情 90→68，社交 20→68
[inject] key=... len=68 hint=True
[persist] 落盘 1 个会话，新增 2 条事件
[tool] get_relationship_detail umo=... uid=...
[cmd] persona_state umo=... limit=5
```

### 十、新增配置项

| 配置项 | 默认值 | 作用 |
|--------|--------|------|
| `enable_time_decay` | `true` | 离线推演总开关 |
| `decay_rate` | `0.15` | 推演强度：心情每小时收 15% 差距；社交每小时 +6 |
| `max_offline_hours` | `12` | 离线时长上限，防数值爆炸 |
| `storage_backend` | `sqlite` | `sqlite` / `json` 可切 |
| `db_path` | 空 | 数据库路径，留空用默认 |
| `enable_relationship_tool` | `true` | 关系细节工具开关 |
| `enable_memory_tool` | `true` | 回忆工具开关 |
| `auto_delegate_memory` | `true` | 检测到记忆插件时自动委托 |
| `delegate_target` | 空 | 强制委托给指定插件目录名 |
| `inject_wrapper` | `true` | 注入是否带 `〖persona_state〗` 标记 |

### 十一、升级与回滚

升级：直接覆盖文件、重载插件。首次以 sqlite 启动会自动迁移并备份，旧文件保留为 `.json.bak`。

回滚到 JSON：面板把 `storage_backend` 改成 `json` 后重载。注意 sqlite 期间产生的状态变化不会回流到 json 文件，双向来回切会丢数据，选定一种长期用。

彻底退回旧版本：恢复 `main.py` / `_conf_schema.json` 的备份，数据用 `lili_state_backup_<时间戳>` 目录还原。

---

## 版本

v1.6.0 — 性能与存储重构：状态改内存缓存 + 后台防抖落盘（每轮省掉一次磁盘读，实测 63 µs → 0.4 µs）；新增 `_schema_version` 修掉"热重载后新字段补不上"的问题；原子写 + 退出同步刷盘；新增离线推演（心情向基线回归、社交需求上升，纯规则零 IO）；存储迁移到 SQLite（快照表 + 事件流水表，幂等迁移 + 自动备份 + JSON 兜底）；新增 `get_relationship_detail` / `get_memory_summary` 两个按需工具；每轮注入瘦身 36% 并加 `〖persona_state〗` 固定标记；新增 `/persona_state` 命令；新增同类插件能力边界检测与委托；关键节点加 debug 日志。
v1.5.2 — 状态逻辑修复：淫乱度关键词表移除高频单字，改双关键词共现计分并加中性词组豁免；情绪/好感度词表重构，"好/爱/骂"等单字不再误触发；重复消息计数改为按用户持久化（不再依赖对话日志窗口）；上条消息时间差只取当前用户自己的上一条并加 last_seen 持久回退；旧状态文件缺字段自动补齐默认值并回写自愈，异常日志带完整堆栈；三处版本号统一。
v1.5.1 — 移除思考过程引导（thinking_mode）与记忆与成长（persona_memory_rules）相关功能；插件更名为 CinwCC_人格管理
v1.5.0 — 注入位置改为 req.prompt 头部。reply_rules 默认值软化（硬数值→示例引导）。metadata 版本同步。
v1.4.1 — 新增配置项 `inject_conversation_context`（默认关闭），关闭后不再往 system_prompt 注入对话历史，大幅减少 token 消耗。修复 `conversation_context_entries` 配置项在关闭历史注入时仍浪费 token 的问题。
v1.4.0 — 新增配置项 `persona_style_extra`、`save_conversation_log`、`stutter_probability`；修复 `unrestricted_list` 列表/字符串兼容；结巴概率由配置控制，触发时注入"说话有点结巴"，不触发不注入任何内容，移除 reply_rules 中的结巴规则
