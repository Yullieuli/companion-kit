#!/bin/bash
S=$HOME/.hermes/scripts
echo "=== 真跑一次事件门 ==="
OUT=$($S/companion_wake.sh 2>&1)
echo "输出: [${OUT}]"
echo "=== 最近决策 ==="
tail -2 /mnt/c/Users/Anran/.companion/logs/initiatives.jsonl
echo "=== 额度 ==="
export COMPANION_HOME=/mnt/c/Users/Anran/.companion
$HOME/.companion-venv/bin/python -m companion --json status 2>/dev/null | head -12
