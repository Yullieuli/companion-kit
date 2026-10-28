"""cost：花费账本与预算闸。

为什么这是 core 而不是"运维细节"：这套东西最大的成本不是模型多聪明，是
**每轮重发一遍固定前缀**。所以花费必须在架构里被约束，而不是月底看账单。

三条约束：
  1. 每轮记账（输入/缓存命中/输出），落到 logs/cost.jsonl
  2. 每日硬预算（默认 ¥3），超了就不许起新会话（已经在跑的轮次不打断）
  3. 起会话之前先估一次价，超过剩余预算就不起 —— 宁可少说一句，不要月底炸

计价单位：元 / 百万 token。价格表可覆盖，默认取 deepseek-flash 的峰时价。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# 元 / 百万 token
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4.1-flash": {"input": 2.0, "cached": 0.04, "output": 8.0},
    "deepseek-v4.1": {"input": 4.0, "cached": 0.08, "output": 16.0},
    "agnes-3.0-flash": {"input": 1.0, "cached": 0.02, "output": 4.0},
}

DEFAULT_BUDGET = {"daily_cny": 3.0, "warn_at": 0.8}

# ── 分级模型 ────────────────────────────────────────────────
# 同一个"想"，花多少钱差三倍。所以不是所有活都配得上贵的模型：
# 判一次"该不该说话"、"这条视频几分"，用最便宜的就行；
# 深读长文、写日记、需要推理的，才升档。
TIERS: dict[str, dict[str, str]] = {
    "cheap": {"model": "agnes-3.0-flash", "why": "只判一次、要的是快不是文采"},
    "work": {"model": "deepseek-v4.1-flash", "why": "日常对话与工具调用，够用且便宜"},
    "deep": {"model": "deepseek-v4.1", "why": "深读/长文/需要真的想一遍"},
}

# 任务 → 档位。改这里比改调用点安全：调用点只写任务名。
TASK_TIER: dict[str, str] = {
    "chat": "work", "reply": "work", "tool_use": "work",
    "group_judge": "cheap", "bili_pick": "cheap", "bili_score": "cheap",
    "initiative": "cheap", "mood_judge": "cheap", "fence_scan": "cheap",
    "memory_extract": "cheap", "dedupe": "cheap",
    "bili_debrief": "deep", "deep_read": "deep", "plan": "deep", "code": "deep",
    "diary": "work", "summary": "work", "opinion_write": "work", "share": "work",
}

# 各档一次调用的规模（token）。用来在起会话前估个价，不是精确账单。
# cached 占大头是**现实**不是乐观：固定前缀在同一会话里命中缓存，
# 按全价算会把"说一句话"估成十倍，于是什么都不敢做。
TIER_TOKENS: dict[str, dict[str, int]] = {
    "cheap": {"fresh_tokens": 1500, "cached_tokens": 8000, "output_tokens": 120},
    "work": {"fresh_tokens": 3000, "cached_tokens": 24000, "output_tokens": 600},
    "deep": {"fresh_tokens": 8000, "cached_tokens": 60000, "output_tokens": 1500},
}

ROUTE_FILE = "cost/route.json"

# 一次"醒来"的典型规模：固定前缀 + 工具定义 + 注入上下文
TYPICAL_TURN = {"fresh_tokens": 20000, "cached_tokens": 0, "output_tokens": 600}


def route_table(home=None) -> dict[str, Any]:
    """档位与任务映射，可被 store 里的 cost/route.json 覆盖（她/他能自己调）。"""
    tiers = {k: dict(v) for k, v in TIERS.items()}
    tasks = dict(TASK_TIER)
    if home is not None:
        try:
            cfg = home._json(ROUTE_FILE, {})  # noqa: SLF001 — store 的唯一出口
        except Exception:
            cfg = {}
        for k, v in (cfg.get("tiers") or {}).items():
            if isinstance(v, dict) and k in tiers:
                tiers[k].update({kk: str(vv) for kk, vv in v.items() if kk in ("model", "why")})
        for k, v in (cfg.get("tasks") or {}).items():
            if str(v) in tiers:
                tasks[str(k)] = str(v)
    return {"tiers": tiers, "tasks": tasks}


def route(task: str, home=None, *, prices: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """这个任务该用哪个档的模型、大概花多少。宿主照着选模型就行。"""
    table = route_table(home)
    name = str(task or "chat")
    tier = table["tasks"].get(name) or "work"
    info = table["tiers"][tier]
    tok = TIER_TOKENS.get(tier, TIER_TOKENS["work"])
    est = cost_cny(tok["fresh_tokens"], tok["cached_tokens"], tok["output_tokens"],
                   info["model"], prices)
    return {"task": name, "tier": tier, "model": info["model"], "why": info["why"],
            "est_cny": round(est, 6), "tokens": tok,
            "price_per_mtok": price_of(info["model"], prices)}


def price_of(model: str, prices: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    table = {**DEFAULT_PRICES, **(prices or {})}
    return table.get(model) or table["deepseek-v4.1-flash"]


def cost_cny(
    fresh_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    model: str = "deepseek-v4.1-flash",
    prices: dict[str, dict[str, float]] | None = None,
) -> float:
    p = price_of(model, prices)
    return round(
        fresh_tokens * p["input"] / 1e6
        + cached_tokens * p["cached"] / 1e6
        + output_tokens * p["output"] / 1e6,
        6,
    )


@dataclass
class Spend:
    rows: list[dict[str, Any]]

    def today(self, now: datetime | None = None) -> list[dict[str, Any]]:
        day = (now or datetime.now()).date().isoformat()
        return [r for r in self.rows if str(r.get("day")) == day]

    def total(self, now: datetime | None = None) -> float:
        return round(sum(float(r.get("cost", 0)) for r in self.today(now)), 6)

    def turns(self, now: datetime | None = None) -> int:
        return len(self.today(now))


def load_rows(home) -> list[dict[str, Any]]:
    return home.read_jsonl("logs/cost.jsonl", limit=5000)


def record_turn(
    home,
    *,
    now: datetime | None = None,
    model: str = "deepseek-v4.1-flash",
    fresh_tokens: int = 0,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    session: str = "",
    note: str = "",
    prices: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    c = cost_cny(fresh_tokens, cached_tokens, output_tokens, model, prices)
    row = {
        "at": now.isoformat(timespec="seconds"),
        "day": now.date().isoformat(),
        "session": session,
        "model": model,
        "fresh": int(fresh_tokens),
        "cached": int(cached_tokens),
        "out": int(output_tokens),
        "cost": c,
        "note": note[:80],
    }
    home.append_jsonl("logs/cost.jsonl", row)
    return row


def today_total(home, now: datetime | None = None) -> float:
    return Spend(load_rows(home)).total(now)


def allow_turn(
    home,
    *,
    now: datetime | None = None,
    est: dict[str, int] | None = None,
    budget: dict[str, Any] | None = None,
    model: str = "deepseek-v4.1-flash",
    prices: dict[str, dict[str, float]] | None = None,
) -> tuple[bool, str, float]:
    """起会话之前问一次。返回 (能否, 理由, 预计花费)。"""
    now = now or datetime.now()
    b = {**DEFAULT_BUDGET, **(budget or {})}
    est = est or TYPICAL_TURN
    projected = cost_cny(
        int(est.get("fresh_tokens", TYPICAL_TURN["fresh_tokens"])),
        0,  # 冷启动按全价估，命中缓存只会更便宜，不会更贵
        int(est.get("output_tokens", TYPICAL_TURN["output_tokens"])),
        model,
        prices,
    )
    spent = today_total(home, now)
    cap = float(b["daily_cny"])
    if spent + projected > cap:
        return False, f"今天已经花了 ¥{spent:.3f}，再起一轮要 ¥{projected:.3f}，超过日预算 ¥{cap:.2f}", projected
    if spent >= cap * float(b["warn_at"]):
        return True, f"注意：今天已花 ¥{spent:.3f}（预算 ¥{cap:.2f} 的 {spent / cap:.0%}）", projected
    return True, "", projected


def summary(home, now: datetime | None = None, budget: dict[str, Any] | None = None) -> str:
    now = now or datetime.now()
    b = {**DEFAULT_BUDGET, **(budget or {})}
    rows = Spend(load_rows(home))
    total = rows.total(now)
    return f"今天 ¥{total:.3f} / ¥{float(b['daily_cny']):.2f}，{rows.turns(now)} 轮"


def cheapest_first(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同样收益时，先选便宜的那个。给自主决策用。"""
    return sorted(candidates, key=lambda c: (float(c.get("score", 0)) / max(1e-6, float(c.get("cost", 0)))), reverse=True)
