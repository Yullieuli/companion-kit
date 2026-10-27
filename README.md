# companion-kit

跨 agent 的「伴侣运行时」：一套人格、一套记忆、一套时钟，dsh / hermes / openclaw / codex 都能挂。

> **状态：规格阶段**。还没写实现代码 —— 先确认 [docs/SPEC.md](docs/SPEC.md) 与 [docs/PLAN.md](docs/PLAN.md)。

## 它解决什么

现在这套「生活感」散在好几个仓库和脚本里：主动搭话一套、自由时间一套、刷 B 站又一套，各自记配额、各自算时间、各自写记忆。结果是**同一件事三份实现，互相打架**——凌晨可能被叫醒、同一句话被记两遍、偏好算两次分。

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

## 一行安装

```bash
curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash
```

它建一个独立 venv（不碰系统 Python）、从 GitHub 装包、初始化 store、把 `companion` 放进 PATH。

| 宿主 | 接法 |
|---|---|
| **DSH** | 桌面端「添加插件」里填 `https://github.com/Yullieuli/companion-kit`<br>或命令：`dsh plugin --profile desktop add github:Yullieuli/companion-kit` |
| **Hermes** | 适配器仍在开发；现在可先用 CLI：`companion context / tick / events` |
| **Codex / OpenClaw** | `companion mcp`（M6 提供） |

DSH 那边是一层薄壳：`package.json` 指向 `dsh/index.js`，插件每 5 分钟问一次 core「该不该醒」，
有 `wake_due` 才用 `sessionController.prompt` 叫她。**壳里没有逻辑**——换宿主不用重写行为。

## 现在能跑什么

```bash
companion init                      # 建 store（幂等）
companion context --budget 1200     # 本轮该注入什么（人格/心情/时间/额度）
companion tick                      # 问一次"现在该干什么"（零 token）
companion events --wait 30          # 等一次 wake_due
companion cost                      # 今天花了多少
```

M0/M1/M1.5/M2 的 core 已完成（76 个单测）：文件协议、时钟、心情、额度、上下文、
成本闸、自主决策、事件队列。还没做的是三个 adapter（hermes / dsh 的投递 / MCP）。

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
