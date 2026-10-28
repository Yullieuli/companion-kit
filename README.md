# companion-kit

跨 agent 的「伴侣运行时」：一套人格、一套记忆、一套时钟，dsh / hermes / openclaw / codex 都能挂。

> **状态：core 已可运行**（M0–M2，76 个单测）。CLI 能跑、DSH 壳可装；
> 规格见 [docs/SPEC.md](docs/SPEC.md)，路线图见 [docs/PLAN.md](docs/PLAN.md)。

## 它解决什么

常见做法是把「生活感」分散在几个脚本里：主动搭话一套、自由时间一套、刷视频又一套，
各自记配额、各自算时间、各自写记忆。结果是**同一件事三份实现，互相打架**——
凌晨可能被叫醒、同一句话被记两遍、偏好算两次分。

这个项目把这些收成三层：

```
宿主（dsh / hermes / openclaw / codex）
        ↓  适配器：只做"把上下文塞进去、把工具接出来"
契约层：MCP tools + Context 注入 + 事件订阅 + 文件协议
        ↓
core：时钟 / 心情 / 人物卡 / 记忆 / 主动决策 / 配额（纯逻辑，可单测）
        ↓
适配器：QQ / B站 / 搜索 / 视觉 / 语音 / 手机
```

**core 不 import 任何宿主 SDK，不直接发网络请求。** 这样它才能在三种宿主里跑同一份逻辑，也才能被单测。

## 一行安装（不用先装 Python，也不用 git）

**Windows**（cmd 或 PowerShell，复制这一行）：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.ps1 | iex"
```

**Linux / macOS / WSL**：

```bash
curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash
```

这一行会**自己做完所有事**，中间不需要你点任何东西：

| 步骤 | 怎么做 |
|---|---|
| 找 Python 3.10+ | 有就用（3.10 / 3.11 / 3.12 都行） |
| 没有 Python | 下载 uv（单文件、免管理员），用它拉一份**私有的 3.12**，不碰系统 |
| 隔离环境 | venv 建在 `~/.companion/venv`，不污染全局 |
| 装包 | 直接装 GitHub Release 的 `.whl`，**不需要 git**（失败才回退 `git+`） |
| 初始化 | 自动 `companion init`，建好 SOUL / 记忆 / 作息 / 心情 |
| PATH | Windows 写用户环境变量，Linux/macOS 写 shell 配置，**不用你 export** |
| DSH | 检测到 `dsh` 就顺手 `dsh plugin add` |

调试用两个环境变量：

- `COMPANION_TICK_MS=1200` 把 tick 从 5 分钟缩到 1.2 秒（别长期开着）
- `COMPANION_DSH_SESSION=<会话id>` 指定 `wake_due` 投递到哪个 DSH 会话；不设则自动取 `sessionController.list()` 里最近更新的那个

DSH 装好后有三个只读/自检路由：

| 路由 | 作用 |
|---|---|
| `/dsh-companion/status` | 心情、额度、上次决策（只读，不扣额度） |
| `/dsh-companion/sessions` | 解析到的目标会话 + 前三个会话摘要 |
| `/dsh-companion/ping` | 立刻投递一条测试消息，返回 `{session, ok, shape, error}`；可用 `?session=<id>` 指定目标 |

**DSH 桌面端**：设置 → 添加插件 → 填

```
https://github.com/Yullieuli/companion-kit
```

装完新开一个终端，`companion cost` 有输出就说明通了。

### 常见疑问

- **必须装 Python 吗**：核心是 Python（ASR、抽帧、图片处理都在这边）。但你**不用手动装**——
  没有的话安装脚本会用 uv 拉一份私有的 3.12，放在 `%LOCALAPPDATA%\companion` 或 `~/.companion` 下，随时可删。
- **`pip install git+…` 是在拉源码吗**：以前是。现在默认走 Release 的 whl，不解压源码、不需要 git。
- **会污染系统吗**：不会。只有 `~/.companion`（store + venv）和一条 PATH。
- **怎么卸载**：删 `~/.companion`，再把 PATH 里那一条去掉即可。
- **pip 报 502 / 代理错误**：Windows 上 pip 会读 `HTTP(S)_PROXY`，代理挂了就误报"包坏了"。
  安装脚本检测到会**自动去掉代理重试**，还失败才报错。

## 现在能做什么

```bash
companion init                          # 建 store（幂等，绝不覆盖 SOUL.md）
companion context --budget 1200         # 本轮该注入什么（人格/状态/记忆/人物卡/生活）
companion status                        # 只读：心情、额度、作息、记忆页数
companion tick                          # 问一次"现在想干什么"（零 token）
companion events --wait 30              # 等一次 wake_due
companion cost                          # 今天花了多少

companion memory write --title 影之刃零 --body "..." --summary "一句话" --tags 游戏 设计
companion memory search 游戏            # 标签 > 摘要 > 正文，越近越靠前
companion memory index                  # 从各页 summary 重新投影索引
companion life add "看完影之刃零" --kind watch --detail "10 分"
companion person learn --who owner --fact "讨厌开放式结局"
companion voice say "今天想跟你说句话"    # 本地 TTS，落盘不发送
companion device look --target desktop   # 截图 + 前台窗口 + 可见窗口标题
companion device act --target phone --act tap --x 540 --y 1200
companion device act --target desktop --act swipe --x 600 --y 400 --x2 600 --y2 900
companion device act --target desktop --act key --key ctrl+s
companion device list                    # 可见窗口（按面积排）
companion device focus --what 记事本      # 切到某个窗口（走窗口白名单）
companion react --mood satisfaction=12 --why "他说想听我说说" \
                --opinion "它敢砍经验值是为了逼玩家真学招式" --opinion-tags 游戏 \
                --memory-title 影之刃零 --memory-summary "敢砍经验值" --said "挺公道"
companion opinion list                  # 她的判断清单（hunch / belief / retired）
companion fence "外部文本" --source 群聊   # 打注围栏：命中几处、消毒后是什么
companion group judge "这游戏真好玩"      # 群聊三档：reply / lurk / ignore
companion sticker add ./图.png --mood 无语 --tags 翻白眼
companion sticker pick --mood 无语        # 没合适的就返回空，不凑合
companion bili status                   # 今天刷了几条、还能不能刷
companion bili tick                     # 自己挑一条、读完、记进生活流水
companion bili commit BV1xx411c7mD --summary "关卡设计讲透了" --rating 8
companion bili share BV1xx411c7mD --score 15 --reason "他肯定喜欢这个战斗系统"
companion mcp --list                    # 列出 25 个 MCP 工具
```

**212 个单测**，全部离线可跑（B 站用假总线，不碰网络）。其中 9 个是契约/回放：
CLI、MCP、直接调用三方拼出的上下文必须逐字节一致，真实一轮的注入能按当时的时钟重建。

| 层 | 做了什么 |
|---|---|
| **记忆**（M3） | 一页一主题，summary 必填；索引是从页投影的（不手写）；孤岛/退休只报告不删 |
| **生活**（M3） | `life.jsonl` 带相对时间（"2 小时前"），`shared` 标记决定要不要跟他讲 |
| **人物卡**（M3） | 关系/亲近/信任三维 + 事实追加（不覆盖，旧事实留着看变化） |
| **手**（M4.5） | 手机(adb) / 桌面(GDI+SendInput) / 浏览器(CDP) / 游戏(窗口)，**五道闸先于能力** |
| **语音**（M5.5） | edge-tts → SAPI → say → espeak 依次回退；唱歌先做 TTS 降级版 |
| **写回**（M3） | `react` 是唯一写入口：心情/人物卡/记忆/观点/生活/说过的话，一次落完，并返回她变成了什么样 |
| **观点**（M3） | hunch → belief → retired；重复说同一句是强化不是新增；衰减 ×0.998/天，被反驳掉得快 |
| **围栏**（§8） | 外部文本打标 + 消毒 + **记忆投毒防线**：被弹幕骗着写下的"事实"会在写入时被滤掉并留痕 |
| **群聊**（M4） | 三档 reply/lurk/ignore：被点名必回、冷却潜水、一小时上限、连续潜水转无视 |
| **表情**（M4） | 按情绪目录 + 标签挑，最近发过的降权，没有对得上的**就不发** |
| **MCP**（M6） | 15 个工具，stdio JSON-RPC；codex / openclaw / 任何 MCP 客户端可直接挂 |

**还没做的**：B 站接入（M5）、hermes adapter、真 SVC 唱歌（要她自己的音色模型）。

## 接成 MCP（codex / openclaw / 任意客户端）

```json
{
  "mcpServers": {
    "companion": {
      "command": "companion",
      "args": ["mcp"]
    }
  }
}
```

工具面分三类：

| 类 | 工具 |
|---|---|
| 想（不花钱） | `companion_context` `companion_status` `companion_tick` `companion_events` `companion_cost` |
| 记 | `companion_memory_write` `companion_memory_search` `companion_life_record` `companion_life_mark_shared` `companion_person` |
| 写 | `companion_react` `companion_opinion` `companion_mood` `companion_fence` |
| 聊 | `companion_group_judge` `companion_group_note` `companion_sticker_pick` `companion_sticker_add` `companion_sticker_stats` |
| 做 | `companion_say` `companion_voice` `device_look` `device_act` `device_open` |

调用顺序约定：每轮开头 `companion_context` → 干活 → `companion_life_record` 记一笔。
主动说话是宿主轮询 `companion_events`，拿到 `wake_due` 才起一轮会话 —— 所以"她没话说"这件事是零成本的。

## 杀软误报

`device` 那组模块会被部分杀软判成 `Trojan/Python.ShellLoader`——它要截屏、模拟输入、
连本地浏览器调试端口，必然同时用到 ctypes、subprocess 和裸 socket，这三样凑在一起
就撞了启发式特征。**是误报**，判断依据与三种处理方式见 [docs/AV.md](docs/AV.md)。

已经做的：把 670 行的 `device.py` 拆成四个模块、安装时 `--no-compile`（不生成 `.pyc`）。
你还可以做：把 `%USERPROFILE%\.companion\venv\Lib\site-packages\companion` 加进信任区，
`companion doctor` 会直接打印这个路径。

## 手的安全闸（不可关闭）

`devices.json` 里五条，全部在代码里判，不靠提示词：

1. 白名单：只控列出的包名 / 窗口 / 域名；
2. 危险动作：关机、卸载、转账、支付、删除、改密码、装 APK —— **直接拒，不二次询问**
   （二次询问会被话术绕过，"不询问"本身就是防线）；
3. 主人限定：群聊 @ 她、陌生人私聊，一律不触发；
4. 留痕：每个动作写 `logs/device.jsonl`，可回放；
5. 幂等 + 超时：同一动作 3 秒内不重复，单动作 10 秒中止。

边界：**只做屏幕与输入**。不注入进程、不读内存、不碰反作弊。

## 三条设计约束

1. **花钱是架构问题**：前缀分层 + 块级去重 + 事件门零成本 + 本地先做 + 分级模型 + 日预算闸。
   目标：每轮新输入 < 5k、缓存命中 > 70%、日花费 < ¥1。
2. **她不需要别人提**：每个能力都有 self-init 入口（`initiative.decide`），按她自己的作息、心情、
   未完成的事挑一个动作；只挑一个，收益差 0.05 以内选便宜的。
3. **手要先有闸**：手机/桌面/浏览器/游戏的能力与安全闸同批上线，白名单 + 危险动作黑名单 + 只认主人。

## 为什么值得重做

| 能力 | 现在的实现 | 冲突 |
|---|---|---|
| 主动开口 | pulse 骰子 / 自由时间 cron / bili night | 三套唤醒抢配额 |
| 记忆 | hindsight + said/life/opinion + 日记 | 四处写，互相不知道 |
| 偏好 | 关键词加分 + 观点 tags | 两套口径 |
| 心情 | 无 | 情绪每轮现算，跨轮不延续 |
| 作息 | 无（只有免打扰） | 凌晨也会说话 |
| 人物卡 | 无 | 对谁说话都一个腔调 |

## 三个参考各取一点

- [dsh-plugin-memory](https://github.com/LittleBlackTong/dsh-plugin-memory)：启动注入 + **投影去重** + 预算按文件分配；
- [dsh-simulated-life](https://github.com/john-walks-slow/dsh-simulated-life)：**文件协议**，任何引擎写 `events.json` 都能驱动生活；
- [ai-companion-kit](https://github.com/pinganbbOvO/ai-companion-kit)：**MCP + 薄包**的划分方式（brain / memory / transport / own-life 各一包）。

## 路线

M0 骨架 → M1 时钟+心情+上下文 → M2 主动+hermes → M3 记忆+人物卡 → M4 群聊+表情 → M5 B站 → M6 多宿主。

细节与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

## License

MIT（待定）
