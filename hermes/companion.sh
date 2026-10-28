#!/bin/sh
# 她自己的命令行入口。参数原样透传给 `companion`：
#   companion.sh status
#   companion.sh life recent --limit 5
#   companion.sh ask list
#   companion.sh ask allow --id 1793170000-abc123
#   companion.sh tick
#
# 环境变量（WSL 里 store 在 Windows 盘上时需要显式给）：
#   COMPANION_HOME  store 目录，默认 $HOME/.companion
#   COMPANION_PY    python 解释器，默认 $HOME/.companion-venv/bin/python
: "${COMPANION_HOME:=$HOME/.companion}"
export COMPANION_HOME
PY="${COMPANION_PY:-$HOME/.companion-venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m companion "$@"
