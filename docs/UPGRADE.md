# 升级说明

> 从 0.9.x 升到 0.9.2+，有一处**行为变更**：设备控制默认关闭。
> 不改配置的话，她会突然"看不见你的屏幕、点不动你的窗口"——这是设计如此，不是坏了。

## 一、必须做：设备默认拒绝

0.9.2 起，新 store 的 `devices.json` 默认是：

```json
{
  "enabled": false,
  "owner_only": true,
  "mcp_owner": false,
  "allow": {
    "phone":   {"packages": []},
    "desktop": {"windows": []},
    "browser": {"domains": []},
    "game":    {"windows": []}
  }
}
```

三条新规则：

| 规则 | 以前 | 现在 |
|---|---|---|
| 设备总开关 | 默认开 | **默认关**（`enabled=false`），要显式打开 |
| 白名单 | 空 = 不限制 | **空 = 拒绝**。`open/launch/focus` 都必须命中白名单 |
| MCP 主人身份 | 信任模型传的 `is_owner=true` | **只认 `devices.json` 的 `mcp_owner`**（默认 false） |

### 老用户怎么改

编辑 `~/.companion/devices.json`：

```json
{
  "enabled": true,
  "mcp_owner": true,
  "allow": {
    "desktop": {"windows": ["记事本", "notepad"]},
    "browser": {"domains": ["bilibili.com"]},
    "phone":   {"packages": ["com.tencent.mm"]}
  }
}
```

- `mcp_owner: true` 表示"这个 MCP 服务是我亲手配给她的"——只有这样，codex / openclaw 连进来
  才算主人会话。**模型在工具参数里写 `is_owner: true` 不再有任何作用。**
- 白名单按目标分：`phone` 看 `packages`，`desktop` / `game` 看 `windows`，`browser` 看 `domains`。
- 危险动作（关机、卸载、转账、改密码…）永远拒绝，白名单也救不了。

## 二、无需改动但值得知道

| 变化 | 说明 |
|---|---|
| 额度票 | `tick` 决策时扣的那一次，`say` 开口时拿票免单（一次性、20 分钟过期、绑定动作） |
| 并发写 | store 写入有跨进程锁（Windows msvcrt / POSIX flock）；多宿主同时写不会丢更新 |
| `react` 幂等 | 传 `turn_id` 时同一轮重试不会重复改心情/记忆/费用 |
| 成本账本 | `react(usage=...)` 的真实用量会进 `logs/cost.jsonl`，供预算闸使用 |
| 设备危险词优先 | 危险词检查在白名单之前，命中直接拒 |

## 三、新增的可选项

- **Docker**：`docker compose up -d` 即可跑 core，store 挂 `/data` 卷（见 README）。
- **CI**：Windows / Linux / macOS 三平台跑单测与打包。
- **DSH 适配器**：事件游标持久化到 `dsh.cursor`，只有投递成功才推进，重启后不重投。

## 四、升级步骤

```bash
# Linux / macOS / WSL
bash install.sh --from /path/to/companion-kit     # 从本地目录装
# 或等 Release 发布后：curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash

# Windows（PowerShell）
.\install.ps1 -From .
```

装完检查一下：

```bash
companion doctor        # 版本、路径、各能力可用性
companion device log    # 设备动作留痕
```

如果 `doctor` 里设备那项显示"没开"，去 `~/.companion/devices.json` 按第一节改。
