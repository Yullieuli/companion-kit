# companion-kit

跨 agent 的「伴侣运行时」：一套人格、一套记忆、一套时钟，dsh / hermes / openclaw / codex 都能挂。

> **状态：core 已可运行**（723 个单测，Windows / WSL / 容器内三处全绿）。CLI 能跑、DSH 壳可装；
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

> 国内访问 `raw.githubusercontent.com` 经常超时。把域名换成 jsDelivr 即可，脚本内容一致：
>
> ```bash
> curl -fsSL https://cdn.jsdelivr.net/gh/Yullieuli/companion-kit@main/install.sh | bash
> ```
>
> Windows 同理，把 `irm` 的地址换成
> `https://cdn.jsdelivr.net/gh/Yullieuli/companion-kit@main/install.ps1`。
> 脚本内部下载 wheel 时也会在 GitHub 直连失败后自动换镜像重试。

这一行会**自己做完所有事**，中间不需要你点任何东西：

| 步骤 | 怎么做 |
|---|---|
| 找 Python 3.10+ | 有就用（3.10 / 3.11 / 3.12 都行） |
| 没有 Python | 下载 uv（单文件、免管理员），用它拉一份**私有的 3.12**，不碰系统 |
| 隔离环境 | venv 建在 `~/.companion/venv`，不污染全局 |
| 装包 | 直接装 GitHub Release 的 `.whl`，**不需要 git**（失败才回退 `git+`） |
| 初始化 | 自动 `companion init`，建好 SOUL / 记忆 / 作息 / 心情 |
| PATH | Windows 写用户环境变量，Linux/macOS 写 shell 配置，**不用你 export** |
| **接宿主** | 探测本机装了哪些宿主，**自动接上**（hermes 钩子 / dsh profile / openclaw / codex 的 MCP 段），不用手抄任何配置 |

> **发布状态**：一行安装依赖 GitHub 上已经推送的 `main` 和对应版本的 Release。
> 如果这条命令 404（仓库还没发布），用本地安装：
> `bash install.sh --from /path/to/companion-kit`（Windows 用 `install.ps1 -From .`）。
> 安装脚本的版本号与 `companion.__version__` 由测试守着，不会再出现"脚本装旧包"。
>
> ⚠️ **从 0.9.x 升级请先看 [docs/UPGRADE.md](docs/UPGRADE.md)**：设备控制默认改成关闭、
> 白名单空即拒绝、MCP 主人身份不再听模型的话——不配白名单的话她会对你的屏幕"失明"。

调试用两个环境变量：

- `COMPANION_TICK_MS=1200` 把 tick 从 5 分钟缩到 1.2 秒（别长期开着）
- `COMPANION_DSH_SESSION=<会话id>` 指定 `wake_due` 投递到哪个 DSH 会话；不设则自动取 `sessionController.list()` 里最近更新的那个

DSH 装好后有三个只读/自检路由：

| 路由 | 作用 |
|---|---|
| `/dsh-companion/status` | 心情、额度、上次决策（只读，不扣额度） |
| `/dsh-companion/sessions` | 解析到的目标会话 + 前三个会话摘要 |
| `/dsh-companion/ping` | 立刻投递一条测试消息，返回 `{session, ok, shape, error}`；可用 `?session=<id>` 指定目标 |

装完新开一个终端，`companion cost` 有输出就说明通了。

### 宿主是自动接的

安装脚本最后会跑一次 `companion connect`：探测本机装了哪些宿主，逐个接上。
**不需要你改任何 JSON / YAML / TOML**。

| 宿主 | 自动做了什么 |
|---|---|
| **hermes** | 复制适配层 → 生成 `.sh`/`.cmd` 入口 → 注入 `config.yaml` 的 `pre_llm_call` / `post_llm_call` → **批准钩子白名单**（不批准会静默不跑） |
| **dsh** | 往 profile 的 `package.json` 加依赖与 bundle → 尽量 `pnpm install` |
| **openclaw** | 有 CLI 就走 `openclaw mcp add`；没有就写 `openclaw.json` 的 `mcp.servers.companion` |
| **codex** | 往 `~/.codex/config.toml` 追加 `[mcp_servers.companion]` |

几条设计上的硬要求：

- **先备份再改**，文件写成 `.bak-companion-<时间戳>`；校验不过**自动还原**。
- **幂等**：已经配过就报"没有需要改的东西"，不重写、不堆备份。
- **不覆盖你手写的段**：`openclaw.json` 里已有 `mcp` 但没 `companion` 时，它宁可失败并告诉你，也不猜。
- **可回滚**：`companion connect --rollback` 把改过的还原、新建的删掉。

```bash
companion connect              # 再探测一次并接上
companion connect --status     # 只看装了哪些，不写
companion connect --dry-run    # 只打印会怎么改
companion connect hermes       # 只接一个
companion connect --rollback   # 撤回
```

**自动没接上时的手动兜底**（只在探测失败时才需要）：
DSH 桌面端 → 设置 → 添加插件 → 填 `https://github.com/Yullieuli/companion-kit`。

> **一个平台一份 store**。Windows 和 WSL 是两个文件系统，`~/.companion` 也是两份——
> 在 WSL 里装的和在 Windows 里装的是两个"她"，记忆不共享。这不是 bug，
> 是"不把一个人的记忆放进跨系统共享目录"的取舍。要共用只有一个办法：
> 两边都设 `COMPANION_HOME` 指向同一个 Windows 路径（WSL 走 `/mnt/c/...`），
> 代价是文件锁与权限会变复杂。宿主同理：WSL 里的 hermes 只有 WSL 侧能接。

## 用 Docker 跑（可选）

容器里能跑 core 全套：决策、记忆、心情、事件流、成本账本、MCP stdio、只读 HTTP。
**不能**桌面控制（没有显示）、语音（镜像里没 TTS）、手机 adb（要映射 USB）、浏览器 CDP（要连宿主 Chrome）——
这四条与下面的平台矩阵一致。

```bash
docker compose up -d                      # 起服务：事件流 + context，只绑 127.0.0.1:7788
docker compose exec companion companion tick   # 注意 exec 不吃 ENTRYPOINT，命令要写全
docker compose exec companion companion doctor
docker compose down -v                    # 停服务并删卷
```

自己构建：

```bash
docker build -t companion-kit .                    # 运行镜像
docker build --target test -t companion-kit:test . # 构建时就在容器里跑全量单测
docker run --rm -v companion-data:/data companion-kit companion doctor
```

- **store 必须挂卷**（`/data`）。不挂 = 容器一重建就失忆，等于换个人。
- 国内网络拉不动基础镜像时：`COMPANION_BASE=m.daocloud.io/docker.io/library/python:3.12-slim docker compose up -d`。

## 现在能做什么

```bash
companion init                          # 建 store（幂等，绝不覆盖 SOUL.md）
companion context --budget 1200         # 本轮该注入什么（人格/状态/记忆/人物卡/生活）
companion status                        # 只读：心情、额度、作息、记忆页数
companion tick                          # 问一次"现在想干什么"（零 token）
companion events --wait 30              # 等一次 wake_due
companion say --text "在吗" --dry-run    # 过三道闸（身份/频率/同话题），不记账
companion serve --port 7788             # 事件流 + /context 的 HTTP 口（只读，只绑本机）
companion cost                          # 今天花了多少
companion cost bili_debrief             # 这件事该用哪个档的模型、约多少钱

companion memory write --title 影之刃零 --body "..." --summary "一句话" --tags 游戏 设计
companion memory search 游戏            # 标签 > 摘要 > 正文，越近越靠前
companion memory index                  # 从各页 summary 重新投影索引
companion life add "看完影之刃零" --kind watch --detail "10 分"
companion person learn --who owner --fact "讨厌开放式结局"
companion person pending                 # 哪张卡攒够 20 条该压了
companion person distill owner           # 出题给宿主的模型去压
companion person distill owner --fact "喜欢独立游戏" --fact "讨厌标题党"   # 收卷
companion voice say "今天想跟你说句话"    # 本地 TTS，落盘不发送
companion resume                         # 断点续传：哪些轮次说了话却没写回
companion distill review                 # 记忆自动提炼：从流水里挖值得记住的模式
companion distill accept --summary "他常在深夜找我"
companion heartbeat --host hermes        # 心跳：看门狗据此判断她是不是还活着
companion device look --target desktop   # 截图 + 前台窗口 + 可见窗口标题
companion device act --target phone --act tap --x 540 --y 1200
companion device act --target desktop --act swipe --x 600 --y 400 --x2 600 --y2 900
companion device act --target desktop --act key --key ctrl+s
companion device observe --target desktop --window 记事本   # 窗口级截图 + 本地 OCR
companion device act --target desktop --act key --key ctrl+s --before
companion device list                    # 可见窗口（按面积排）
companion device focus --what 记事本      # 切到某个窗口（走窗口白名单）
companion device replay --last 5          # 把最近几个动作列出来（默认真跑要 --yes）
companion ask list                      # 她想开程序/切窗口，正在等你答应
companion ask allow --id 1793170000-abc123
companion ask reply --text "可以"        # 主人在聊天里回一句，自动落到对应待办上
companion device open --target desktop --what notepad --ask-id 1793170000-abc123
companion react --mood satisfaction=12 --why "他说想听我说说" \
                --opinion "它敢砍经验值是为了逼玩家真学招式" --opinion-tags 游戏 \
                --memory-title 影之刃零 --memory-summary "敢砍经验值" --said "挺公道"
companion opinion list                  # 她的判断清单（hunch / belief / retired）
companion fence "外部文本" --source 群聊   # 打注围栏：命中几处、消毒后是什么
companion group judge "这游戏真好玩"      # 群聊三档：reply / lurk / ignore
companion sticker add ./图.png --mood 无语 --tags 翻白眼
companion sticker pick --mood 无语        # 没合适的就返回空，不凑合
companion sticker extract ./图.png        # 自动收进库里：认情绪、打标签
companion sticker seed                  # 播种内置表情（16 张，8 种情绪各 2 张）
companion webpage read --url https://... --file page.html   # 抽正文 + 摘要 + 缓存
companion webpage pick --query "显卡 显存" --pages '[{"title":"...","url":"..."}]'
companion bili status                   # 今天刷了几条、还能不能刷
companion bili tick                     # 自己挑一条、读完、记进生活流水
companion bili commit BV1xx411c7mD --summary "关卡设计讲透了" --rating 8
companion bili share BV1xx411c7mD --score 15 --reason "他肯定喜欢这个战斗系统"
companion mcp --list                    # 列出 34 个 MCP 工具
```

**676 个单测**，全部离线可跑（B 站用假总线，不碰网络）。其中 9 个是契约/回放：
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
| **表情**（M4） | 按情绪目录 + 标签挑，最近发过的降权，没有对得上的**就不发**；**内置 16 张**（`init` 自动播种）+ **自动提取**（认情绪、打标签） |
| **请示**（§5.5） | 动手前先问一声；主人在聊天里回一句就自动落账；**模型自己批的票开不了设备**——那等于它替主人答应了 |
| **网页**（F 组） | 抽正文 → 摘要 → 挑页 → 缓存；外部文本先过围栏消毒 |
| **MCP**（M6） | 34 个工具，stdio JSON-RPC（强制 UTF-8）；codex / openclaw / 任何 MCP 客户端可直接挂 |

**验证到哪一步了**：见 [docs/VERIFY.md](docs/VERIFY.md)（哪些实测通过、哪些只做了一半、哪些还没验）。

**还没做的**：真 SVC 唱歌（要她自己的音色模型 + 显存）。B 站、hermes 已接完。

### 平台与前置条件（实测边界）

| 环境 | 核心 / MCP / HTTP | 桌面截图与输入 | 手机(adb) | 浏览器(CDP) | 语音 | OCR |
|---|---|---|---|---|---|---|
| Windows | ✅ | ✅ GDI + SendInput | ✅ 装 platform-tools | ✅ 需 Chrome 开 `--remote-debugging-port=9222` | ✅ SAPI / edge-tts | ✅ 装 tesseract |
| WSL | ✅ | ⚠️ 默认不支持；配 `devices.json: windows.python/src` 后转发到 Windows 执行 | ✅ | ✅（Windows 侧 Chrome） | ⚠️ espeak / edge-tts 可用 | ✅ tesseract |
| Linux | ✅ | ❌（X11/Wayland 未实现） | ✅ | ✅ | ✅ espeak / edge-tts | ✅ tesseract |
| macOS | ✅ | ❌（未实现） | ✅ | ✅ | ✅ `say` / edge-tts | ✅ 装 tesseract |
| Docker | ✅ | ❌（无显示） | ❌（需映射 USB/网络 adb） | ❌（需能连宿主 Chrome） | ❌（镜像内无 TTS） | ✅ 装 tesseract |

> 桌面这块不是"应该能跑"——Win32 API 只有 Windows 有，别的系统调用会抛 `DesktopError`。
> `companion doctor` 会在非 Windows 上直接标出来，不会给一个假的 ✓。

### 宿主钩子没生效？先看 doctor

hermes 这类宿主对 shell 钩子有 **consent**：`config.yaml` 里写了 `hooks:` 还不算，
每个 (事件, 命令) 对还得在 `shell-hooks-allowlist.json` 里被批准过。
**没批准就静默跳过**——不报错、不进日志、钩子就是不跑。

```bash
companion doctor        # 有"钩子批准"检查项：哪些配了但没批准，一眼看出
```

装 companion 时会自动批准并自检；`hermes/allowlist_hooks.py` 也能手动管：

```bash
python hermes/allowlist_hooks.py check --home ~/.hermes   # 还差哪些批准
python hermes/allowlist_hooks.py list  --home ~/.hermes   # 当前批准了什么
```

## 接成 MCP（其它 MCP 客户端）

`companion connect` 覆盖 hermes / dsh / openclaw / codex 四家。**其它 MCP 客户端**
（Claude Desktop、Cursor、自研 agent）还得自己加一段——它们的配置位置我们不去猜，
猜错就是把人家的客户端弄得起不来。

```json
{
  "mcpServers": {
    "companion": {
      "command": "C:\\Users\\<你>\\.companion\\venv\\Scripts\\companion.exe",
      "args": ["mcp"],
      "env": { "COMPANION_HOME": "C:\\Users\\<你>\\.companion" }
    }
  }
}
```

两条不能省：

- **用绝对路径**：GUI 宿主不继承你新写到环境变量里的 PATH，写 `companion` 它会找不到。
- **带上 `COMPANION_HOME`**：不然宿主在别的工作目录启动时会读到一个空 store，
  表现为"她失忆了"，而且不报错。

Linux / macOS 上是 `~/.companion/venv/bin/companion`。
没有 `companion` 可执行文件时，用 `<venv 里的 python> -m companion mcp` 等价。

工具面按用途分：

| 类 | 工具 |
|---|---|
| 想 | `companion_context` `companion_status` `companion_tick` `companion_events` `companion_cost` |
| 记 | `companion_memory_write` `companion_memory_search` `companion_life_record` `companion_life_mark_shared` `companion_person` |
| 写 | `companion_react` `companion_opinion` `companion_mood` `companion_fence` |
| 聊 | `companion_group_judge` `companion_group_note` `companion_sticker_pick` `companion_sticker_add` `companion_sticker_stats` |
| 做 | `companion_say` `companion_voice` `device_look` `device_act` `device_open` `companion_ask` |
| 刷 | `companion_bili`（status / tick / commit / share） |

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

## 三个参考各取一点

- [dsh-plugin-memory](https://github.com/LittleBlackTong/dsh-plugin-memory)：启动注入 + **投影去重** + 预算按文件分配；
- [dsh-simulated-life](https://github.com/john-walks-slow/dsh-simulated-life)：**文件协议**，任何引擎写 `events.json` 都能驱动生活；
- [ai-companion-kit](https://github.com/pinganbbOvO/ai-companion-kit)：**MCP + 薄包**的划分方式（brain / memory / transport / own-life 各一包）。

## 路线

M0 骨架 → M1 时钟+心情+上下文 → M2 主动+hermes → M3 记忆+人物卡 → M4 群聊+表情 → M5 B站 → M6 多宿主。

细节与验收标准见 [docs/PLAN.md](docs/PLAN.md)。

## License

MIT，见 [LICENSE](LICENSE)。

---

参与开发看 [CONTRIBUTING.md](CONTRIBUTING.md) ｜ 安全问题走 [SECURITY.md](SECURITY.md)（不要开公开 issue）｜ 变更记录在 [CHANGELOG.md](CHANGELOG.md)
