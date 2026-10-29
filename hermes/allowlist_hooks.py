#!/usr/bin/env python3
"""hermes 的 shell 钩子批准表（allowlist）—— 薄封装，逻辑在 core。

为什么需要这个：
hermes 对 shell 钩子有 consent 机制 —— config.yaml 里写了 `hooks:` 还不算，
每个 (事件, 命令) 对还必须在 `shell-hooks-allowlist.json` 里被批准过。
**没批准的话，hermes 会静默跳过这个钩子**：不报错、不进日志、钩子就是不跑。

2026-10-29 实测踩过：按文档装完 companion_hook.sh + companion_post.sh，
config.yaml 里明明有，钩子却一个都不触发。查了两个小时才发现是 allowlist 没批准。
所以安装器必须顺手把钩子加进来，而且装完要自检。

用法：
    python allowlist_hooks.py add   --home ~/.hermes --event post_llm_call --command /path/x.sh
    python allowlist_hooks.py check --home ~/.hermes
    python allowlist_hooks.py list  --home ~/.hermes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from companion import hosthooks
except ImportError:  # 没装 companion 也能用：退回本地实现
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    try:
        from companion import hosthooks  # type: ignore
    except ImportError:
        print("找不到 companion 包 —— 先装 companion（pip install companion-kit），"
              "或用 --home 指向已装好的环境", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hermes shell 钩子批准表管理")
    ap.add_argument("action", choices=["add", "check", "list"])
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    ap.add_argument("--event", default="")
    ap.add_argument("--command", default="")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser()
    if args.action == "add":
        if not args.event or not args.command:
            ap.error("add 需要 --event 与 --command")
        print(json.dumps(hosthooks.approve(home, args.event, args.command), ensure_ascii=False))
        return 0

    if args.action == "check":
        r = hosthooks.check(home)
        if r["ok"]:
            print(f"✓ {r['total']} 个钩子全部已批准")
        else:
            print(f"✗ {len(r['missing'])}/{r['total']} 个钩子没批准 —— 它们不会执行：")
            for e, c in r["missing"]:
                print(f"    {e} -> {c}")
            print(f"    批准表：{r['allowlist']}")
        return 0 if r["ok"] else 1

    for e, c in sorted(hosthooks.approved_pairs(hosthooks.load(hosthooks.allowlist_path(home)))):
        print(f"{e} -> {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
