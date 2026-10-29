# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

本文件从 0.3.0 开始记录。0.2.x 及更早的变更请查阅 commit 历史。

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
