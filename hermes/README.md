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

## 第二步：换时钟

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
