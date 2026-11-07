# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

本文件从 0.3.0 开始记录。0.2.x 及更早的变更请查阅 commit 历史。

## [0.4.1] - 2026-11-07

### 新增

- **`companion connect hermes` 会装定时叫醒**。之前这一步一直是手工加的：
  只装 hooks 和 MCP 的话，她是「只会回答」的——能聊、能刷片、能发表情，
  但永远不会主动开口。现在写进 `~/.hermes/cron/jobs.json`（`*/15`，幂等，
  已有同名任务就跳过）。唤醒提示是通用的，不写死名字和口癖，人格来自 SOUL.md。
- **`companion doctor` 加「能不能跑」的检查**。原来只查模块能不能导入，
  全绿但新机器上她一句话都说不出、一张片都刷不了。现在多查四项：
  人设（还是占位模板吗）、B 站凭证、表情库、生活记录 —— 每项给出「缺了会怎样 + 怎么补」。

### 移除

- 仓库根的 `CODE_OF_CONDUCT.md`、`CONTRIBUTING.md`、`SECURITY.md`、`LICENSE`
  四个文件。这个项目没有精力维护社区流程和贡献者协议，留着是空承诺。
  README、Issue 模板、`package.json` 里指向它们的引用一并清掉。
  `pyproject.toml` 里的 `license = { text = "MIT" }` 保留（不影响构建和 wheel 元数据）。

## [0.4.0] - 2026-11-05

### 新增

- **性格层**：经历会沉淀成倾向（`character`），刷片、聊天、判断都会长进去；
  注入的是一句「你在意什么」，不是一份统计表。
- **会自己发表情、发截图、发语音**：新增 `relay` 层把文件搬到 NapCat 能读到的
  共享目录，`sticker_pick` / `device_look` / `voice` 的返回值里直接带 CQ 码。
  唤醒包也会提示「想配张表情就配，不想配就纯文字」——给能力，不规定频次。
- **B 站评论**：`companion_bili` 新增 `action="comment"`，发完自动调
  `verify_comment` 三层对照，如实回报 `public` / `self_only`（影子审核）。
- **表情库可删**：新增 `companion_sticker_remove`。
- **自由时间并入同一套时钟**：不再和 `pulse` 各自掷骰子。

### 变更

- 刷片来源配比重做：去掉 `ranking`（和 `popular` 重复），搜索词从 2 个加到 3 个，
  并给来源加权（自己搜的 +3、关注/相关 +1、平台推的 0）。
  实测候选池里「她自己选的」从 18% 升到约 43%。
- `taste.like` 清成纯方向词（纪录片/手工/科普/独立游戏/动漫），不再硬编码 UP 名和作品名。
- 分享门槛 `min_score` 从 13 降到 9（13 在现有打分体系下永远达不到）。

### 修复

- **一天连刷四条同一部作品**：打分里「评测」+3 和 `taste.like` 的 +3 叠成 11 分，
  其他方向最高 4 分，永远轮不到别人。
- **作品去重被书名号骗过**：`《除了战斗，影之刃零还能玩到什么？》` 书名号里是一整句话，
  取出来和已知作品名对不上，去重直接失效。
- **搜索结果标题带 `<em class="keyword">` 高亮标签**，污染打分和 `life` 记录。
- **环境事件从来没生效**：`drive.py` 调用 `mood_rules.ambient()` 却没 import，
  `NameError` 被 `except` 吞掉——「被冷落 / 很久没见 / 深夜」三种事件一次都没跑过。
  同时修了调用顺序（原来在 mood 读取之后，决策看到的还是旧情绪）。
- **心情按调用次数累积**：`tick` 把衰减结果写回，调用越频繁掉得越快。
- **主动开口插进正在进行的对话**：主人 15 分钟内说过话时不打断。
- **`ttl=0` 的缓存当新鲜**：边界用了 `<=`，Windows 的 `time.time()` 精度粗，
  写完立刻读 `age` 正好是 0。
- 唱了不发（只生成音频没发出去）、搜索死锁、`--version` 缺失、成本账本永远为空。

### 工程

- 清理死代码：6 个生产零引用的函数、22 个未使用的 import；`.bak` 临时备份移出仓库。
- CI 顶层钉 UTF-8（`LANG`/`LC_ALL`/`PYTHONUTF8`）：ubuntu runner 的 locale 不保证 UTF-8，
  中文文件名写进去读出来对不上，关键词识别会落空。
- 补齐 `character` / `relay` / `mood` 的测试，全量 758 项。

## [0.3.0] - 2026-10-29

### 新增

- `companion connect`：探测并接入本机已安装的宿主，目前覆盖 hermes、dsh、openclaw、codex。
  支持 `--dry-run`（只打印计划）、`--status`（只探测）、`--rollback`（还原改动）。
- 安装脚本在初始化后自动执行 `companion connect`，可用 `--no-connect` / `-NoConnect` 关闭。
- `.github/workflows/release.yml`：推送 `v*` tag 时自动校验版本、运行测试、构建并发布 wheel。
- wheel 内置 hermes 适配层（安装到 `share/companion/hermes`），pip 安装后可直接接入 hermes。
- 安装脚本下载 wheel 时支持镜像回退（GitHub → ghproxy.net → gh-proxy.com → git）。
- 新增 `CONTRIBUTING.md`、`CHANGELOG.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`。
- 新增 Issue 表单（缺陷 / 功能建议）与 Pull Request 模板。
- `pyproject.toml` 补充 `readme`、`authors`、`keywords`、`classifiers`、`project.urls`。

### 变更

- CI 增加 Windows 安装演练，Windows 上真实执行 `install.ps1`。
- 文档中的示例路径改为通用写法。
- README 的国内安装说明改用 ghproxy（jsDelivr 对 `@main` 有缓存）。

### 修复

- 跨平台共用 store 时，`connect --rollback` 会还原到其他平台的文件。
- dsh 修改单个 profile 时，连带重写其余 profile。
- Windows 上 `config.yaml` 中的路径因反斜杠被当作转义序列而解析失败。
- Windows 上 `pnpm`、`openclaw` 等 `.cmd` 命令无法被直接调用。
- Windows 上 CLI 输出中文时的编码错误。
- Python 3.10 上子进程缺少 `SYSTEMROOT` 导致解释器无法启动。
- `life._day_key` 在不同平台上对异常时间戳的返回值不一致。
