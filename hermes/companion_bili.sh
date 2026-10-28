#!/bin/sh
# 她自己的 B 站入口。参数原样透传给 `companion bili`：
#   companion_bili.sh status
#   companion_bili.sh tick
#   companion_bili.sh commit BV1xx411c7mD --summary "关卡设计讲透了" --rating 8
#   companion_bili.sh share BV1xx411c7mD --score 15 --reason "他肯定喜欢这个战斗系统"
#
# 两个环境变量（装在 WSL 里、store 在 Windows 盘上时需要显式给）：
#   COMPANION_HOME  store 目录，默认 $HOME/.companion
#   COMPANION_PY    python 解释器，默认 $HOME/.companion-venv/bin/python
: "${COMPANION_HOME:=$HOME/.companion}"
export COMPANION_HOME
PY="${COMPANION_PY:-$HOME/.companion-venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m companion bili "$@"
