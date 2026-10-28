# 验证清单（2026-10-28 · v0.9.2）

> 结论先说：**核心引擎与 MCP 是实测通过的；"一行安装"目前不成立**（仓库未推送 + Release 无 0.9.x），
> 桌面控制只有 Windows，dsh 没有真机跑过。下面每一条都写清"怎么验的"和"没验的部分"。
>
> 环境：Windows 11 / Python 3.13.14 ｜ WSL Ubuntu / Python 3.12.3 ｜ Docker 29.7.2 ｜
> openclaw 2026.10.5 ｜ hermes gateway（systemd user，active）

## 一、已验证（有命令、有输出）

| # | 项 | 证据 | 结论 |
|---|---|---|---|
| 1 | 全量单测（Windows） | `python -m unittest discover -s tests -q` → `Ran 345 tests ... OK` | ✅ |
| 2 | 全量单测（Linux） | WSL 同一命令 → `Ran 345 tests ... OK` | ✅ 同一份代码双平台绿 |
| 3 | MCP 协议（真子进程） | `tests/test_mcp_protocol.py` 5 例：握手协商 `2025-06-18`、26 工具、未知工具 `-32601`、坏 JSON `-32700`、通知不回包 | ✅ |
| 4 | MCP 在 GBK 环境下的编码 | 以 `PYTHONIOENCODING=gbk` 起进程，stdout 仍是合法 UTF-8 JSON（修复前解不开） | ✅ |
| 5 | 真实 MCP 客户端 | `openclaw mcp add companion-probe --command .../companion --arg mcp` → `26 tools, Codex approval auto`，注解齐全无警告 | ✅ |
| 6 | 本地安装脚本（Linux） | 干净 venv 跑 `sh install.sh --from . --no-path --no-init` → 装上 0.9.1，`sh` 自动切 bash | ✅ |
| 7 | Windows 安装脚本 | `install.ps1 -DryRun` 与 `-From . -DryRun` 都出正确计划，不落盘 | ✅（只验计划） |
| 8 | 语音合成（Windows） | `companion voice say "今天想跟你说句话"` → `3.26s / sapi` 的 wav | ✅ |
| 9 | OCR（WSL） | tesseract + chi_sim 识别「设置 通用 音量」，置信 96.6 | ✅ |
| 10 | 桌面截图/窗口/观察（Windows） | 真机 `device look / list / observe`，双屏 3840×1080 正确 | ✅ |
| 11 | 设备 WSL 转发 | WSL 里 `device list` 由 Windows 侧执行并回传（需 `devices.json` 配 python/src） | ✅（本机已配） |
| 12 | B 站全链路 | 真机 tick → 字幕/弹幕/热评/3×3 抽帧 → life 落库 | ✅（需凭证） |
| 13 | B 站额度一本账 | 今天 2 次刷片从旧 state 迁进 `quota.json` 的 `bili` 桶；镜像清零也不会多刷 | ✅ |
| 14 | 上下文契约 | CLI == MCP == 直接 build 逐字节一致；`/context` 与 CLI 同源 | ✅ |
| 15 | hermes 注入/写回/唤醒 | 网关 active；钩子注入正常；`companion_wake.py` 静默刷片；`life.py` 双写 | ✅（写回见 §三） |

## 二、只算"做了一半"（能跑，但有一半没接上）

| 项 | 已完成的一半 | 缺的一半 |
|---|---|---|
| **hermes 的说话三道闸** | 钩子把「你刚说过…」注入给她，重复的内容会被她自己避开 | **身份锁 / 同话题拦截不生效**：hermes 不调用 `companion_say`，而 `pre_llm_call` 发生在生成之前，拦不住已经生成的消息 |
| **ask 请示流程** | CLI / MCP 都能建请求、批准、带 `ask_id` 重试 | **没有宿主把待办摆到主人面前**：hermes 不轮询 `ask list`，等于要人自己敲命令看 |
| **dsh 适配器** | 代码完整（tick / events / context / 状态路由），包内自带 `src` | **本机没装 dsh，没跑过**；且只接了 tick 与注入，bili/voice/sticker/ask 都没接 |
| **斗图轮次上限** | `pick` 超 3 张返回空，单测覆盖 | 需要宿主在发送后调用 `mark_sent` 才计数；目前没有宿主用表情 |
| **Docker** | 代码在 Linux 容器里理论上可跑（无平台硬依赖） | **镜像拉不下来（本环境 registry 超时），没实测**；容器内无显示/TTS/adb |
| **Linux / macOS 桌面** | 调用会抛明确的 `DesktopError`，不会静默出错 | **没有实现**（X11/Wayland、macOS 截屏与输入都没写） |

## 三、还没验证（需要真实条件）

| 项 | 为什么没验 | 怎么算验过 |
|---|---|---|
| hermes 写回 `turns.jsonl` | 装好钩子后还没有一次真实对话，`turns.jsonl` 仍 0 行 | 你跟她说一句话后，`companion turns --limit 3` 有内容 |
| 心情推力 | 同上（心情只在真实开口后推） | `companion mood` 的数值离开 0 |
| 被回应结账 | `initiative/feedback.json` 有 `last_at`、`settled=0`，还没遇到 TO_OWNER 动作 | 她主动说话后 6 小时内你回一句，`settled` +1 |
| codex 本体 | 本机没装 codex；用 openclaw（同一套 MCP）代验协议 | `codex mcp` 里能看到 26 个工具 |
| dsh 桌面端 | 没装 | 装上后 `/dsh-companion/status` 有返回、tick 能投递 |
| Docker | 镜像拉不下来 | `docker run` 里 345 测试绿 + `companion doctor` 正常 |

## 四、模块之间会不会互相干扰 / 重复消耗

| 关注点 | 结论 | 依据 |
|---|---|---|
| 谁在决定"现在做什么" | 单主人锁，TTL 内只有一台宿主能决策 | `drive._claim` + `test_second_host_cannot_steal_the_clock` |
| 说话额度会不会扣两次 | **修好了**：决策时扣一次并发"票"，`say` 拿票免单；票一次性、20 分钟过期、绑定动作 | `quota.set_pending/take_pending` + `tests/test_guard.PendingTicketTest` |
| 心情会不会双写 | 唯一写入方是 `mood_rules`（钩子调用）；`react` 只在宿主显式传 delta 时写，hermes 不传 | `grep` 调用点 + `test_mood_rules` |
| 生活流水会不会两本账 | `life/life.jsonl` 是唯一账本；旧 `proactive/life.jsonl` 只是 `life.py` 写的投影 | SPEC §5.4 + 迁移记录 |
| B 站额度会不会双记 | `quota.json` 命名桶是唯一来源，`bili/state.json` 只是镜像 | `test_quota_is_authoritative_even_if_mirror_lost` |
| 事件会不会被一个读者吃光 | **修好了**：显式传 `since` 的读者不再推进共享 cursor；HTTP `/events` 本来就不推 | `events.poll` + `test_explicit_since_does_not_move_shared_cursor` |
| 两宿主同时装 | 不会互相打断：各自 session 注入、各自日志；但只有一个能决策（另一个 `blocked=host`） | `drive.tick` 早退分支 |
| 并发写 JSON | ⚠️ 残留风险：`save_json` 是原子替换，但没有跨进程锁，极端同时写会丢一次更新 | `store.save_json` |
| 上下文重复注入 | 钩子按 (session, 块指纹) 去重；「【现在】」的时间被抹掉再比对 | `_render_delta` + 5 个测试 |

## 五、宿主能力矩阵（装完之后实际能用的）

| 能力 | hermes | dsh | MCP（codex / openclaw） |
|---|---|---|---|
| 每轮注入上下文 | ✅ hook | ⚠️ 有 `/dsh-companion/context`，是否自动注入取决于 dsh 会话 | ✅ `companion_context` |
| 主动唤醒 | ✅ `companion_wake.py` | ✅ 5 分钟 tick → `sessionController.prompt`（未实测） | ⚠️ 需客户端自己轮询 `companion_events` |
| 说话三道闸 | ❌ 不走 `say`（只有注入提示） | ❌ | ✅ `companion_say` |
| 写回（心情/记忆/观点） | ✅ `react --from` | ❌ | ✅ `companion_react` |
| 刷 B 站 | ✅ `companion_bili.sh` | ❌（只认 `wake_due`，忽略 `bili_due`） | ✅ `companion_bili` |
| 语音 / 表情 / 请示 | ❌ | ❌ | ✅ |
| 设备 | ❌ | ❌ | ✅ `device_*` |

> 一句话：**MCP 是能力最全的宿主**；hermes 强在"活着"（注入、唤醒、写回）；dsh 目前只算"能叫醒 + 能注入"。

## 六、本轮修掉的问题（都有回归测试）

1. **MCP 输出不是 UTF-8**：Windows 控制台 GBK 时中文写成 GBK 字节，客户端 JSON 解析失败 → 进程启动时强制 utf-8（`test_context_returns_utf8_json`）
2. **协议版本写死**：客户端要 `2025-06-18` 也回 `2024-11-05` → 按客户端支持协商；`serverInfo.version` 从 `0.1.0` 改为真实版本
3. **工具没有安全注解**：openclaw 提示"每次调用都要批准" → 26 个工具全部加 `readOnlyHint/destructiveHint/openWorldHint`，真实客户端复验无警告
4. **额度可能扣两次**：drive 决策扣一次、`say` 再扣一次 → 加"票"（一次性 / 20 分钟 / 绑定动作）
5. **事件游标被抢**：dsh 传 `since` 也推进全局 cursor，人再读就空了 → 显式 `since` 不再写全局
6. **安装脚本版本漂移**：`install.sh` 写 0.2.8、`install.ps1` 写 0.2.3、代码 0.9.1 → 单一来源 + 测试守着；`--from` 前补 setuptools；`sh` 调用自动切 bash；ps1 补 `-From`
7. **Linux 上单测红**：测试少 patch 了 `screen_origin`；顺带修掉
8. **doctor 的假 ✓**：非 Windows 上 `devwin` 导入成功就打 ✓ → 现在会标"仅 Windows，本机调用会报错"
9. **B 站空 store 说"可以刷"**：没凭证时 `due=true` → 现在 `logged_in=false` + 明确说缺凭证，drive 也不再把刷片排进候选
10. **死配置**：`pyproject` 声明了不存在的 `templates/*.md` → 删掉

## 七、剩余待办（按阻断程度）

| 级别 | 事项 | 现状 |
|---|---|---|
| **P0** | 推送 GitHub + 发 0.9.x Release（带 whl） | 本地领先 `origin/main` 19 个提交；一行安装现在必然 404 |
| P1 | hermes 侧真正执行身份锁/同话题 | 需要宿主提供"发出前"钩子，或 hermes 改为经 `companion_say` 出口 |
| P1 | 把 `ask` 待办呈现给主人 | 需要一个宿主轮询并把 `say` 文本发出来 |
| P1 | dsh 真机验收 | 装 dsh 后跑 `/dsh-companion/status` 与一次 tick |
| P2 | macOS / Linux 桌面实现 | 目前明确不支持；WSL 只能转发到 Windows |
| P2 | Docker 镜像与挂载说明 | 未实测；`COMPANION_HOME` 必须挂卷，否则重建即失忆 |
| P2 | 并发写锁 | 极端并发下可能丢一次更新 |
