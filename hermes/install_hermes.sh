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
#   COMPANION_PY    python 解释器，默认 $HOME/.companion-venv/bin/python
set -eu

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
COMPANION_HOME="${COMPANION_HOME:-$HOME/.companion}"
COMPANION_PY="${COMPANION_PY:-$HOME/.companion-venv/bin/python}"

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
[ -x "$COMPANION_PY" ] || say "⚠ $COMPANION_PY 不存在：先跑仓库根的 install.sh"
run mkdir -p "$SCRIPTS"

# ① 带路径的包装脚本：把 store 与解释器钉死，免得每次调用都要 export
write_wrapper() {
  name="$1"; shift
  body="$1"
  target="$SCRIPTS/$name"
  tmp="$target.tmp.$$"
  printf '%s\n' "$body" > "$tmp"
  chmod +x "$tmp"
  if [ "$APPLY" = 1 ]; then
    [ -f "$target" ] && cp -p "$target" "$target.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$tmp" "$target"
    say "  ✓ $name"
  else
    rm -f "$tmp"
    say "  [dry] 写 $name"
  fi
}

say "① 包装脚本"
write_wrapper companion_hook.sh "#!/bin/sh
export COMPANION_HOME=$COMPANION_HOME
exec $COMPANION_PY $SCRIPTS/companion_hook.py"
write_wrapper companion_ctx.sh "#!/bin/sh
export COMPANION_HOME=$COMPANION_HOME
exec $COMPANION_PY -m companion context --budget 700 --no-soul"
write_wrapper companion_wake.sh "#!/bin/sh
export COMPANION_HOME=$COMPANION_HOME
exec $COMPANION_PY $SCRIPTS/companion_wake.py"
write_wrapper companion_bili.sh "#!/bin/sh
export COMPANION_HOME=$COMPANION_HOME
exec $COMPANION_PY -m companion bili \"\$@\""
write_wrapper companion.sh "#!/bin/sh
export COMPANION_HOME=$COMPANION_HOME
exec $COMPANION_PY -m companion \"\$@\""

# ② 适配层本体（Python，不带路径）
say ""
say "② 适配层"
for f in companion_hook.py companion_wake.py companion_shared.py; do
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
    if [ "$CONFIG" = 1 ] && [ "$APPLY" = 1 ]; then
      cp -p "$CFG" "$CFG.bak-companion-$(date +%Y%m%d-%H%M%S)"
      "$COMPANION_PY" - "$CFG" "$SCRIPTS/companion_hook.sh" <<'PY'
import sys, re, pathlib
cfg, hook = pathlib.Path(sys.argv[1]), sys.argv[2]
text = cfg.read_text(encoding="utf-8")
if "companion_hook.sh" in text:
    print("  · 已存在，未改动"); raise SystemExit(0)
lines = text.splitlines(True)
# 找到 hooks: 段与其中已有的 pre_llm_call:
start = next((i for i, l in enumerate(lines) if l.rstrip() == "hooks:"), None)
if start is None:
    lines += ["\nhooks:\n", "  pre_llm_call:\n", f'    - command: "{hook}"\n', "      timeout: 10\n"]
else:
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "\t"))), len(lines))
    pre = next((i for i in range(start + 1, end) if lines[i].strip() == "pre_llm_call:"), None)
    if pre is None:
        lines.insert(start + 1, "  pre_llm_call:\n")
        lines.insert(start + 2, f'    - command: "{hook}"\n')
        lines.insert(start + 3, "      timeout: 10\n")
    else:
        # 插到该列表末尾：找到下一条同级键
        j = pre + 1
        while j < end and (lines[j].startswith("      ") or lines[j].startswith("    - ")):
            j += 1
        lines.insert(j, f'    - command: "{hook}"\n')
        lines.insert(j + 1, "      timeout: 10\n")
cfg.write_text("".join(lines), encoding="utf-8")
try:                                   # 有 pyyaml 就做语法校验（venv 里通常没有）
    import yaml
    yaml.safe_load(cfg.read_text(encoding="utf-8"))
    print("  ✓ hooks.pre_llm_call 已追加（YAML 校验通过）")
except ImportError:
    print("  ✓ hooks.pre_llm_call 已追加（没装 pyyaml，跳过语法校验；跑 hermes doctor 看一眼）")
PY
      say "  改完重启：hermes gateway restart"
    else
      say "  （加 --apply --config 让我代改；会自动备份）"
    fi
  fi
else
  say "  ⚠ 没有 config.yaml"
fi

# ④ 唤醒：cron 的 monitor_script
say ""
say "④ 随机唤醒（可选）"
say "  它需要一条 cron 任务，monitor_script 指向 companion_wake.sh，*/15 分钟。"
say "  没有的话她会只会在你说话时才动，不会主动找你。"
say ""
say "装完了。验证：echo '{\"session_id\":\"t\",\"platform\":\"napcat\",\"user_message\":\"在吗\"}' | $SCRIPTS/companion_hook.sh"
