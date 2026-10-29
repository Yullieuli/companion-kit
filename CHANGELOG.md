# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **`companion connect`**：探测本机已装的 hermes / dsh / openclaw / codex 并自动接入，
  不用手抄 JSON / YAML / TOML。四段式（探测 → 计划 → 备份 → 写入 → 校验），
  支持 `--dry-run` / `--status` / `--rollback`，幂等。
- **安装脚本默认接线**：`install.sh` / `install.ps1` 装完自动跑一次 `connect`，
  新增 `--no-connect` / `--dry-connect`。
- **`release.yml`**：打 tag 即发布。校验五处版本一致、跑全量测试、构建 wheel、
  干净 venv 冒烟、上传 Release 附件。
- **镜像回退**：wheel 下载走 GitHub → `ghproxy.net` → `gh-proxy.com` → `git+`，
  README 给出 jsDelivr 的安装地址，解决国内直连 `raw.githubusercontent.com` 超时。
- **wheel 自带 hermes 适配层**（`share/companion/hermes`）：pip 安装的用户也能 `connect hermes`。

### 修复

- **跨平台共用 store 时回滚认错文件**：`connect` 的注册表 key 从「宿主名」改成
  「宿主名@配置目录」。Windows 与 WSL 共用 `COMPANION_HOME` 时，后者不再覆盖前者。
- **dsh 多余的 profile 重写**：一个 profile 需要改时，不再连带重写其他 profile。
- **Windows 两个必踩坑**：YAML 双引号里的 `\U` 被当转义（改用单引号）；
  `pnpm` / `openclaw` 是 `.cmd`，裸名字 spawn 找不到（改用 `shutil.which` 解析全路径）。
- **CI 的 Windows 安装检查是死代码**：`if: runner.os == 'Windows'` 写在
  一个没有 Windows 的矩阵里，从未执行。现在 Windows 真跑 `install.ps1`。

## [0.9.2] - 2026-10-29

### 修复

- `guard`：「别重复」在 hermes 路径上是死代码 —— 改为同时读 `turns.jsonl`。
- 工程审计：MCP 编码、工具注解、额度票、事件游标隔离、安装脚本与文档对齐。

## [0.9.1] - 2026-10-29

### 新增

- `initiative`：被回应后结账（主动做的事有人接，下次加分）。
- `people`：人物卡每 20 条自动提炼。

## [0.9.0] - 2026-10-29

### 新增

- 补齐 `PLAN.md` 里剩下的 7 项，含记忆自动提炼、断点续传、看门狗。

## [0.8.0] - 2026-10-28

### 新增

- `guard`：说话前的三道闸（频率 / 同话题 / 静默衰减）。
- `serve`：只读事件流 HTTP（`/context`、`/events` 长轮询）。
