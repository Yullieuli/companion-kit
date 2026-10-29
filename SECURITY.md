# 安全策略

## 报告漏洞

请用 GitHub 的[私密漏洞报告](https://github.com/Yullieuli/companion-kit/security/advisories/new)，
**不要**开公开 issue —— 这个项目跑在别人的机器上，还握着设备控制权限，
公开细节等于给所有已部署的实例发通知。

## 我们承诺

- 3 天内确认收到。
- 30 天内给出修复，或说明为什么暂时修不了、你能怎么规避。

个人项目，没有值班表，但不会装没看见。

## 在范围内

| 类型 | 例子 |
|---|---|
| **凭据泄露** | store 里的 cookie / token 被写进 `logs/*.jsonl` 或注入进上下文 |
| **主人边界被绕过** | 非主人会话拿到隐私类回答，或触发 `device_*` 动作 |
| **注入** | 弹幕 / 网页 / 群消息里的文本被当成指令执行；记忆投毒写入成功 |
| **设备闸失效** | 白名单、危险动作黑名单、主人限定、幂等、超时，任意一条能被绕过 |
| **任意代码执行** | 通过 MCP 工具或宿主钩子诱导执行任意 shell |

## 不在范围内

- **杀软误报**：`device` 模块用 ctypes + subprocess + 裸 socket，撞启发式特征。
  判断依据见 [docs/AV.md](docs/AV.md)，这不是漏洞。
- **宿主自身的漏洞**：hermes / dsh / openclaw / codex 的问题请报给对应项目。
- **你自己改 store 导致的行为**：`SOUL.md`、`devices.json` 写错不算安全问题。
- **需要物理接触本机**的攻击。

## 已知设计边界（不是漏洞，但你应该知道）

- **桌面控制只有 Windows 实现**。Linux / macOS 会抛 `DesktopError`，不会静默降级成"什么都没做"。
- **MCP 会话默认不算主人**。`codex` / `openclaw` 连进来的调用者身份未知，
  所以 `device_*` 在这条链路上默认被拒。
- **WSL 与 Windows 各有一份 store**（除非你显式把 `COMPANION_HOME` 指到同一处）。
  两份记忆不会自动同步。
- **凭据只从本机读**：仓库里没有任何 token，B 站 cookie 只存在 `~/.companion/bili/credential.json`。
