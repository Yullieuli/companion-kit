# 实施计划：companion-kit

> 规格见 [SPEC.md](SPEC.md)。这里只写"怎么做、做到什么算完"。
> 每个任务控制在 2–5 分钟，做完就能提交；带 ⛔ 的是必须先有测试的任务。

## 约定

- 语言：Python 3.11（core + MCP），Node 只写 dsh adapter 的壳。
- 目录：`core/`（纯逻辑）、`ports/`（接口定义）、`adapters/`（IO）、`mcp/`（服务端）、`adapters-js/`（dsh）。
- 提交粒度：一个任务一个 commit，message 用 `feat(core): ...` 这种。
- 测试：`pytest -q` 必须全绿才提交；core 不许 import 网络库。

---

## M0 · 骨架与协议（半天）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 0.1 | 建仓库骨架 | 目录树 + `pyproject.toml` + `.gitignore` | `pip install -e .` 成功 |
| 0.2 | store 初始化 | `core/store.py:init_home()` 写 SOUL/MEMORY/rhythm/mood 模板 | 跑一次生成 8 个文件，二次跑不覆盖 |
| 0.3 | ⛔ store 读写单测 | `tests/test_store.py` | 空目录、半残缺文件、只读盘三种情况都不崩 |
| 0.4 | MCP 空壳 | `mcp/server.py` 暴露 `tools/list` + 一个 `companion_context` 返回空串 | `python -m companion.mcp --selftest` 通 |
| 0.5 | CLI 骨架 | `companion init / context / say --dry` | `--help` 有 4 个子命令 |

## M1 · 时钟 + 心情 + 上下文（1–2 天）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 1.1 | ⛔ 时间感知 | `core/clock.py:describe(now)` → "周日 21:07，距上次聊天 6 小时" | 固定 now 的 6 个用例全绿 |
| 1.2 | ⛔ 作息窗口 | `core/clock.py:windows()`，支持跨午夜、多窗口 | 跨午夜/零次/窗口挤不下 三个用例 |
| 1.3 | ⛔ 心情状态机 | `core/mood.py`：三轴、±12 上限、24h 回归、低潮期 | 边界用例 8 个：暴涨截断、低潮不降、恢复加成 |
| 1.4 | ⛔ 配额与衰减 | `core/quota.py`：8/天、45min、3 天降额、7 天归零 | 时间旅行用例（注入 now） |
| 1.5 | 上下文拼装 | `core/context.py:build(budget)` 五块按优先级裁剪 | 给 500/1500/3000 三档预算，输出长度不超 |
| 1.6 | ⛔ 投影去重 | 每块 hash + `ContextCache` | 同输入两次 hash 相同；改一行只有一块变 |
| 1.7 | 接 MCP | `companion_context` 返回真实拼装结果 | 空 store 也不报错 |

## M1.5 · 成本账本与预算闸（半天，已完成 2026-10-27）

| # | 任务 | 状态 |
|---|---|---|
| 1.5.1 | ⛔ 计价与记账（`cost.cost_cny` / `record_turn` / `today_total`） | ✅ 6 个用例 |
| 1.5.2 | ⛔ 起会话前估价 + 日预算闸（`allow_turn`） | ✅ |
| 1.5.3 | `companion cost` 看今天花了多少 | 待接 CLI |

## M2 · 主动说话 + 事件订阅 + hermes adapter（2–3 天）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 2.1 | ⛔ 主动决策 | `core/drive.py:decide(now, state)` → 理由/静默 | "有事可做/没事掷骰/窗口外/配额满/主人在说话" 五分支 |
| 2.2 | ⛔ 事件队列 | `core/events.py`：`wake_due`、`mood_shift`、cursor | 重启后 cursor 不丢，不重复投递 |
| 2.3 | HTTP 事件口 | `GET /events` 长轮询 + `GET /context` | curl 能拿到 JSON |
| 2.4 | adapter-hermes | 插件壳：注册两个工具 + hook 里注入 context | 真机一轮对话，注入里出现心情/时间 |
| 2.5 | ⛔ 契约测试 | 同一 fixture，hermes/CLI/MCP 三处 context 逐字节一致 | 差异即红 |
| 2.6 | 灰度开关 | `rhythm.enabled=false` 时行为与旧版一致 | 关掉后 hermes 侧日志显示 no-op |

## M2.5 · 自主性引擎（1–2 天，已完成核心）

| # | 任务 | 状态 |
|---|---|---|
| 2.5.1 | ⛔ 候选动作打分（说话/语音/唱歌/分享/日记/沉默） | ✅ |
| 2.5.2 | ⛔ 硬闸：睡眠、额度、惰性骰子、预算 | ✅ |
| 2.5.3 | ⛔ 性价比 tie-break（收益差 ≤0.05 选便宜） | ✅ |
| 2.5.4 | `logs/initiatives.jsonl` 留痕 + 被回应加分 | 待做 |
| 2.5.5 | 接进 `drive` 事件队列，产出 `wake_due(reason)` | 待做 |

## M3 · 记忆 + 人物卡（3–4 天）

| # | 任务 | 产出 |
|---|---|---|
| 3.1 | ⛔ 记忆页格式 | frontmatter 校验（tags/summary/last_access），非法即拒 |
| 3.2 | 索引编译 | `index.md` 从 summary 投影生成，逐字节幂等 |
| 3.3 | 启动注入预算 | 按文件均分 + 余额优先给 index（照 memory 插件那套） |
| 3.4 | ⛔ 相关性排序 | 关键词 + tag + 最近访问，三条权重可调 |
| 3.5 | 人物卡 | 一人一卡，20 条提炼一次，好感/熟悉度双轴 + 衰减 |
| 3.6 | ⛔ 人物卡注入 | 当前对象卡进 context 第四块；陌生人只给"没聊熟"标记 |

## M4 · 群聊 + 表情（3–4 天）

| # | 任务 | 产出 |
|---|---|---|
| 4.1 | ⛔ 三档判断 | REPLY / CHIME / SKIP 的规则表 + 单测 |
| 4.2 | 纯净度与冷却 | 只回 @、群里 20 秒冷却、同话题不追问 |
| 4.3 | 表情库 | 单张收藏、按情绪检索、发送概率 |
| 4.4 | ⛔ 斗图边界 | 2–3 轮收手，超轮次静默（单测） |

## M4.5 · 手：手机 / 桌面 / 浏览器 / 游戏（4–6 天）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 4.5.1 | ⛔ port 定义 | `ports/device.py`：Screen/Input/App/Observe 四个 Protocol | 假实现能跑通，真实现签名一致 |
| 4.5.2 | ⛔ 安全闸 | `core/device_gate.py`：白名单/黑名单/主人限定/幂等 | 8 个用例：越权、危险动作、群聊触发、重复点击 |
| 4.5.3 | Android adapter | `adapters/android.py`（adb）：截图/点按/滑动/输入/启动 | 真机：截图 → 点开设置 → 回桌面 |
| 4.5.4 | 桌面 adapter | `adapters/desktop_win.py`：SendInput + BitBlt + EnumWindows | 截图里能看到当前前台窗口标题 |
| 4.5.5 | 浏览器 adapter | `adapters/browser_cdp.py`：CDP 截图/点击/输入/导航 | 打开一个页面并点中一个按钮 |
| 4.5.6 | 游戏 adapter | `adapters/game.py`：窗口捕获 + 坐标映射 + 前台校验 | 1080p/2K 各点中同一个按钮 |
| 4.5.7 | 留痕与回放 | `logs/device.jsonl` + `companion device replay` | 回放一条动作链，前后截图对得上 |
| 4.5.8 | MCP 三工具 | `device_look` / `device_act` / `device_open` | 三种宿主都能调 |

> 顺序说明：**先有闸，再有手**。4.5.2 不绿，后面五项一律不许合。

## M5 · B 站 + 搜索（3–5 天）

| # | 任务 | 产出 |
|---|---|---|
| 5.1 | 迁移 bili-whale | `adapters/bili/` 复用 v0.2 的六路 + 分享闸 |
| 5.2 | 统一配额 | 刷片/分享走 core 的 quota，不再各记一本 |
| 5.3 | 搜索缓存 | 同一关键词 10 分钟内复用结果 |

## M5.5 · 主动语音与唱歌（2–4 天）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 5.5.1 | TTS 适配 | `adapters/tts.py`：edge-tts / GPT-SoVITS 二选一 | 生成 mp3，时长可读 |
| 5.5.2 | 主动语音 | `companion_voice(text)` + 触发规则（5.7） | 她主动发一条，而不是被问 |
| 5.5.3 | 唱歌降级版 | TTS 念白 + 节奏 | 30 秒内，能听出"在哼" |
| 5.5.4 | SVC（可选） | so-vits-svc 离线推理 | 需音色模型；CPU 超时就降级 |
| 5.5.5 | ⛔ 边界闸 | 只私聊 / ≤30s / 不上公开平台 | 4 个用例：群聊、超长、公开、未授权 |

## M6 · 多宿主（3–5 天）

| # | 任务 | 产出 |
|---|---|---|
| 6.1 | adapter-dsh | cordis 壳 + 同一 HTTP 口 |
| 6.2 | openclaw / codex | 纯 MCP 配置 + 文档 |
| 6.3 | ⛔ 三宿主回放 | 同一 turns.jsonl 回放，注入一致 |

---

## 风险与退路

| 风险 | 退路 |
|---|---|
| MCP 在 hermes 里没有原生客户端 | 先走 CLI adapter（子进程），行为一致只是慢 20ms |
| 迁移期两套状态并存 | `rhythm.enabled=false` 灰度开关，随时退回旧版 |
| 心情状态机把性格写死 | 三轴只影响"语气与频率"，不改人格；SOUL.md 永远是权威 |
| 群聊误判打扰别人 | 默认只回 @，CHIME 需要显式白名单 |

## 第一刀切在哪

M0+M1 大约一天半，做完就能证明三件事：**store 协议成立、时钟只有一套、context 能被三种宿主同样地拿到**。
这三件成立，后面都是往里填模块；不成立就早点发现，不至于写了三个月才发现地基是歪的。
