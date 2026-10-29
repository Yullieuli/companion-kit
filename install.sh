#!/usr/bin/env bash
# 有人会用 `sh install.sh`（dash 不认 pipefail，报错信息还很难懂）。
# 自己换到 bash 再跑，别让安装停在第一行。
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

# companion-kit 一行安装（Linux / macOS / WSL）
#
#   curl -fsSL https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.sh | bash
#
# 全自动：找 Python（没有就用 uv 装一个）→ 建独立 venv → 装包（Release 的 whl，
# 不需要 git）→ 初始化 store → 写 PATH（不用你手动 export）。
#
# 参数：
#   --dsh           顺手注册 DSH 插件（旧参数；现在默认会自动探测所有宿主）
#   --python PATH   指定解释器
#   --home PATH     store 目录（默认 ~/.companion）
#   --from PATH     从本地目录装（开发用）
#   --no-init       不初始化 store
#   --no-path       不改 shell 配置
#   --no-connect    不自动接宿主（hermes/dsh/openclaw/codex）
#   --dry-connect   只打印会怎么接，不写宿主配置
set -uo pipefail

REPO="${COMPANION_REPO:-https://github.com/Yullieuli/companion-kit}"
# 版本只有一个来源：发版时改这一行（或用 COMPANION_VERSION 覆盖）。
# 以前这里和文件名各写一次，结果一个是 0.2.8、一个是 0.2.3，装上去的是半年前的包。
VERSION="${COMPANION_VERSION:-0.3.0}"
WHEEL="${COMPANION_WHEEL:-$REPO/releases/download/v$VERSION/companion_kit-$VERSION-py3-none-any.whl}"
HOME_DIR="${COMPANION_HOME:-$HOME/.companion}"
PYTHON="${COMPANION_PYTHON:-}"
FROM=""
DO_INIT=1
DO_PATH=1
DO_DSH=0
DO_CONNECT=1
DRY_CONNECT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON="$2"; shift 2 ;;
    --home) HOME_DIR="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --no-init) DO_INIT=0; shift ;;
    --no-path) DO_PATH=0; shift ;;
    --no-connect) DO_CONNECT=0; shift ;;
    --dry-connect) DRY_CONNECT=1; shift ;;
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
  # Python 3.12 起 venv 默认不带 setuptools，而本地装/源码装要它来做构建。
  # 不先补这一步，`--from .` 在一台新机器上必然失败（报 BackendUnavailable）。
  "$VPY" -m pip install -q --upgrade setuptools wheel >/dev/null 2>&1 || true
  "$VPY" -m pip install -q --no-compile -e "$FROM" || { echo "本地安装失败" >&2; exit 1; }
else
  say "装 whl（不需要 git）"
  # GitHub Release 的附件在国内经常拉不动（连接超时，不是 404）。
  # 依次试直连与两个常用镜像；都失败才回退 git。
  # 镜像只是"再给一次机会"，所以失败不报错、不打印噪音。
  INSTALLED=0
  for url in "$WHEEL" \
             "https://ghproxy.net/$WHEEL" \
             "https://gh-proxy.com/$WHEEL"; do
    if "$VPY" -m pip install -q --no-compile "$url" >/dev/null 2>&1; then
      INSTALLED=1
      case "$url" in
        "$WHEEL") : ;;
        *) say "直连不通，用镜像装上了" ;;
      esac
      break
    fi
  done
  if [ "$INSTALLED" != "1" ]; then
    say "whl 与镜像都不通，回退 git（需要 git）"
    "$VPY" -m pip install -q --no-compile "git+$REPO" \
      || "$VPY" -m pip install -q --no-compile "git+https://ghproxy.net/$REPO" \
      || { echo "安装失败：whl、镜像与 git 都不通。挂代理后重跑，或用 --from 本地安装" >&2; exit 1; }
  fi
fi

# ── 4. 初始化 store ──────────────────────────────────────
if [ "$DO_INIT" = "1" ]; then
  if ! COMPANION_HOME="$HOME_DIR" "$VPY_BIN/companion" init >/dev/null; then
    echo "初始化 store 失败" >&2
    exit 1
  fi
  say "store 已初始化：$HOME_DIR"
fi

# ── 5. PATH（写进 shell 配置，不用手动 export）────────────
if [ "$DO_PATH" = "1" ]; then
  case ":$PATH:" in
    *":$VPY_BIN:"*) say "PATH 里已有" ;;
    *)
      LINE="export PATH=\"$VPY_BIN:\$PATH\""
      add_path_to_rc() {
        rc="$1"
        [ -f "$rc" ] || : > "$rc" || return 1
        if ! grep -qF "$VPY_BIN" "$rc" 2>/dev/null; then
          printf '\n# companion-kit\n%s\n' "$LINE" >> "$rc" || return 1
          say "已写入 $rc"
        fi
      }
      for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        add_path_to_rc "$rc" || { echo "写 PATH 失败：$rc" >&2; exit 1; }
      done
      # 新系统经常什么 rc 都没有；尤其 macOS 的 zsh 不读 .profile。
      case "${SHELL##*/}" in
        zsh) PRIMARY_RC="$HOME/.zshrc" ;;
        bash) PRIMARY_RC="$HOME/.bashrc" ;;
        *) PRIMARY_RC="$HOME/.profile" ;;
      esac
      add_path_to_rc "$PRIMARY_RC" || { echo "写 PATH 失败：$PRIMARY_RC" >&2; exit 1; }
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

# ── 6. 接宿主（默认全自动）─────────────────────────────────
# 这一步是"一行安装"的另一半：装完 core 之后自动探测本机有哪些宿主，
# 接上它们（hermes 钩子 / dsh profile / openclaw / codex 的 MCP 段）。
# 失败不让整个安装失败——core 是好的，宿主没接上可以稍后重跑。
if [ "$DO_CONNECT" = "1" ]; then
  say ""
  say "接宿主（探测到几个接几个）"
  if [ "$DRY_CONNECT" = "1" ]; then
    COMPANION_HOME="$HOME_DIR" "$VPY_BIN/companion" connect --dry-run || true
  else
    if ! COMPANION_HOME="$HOME_DIR" "$VPY_BIN/companion" connect; then
      say "有宿主没接上。不影响 core，修好后单独重试：companion connect"
    fi
  fi
elif [ "$DO_DSH" = "1" ]; then
  # 旧参数：只注册 DSH，不碰其它宿主
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

宿主已经自动接过了（上面那几行 ✓）。要复查或补接：
  companion connect            # 再探测一次
  companion connect --status   # 只看装了什么
  companion connect --rollback # 撤回接入

人格在 $HOME_DIR/SOUL.md —— 先改这个，她才像"她"。
EOF
