#!/bin/bash
set -e
PY=$HOME/.companion-venv/bin/python
S=$HOME/.hermes/scripts
$PY -m pip install -q --no-compile --no-cache-dir --force-reinstall --no-deps /mnt/c/Users/Anran/AppData/Local/Temp/companion_kit-0.2.4-py3-none-any.whl
$PY -c "import companion; print('WSL', companion.__version__)"

# 脚本就位
cp /mnt/c/Users/Anran/Documents/workbuddy/companion-kit/hermes/companion_wake.py $S/companion_wake.py
cp /mnt/c/Users/Anran/Documents/workbuddy/companion-kit/hermes/companion_hook.py $S/companion_hook.py
chmod +x $S/companion_wake.py $S/companion_hook.py

cat > $S/companion_ctx.sh <<'W'
#!/bin/sh
# 查自己现在的状态：心情/额度/作息/观点/生活（不含人格，人格在她自己的系统提示里）
export COMPANION_HOME=/mnt/c/Users/Anran/.companion
exec /home/anran/.companion-venv/bin/python -m companion context --budget 700 --no-soul
W
cat > $S/companion_wake.sh <<'W'
#!/bin/sh
# 事件门：该说话就打印 WAKE|理由|动作，否则什么都不打印
export COMPANION_HOME=/mnt/c/Users/Anran/.companion
exec /home/anran/.companion-venv/bin/python /home/anran/.hermes/scripts/companion_wake.py
W
chmod +x $S/companion_ctx.sh $S/companion_wake.sh

echo "=== ctx（应无人格模板）==="
$S/companion_ctx.sh | head -8
echo "=== wake --dry-run ==="
$S/companion_wake.sh --dry-run 2>/dev/null || true
echo "[dry-run 结束]"
