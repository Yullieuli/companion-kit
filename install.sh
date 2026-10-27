#!/usr/bin/env bash
# companion-kit 一行安装
#
#   curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash
#
# 它会：建一个独立 venv（不污染系统 Python）→ 从 GitHub 装本包 → 初始化 store →
# 把 `companion` 放到 PATH → 打印各宿主怎么接。
#
# 参数：
#   --python PATH   指定 python3.10+（默认自动找）
#   --home PATH     store 目录（默认 ~/.companion）
#   --no-init       只装，不初始化
#   --from PATH     从本地目录装（开发用，替代 git）
set -euo pipefail

REPO="${COMPANION_REPO:-https://github.com/Yullieuli/companion-kit}"
HOME_DIR="${COMPANION_HOME:-$HOME/.companion}"
PYTHON="${COMPANION_PYTHON:-}"
FROM=""
DO_INIT=1

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift 2 ;;
    --home) HOME_DIR="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --no-init) DO_INIT=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

echo "== companion-kit =="
echo "store: $HOME_DIR"

# 1) 找 Python（>=3.10）
if [ -z "$PYTHON" ]; then
  for c in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys; print("%d%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
      if [ "$v" -ge 310 ] 2>/dev/null; then PYTHON="$(command -v "$c")"; break; fi
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "找不到 Python 3.10+。装一个再来：https://www.python.org/downloads/" >&2
  exit 1
fi
echo "python: $PYTHON ($("$PYTHON" -V 2>&1))"

# 2) venv
VENV="$HOME_DIR/venv"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate" 2>/dev/null || . "$VENV/Scripts/activate"

# 3) 装包
if [ -n "$FROM" ]; then
  echo "从本地装：$FROM"
  python -m pip install -q --upgrade pip
  python -m pip install -q -e "$FROM"
else
  echo "从 GitHub 装…"
  python -m pip install -q --upgrade pip
  python -m pip install -q "git+$REPO"
fi

# 4) 初始化 store
if [ "$DO_INIT" = "1" ]; then
  COMPANION_HOME="$HOME_DIR" companion init
fi

# 5) 放到 PATH
BIN_DIR="${COMPANION_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/companion" "$BIN_DIR/companion" 2>/dev/null || \
  ln -sf "$VENV/Scripts/companion" "$BIN_DIR/companion"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "提示：把 $BIN_DIR 加进 PATH，才能直接敲 companion" ;;
esac

cat <<EOF

装好了。接下来选一个宿主：

  DSH（桌面端「添加插件」也支持这个地址）
    dsh plugin --profile desktop add $REPO

  Hermes
    在 ~/.hermes/config.yaml 的 plugins.enabled 里加 - companion
    （适配器仍在开发，当前可先用 CLI：companion context / tick / events）

  Codex / OpenClaw 等只支持 MCP 的宿主
    companion mcp     # 尚未实现，M6 提供

自检：
  companion cost
  companion tick
  companion context --budget 1200

store 在 $HOME_DIR：
  SOUL.md            人格（先改这个）
  rhythm.json        作息窗口
  mood/state.json    心情
  logs/              花费、事件、她主动做过什么
EOF
