# hermes 适配层（未激活）

这两个脚本把 companion 接到 hermes 上，**分两步，可以只上第一步**。

| 脚本 | 作用 | 风险 |
|---|---|---|
| `companion_hook.py` | `pre_llm_call` 注入：人格 / 心情 / 额度 / 作息 / 观点 / 生活 | 低：与现有 `said.py` 并存，内容不重叠，坏了只是少一段上下文 |
| `companion_wake.py` | 替换"该不该叫醒她"的判断，设成 cron 的 `monitor_script` | 中：要停掉 `pulse.py` 的骰子和三个固定自由时间 |

## 前置（已验证）

```bash
# WSL 里装一份，但和 Windows 共用同一份 store
python3 -m venv ~/.companion-venv
~/.companion-venv/bin/python -m pip install --no-compile <whl>
export COMPANION_HOME=/mnt/c/Users/Anran/.companion
~/.companion-venv/bin/python -m companion status     # 能看到 Windows 那边的 quota
```

WSL 连不上 GitHub（实测超时），所以 whl 先下到 Windows 再从这个路径装：
`/mnt/c/Users/Anran/AppData/Local/Temp/companion_kit-*.whl`

## 第一步：注入（可单独上）

测：

```bash
echo '{"session_id":"t","platform":"napcat","user_message":"在吗"}' \
  | COMPANION_HOME=/mnt/c/Users/Anran/.companion \
    ~/.companion-venv/bin/python companion_hook.py
```

装：把 `~/.hermes/scripts/companion_hook.py` 复制过去，在 `config.yaml` 里加：

```yaml
hooks:
  pre_llm_call:
    - command: "/usr/bin/python3 /home/anran/.hermes/scripts/said.py hook-pre"
      timeout: 10
    - command: "COMPANION_HOME=/mnt/c/Users/Anran/.companion /home/anran/.companion-venv/bin/python /home/anran/.hermes/scripts/companion_hook.py"
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
COMPANION_HOME=/mnt/c/Users/Anran/.companion \
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
