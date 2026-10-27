#!/usr/bin/env bash
# companion-kit 一行安装（Linux / macOS / WSL）
#
#   curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash
#
# 全自动：找 Python（没有就用 uv 装一个）→ 建独立 venv → 装包（Release 的 whl，
# 不需要 git）→ 初始化 store → 写 PATH（不用你手动 export）。
#
# 参数：
#   --dsh           顺手注册 DSH 插件
#   --python PATH   指定解释器
#   --home PATH     store 目录（默认 ~/.companion）
#   --from PATH     从本地目录装（开发用）
#   --no-init       不初始化 store
#   --no-path       不改 shell 配置
set -uo pipefail

REPO="${COMPANION_REPO:-https://github.com/Yullieuli/companion-kit}"
WHEEL="${COMPANION_WHEEL:-$REPO/releases/download/v0.0.1/companion_kit-0.0.1-py3-none-any.whl}"
HOME_DIR="${COMPANION_HOME:-$HOME/.companion}"
PYTHON="${COMPANION_PYTHON:-}"
FROM=""
DO_INIT=1
DO_PATH=1
DO_DSH=0

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift 2 ;;
    --home) HOME_DIR="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --no-init) DO_INIT=0; shift ;;
    --no-path) DO_PATH=0; shift ;;
    --dsh) DO_DSH=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
echo "== companion-kit =="
say "store: $HOME_DIR"

# ── 1. Python 3.10+（没有就装 uv，用 uv 拉）───────────────
ok_py() { [ -n "${1:-}" ] && "$1" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)' >/dev/null 2>&1; }

if ! ok_py "$PYTHON"; then
  PYTHON=""
  for c in python3.12 python3.11 python3.10 python3 python; do
    p="$(command -v "$c" 2>/dev/null || true)"
    if ok_py "$p"; then PYTHON="$p"; break; fi
  done
fi

if [ -z "$PYTHON" ]; then
  say "没找到 Python 3.10+，用 uv 装一个（不需要 root）"
  UV="$(command -v uv 2>/dev/null || true)"
  if [ -z "$UV" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || {
      echo "uv 装不上。请手动装 Python 3.10+ 后重跑。" >&2; exit 1; }
    UV="$HOME/.local/bin/uv"
  fi
  "$UV" python install 3.12 >/dev/null 2>&1 || { echo "uv 拉 Python 失败" >&2; exit 1; }
  PYTHON="$("$UV" python find 3.12 2>/dev/null | head -1)"
  [ -n "$PYTHON" ] || { echo "uv 装完但找不到解释器" >&2; exit 1; }
fi
say "python: $PYTHON ($("$PYTHON" -V 2>&1))"

# ── 2. venv ───────────────────────────────────────────────
VENV="$HOME_DIR/venv"
if [ ! -x "$VENV/bin/python" ] && [ ! -x "$VENV/Scripts/python.exe" ]; then
  "$PYTHON" -m venv "$VENV" || { echo "建 venv 失败" >&2; exit 1; }
fi
if [ -x "$VENV/bin/python" ]; then
  VPY="$VENV/bin/python"; VPY_BIN="$VENV/bin"
else
  VPY="$VENV/Scripts/python.exe"; VPY_BIN="$VENV/Scripts"
fi
say "venv: $VENV"

# ── 3. 装包（whl 优先，失败回退 git）──────────────────────
"$VPY" -m pip install -q --upgrade pip >/dev/null 2>&1

if [ -n "$FROM" ]; then
  say "从本地装：$FROM"
  "$VPY" -m pip install -q -e "$FROM" || { echo "本地安装失败" >&2; exit 1; }
else
  say "装 whl（不需要 git）"
  if ! "$VPY" -m pip install -q "$WHEEL"; then
    say "whl 失败，回退 git"
    "$VPY" -m pip install -q "git+$REPO" || { echo "安装失败：whl 与 git 都不通" >&2; exit 1; }
  fi
fi

# ── 4. 初始化 store ──────────────────────────────────────
if [ "$DO_INIT" = "1" ]; then
  COMPANION_HOME="$HOME_DIR" "$VPY_BIN/companion" init >/dev/null
  say "store 已初始化：$HOME_DIR"
fi

# ── 5. PATH（写进 shell 配置，不用手动 export）────────────
if [ "$DO_PATH" = "1" ]; then
  case ":$PATH:" in
    *":$VPY_BIN:"*) say "PATH 里已有" ;;
    *)
      LINE="export PATH=\"$VPY_BIN:\$PATH\""
      for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        grep -qF "$VPY_BIN" "$rc" 2>/dev/null && continue
        printf '\n# companion-kit\n%s\n' "$LINE" >> "$rc"
        say "已写入 $rc"
      done
      export PATH="$VPY_BIN:$PATH"
      ;;
  esac
  # 记住 store 位置
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$rc" ] || continue
    grep -qF "COMPANION_HOME" "$rc" 2>/dev/null && continue
    printf 'export COMPANION_HOME="%s"\n' "$HOME_DIR" >> "$rc"
  done
  export COMPANION_HOME="$HOME_DIR"
fi

# ── 6. 可选：注册 DSH ────────────────────────────────────
if [ "$DO_DSH" = "1" ]; then
  if command -v dsh >/dev/null 2>&1; then
    say "注册到 DSH…"
    dsh plugin --profile desktop add "$REPO" || say "DSH 注册没成功；可在「添加插件」里填 $REPO"
  else
    say "没检测到 dsh，跳过"
  fi
fi

cat <<EOF

装好了。新开终端后直接：
  companion context --budget 1200
  companion tick
  companion cost

DSH：设置 → 添加插件 → 填
  $REPO

人格在 $HOME_DIR/SOUL.md —— 先改这个，她才像"她"。
EOF
