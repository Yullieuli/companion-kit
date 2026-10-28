"""ask：有些事她得先问一声。

为什么单独一层：`initiative` 里把能力分成 auto / ask / never 三档，
auto 的闸在各自模块里（额度、作息、白名单），never 的是硬拒。
只有 **ask** 需要一处"她说过、他答应过"的凭据——否则模型自己就能替主人答应自己。

凭据是**一次性、有期限、绑定具体动作**的：
  · 一次性：用过就废，防重放
  · 有期限：30 分钟没回就当没问过，别拿早上的许可开晚上的应用
  · 绑定动作：允许"开浏览器"不等于允许"开浏览器并输入密码"

这不是安全边界（安全靠白名单和危险词），是**礼貌边界**：
她可以自己动手，但会先说一句。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .store import Home

FILE = "logs/asks.json"
TTL_MIN = 30
KEY_FIELDS = ("x", "y", "x2", "y2", "text", "key", "window", "what", "url", "package")

VERB = {
    "open": "打开", "launch": "打开", "focus": "切到", "tap": "点一下",
    "click": "点一下", "swipe": "滑一下", "type": "打字", "key": "按键",
}


def _sig(action: str, args: dict[str, Any], target: str) -> str:
    """动作指纹：只认真正会改变世界的字段，忽略附加参数。"""
    body = {k: str(args.get(k)) for k in KEY_FIELDS if args.get(k) not in (None, "")}
    raw = json.dumps({"target": target, "action": action.lower(), "args": body},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _load(home: Home) -> list[dict[str, Any]]:
    try:
        d = json.loads((home.root / FILE).read_text(encoding="utf-8") or "[]")
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def _save(home: Home, rows: list[dict[str, Any]]) -> None:
    p = home.root / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _said(action: str, args: dict[str, Any], target: str) -> str:
    """一句能直接说出口的话。她可以改写语气，但意思不能变。"""
    verb = VERB.get(action.lower(), "动一下")
    what = (args.get("what") or args.get("window") or args.get("url")
            or args.get("text") or args.get("package") or "")
    where = {"phone": "手机", "desktop": "电脑", "browser": "浏览器", "game": "游戏"}.get(target, "")
    body = f"{where}上{verb}{what}" if where else f"{verb}{what}"
    return f"我想{body}，可以吗？"


def request(home: Home, action: str, args: dict[str, Any] | None = None, *,
            target: str = "", why: str = "", ttl_min: int = TTL_MIN,
            now: datetime | None = None) -> dict[str, Any]:
    """记一条请求。同一个动作还没答复时不重复问——她不会追着问同一件事。"""
    now = now or datetime.now()
    args = args or {}
    sig = _sig(action, args, target)
    rows = _load(home)
    for r in reversed(rows):
        if r.get("sig") == sig and r.get("status") == "pending" and _alive(r, now):
            return r
    row = {
        "id": f"{int(now.timestamp())}-{sig[:6]}",
        "at": now.isoformat(timespec="seconds"),
        "expires": (now + timedelta(minutes=max(1, ttl_min))).isoformat(timespec="seconds"),
        "target": target,
        "action": action.lower(),
        "args": {k: v for k, v in args.items() if v not in (None, "")},
        "why": (why or "")[:80],
        "sig": sig,
        "status": "pending",
        "say": _said(action, args, target),
    }
    rows.append(row)
    _save(home, rows)
    return row


def _alive(row: dict[str, Any], now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(row.get("expires"))) > now
    except Exception:
        return False


def _sweep(rows: list[dict[str, Any]], now: datetime) -> bool:
    """过期的标成 expired：没答复的、答应了但没用掉的都算。

    答应了却没用，本身也是一个信息：她当时想做，后来没做。
    留着 "allowed" 会让 `ask list` 看起来永远有一件悬着的事。
    """
    changed = False
    for r in rows:
        if r.get("status") in ("pending", "allowed") and not _alive(r, now):
            r["status"] = "expired"
            changed = True
    return changed


def pending(home: Home, *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    rows = _load(home)
    if _sweep(rows, now):
        _save(home, rows)
    return [r for r in rows if r.get("status") == "pending"]


def list_all(home: Home, *, limit: int = 20, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    rows = _load(home)
    if _sweep(rows, now):
        _save(home, rows)
    return rows[-limit:]


def decide(home: Home, ask_id: str, allow: bool, *, now: datetime | None = None,
           by: str = "owner") -> dict[str, Any]:
    """主人答复。只认 pending 的，过期的不给补票。"""
    now = now or datetime.now()
    rows = _load(home)
    _sweep(rows, now)
    for r in rows:
        if str(r.get("id")) == str(ask_id):
            if r.get("status") != "pending":
                return {"ok": False, "why": f"这条已经是 {r.get('status')}，不能改", "ask": r}
            r["status"] = "allowed" if allow else "denied"
            r["decided_at"] = now.isoformat(timespec="seconds")
            r["decided_by"] = by
            _save(home, rows)
            return {"ok": True, "ask": r}
    return {"ok": False, "why": "没有这条请求"}


def token(home: Home, action: str, args: dict[str, Any] | None = None, *,
          target: str = "", now: datetime | None = None) -> str:
    """有没有针对这个动作的有效许可。返回 ask id，没有就空串。"""
    now = now or datetime.now()
    sig = _sig(action, args or {}, target)
    rows = _load(home)
    if _sweep(rows, now):
        _save(home, rows)
    for r in reversed(rows):
        if r.get("sig") == sig and r.get("status") == "allowed" and _alive(r, now):
            return str(r.get("id"))
    return ""


def verify(home: Home, ask_id: str, action: str, args: dict[str, Any] | None = None, *,
           target: str = "") -> bool:
    """主人给的那张许可，是不是就是这件事。

    没有这一步，一句"可以"就能被拿去干别的——那这个流程只是装饰。
    """
    sig = _sig(action, args or {}, target)
    for r in _load(home):
        if str(r.get("id")) == str(ask_id):
            return r.get("sig") == sig and r.get("status") == "allowed"
    return False


def consume(home: Home, ask_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """用掉一张许可。一次性，用完立刻作废——防止同一句"可以"被反复使用。"""
    now = now or datetime.now()
    rows = _load(home)
    for r in rows:
        if str(r.get("id")) == str(ask_id):
            if r.get("status") != "allowed":
                return {"ok": False, "why": f"这条不是 allowed（{r.get('status')}）"}
            r["status"] = "used"
            r["used_at"] = now.isoformat(timespec="seconds")
            _save(home, rows)
            return {"ok": True, "ask": r}
    return {"ok": False, "why": "没有这条许可"}


def required(cfg: dict[str, Any], target: str, action: str) -> bool:
    """这个动作要不要先问。配置在 devices.json 的 ask 段里。"""
    a = (cfg or {}).get("ask") or {}
    if not a.get("enabled", True):
        return False
    if str(target or "") in set(a.get("targets") or []):
        return True
    return str(action or "").lower() in set(a.get("actions") or [])
