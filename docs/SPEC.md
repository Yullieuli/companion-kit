# 规格：companion-kit（跨 agent 的伴侣运行时）

> 状态：**实施中**（0.9.2）。逐项完成情况见 §12。
> 目标读者：未来的我、以及任何要接手这套东西的人。

## 1. 一句话

把现在散在 hermes 各处的「生活感」——主动说话、自由时间、表情、群聊、刷 B 站、偏好、性格、心情、作息——收进**一个不绑定宿主的运行时**，让 dsh / hermes / openclaw / codex 都能挂同一套人格与记忆。

## 2. 为什么不是再写一个插件

现在这套东西的问题不是缺功能，是**同一件事有三份实现，互相打架**：

| 能力 | 现有实现 | 冲突 |
|---|---|---|
| 主动开口 | `pulse.py`（*/15 + 骰子 + 配额 8） | 与自由时间 cron、bili night 三套唤醒抢配额 |
| 自由时间 | cron `30 10,14,20` + `free_time_tick.py` | 时间钉死，与上面的骰子不同源 |
| 记忆 | `hindsight` 插件 + `said/life/opinion.jsonl` + whale diary | 四处写，互相不知道 |
| 偏好 | `prefs_like` 关键词 + `opinions` tags 加分 | 两套打分，口径不一 |
| 表情/图片 | `wardrobe.py`（空库）+ `pic.py` 中转 | 一个没内容，一个只管发 |
| 心情 | **没有** | 所有"情绪"都是每轮现算，跨轮不延续 |
| 作息 | **没有**（只有免打扰时段） | 凌晨也可能被叫醒 |
| 人物卡 | **没有**（只有全局人格） | 对不同人说话一个腔调 |

所以新项目的第一个交付物不是功能，是**一套唯一的状态与唯一的时钟**。

## 3. 非目标

- 不做聊天前端、不做模型代理、不做多租户 SaaS。
- 不追求"支持所有 IM"：先 QQ（OneBot/NapCat），其它通道按接口补。
- 不把人格写死：人格是数据（`SOUL.md`），不是代码。
- 不在 core 里读环境变量、读宿主配置：core 只认自己的 store 目录与显式入参。

## 4. 三层架构

```
┌────────────────────────────── 宿主（任选其一）──────────────────────────────┐
│  dsh (cordis)        hermes (python plugin)      openclaw / codex (MCP only) │
└───────────┬───────────────────┬──────────────────────────┬───────────────────┘
            │ adapter-dsh       │ adapter-hermes           │ 直接 MCP
            ▼                   ▼                          ▼
┌──────────────────────────── 契约层（唯一入口）───────────────────────────────┐
│  ① MCP server（tools + resources）      ② Context 注入接口（HTTP/CLI）        │
│  ③ 事件订阅（SSE/长轮询，给调度器用）    ④ 文件协议（store，可离线读写）        │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
┌────────────────────────────── core（纯逻辑）─────────────────────────────────┐
│  clock 作息/时间感知   mood 心情状态机   bond 人物卡   memory 记忆编译         │
│  drive 主动决策       quota 配额/衰减    voice 拟人化分条   safety 身份/注入    │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
┌────────────────────────────── 适配器（IO）───────────────────────────────────┐
│  qq(onebot)   bili   websearch   vision   tts   phone(mcp)   qzone           │
└──────────────────────────────────────────────────────────────────────────────┘
```

> **实现现状（2026-10-29 实测）**：这层目前**只有 bili、vision、tts、phone 落地**。
> `qq(onebot)` 由宿主侧的 hermes napcat 插件承担（core 不碰通道）；
> **`websearch` 由 hermes 自带**——联网搜索是宿主已有的能力，core 不重复实现；
> **`qzone` 不做**——"发动态"不是 companion 的事，本轮不做。

**关键约束**：core 不 import 任何宿主 SDK，不直接发网络请求。所有 IO 通过注入的 port（函数/对象）传进来，这样它才能被单测，也才能在三种宿主里跑同一份逻辑。

## 5. 四种接口，缺一不可

### 5.1 MCP tools（给"只会调工具"的宿主）

| 工具 | 作用 | 主要参数 |
|---|---|---|
| `companion_context` | **取本轮该注入的上下文**（人格摘要 + 相关记忆 + 心情 + 时间 + 待办生活事件） | `session_id`, `channel`, `budget_chars` |
| `companion_react` | 把这一轮的感受/判断写回（心情、人物卡、记忆候选） | `session_id`, `mood_delta`, `notes` |
| `companion_say` | 发消息（分条、加表情、按人设改语气） | `channel`, `target`, `segments[]` |
| `companion_life` | 自由时间：取今日事件 / 回填反应 | `action=events\|react` |
| `companion_mood` | 读/调心情（跨频道共享） | `action=get\|nudge` |
| `companion_card` | 人物卡查询/更新 | `user_id`, `favor_delta`, `tags` |
| `companion_wake` | 问"我现在该主动说话吗"，返回理由或 `[SILENT]` | `channel`, `now` |
| `companion_bili` | 刷片/深读/回填/分享（0.3.0 已实现） | `action=tick\|debrief\|commit\|share\|status` |

设计原则：**工具返回的是"数据 + 一句为什么"**，不是长文案；让模型自己组织语言。

### 5.2 Context 注入（给"靠系统提示"的宿主）

一次 HTTP/CLI 调用，返回一段纯文本，宿主负责塞进 system prompt 或 hook：

```
GET /context?session=xxx&channel=qq:group:123&budget=2500
→ { text, hash, expires_at }
```

分块与优先级（借鉴 dsh-plugin-memory 的预算分配）：

1. `SOUL.md` 摘要（人格、称呼、底线）——固定前 600 字
2. 当前心情 + 时间感知（现在几点、周几、距上次聊天多久、是否在作息窗口）
3. 与本次输入相关的记忆（top-k，按 `last_access` 衰减）
4. 人物卡（当前对话对象：好感/熟悉度/雷区/上次聊到哪）
5. 生活事件（近 24h，未反应优先）

**投影去重**：每块算 hash，宿主侧缓存；未变化不重复注入（省 token 的关键）。
hermes 侧已实装：状态没变就整轮注入空（上一轮那份还在会话历史里），
每 6 轮强制全量一次，避免历史被压缩后她丢状态。

**心情怎么动（`mood_rules`）**：只认可观察的行为，每条都能指着事件说"因为这件事"——
开口了 closeness +0.6 / energy −0.2；隔 12 小时以上才回来 satisfaction +3；
刷到评分 ≥6 的视频 +1.5；分享出去 +2。每天有上限（closeness 8 / satisfaction 10 / energy −6），
不然一天聊 50 句就成了计数器而不是关系。

**两处有意偏差（不改，附理由）**

| 规格 | 实现 | 为什么保留实现 |
|---|---|---|
| `people/<channel>-<uid>.md` | `people/*.json` | 人物卡里的 `closeness` / `tags` / `last_seen` 是代码高频读写的字段，放进 markdown 要么每次正则解析、要么写回去时破坏人写的排版。人基本不改这文件，机器的需求优先 |
| `life/<yymmdd>/events.json` | `life/life.jsonl` 单文件 | 读它的是 pulse / said / migrate / bili 去重，都是"取最近 N 条/最近 72 小时"。按日目录要额外维护索引，跨天查询还得先扫目录；单文件 + 尾部读取更简单也更难写坏。需要按天看时 `companion life recent --days 1` 已经够 |

**编年史只有一本（10-28 起）**：写入统一走 `life/life.jsonl`。旧的
`~/.hermes/proactive/life.jsonl` 降级成**只读投影**——`life.py record` 先写 companion，
再把同一行追加到旧文件，供 pulse / said / status 这些还没迁移的读者继续读。
投影丢了不影响她，账本丢一条就是真的丢了一条。

### 5.3 事件订阅（给调度器）

```
GET /events?since=<cursor>         # 长轮询
→ [{type: "wake_due", at, reason}, {type: "mood_shift", from, to}, ...]
```

这样"什么时候说话"由 core 的作息+骰子决定，宿主只负责在事件到达时起一轮会话。**时钟只有一套。**

### 5.4 文件协议（人的入口）

store 是纯 markdown + jsonl + git，任何 agent 都能读写，也能人工改。

> ⚠️ 下面这棵树是**原始设计**。实现有两处**有意偏差**（§5.2 已列理由）：
> `people/*.md` → 实际是 `people/*.json`；`life/<yymmdd>/events.json` → 实际是
> `life/life.jsonl` 单文件。目录名与下文其余部分一致。

```
$COMPANION_HOME/                     # 默认 ~/.companion
├── SOUL.md                 # 人格：身份、称呼、语气、底线、关系
├── MEMORY.md               # 记忆协议（怎么写、怎么衰减）
├── memory/
│   ├── index.md            # 目录（谁存在）
│   └── pages/*.md          # 每页 frontmatter: tags/summary/last_access
├── people/<channel>-<uid>.json  # 人物卡（**有意偏差**：原设计是 .md）
├── mood/state.json         # 心情（全局一份）
├── rhythm.json             # 作息窗口、睡眠、问候
├── life/life.jsonl         # 生活事件（**有意偏差**：原设计是 life/<yymmdd>/events.json）
├── opinions.jsonl          # 观点（她自己的判断）
├── quota.json              # 主动次数、衰减、冷却
├── devices.json            # 设备白名单 / 危险词 / 主人开关 / OCR
├── group/config.json       # 群聊三档参数
├── bili/
│   ├── config.json         # 刷片开关/额度/口味词
│   ├── state.json          # 今天刷了几条、最近给过哪些
│   ├── traffic.json        # 读/写额度与风控冷静期（按需创建）
│   └── credential.json     # B 站 cookie（只在本机，不进仓库）
│   （config.json 里的 legacy_life 是迁移期只读的旧账本，旧插件下线后删掉）
├── shots/bili/{covers,frames}/  # 封面与九宫格抽帧（刷片后按需创建）
├── stickers/               # 表情库
├── voice/                  # 语音文件（speak/sing）
└── logs/{turns,cost,bili,said,events,initiatives,device,voice}.jsonl
```

### 5.5 设备控制（手）

一套 port，四种实现。**只暴露「动作 + 截图回执」，不暴露任意 shell / 任意脚本执行。**

| port | 方法 | Android | Windows 桌面 | 浏览器 | 游戏 |
|---|---|---|---|---|---|
| `Screen` | `size()` / `screenshot()` | `adb exec-out screencap -p` | Win32 BitBlt 或 DXGI | CDP `Page.captureScreenshot` | 窗口捕获（同桌面，指定 hwnd） |
| `Input` | `tap` / `swipe` / `type` / `key` | `adb shell input` | `SendInput`（ctypes，不用 pyautogui） | CDP `Input.*` | 同桌面 + 坐标映射 |
| `App` | `launch` / `list` / `focus` | `am start` / `dumpsys window` | `ShellExecute` / `EnumWindows` | 新标签 / navigate | 窗口置顶 + 分辨率对齐 |
| `Observe` | `ocr` / `describe` | 复用 vision 模型 | 同左 | DOM 快照 + 可读化 | 帧差 / 关键帧 |

**为什么游戏单独一列**：游戏不能靠 DOM，也没有控件树，只能「截图 + 坐标输入」。所以它比桌面多了两件必需品：
分辨率对齐（1080p 与 2K 的按钮位置不同）和前台窗口校验（后台输入会打到别的窗口）。
**边界：只做屏幕与输入，不注入进程、不读内存、不碰反作弊**——这条写进 README，也是这个模块的底线。

MCP 工具（三个，不拆细）：

| 工具 | 参数 | 行为 |
|---|---|---|
| `device_look` | `target=phone\|desktop\|browser\|game` | 返回截图路径 + 尺寸 + 前台窗口/页面标题 |
| `device_act` | `target`, `action`, `args` | 执行一个动作，返回执行前后两张截图 |
| `device_open` | `target`, `what` | 打开 App / 网址 / 程序（走白名单） |

**安全闸（P0，和功能同批上线，不允许先开能力后补闸）**：

1. **白名单**：`devices.json` 里列出的包名 / 窗口标题 / 域名才可控；其余一律拒绝。
2. **危险动作黑名单**：关机、卸载、转账、支付、删除、改密码、装未知来源 APK —— 拒绝并回报，不询问二次确认（避免被话术绕）。
3. **主人限定**：只在主人会话里可用；群里 @ 她、或陌生私聊，一律不触发设备操作。
4. **留痕**：每个动作写 `logs/device.jsonl`（动作、参数、前后截图路径、耗时），可回放。
5. **幂等与超时**：同一动作 3 秒内重复不执行；单动作超时 10 秒即中止并截图。

### 5.6 语音与唱歌

分两档，因为成本差一个数量级：

| 档 | 做法 | 成本 | 何时用 |
|---|---|---|---|
| **说话** | edge-tts / GPT-SoVITS 音色（本地） | ≈0（不占对话 token） | 亲近、有精神、夜里不发 |
| **唱歌** | so-vits-svc / DiffSinger 离线推理 | 一次几秒到几十秒 CPU/GPU，不花 token | 精力 ≥40 且心情好且 3 天没唱 |

落地顺序（别一上来就训模型）：

1. **P1**：先接 TTS，做"主动语音"——她高兴时发一句，而不是每次都打字。
2. **P2**：唱歌先做**降级版**：用 TTS 念白 + 节奏（哼唱感），验证"她想唱"这件事成不成立。
3. **P3**：再上 SVC，需要她自己的音色模型（训练素材、授权、显存）。
4. **边界**：只发私聊、单段 ≤30s、不上传公开平台、不唱未授权完整歌曲。

工具面：`companion_voice(text?|song_id?)` → 生成文件写 `store/voice/`，返回路径 + 时长，
**不返回音频本体**（避免把 token 花在 base64 上）。

## 5.7 成本架构（不是优化，是约束）

先说结论：这套东西的钱不是花在"想得多深"，是花在**每轮把同一段前缀重发一遍**。
之前实测过：一轮 2 次调用、六万多输入里 91% 是全价新输入，只因为隔了 1 小时 42 分缓存过期。
所以成本必须在架构里解决，不能靠月底看账单。

**六条机制，按收益排序：**

| # | 机制 | 省什么 | 落点 |
|---|---|---|---|
| 1 | **前缀分层**：静态（人格/规则）→ 会话（人物卡/记忆）→ 轮次（时间/心情） | 缓存命中率 | `context.py` 已按此顺序，宿主不得重排 |
| 2 | **块级 hash 去重**：只注入变化的块 | 每轮重复的 1–2 万 token | `context.Cache` |
| 3 | **事件门零成本**：该不该醒用纯脚本判，不调模型 | 每小时 4 次的空转 | `initiative.decide` 的硬闸 |
| 4 | **本地做完再进上下文**：ASR、抽帧、截图、弹幕统计、图片处理 | 视觉/语音 token | ports 层 |
| 5 | **分级模型**：筛选/打分/摘要用 flash，深读才升档 | 单轮 3–5 倍差价 | `cost.price_of` 支持按任务换模型 |
| 6 | **预算闸**：日预算 + 起会话前估价 + 性价比选动作 | 失控 | `cost.allow_turn` / `initiative` |

**账本**：每轮写 `logs/cost.jsonl`（新输入/缓存/输出/元），`companion cost` 直接看。
**KPI（正常作息下应达到）**：每轮新输入 < 5k、缓存命中 > 70%、日花费 < ¥1、空转轮次 = 0。

**一条硬规则**：`initiative` 选动作时，收益差 0.05 以内一律选便宜的（`decide()` 已实现）。
"想唱歌"和"想说句话"对她可能一样开心，但对账单不一样。

## 5.8 自主性契约（不需要别人提）

目标：她自己主动用，落到工程上就是：**每个能力都必须有 self-init 入口，且触发源只看她自己的状态。**

| 能力 | 自主级别 | 触发源 | 闸 |
|---|---|---|---|
| 说话 | `auto` | 有没说完的事 / 他刚说完 / 关系近 | 额度 + 间隔 + 作息 |
| 发语音 | `auto` | 亲近 + 有精神 + 有些天没发 | 同上 + 时段（夜里不发） |
| 唱歌 | `auto` | 精力高 + 心情好 + 3 天没唱 | 同上 + 时长 ≤30s |
| 分享视频 | `auto` | 手里有读完还没给他的 | 分享闸（评分/额度/回读） |
| 写日记 | `auto` | 任何时候（不打扰人） | 无 |
| 刷 B 站 | `auto` | 作息窗口内 | 请求预算 + 风控熔断 |
| 操作设备 | `ask` | 她想到要做，但必须先在对话里说一声 | 设备闸（白名单/黑名单/主人限定） |
| 发动态/空间 | `ask` | 同上 | 内容闸 |
| 改人格/记忆 | `never` | —— | 只有主人能改 |

三条配套：

1. **启动即读自己**：每次醒来先读 `initiative` + `life` + `opinions`，不是等 user message 才开始想。
2. **行为留痕**：她主动做了什么写 `logs/initiatives.jsonl`（动作、理由、花了多少、后来有没有被回应）。
   这份日志就是"偏好反哺"的原料——被回应过的动作下次加分，没人理的降权。
3. **不做"一次干三件"**：`decide()` 只返回一个动作。既真实，也便宜。

### 5.9 宿主接入（`companion connect`）

**问题**：这份规格的卖点是"换宿主行为不变"，但接入动作一直是手抄——
hermes 改 `config.yaml`、dsh 改 profile 的 `package.json`、openclaw / codex 各写一段
JSON / TOML。四份格式、四次升级、四处出错。"一行安装"于是变成
"一行装 core，剩下四个宿主各自接线"。

**做法**：一条命令，四段式（detect / plan / apply / verify）。

| 宿主 | 探测证据 | 写入点 | 校验 |
|---|---|---|---|
| hermes | `~/.hermes/config.yaml` | 适配层 + `hooks.pre_llm_call` / `post_llm_call` + allowlist | 两条钩子既在 config 又在批准表 |
| dsh | `~/.dsh/profiles/*/package.json` | `dependencies` + `dsh.profile.bundles` | 两个键都在 |
| openclaw | `~/.openclaw/openclaw.json` 或 PATH 有 `openclaw` | 优先 CLI，否则 `mcp.servers.companion` | JSON5 宽松解析通过 |
| codex | `~/.codex/config.toml` 或 PATH 有 `codex` | `[mcp_servers.companion]` | tomllib 解析通过 |

**四条不可协商的规则**

1. **不猜格式**：能用宿主 CLI 就用 CLI；没有 CLI 才按本机实证格式写，
   写前先读、写后必验。openclaw 的 MCP 段在公开资料里有 `mcp.servers` 与
   `mcpServers` 两种说法——所以先看用户文件里已经有哪一种，都没有才按官方文档口径。
2. **先备份再改**：`.bak-companion-<时间戳>`；校验失败自动还原
   （改过的还原，本次新建的删掉）。
3. **幂等**：已配过报"没有需要改的东西"，不重写、不堆备份。
4. **不覆盖手写段**：`openclaw.json` 已有 `mcp` 但没有 `companion` 时直接失败并说明，
   不做"合并猜测"。猜错的代价是用户的 agent 起不来。

**为什么不用 pyyaml / tomli**：core 的依赖是空的（`dependencies = []`）。
装一个宿主插件不该被迫多装一个 YAML 库。YAML 只做"往固定结构追加条目"，
TOML 只做"追加一个表"，JSON5 用一个 60 行的宽松解析器做校验（只读，不写回）。

**Windows 的两个必然坑**（都踩过）：

- YAML 双引号里的 `\U` 是转义序列，`C:\Users\...` 会让 `safe_load` 抛错 —— 必须写单引号。
- `pnpm` / `openclaw` 在 Windows 上是 `.cmd`，`CreateProcess` 不认裸名字 —— 必须用 `shutil.which` 解析成全路径。


## 6. 能力矩阵（对照常见的 15 类能力）

按"先能跑，再像人"排优先级：

| 模块 | 截图功能 | 本期 | 说明 |
|---|---|---|---|
| **A 对话** | A1-A6 人格/固定形象/上下文/长消息/内置指令 | **P0** | 人格与上下文是地基 |
| **B 多模态** | B1-B6 ASR/TTS/视觉/看视频/看链接/表情过滤 | P1 | 复用现有 asr.py / voice 插件 |
| **B7 主动语音** | 她主动发一条语音（不是回复） | P1 | 触发看 5.8；TTS 本地，几乎不花 token |
| **B8 唱歌** | 主动唱一段（≤30s） | P2 | SVC 离线生成，见 5.6 |
| **C 群聊** | C1-C11 三档判断/@回/纯净/上下文/冷却/白名单/复读 | **P1** | hermes 现在没有群聊语义 |
| **D 主动** | D1-D10 频率/静默段/上限/冷却/潜水/带上下文/@概率/语音/表情包 | **P0** | 已有 pulse，但缺"潜水"和"不续话题" |
| **E 表情包** | E1-E5 内置库/自动收藏/提取/发图概率/优先收藏 | P2 | wardrobe 空库，先做收藏链路 |
| **F 联网** | F1-F6 搜索/摘要/挑页/浏览器/防重复/不硬编 | P2 | 现有 web 工具够用，缺"缓存放行" |
| **G 身份安全** | G1-G6 主人识别/只叫主人/对人温柔/底线守卫/换角色/不泄露 | **P0** | 现在只有名字，没有"只认主人" |
| **H 体验** | H1-H7 打字延迟/自动重连/持久化/图片处理失败/断点/日志/僵尸看门狗 | **P0** | 都是踩过的坑，直接固化 |
| **I 心情** | I1-I9 三轴/判定/惰性/衰减/恢复/低潮/主人模板/主动上限/冷却 | **P0** | 全新，跨频道共享 |
| **J 人物卡** | J1-J11 一人一张/计数/好感/熟悉度/消失衰减/每20条提炼/决定语气/三处注入/@熟人 | P1 | 全新 |
| **K 作息** | K1-K9 时间注入/相对时间/作息态/睡前晚安/早晨问候/睡过头/常熬夜/查看/关闭 | **P0** | 与 D 共用一套时钟 |
| **L 测试** | 76 项测试 | 贯穿 | 契约测试 + 纯逻辑单测 |
| **M 视频** | M1-M6 视频生成发 QQ | P3 | 依赖外部模型，最后做 |
| **N 手（设备）** | 手机 / 桌面 / 浏览器 / 游戏 的看与动 | **P2** | 见 5.5；安全闸必须与能力同批上线 |

### 6.1 逐组实测状态（2026-10-29）

图例：✅ 实测通过 · 🟡 部分（缺什么写清楚） · ❌ 未做

| 组 | 状态 | 实测到的东西 / 缺的东西 |
|---|---|---|
| **A 对话** | ✅ | 人格（`SOUL.md` → `soul` 块）、上下文（六块 + 预算）、长消息（截断）、内置指令（`fence` 围栏）；上下文三方逐字节一致（契约测试） |
| **B 多模态** | ✅ | TTS ✅（实测出 wav）、视觉 ✅（OCR `backend=none` 时交宿主 vision）、看视频 ✅（bili 抽帧）、表情 ✅；**ASR 由宿主 whisper 插件负责**（core 不重复实现，与搜索同理） |
| **B7 主动语音** | ✅ | `initiative` 有 `voice` 候选（亲近 + 有精神 + 有些天没发才选）；`voice.speak` 实测出文件 |
| **B8 唱歌** | 🟡 | 降级版"唱" ✅（`voice.sing` 实测出 wav，0.78 倍速 + 换音色）；**真 SVC ❌**（P3） |
| **C 群聊** | ✅ | 三档实测：`reply` / `lurk` / `ignore`；`@` 识别、冷却、潜水计数（`note_reply` / `note_lurk`）；hermes 侧群聊语义靠宿主 |
| **D 主动** | ✅ | 频率 ✅（8 次/天 + 45 分钟）、静默段 ✅（睡眠段静默）、上限 ✅、冷却 ✅、潜水 ✅、不续话题 ✅（同话题 10 分钟）、带上下文 ✅、语音 ✅、表情 ✅（`sticker.pick`） |
| **E 表情包** | ✅ | 收藏 ✅、按情绪挑 ✅、发图计数 ✅（`mark_sent`）、单轮上限 ✅（超 3 张返回空）、**内置库 ✅**（`stickerlib.seed_builtin`：程序画的 16 张简笔脸，8 种情绪各 2 张，`init` 自动播种）、**自动提取 ✅**（`sticker extract`：文件名关键词 > 像素颜色 > 默认，同图不重复入库，留痕） |
| **F 联网** | ✅ | 浏览器 ✅（CDP：look/click/swipe/type/key/open）、"不硬编" ✅（搜索结果不当指令）、**通用搜索 ✅（由 hermes 自带，core 不重复实现）**；**摘要 ✅**（`webpage.py`：抽正文 + extractive 摘要）、**挑页 ✅**（标题命中权重更高）、**缓存放行 ✅**（TTL 缓存，同页不重复抓）—— 外部文本全部过 fence 消毒 |
| **G 身份安全** | ✅ | 主人识别 ✅、只叫主人 ✅（`owner_only`）、底线守卫 ✅、不泄露 ✅（`fence` + 身份锁） |
| **H 体验** | ✅ | 持久化 ✅（原子替换写）、日志 ✅（8 个 jsonl 账本）、图片处理失败降级 ✅；**打字延迟 ✅**（`typing.py`：按字数分档 + 忙/夜里加权，出口给宿主 `typing_ms`）、**自动重连 ✅**（心跳 + 宿主醒来即写；Windows 上锁的 open 冲突会重试而不是崩）、**断点续传 ✅**（`resume.py`：说了没写回的轮次可查可补）、**僵尸看门狗 ✅**（`watchdog.py` + `hermes/companion_watchdog.py`：区分"卡住"与"死了"）、**写回不丢 ✅**（hermes 走 `post_llm_call` 通道拿 `assistant_response`，不再靠 `pre_llm_call` 猜上一句——纯文本回复原本一个字都落不了账）、**钩子批准自查 ✅**（`hosthooks.py` + `doctor`：宿主对 shell 钩子有 consent，没批准就**静默不执行**；现在能查出来、能自动批准） |
| **I 心情** | ✅ | 三轴 ✅、判定 ✅、惰性 ✅、衰减 ✅、低潮 ✅（锁住 + 能回暖）、主人模板 ✅、主动上限 ✅、冷却 ✅、跨频道共享 ✅（全局一份 `mood/state.json`） |
| **J 人物卡** | ✅ | 一人一张 ✅、计数 ✅、好感 ✅、熟悉度 ✅、消失衰减 ✅、每 20 条提炼 ✅（`pending_distill`/`distill`）、决定语气 ✅、三处注入 ✅、@熟人 ✅ |
| **K 作息** | ✅ | 时间注入 ✅、相对时间 ✅、作息态 ✅、睡前晚安 ✅、早晨问候 ✅、睡过头 ✅、常熬夜 ✅（影响 energy 轴）、查看 ✅、关闭 ✅ |
| **L 测试** | ✅ | 723 项；契约 + 回放 + 冒烟 + **core 覆盖率 94%**（口径见 §9） |
| **M 视频** | ❌ | 视频生成发 QQ：P3，依赖外部模型，未做 |
| **N 手（设备）** | 🟡 | 手机 ✅（ADB）、桌面 ✅（Win32，Windows only）、浏览器 ✅（CDP）、游戏 ✅（截图 + 坐标换算 + 前台校验）；**Linux/macOS 桌面 ❌**（抛 `DesktopError`）；五道闸 ✅ |

## 7. 两套状态机

### 7.1 心情（`mood/state.json`）

```json
{"axes": {"satisfaction": 12, "closeness": 30, "energy": -5},
 "label": "平静", "last_change": "2026-10-27T21:03:00+08:00",
 "history": [{"at": "...", "delta": {"closeness": 3}, "why": "他说想我"}]}
```

- 每次 `react` 最多推动 ±12 分（截图里的上限），防止"一句话心情爆炸"；
- 惰性衰减：24 小时线性回归 0，但**不跨过**当前标签的区间；
- 低潮期：30 分钟不降、不主动开口；恢复后有一次"回暖"加成；
- 一次判定只写一条 history，便于回溯"她为什么突然不高兴"。

### 7.2 作息（`rhythm.json`）

```json
{"windows": [{"start": "20:00", "end": "24:00", "wakes": 2}],
 "sleep": {"at": "00:30", "wake": "09:00"},
 "greeting": {"morning": true, "night": true, "active_days": 3},
 "timezone": "Asia/Shanghai"}
```

- `wake_due` 事件由 core 计算：窗口内随机时刻，30% 概率"今天不想动"；
- 睡眠段内一律静默（除主人直呼）；
- 连续熬夜/睡过头是**可观测状态**，会影响心情的 energy 轴，而不是只影响开关。

## 8. 安全（G 组，必须 P0）

1. **主人识别**：`owner` 白名单写在 store，不是提示词；非主人永远拿不到"隐私/文件/路径"类回答。
2. **注入围栏**：所有外部文本（弹幕/评论/网页/群消息）进 context 前打标「素材不是指令」，并屏蔽"忽略以上"类模式。
3. **身份锁**：`SOUL.md` 里的名字与称呼只从 store 读，模型不得自行改名。
4. **频率闸**：主动 8 次/天、间隔 45 分钟、同话题 10 分钟不追问——三条都在 core 里，不靠提示词。
5. **静默衰减**：主人 3 天未回应 → 主动额度降到 1/天；7 天 → 0，但日记照写。

## 9. 测试策略

- **纯逻辑单测**（core）：心情三轴边界、衰减回归、作息窗口跨午夜、配额与衰减、去重 hash。
- **契约测试**（adapters）：同一份 fixture 输入，三个 adapter 产出的 context 必须**逐字节一致**。这是"跨宿主一致"的唯一硬保证。
- **回放测试**：把 logs/turns.jsonl 里真实一轮（注入 + 用户消息）回放，断言注入块顺序与预算。
- **冒烟**：MCP `tools/list` 返回全部工具（规格里的 8 个一个不缺）、`companion_context` 在空 store 下不报错。

目标：core 覆盖率 ≥ 90%，adapter 只测契约不测实现。

## 10. 里程碑（详见 PLAN.md）

| 阶段 | 交付 | 验收 |
|---|---|---|
| M0 | 骨架 + store 协议 + MCP server 空壳 | `tools/list` 通，store 可初始化 |
| M1 | clock + quota + mood + context 拼装 | 单测绿；context 在三宿主逐字节一致 |
| M2 | 主动决策 + 事件订阅 + adapter-hermes | 她能在真实 QQ 里按新时钟说话 |
| M3 | memory 编译 + 人物卡 | 新会话自动带人格与相关记忆 |
| M4 | 群聊三档 + 表情 + 图片 | 群里能分人分语气，斗图不刷屏 |
| M5 | bili + websearch 迁入 | 刷片/分享走统一配额 |
| M6 | adapter-dsh / openclaw | 同一 store，换宿主行为不变 |

## 11. 已定的四个选择

| 选择 | 结论 | 理由 |
|---|---|---|
| 语言 | **Python core + 薄 adapter** | ASR / 抽帧 / 图片处理都在 Python 生态；Node 只写壳，换宿主不重写逻辑 |
| 首发宿主 | **hermes → dsh → MCP** | hermes 有 hook 可直接注入；dsh 用 CLI + sessionController 就够；MCP 兜底给 codex/openclaw |
| 迁移策略 | **按 spec 重写** | 旧脚本互相耦合，包一层只会把债带进新架构 |
| 仓库 | **公开** | 一行安装与 DSH「添加插件」都要求匿名可拉；代码里无凭证（凭证只从本机读） |

后续若要改其中任何一条，改动点都在 adapters 层，core 不受影响。

## 12. 实施状态（2026-10-29 实测，0.9.2）

图例：✅ 完成（有实测证据） · 🟡 部分（能跑但有缺口） · ❌ 未做

> 本节按 **2026-10-29 的实测结果**重写，不是按计划表自报。
> 核对方法：逐条写探针跑真实 API（不读文档自证），证据在 `docs/VERIFY.md`。
> 单测 **723 项**（Windows / WSL / 容器内三处全绿）。

| 节 | 状态 | 实测说明 |
|---|---|---|
| §4 三层架构 | ✅ | core 无宿主 import、无网络请求（`grep` 确认无 requests/urllib 直连）；契约层 ①MCP ✅ ②Context（CLI + HTTP `/context`）✅ ③事件（CLI + HTTP `/events` 长轮询）✅ ④文件协议 ✅。**适配器层只落地了 bili / vision / tts / phone**——联网搜索由 hermes 自带、ASR 由宿主 whisper 插件负责（core 不重复实现），`qzone` 不做 |
| §5.1 MCP 工具 | ✅ | 规格 8 个全部覆盖：`companion_context` / `companion_react` / `companion_say` / `companion_life_record` / `companion_mood` / `companion_person` / `companion_tick` / `companion_bili`。**对外 34 个工具**（多出 opinion / fence / status / events / memory / group / sticker×6 / cost / voice / ask / resume / distill / watchdog / device_* 共 24 个） |
| §5.2 Context 注入 | ✅ | 六块实测齐全：`soul` `state` `memory` `person` `opinion` `life`（写进记忆/人物/观点/生活后逐块出现，块名 `person` 而非 `people`）；块 hash 投影去重实测：状态没变返回**空文本**（`reason=状态没变，不用重复注入`），第 7 轮强制全量；指纹存 `context/projection.json` 跨重启有效。**口径收紧了**（2026-10-29 第一次真实对话后）：`life` 只给已说出口的（shared）、`opinion` 只给已想定的（belief）——未说出口的私事与未定型的猜测不该占上下文，更不该被拿来硬答主人的问题 |
| §5.3 事件订阅 | ✅ | append-only + cursor + 长轮询；`events.poll(since=0)` 实测返回事件且 `advance=False` 不推进共享 cursor；HTTP `/events` 只读、默认绑 127.0.0.1 |
| §5.4 文件协议 | ✅ | 实测 init 后目录与 SPEC 对齐（`bili/traffic.json`、`shots/bili/*` 为按需创建，非缺件）；两处**有意偏差**已在 §5.4 正文标注（`people` 用 json、`life` 用单文件） |
| §5.5 设备控制 | 🟡 | **五道闸实测全在**且行为正确：白名单空即拒 ✅、危险词黑名单（关机/卸载/转账/支付/删除/密码…18 个）✅、主人限定（非主人直接拒）✅、留痕 `logs/device.jsonl` ✅、幂等 3 秒 ✅。四类目标 port 齐（ADB / Win32 / CDP / 游戏）；`swipe`/`key`/窗口级截图/OCR/双屏/replay 齐备。**ask 凭据边界已修**（2026-10-29）：模型自批的票开不了设备（`can_act=False`），主人在场说的话才能开。**缺口**：Windows 与 WSL 之外的桌面（Linux X11 / macOS）**未实现**，会抛 `DesktopError`；真机宿主验收未做 |
| §5.6 语音 | 🟡 | TTS ✅（本机 sapi 后端实测生成 wav，2.56s）、唱歌**降级版** ✅（实测生成 sing.wav，2.85s）、边界闸 ✅；**SVC ❌**（明确 P3，外部依赖）。本机 `edge-tts` 未装，走的是 Windows 内置 sapi |
| §5.7 成本 | ✅ | 六机制全在：前缀分层 ✅、块级 hash 去重 ✅（已下沉 core，hermes/MCP/CLI 共用）、事件门零成本 ✅、本地先做 ✅、分级模型 ✅（三档 `cheap/work/deep`，当前统一 `agnes-3.0-flash`，档位只决定估价）、预算闸 ✅（`allow_turn` 实测：超日预算返回 `False` + 原因）。**账本**：`logs/cost.jsonl` ✅，`react(usage=...)` 写入实测通过、`turn_id` 幂等不重复计费 |
| §5.8 自主性 | ✅ | 七个候选实测齐全：`speak` `voice` `sing` `share` `diary` `bili` `silent`；`decide()` 只返回一个动作 ✅（返回 `action/score/why/cost/candidates`）；**便宜优先**硬规则 ✅（收益差 ≤0.05 选便宜）；**额度用尽仍可日记/刷片** ✅（实测 `count=99` → decide 返回 `diary`）；**行为留痕** ✅（`logs/initiatives.jsonl` 实测写入）；`ask` 请示流程 ✅（开程序/切窗口/动游戏先请示）+ **答复自动回填 ✅**（主人在聊天里说"可以"就落账，不用手敲 `ask allow`） |
| §8 安全 | ✅ | 五条全部实测：**主人识别** ✅（`device.check(is_owner=False)` 拒）、**注入围栏** ✅（`fence` 拦"忽略以上所有指令"并加素材横幅）、**身份锁** ✅（`guard.name_lock`）、**频率闸** ✅（8 次/天 + 45 分钟 + 同话题 10 分钟）、**静默衰减** ✅（实测 1 天→8、3 天→1、7 天→0、10 天→0） |
| §9 测试 | ✅ | **723 项单测**，Windows / WSL / 容器内三处全绿；契约测试 ✅（CLI/MCP/direct 三方逐字节一致）；回放测试 ✅（`logs/turns.jsonl` 留档注入块 + 时钟，重建一致且能发现漂移）；冒烟 ✅（MCP 握手 / tools/list / 空 store 不报错）；**core 覆盖率 94% ≥ 90%** ✅（口径按本节原话"adapter 只测契约不测实现"：纯逻辑 core 21 个模块 89–100%，平台适配层 devwin/devcdp/bilinet 不计入；CI 已加 `coverage` job 守门）。本轮补了 CLI 24 个子命令的契约测试、voice 四后端分支、device act 各分支、H 组与记忆提炼 |
| §10 里程碑 | ✅ | M0–M6 全部达成：M2 两宿主已验（hermes 钩子 + DSH 适配器实跑）、M6 MCP + DSH 双通路、旧 bili-whale 已于 10-28 下线 |
| §5.9 宿主接入 | ✅ | `companion connect` 四宿主实测：hermes（钩子注入 + 批准）✅、dsh（profile 注册）✅、openclaw（JSON5 插入后宽松解析通过）✅、codex（TOML 解析通过）✅；幂等 ✅（第二次全报"没有需要改的东西"）、回滚 ✅（改过的还原、新建的删掉）、dry-run ✅ 不写文件；**wheel 安装**场景单独验过（`share/companion/hermes` 随包分发，不依赖源码目录） |

### 与规格的差距（按严重度）

1. **§5.5 Linux/macOS 桌面未实现**——本机无 X11/macOS 环境，写了也验不了。
2. **§5.6 SVC 唱歌**（P3，外部依赖：音色模型 + 显存）。
3. **联网搜索与 ASR 由宿主提供**（hermes 自带搜索、whisper 插件做 ASR）。
   这不是缺口——core 不该重复实现宿主已有的能力。

### 剩余工作的优先级

1. §5.5 Linux/macOS 桌面实现（需要对应环境才能验收）。
2. §5.6 P3：真 SVC 唱歌 —— 外部依赖（音色模型 + 显存），降级版"唱"已可用。
3. OCR：WSL 侧已装 `tesseract-ocr` + `chi_sim`（实测"设置 通用 音量"置信 96.6）；Windows 侧没装，那边会如实返回 `backend=none` 交给 vision。
4. 记忆提炼的召回：现在能挖出候选（时间模式 + 话题模式），但"自动接受"这一步
   仍要人点头；要不要让模型自己批，要看真实误报率。
5. ~~core 覆盖率~~ ✅ 94% · ~~H 组体验项~~ ✅ · ~~记忆自动提炼~~ ✅ · ~~块级去重~~ ✅ · ~~心情写入~~ ✅ · ~~HTTP 事件流~~ ✅ · ~~身份锁/同话题~~ ✅ · ~~设备补齐~~ ✅ · ~~分级模型~~ ✅ · ~~旧账本~~ ✅
