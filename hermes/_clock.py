import json, shutil
from datetime import datetime
from pathlib import Path

# ── 1. 作息：保住她现有的活动时段，只加一个真的睡眠段 ──
r = Path('/mnt/c/Users/Anran/.companion/rhythm.json')
shutil.copy2(r, r.with_suffix(f'.json.bak-{datetime.now():%Y%m%d-%H%M%S}'))
d = json.loads(r.read_text(encoding='utf-8'))
d['windows'] = [{"start": "08:30", "end": "24:00", "wakes": 8}]
d['sleep'] = {"at": "01:00", "wake": "08:30"}
r.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('作息 ->', d['windows'], d['sleep'])

# ── 2. 事件门换成 companion ──
p = Path('/home/anran/.hermes/cron/jobs.json')
shutil.copy2(p, p.with_suffix(f'.json.bak-companion-clock-{datetime.now():%Y%m%d-%H%M%S}'))
raw = p.read_text(encoding='utf-8')
data = json.loads(raw)
items = data if isinstance(data, list) else data.get('jobs', [])
hit = 0
for j in items:
    if j.get('id') == 'ff94beca08f7':
        j['monitor_script'] = 'companion_wake.sh'
        pr = j.get('prompt') or ''
        # 她查自己状态的方式换成 companion（life.py 是旧的一份账）
        old_line = '想知道自己最近干过什么，可以先跑一句：`python3 ~/.hermes/scripts/life.py recent 3`。那是你自己的生活记录，看到什么就说什么。'
        new_line = '想知道自己现在的状态（几点、什么心情、今天说过几次话、最近在做什么），可以先跑一句：`~/.hermes/scripts/companion_ctx.sh`。那是你自己的记录，看到什么就说什么。'
        if old_line in pr:
            pr = pr.replace(old_line, new_line)
            j['prompt'] = pr
            print('提示词已更新')
        hit += 1
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print('改了', hit, '个 job 的 monitor_script')
