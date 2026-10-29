#!/bin/sh
# 把 hermes 适配层装好：注入钩子、唤醒脚本、B 站入口、通用入口。
#
# 默认**只说不做**（打印将要做什么）。真要装加 --apply。
# 配置（config.yaml）默认不动 —— 里面已经有别的钩子，自动改 YAML 是最容易出事的地方；
# 只把该加的 YAML 片段打印出来，或者用 --config 让它带着备份去改。
#
# 用法：
#   sh install_hermes.sh                      # 看会做什么
#   sh install_hermes.sh --apply              # 装脚本（不动 config）
#   sh install_hermes.sh --apply --config     # 连 hooks 一起接上（先备份）
#
# 认这几个环境变量（都有默认值，按需覆盖）：
#   HERMES_HOME     hermes 目录，默认 $HOME/.hermes
#   COMPANION_HOME  store 目录，默认 $HOME/.companion
#   COMPANION_PY    python 解释器，默认 $COMPANION_HOME/venv/bin/python
set -eu

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
COMPANION_HOME="${COMPANION_HOME:-$HOME/.companion}"
COMPANION_PY="${COMPANION_PY:-$COMPANION_HOME/venv/bin/python}"

APPLY=0
CONFIG=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --config) CONFIG=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$arg" >&2; exit 2 ;;
  esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPTS="$HERMES_HOME/scripts"

say() { printf '%s\n' "$*"; }
run() { if [ "$APPLY" = 1 ]; then "$@"; else say "  [dry] $*"; fi; }

say "hermes    : $HERMES_HOME"
say "companion : $COMPANION_HOME"
say "python    : $COMPANION_PY"
say "模式      : $([ "$APPLY" = 1 ] && echo 真装 || echo dry-run)"
say ""

[ -d "$HERMES_HOME" ] || { say "✗ 没找到 $HERMES_HOME（先装 hermes）"; exit 1; }
if [ ! -x "$COMPANION_PY" ]; then
  if [ "$APPLY" = 1 ]; then
    say "✗ $COMPANION_PY 不存在或不可执行：先跑仓库根的 install.sh"
    exit 1
  fi
  say "⚠ $COMPANION_PY 不存在：先跑仓库根的 install.sh"
fi
run mkdir -p "$SCRIPTS"

# ① 带路径的包装脚本：把 store 与解释器钉死，免得每次调用都要 export
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_wrapper() {
  name="$1"; shift
  body="$1"
  target="$SCRIPTS/$name"
  if [ "$APPLY" != 1 ]; then
    say "  [dry] 写 $name"
    return
  fi
  tmp="$target.tmp.$$"
  printf '%s\n' "$body" > "$tmp"
  chmod +x "$tmp"
  [ -f "$target" ] && cp -p "$target" "$target.bak-$(date +%Y%m%d-%H%M%S)"
  mv "$tmp" "$target"
  say "  ✓ $name"
}

Q_HOME=$(shell_quote "$COMPANION_HOME")
Q_PY=$(shell_quote "$COMPANION_PY")
Q_SCRIPTS=$(shell_quote "$SCRIPTS")
Q_HOST=$(shell_quote "${COMPANION_HOST:-hermes}")
Q_HOOK=$(shell_quote "$SCRIPTS/companion_hook.py")
Q_POST=$(shell_quote "$SCRIPTS/companion_post.py")
Q_WAKE=$(shell_quote "$SCRIPTS/companion_wake.py")
Q_GATE=$(shell_quote "$SCRIPTS/companion_gate.py")

say "① 包装脚本"
write_wrapper companion_hook.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY $Q_HOOK"
# 后置写回：post_llm_call 拿得到"她这一轮说了什么"，pre_llm_call 拿不到。
# 这条通道是 turns.jsonl 不再是空的关键。
write_wrapper companion_post.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY $Q_POST"
write_wrapper companion_ctx.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY -m companion context --budget 700 --no-soul"
write_wrapper companion_wake.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY $Q_WAKE"
write_wrapper companion_bili.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY -m companion bili \"\$@\""
write_wrapper companion.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY -m companion \"\$@\""
write_wrapper companion_gate.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
exec $Q_PY $Q_GATE"

# 看门狗 wrapper：判断 hermes 是"卡住"还是"死了"
write_wrapper companion_watchdog.sh "#!/bin/sh
export COMPANION_HOME=$Q_HOME
export COMPANION_HOST=$Q_HOST
exec $Q_PY $Q_SCRIPTS/companion_watchdog.py \"\$@\""

# ② 适配层本体（Python，不带路径）
say ""
say "② 适配层"
for f in companion_hook.py companion_post.py companion_wake.py companion_shared.py companion_gate.py patch_napcat_guard.py companion_watchdog.py allowlist_hooks.py; do
  if [ -f "$HERE/$f" ]; then
    run cp "$HERE/$f" "$SCRIPTS/$f"
    [ "$APPLY" = 1 ] && say "  ✓ $f"
  else
    say "  ⚠ 缺 $f（跳过）"
  fi
done

# ③ config.yaml 的钩子：先看有没有，再决定说不说
say ""
say "③ config.yaml 的钩子"
CFG="$HERMES_HOME/config.yaml"
if [ -f "$CFG" ]; then
  if grep -q "companion_hook.sh" "$CFG"; then
    say "  ✓ 已经接上了（pre_llm_call → companion_hook.sh）"
  else
    say "  还没接。需要把下面这段并进 hooks: 下面（并保留你原有的条目）："
    say ""
    say "    pre_llm_call:"
    say "      - command: \"$SCRIPTS/companion_hook.sh\""
    say "        timeout: 10"
    say ""
    say "    post_llm_call:"
    say "      - command: \"$SCRIPTS/companion_post.sh\""
    say "        timeout: 10"
    say ""
    if [ "$CONFIG" = 1 ] && [ "$APPLY" = 1 ]; then
      cp -p "$CFG" "$CFG.bak-companion-$(date +%Y%m%d-%H%M%S)"
      "$COMPANION_PY" - "$CFG" "$SCRIPTS/companion_hook.sh" "$SCRIPTS/companion_post.sh" <<'PY'
import sys, pathlib
cfg = pathlib.Path(sys.argv[1])
hook, post = sys.argv[2], sys.argv[3]
text = cfg.read_text(encoding="utf-8")
lines = text.splitlines(True)
start = next((i for i, l in enumerate(lines) if l.rstrip() == "hooks:"), None)
if start is None:
    lines += ["\nhooks:\n",
              "  pre_llm_call:\n", f'    - command: "{hook}"\n', "      timeout: 10\n",
              "  post_llm_call:\n", f'    - command: "{post}"\n', "      timeout: 10\n"]
else:
    # end 是"hooks: 段之后的第一个同级键"的位置。
    # 注意：插入会改变后续索引，所以每次插入后都要重算，
    # 否则第二条 hook 会插到错误的位置（2026-10-29 实测踩过：
    # pre_llm_call 的条目跑到了 post_llm_call 段里）。
    def hooks_end():
        s = next((i for i, l in enumerate(lines) if l.rstrip() == "hooks:"), None)
        if s is None:
            return len(lines)
        return next((i for i in range(s + 1, len(lines))
                     if lines[i] and not lines[i].startswith((" ", "\t"))), len(lines))

    def ensure_list(key, cmd):
        """把一条 command 追加到 hooks.<key> 下。已存在就不动。"""
        if f'"{cmd}"' in "".join(lines):
            return
        e = hooks_end()
        s = next((i for i, l in enumerate(lines) if l.rstrip() == "hooks:"), None)
        k = next((i for i in range(s + 1, e) if lines[i].strip() == key + ":"), None)
        if k is None:
            e = hooks_end()
            lines.insert(e, f"  {key}:\n")
            lines.insert(e + 1, f'    - command: "{cmd}"\n')
            lines.insert(e + 2, "      timeout: 10\n")
            return
        j = k + 1
        while j < e and (lines[j].startswith("      ") or lines[j].startswith("    - ")):
            j += 1
        lines.insert(j, f'    - command: "{cmd}"\n')
        lines.insert(j + 1, "      timeout: 10\n")

    ensure_list("pre_llm_call", hook)
    ensure_list("post_llm_call", post)
cfg.write_text("".join(lines), encoding="utf-8")
# 写完立刻自检：改坏 YAML 的话，hermes 启动直接就崩了 —— 那比没装还糟。
# 2026-10-29 实测踩过：手写替换时把 "\n" 写成了字面量，config 直接变成非法 YAML。
try:
    import yaml
    yaml.safe_load(cfg.read_text(encoding="utf-8"))
    print("  ✓ hooks.pre_llm_call / hooks.post_llm_call 已追加（YAML 校验通过）")
except ImportError:
    print("  ✓ hooks.pre_llm_call / hooks.post_llm_call 已追加（没装 pyyaml，跳过语法校验；跑 hermes doctor 看一眼）")
except Exception as e:
    # 校验失败要立刻说清楚，并指出备份在哪
    print(f"  ✗ YAML 校验失败：{e}")
    print(f"    配置：{cfg}")
    print("    已自动从备份恢复")
    bak = sorted(cfg.parent.glob(cfg.name + ".bak-companion-*"))[-1]
    if bak.exists():
        cfg.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"    已恢复：{bak}")
    raise SystemExit(1)
PY
      say "  改完重启：hermes gateway restart"
    else
      say "  （加 --apply --config 让我代改；会自动备份）"
    fi
  fi
else
  say "  ⚠ 没有 config.yaml"
fi

say ""
say "③-b 钩子批准（allowlist）"
# hermes 对 shell 钩子有 consent：config.yaml 里写了还不够，
# 每个 (事件, 命令) 对还得在 shell-hooks-allowlist.json 里被批准过。
# 没批准 = 静默不执行：不报错、不进日志、钩子就是不跑。
# 2026-10-29 实测踩过：config 明明有，钩子一个都不触发，查了两小时才发现是这张表。
ALLOW="$HERMES_HOME/shell-hooks-allowlist.json"
if [ "$APPLY" = 1 ]; then
  "$COMPANION_PY" "$HERE/allowlist_hooks.py" add --home "$HERMES_HOME" \
    --event pre_llm_call --command "$SCRIPTS/companion_hook.sh" || true
  "$COMPANION_PY" "$HERE/allowlist_hooks.py" add --home "$HERMES_HOME" \
    --event post_llm_call --command "$SCRIPTS/companion_post.sh" || true
  # 收尾自检：还差批准就大声报出来，别让它悄悄不跑
  if "$COMPANION_PY" "$HERE/allowlist_hooks.py" check --home "$HERMES_HOME"; then
    say "  ✓ 钩子都批准了（表：$ALLOW）"
  else
    say "  ⚠ 有钩子没批准 —— 它们不会执行。批准后再重启 hermes。"
  fi
else
  say "  [dry] 会把这两条加进 $ALLOW："
  say "        pre_llm_call  → $SCRIPTS/companion_hook.sh"
  say "        post_llm_call → $SCRIPTS/companion_post.sh"
  say "  （不加 --apply 我不动它）"
fi

# ④ 发送前闸：把闸插进 napcat 的发送漏斗（改宿主文件，先备份，可回滚）
say ""
say "④ 发送前闸（改 napcat 插件）"
NAPCAT="$HERMES_HOME/plugins/napcat/main.py"
if [ -f "$NAPCAT" ]; then
  if [ "$APPLY" = 1 ]; then
    "$COMPANION_PY" "$SCRIPTS/patch_napcat_guard.py" --napcat "$NAPCAT" \
      --gate "$SCRIPTS/companion_gate.py" --python "$COMPANION_PY" || true
    say "  回滚：$COMPANION_PY $SCRIPTS/patch_napcat_guard.py --napcat $NAPCAT --revert"
  else
    say "  [dry] 会把发送前闸插进 $NAPCAT"
    say "  （加 --apply 我才动它；动之前自动备份，--revert 可回滚）"
  fi
else
  say "  ⚠ 没找到 $NAPCAT（没装 napcat 插件就不用管）"
fi

# ⑤ 唤醒：cron 的 monitor_script
say ""
say "⑤ 随机唤醒（可选）"
say "  它需要一条 cron 任务，monitor_script 指向 companion_wake.sh，*/15 分钟。"
say "  没有的话她会只会在你说话时才动，不会主动找你。"
say ""
say "装完了。验证：echo '{\"session_id\":\"t\",\"platform\":\"napcat\",\"user_message\":\"在吗\"}' | $SCRIPTS/companion_hook.sh"
