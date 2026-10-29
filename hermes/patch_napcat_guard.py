"""把 companion 的发送前闸插进 hermes 的 napcat 插件。

为什么改这里：hermes 的钩子协议里 `pre_llm_call` 只能注入上下文，
`pre_verify` 只能要求"继续"，**拦不住已经生成的最终回复**。
渠道的发送函数是唯一真正的拦截点 —— napcat 的 `send()` 是所有出站文本的
单一漏斗（它自己就在那里过滤内部失败通知），所以闸插在这里最合适。

三条纪律：
  · **幂等**：打过一次就认得出来，重复运行不会再插一遍
  · **可回滚**：先备份，--revert 恢复
  · **先预览**：--dry-run 只说要改什么，不落盘

用法：

    python patch_napcat_guard.py --dry-run          # 看要改什么
    python patch_napcat_guard.py                    # 真改（自动备份）
    python patch_napcat_guard.py --revert           # 撤销
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

MARKER = "# companion-send-gate"
ANCHOR = "async def send(self, chat_id: str, content: str = \"\", **kwargs) -> SendResult:"

DEFAULT_NAPCAT = "~/.hermes/plugins/napcat/main.py"
DEFAULT_GATE = "~/.hermes/scripts/companion_gate.py"

BLOCK = '''
        {marker} ── companion 发送前闸 ────────────────────────────
        # 身份锁（她不能自己改名）/ 同话题 10 分钟不追问，在这里被真正拦下。
        # 幂等标记：companion-send-gate（patch_napcat_guard.py 认这个）
        try:
            import json as _cjson, os as _cos, subprocess as _csub
            _gate = {gate!r}
            if _cos.path.exists(_gate):
                _res = _csub.run([{python!r}, _gate],
                                 input=_cjson.dumps({{"text": content or "", "mode": "reply"}},
                                                    ensure_ascii=False),
                                 capture_output=True, text=True, timeout=5)
                _out = _cjson.loads(_res.stdout or "{{}}")
                if not _out.get("ok", True):
                    logger.warning("companion 闸拦下这条消息：%s", _out.get("why"))
                    return SendResult(success=False,
                                      error="companion 闸：%s" % (_out.get("why") or _out.get("blocked")))
        except Exception as _ce:
            logger.debug("companion 闸异常，放行：%s", _ce)
'''


def find_anchor(text: str) -> int:
    """返回锚点所在行号（1 起）；找不到返回 0。"""
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip() == ANCHOR.strip():
            return i
    return 0


def build_block(gate: str, python: str) -> str:
    return BLOCK.format(marker=MARKER, gate=gate, python=python)


def apply_patch(target: Path, gate: str, python: str) -> tuple[bool, str]:
    """返回 (是否改动了, 说明)。幂等。"""
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"读不了 {target}：{e}"
    if MARKER in text:
        return False, f"已经打过补丁（{MARKER}），没动"
    line = find_anchor(text)
    if not line:
        return False, (f"{target} 里找不到发送函数（{ANCHOR}）。"
                       "napcat 版本可能变了，先确认它还在用 send() 这条漏斗")
    lines = text.splitlines(keepends=True)
    block = build_block(gate, python)
    new_lines = lines[:line] + [block] + lines[line:]
    backup = target.with_name(target.name + f".bak-gate-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    target.write_text("".join(new_lines), encoding="utf-8")
    return True, f"已插入（第 {line + 1} 行起），备份：{backup}"


def revert(target: Path) -> tuple[bool, str]:
    backups = sorted(target.parent.glob(target.name + ".bak-gate-*"))
    if not backups:
        return False, "没有找到 .bak-gate-* 备份，无法回滚"
    latest = backups[-1]
    shutil.copy2(latest, target)
    return True, f"已回滚到 {latest.name}"


def main() -> int:
    ap = argparse.ArgumentParser(description="把 companion 发送前闸插进 napcat 插件")
    ap.add_argument("--napcat", default=DEFAULT_NAPCAT, help="napcat/main.py 路径")
    ap.add_argument("--gate", default=DEFAULT_GATE, help="companion_gate.py 路径")
    ap.add_argument("--python", default="", help="跑闸脚本的解释器（默认 python3）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    ap.add_argument("--revert", action="store_true", help="撤销补丁")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    target = Path(os.path.expanduser(args.napcat))
    gate = str(Path(os.path.expanduser(args.gate)).resolve())
    python = args.python or os.environ.get("COMPANION_PYTHON") or "python3"

    if not target.exists():
        out = {"ok": False, "why": f"找不到 {target}（先装 hermes 的 napcat 插件）"}
        print(json.dumps(out, ensure_ascii=False) if args.json else f"✗ {out['why']}")
        return 1

    if args.revert:
        ok, why = revert(target)
        out = {"ok": ok, "why": why}
        print(json.dumps(out, ensure_ascii=False) if args.json else ("✓ " if ok else "✗ ") + why)
        return 0 if ok else 1

    if args.dry_run:
        text = target.read_text(encoding="utf-8")
        if MARKER in text:
            out = {"ok": True, "why": "已经打过补丁，不需要改", "changed": False}
        elif not find_anchor(text):
            out = {"ok": False, "why": f"找不到发送函数锚点：{ANCHOR}", "changed": False}
        else:
            out = {"ok": True, "why": f"会在第 {find_anchor(text) + 1} 行后插入发送前闸",
                   "changed": False, "gate": gate, "python": python}
        print(json.dumps(out, ensure_ascii=False) if args.json else ("· " + out["why"]))
        return 0 if out["ok"] else 1

    changed, why = apply_patch(target, gate, python)
    out = {"ok": True, "changed": changed, "why": why, "gate": gate, "python": python}
    print(json.dumps(out, ensure_ascii=False) if args.json else ("✓ " if why else "· " + why))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
