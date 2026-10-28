#!/bin/bash
set -e
PY=$HOME/.companion-venv/bin/python
S=$HOME/.hermes/scripts
$PY -m pip install -q --no-compile --no-cache-dir --force-reinstall --no-deps /mnt/c/Users/Anran/AppData/Local/Temp/companion_kit-0.2.5-py3-none-any.whl
$PY -c "import companion; print('WSL', companion.__version__)"
cp /mnt/c/Users/Anran/Documents/workbuddy/companion-kit/hermes/companion_wake.py $S/companion_wake.py
cat > $S/companion_wake.sh <<'W'
#!/bin/sh
# 事件门：该说话就打印 WAKE|理由|动作，否则什么都不打印
export COMPANION_HOME=/mnt/c/Users/Anran/.companion
export COMPANION_HOST=hermes
exec /home/anran/.companion-venv/bin/python /home/anran/.hermes/scripts/companion_wake.py
W
chmod +x $S/companion_wake.sh
echo "=== hermes 抢占时钟 ==="
$S/companion_wake.sh
echo "[输出结束]"
echo "=== 主人文件 ==="
cat /mnt/c/Users/Anran/.companion/logs/tick_owner.json
