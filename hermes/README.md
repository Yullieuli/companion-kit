# hermes 适配层（未激活）

这两个脚本把 companion 接到 hermes 上，**分两步，可以只上第一步**。

| 脚本 | 作用 | 风险 |
|---|---|---|
| `companion_hook.py` | `pre_llm_call` 注入：人格 / 心情 / 额度 / 作息 / 观点 / 生活 / 待你点头的事 | 低：与现有 `said.py` 并存，内容不重叠，坏了只是少一段上下文 |
| `companion_wake.py` | 替换"该不该叫醒她"的判断，设成 cron 的 `monitor_script` | 中：要停掉 `pulse.py` 的骰子和三个固定自由时间 |
| `companion_gate.py` | **发送前闸**：拦住改名与同话题重复（回复模式不吃额度闸） | 低：闸异常时放行并留痕，不会把对话搞挂 |
| `patch_napcat_guard.py` | 把闸插进 napcat 的 `send()`（幂等 / 自动备份 / `--revert` 可回滚） | 中：改宿主文件，默认 dry-run，`--apply` 才落盘 |

## 第三步：发送前闸（把"说出去之前"变成可判定）

hermes 的钩子只有 `pre_llm_call`（注入）与 `pre_verify`（要求继续），**拦不住已经生成的最终回复**。
真正的拦截点是渠道的发送函数 —— napcat 的 `send()` 是所有出站文本的单一漏斗。

```bash
# 预览要改什么（不改文件）
python3 ~/.hermes/scripts/patch_napcat_guard.py --dry-run

# 真插（自动备份 .bak-gate-时间戳）
python3 ~/.hermes/scripts/patch_napcat_guard.py

# 撤销
python3 ~/.hermes/scripts/patch_napcat_guard.py --revert
```

插进去的闸调 `companion_gate.py`：

| 模式 | 判定 | 用途 |
|---|---|---|
| `reply`（默认） | 身份锁 + 同话题 10 分钟不追问 | 回复主人。**不看额度**：回复不占主动开口额度 |
| `active` | 上面两项 + 额度/间隔 | 主动开口前预检 |

自己也能测：

```bash
echo '{"text": "以后叫我小璃吧"}' | ~/.hermes/scripts/companion_gate.py
# → {"ok": false, "blocked": "identity", ...}   退出码 1
echo '{"text": "在吗"}' | ~/.hermes/scripts/companion_gate.py
# → {"ok": true, ...}                             退出码 0
```

## 第四步：请示单要有人看

她动手前会问一声（`companion ask`）。没有人呈现就等于没问过：

- **hermes**：钩子会把「等你点头」摆进注入上下文，主人聊天时就能看见。
- **DSH**：适配器会检测待办并投递给主人会话（同一件事不会反复催）。

主人答应了要回填：

```bash
companion ask list              # 看还等哪些
companion ask allow <ask_id>    # 答应
companion ask deny  <ask_id>    # 拒绝
```

## 前置（已验证）
```bash
# WSL 里装一份，但和 Windows 共用同一份 store
python3 -m venv ~/.companion-venv
~/.companion-venv/bin/python -m pip install --no-compile <whl>
export COMPANION_HOME=/mnt/c/Users/you/.companion
~/.companion-venv/bin/python -m companion status     # 能看到 Windows 那边的 quota
```

WSL 连不上 GitHub（实测超时），所以 whl 先下到 Windows 再从这个路径装：
`/mnt/c/Users/you/AppData/Local/Temp/companion_kit-*.whl`

## 第一步：注入（可单独上）

测：

```bash
echo '{"session_id":"t","platform":"napcat","user_message":"在吗"}' \
  | COMPANION_HOME=/mnt/c/Users/you/.companion \
    ~/.companion-venv/bin/python companion_hook.py
```

装：把 `~/.hermes/scripts/companion_hook.py` 复制过去，在 `config.yaml` 里加：

```yaml
hooks:
  pre_llm_call:
    - command: "/usr/bin/python3 /home/you/.hermes/scripts/said.py hook-pre"
      timeout: 10
    - command: "COMPANION_HOME=/mnt/c/Users/you/.companion /home/you/.companion-venv/bin/python /home/you/.hermes/scripts/companion_hook.py"
      timeout: 10
```

然后 `touch ~/.hermes/restart.signal`。

**两条自动跳过**：`session_id` 以 `local-` 开头的 cron 会话、平台不是 napcat/qq 的会话。
SOUL 还是模板时会整段不注入（避免把占位符塞进她的提示词）。

## 第二步：换时钟（已完成 2026-10-28，只换了「主动搭话」）

**实际只把「随机唤醒」这一个 job 的事件门换成了 `companion_wake.sh`**，没有动
`随机日记`（*/20）、`随机说说`（*/30）、`自由时间`（10:30/14:30/20:30）。

原因：这三个 job 各自带「上下文包」（`【Script Output】`），她的提示词依赖那些包；
而 companion 的 `initiative` 里还没有「刷 B 站」这个动作（M5 没做），
现在把自由时间关掉，她就**没有生活可过了**——那反而是退步。

等 M5 把 B 站接进来、`initiative` 能选「去看点什么」之后，再合并这三个 job。

### 时钟只能有一个主人

`drive.tick` 加了宿主锁 `logs/tick_owner.json`（30 分钟有效）：
非主人 tick 直接返回 `blocked=host`，不扣额度、不写事件。

踩到的坑：DSH 插件用 `PYTHONPATH` 指向**包内自带的 src**（0.0.8），
升级 `~/.companion/venv` 对它无效，它照旧每 5 分钟偷一次额度（实测 09:00/09:50/10:40）。
所以要么把插件更新到 0.2.5+（`dsh plugin add github:Yullieuli/companion-kit#main`），
要么在它的 `src/companion/drive.py` 里临时 `return`。

### 作息

`rhythm.json` 改成「睡眠 01:00–08:30，其余醒着」。原来默认是 20:00–24:00 + 睡到 09:00，
照那个来，她 08:52 的早安问候会被当成「在睡觉」直接拦掉。

## 第二步：换时钟（旧说明，留档）

测：

```bash
COMPANION_HOME=/mnt/c/Users/you/.companion \
  ~/.companion-venv/bin/python companion_wake.py --dry-run --json
```

`--dry-run` 只读（不扣额度、不写事件），用来观察"现在会怎么判"。
不带 `--dry-run` 才是真的 tick。

输出约定：**该说话就打印一行 `WAKE|理由|动作`，不该说就什么都不打印**。
hermes 的 cron 判定是"输出变了就起会话"，而空输出被当成"没叫我"，
所以这个约定正好把她的作息接管过来。

切换时要同时做三件事，否则会有两套闹钟抢配额：

1. 「随机唤醒」cron 的 `monitor_script` 改成 `companion_wake.py`
2. 删掉 `pulse.py` 里的 `IDLE_SHUFFLE` 骰子
3. 停掉 `30 10,14,20` 那三个固定自由时间

## 回退

两步都是配置改动，回退 = 把 `config.yaml` 和 `jobs.json` 换个备份再重启 gateway。
备份文件名统一带时间戳，`ls ~/.hermes/*.bak-*` 就能找到。
