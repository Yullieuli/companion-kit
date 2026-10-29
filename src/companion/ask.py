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
    """主人答复。只认 pending 的，过期的不给补票。

    `by` 是**答复的来源**，决定了这张票能不能开设备：
      · "owner"  —— 主人在本机/聊天里亲自答复的，有凭据，能开设备
      · "model"  —— 模型自己调来的（走 MCP）。这种答复**不能**开设备，
                     因为那等于"模型自己替主人答应自己"。
    """
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
            r["can_act"] = (by != "model") and bool(allow)
            if allow and by == "model":
                # 模型自己点的头：留痕，但不发通行证
                r["why_not"] = "这是模型自己批准的，不能当作主人的同意"
            _save(home, rows)
            return {"ok": True, "ask": r, "can_act": r.get("can_act")}
    return {"ok": False, "why": "没有这条请求"}


def token(home: Home, action: str, args: dict[str, Any] | None = None, *,
          target: str = "", now: datetime | None = None) -> str:
    """有没有针对这个动作的有效许可。返回 ask id，没有就空串。

    只认**主人在场**的许可（can_act）。模型自己批的那条不算——
    否则模型可以调 allow 拿到票，自己开程序，"先问一声"就成了摆设。
    """
    now = now or datetime.now()
    sig = _sig(action, args or {}, target)
    rows = _load(home)
    if _sweep(rows, now):
        _save(home, rows)
    for r in reversed(rows):
        if (r.get("sig") == sig and r.get("status") == "allowed"
                and r.get("can_act") and _alive(r, now)):
            return str(r.get("id"))
    return ""


def verify(home: Home, ask_id: str, action: str, args: dict[str, Any] | None = None, *,
           target: str = "") -> bool:
    """主人给的那张许可，是不是就是这件事。

    没有这一步，一句"可以"就能被拿去干别的——那这个流程只是装饰。

    同样只认主人在场的许可：模型自批的那张不能用来开设备。
    """
    sig = _sig(action, args or {}, target)
    for r in _load(home):
        if str(r.get("id")) == str(ask_id):
            return (r.get("sig") == sig and r.get("status") == "allowed"
                    and bool(r.get("can_act")))
    return False


# ── 主人的答复（自动回填）───────────────────────────────
#
# 为什么需要它：主人在聊天里说"可以"，本该立刻生效，
# 但旧流程要人去敲 `companion ask allow <id>`——一道没人会记得的手工步骤。
#
# 为什么不能只靠"模型调了 allow"：实测发现模型可以自己批准自己的请示单，
# 然后拿 ask_id 通过设备闸——这等于"模型自己替主人答应自己"，
# 恰好是这套流程存在的理由的反面。
#
# 所以这里只认**主人自己的话**：
#   · 文本要像人话（"可以" / "行" / "别" / "算了"），不是任意字符串
#   · 必须能对上唯一一条待办（对不上就装没看见，不猜）
#   · 必须来自主人会话，不是群里别人说的
#   · 拒绝优先于同意：宁可漏放，不可错放
ALLOW_WORDS = ("可以", "行吧", "好吧", "好啊", "去吧", "动手吧", "同意", "答应", "随你", "你决定", "ok", "yes")
DENY_WORDS = ("不行", "不要", "别", "算了", "不用", "停", "别动", "拒绝", "不同意", "no", "cancel")


def _norm_text(t: str) -> str:
    """把一句话压成"是不是同意/拒绝"的判据：去掉标点空白，只留小写。"""
    out = []
    for ch in str(t or ""):
        if ch.isalnum() or "一" <= ch <= "鿿":
            out.append(ch.lower())
    return "".join(out)


def read_intent(text: str) -> str:
    """读一句主人话的意思：'allow' / 'deny' / ''（读不懂就不猜）。"""
    t = _norm_text(text)
    if not t or len(t) > 40:
        return ""
    # 拒绝优先：一句话里同时出现"可以"和"不要"时，主人多半在否定
    for w in DENY_WORDS:
        if w in t:
            return "deny"
    for w in ALLOW_WORDS:
        if w in t:
            return "allow"
    return ""


def decide_by_reply(home: Home, text: str, *, now: datetime | None = None,
                    by: str = "owner") -> dict[str, Any]:
    """主人在对话里回话了 —— 自动把它落到对应的待办上。

    返回 {ok, ask, intent, why}。ok=False 时 why 说明为什么没落上。

    保守到有点死板是故意的：
      · 读不懂就不动（不是所有"行"都是在答应这件事）
      · 有多条待办时不动（不知道他在答哪条）
      · 拒绝优先（宁可错杀）
    """
    now = now or datetime.now()
    intent = read_intent(text)
    if not intent:
        return {"ok": False, "intent": "", "why": "这句话读不出是同意还是拒绝"}
    rows = _load(home)
    _sweep(rows, now)
    waiting = [r for r in rows if r.get("status") == "pending"]
    if not waiting:
        return {"ok": False, "intent": intent, "why": "没有待答复的请示"}
    if len(waiting) > 1:
        return {"ok": False, "intent": intent, "why": f"有 {len(waiting)} 条请示待答，说不清是哪一条"}
    row = waiting[0]
    out = decide(home, str(row.get("id")), intent == "allow", now=now, by=by)
    out["intent"] = intent
    out["reply"] = str(text)[:60]
    return out


def spoken(home: Home, ask_id: str) -> bool:
    """这条请示她真的说出口了吗？

    没有这一条，"模型自己批准自己"就成立：模型调 ask allow 拿到票，
    直接开程序，中间从没人在任何地方被问过。
    所以批准前要能证明：**她先把这句话说了出去**。
    """
    rows = _load(home)
    row = None
    for r in rows:
        if str(r.get("id")) == str(ask_id):
            row = r
            break
    if not row:
        return False
    if row.get("spoken"):
        return True
    say = str(row.get("say") or "")
    if not say:
        return False
    # 她说过的话有两个地方：said.jsonl（走 say 出口）和 turns.jsonl（宿主钩子记的）
    for log in ("logs/said.jsonl", "logs/turns.jsonl"):
        for r in home.read_jsonl(log, limit=300):
            blob = str(r.get("text") or r.get("said") or "")
            if blob and (say in blob or blob in say):
                return True
    return False


def mark_spoken(home: Home, ask_id: str) -> bool:
    """标记"这条请示已经说给她主人听了"。"""
    rows = _load(home)
    changed = False
    for r in rows:
        if str(r.get("id")) == str(ask_id) and not r.get("spoken"):
            r["spoken"] = True
            changed = True
    if changed:
        _save(home, rows)
    return changed


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
