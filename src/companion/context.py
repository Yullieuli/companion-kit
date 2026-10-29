"""context：把 store 里的东西拼成一段能直接塞进 system prompt 的文本。

为什么单独一层：三种宿主注入方式不同（hermes 是 hook、dsh 是 systemPrompt.context、
codex 是 MCP 工具），但它们要的**内容必须逐字节一致**，否则"同一个人换个宿主就变性格"。

三条规则：
  · 顺序固定：人格 → 状态（心情/时间/额度）→ 记忆 → 人物卡 → 生活事件
  · 有预算：超了就从最低优先级的尾部截，绝不无声丢整块
  · 每块带 hash：宿主可以只注入变化的部分（省 token 的关键）
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import clock, mood, quota
from .store import Home

DEFAULT_BUDGET = 2000
SOUL_CAP = 600
ELLIPSIS = "…"

# 增量投影：每 N 轮强制全量一次
FULL_EVERY = 6
# 每个会话记住的块指纹存在 store 里（跨进程、跨重启都能续上）
PROJECTION_FILE = "context/projection.json"
# 最多记多少个会话的指纹，别让它无限长
PROJECTION_MAX_SESSIONS = 40


@dataclass
class Block:
    name: str
    text: str
    priority: int = 50
    # 常驻：每轮都得重新注入（人格 / 记忆 / 生活）。
    # 为什么：投影能省 token，但**模型不会跨轮记住注入内容** ——
    # 上一轮给了"她今天看了三条显卡评测"，这一轮不给，她就真不知道了。
    # 2026-10-29 实测：第二轮投影判定"life 没变"→ 注入空 → 她完全不提自己的事。
    # 只有 state（心情/额度/时间）这种真·变量才适合走增量。
    sticky: bool = False

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Context:
    text: str
    blocks: list[Block] = field(default_factory=list)
    # 回放要用：记下这一份是用哪个时刻拼的，否则重建时「【现在】」那一行对不上
    now: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "blocks": [{"name": b.name, "chars": len(b.text), "hash": b.hash} for b in self.blocks],
            "total_chars": len(self.text),
            "now": self.now.isoformat(timespec="seconds") if self.now else "",
        }


def _head(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - len(ELLIPSIS))] + ELLIPSIS


def _state_block(
    home: Home,
    now: datetime,
    last_chat: float | None,
    owner_last_seen: float | None,
    quota_cfg: dict[str, Any] | None,
) -> str:
    rhythm = home.rhythm()
    lines = [f"【现在】{clock.describe(now, last_chat)}"]
    if clock.in_sleep(rhythm, now):
        wake = clock.sleep_until(rhythm, now)
        lines.append(f"【状态】你在睡觉（{wake:%H:%M} 醒）。除非主人直接叫你，否则别主动说话。")
    elif clock.in_window(rhythm, now):
        lines.append("【状态】在作息窗口里。")
    else:
        lines.append("【状态】不在作息窗口，安静一点。")
    m = mood.decay(home.mood(), now)
    lines.append(f"【心情】{mood.summary(m)}")
    q = home.quota() or {}
    lines.append(f"【开口额度】{quota.summary(q, quota_cfg, now, owner_last_seen)}")
    kind = clock.greeting_kind(rhythm, now)
    if kind == "morning":
        lines.append("【问候】这是今天的第一次，可以先说句早。")
    elif kind == "night":
        lines.append("【问候】夜深了，说话轻一点。")
    return "\n".join(lines)


def build(
    home: Home,
    *,
    now: datetime | None = None,
    budget: int = DEFAULT_BUDGET,
    last_chat: float | None = None,
    owner_last_seen: float | None = None,
    quota_cfg: dict[str, Any] | None = None,
    include_soul: bool = True,
    memory_text: str = "",
    opinion_text: str = "",
    person_text: str = "",
    life_text: str = "",
) -> Context:
    now = now or datetime.now()
    home.init()

    # 灰度总开关（PLAN 2.6）：关掉就等于没有 companion —— 不注入、不决策。
    # 迁移期两套并存时，这一下能立刻退回旧版本行为，不用卸载、不用改宿主。
    if not bool((home.rhythm() or {}).get("enabled", True)):
        return Context(text="", blocks=[], now=now)

    blocks: list[Block] = []
    # 宿主自己已经有人格时（hermes 把它当系统提示），别再注入一份：
    # 重复会导致两套语气打架，而且要挤占预算
    soul = _head(home.soul(), min(SOUL_CAP, max(0, budget // 3))) if include_soul else ""
    if soul:
        blocks.append(Block("soul", soul, 10, sticky=True))
    state = _state_block(home, now, last_chat, owner_last_seen, quota_cfg)
    # state 是唯一适合走增量的：心情/额度/时间本来就在变。
    # 但它"没变"的时候她仍需要知道现在几点、还能不能开口 ——
    # 所以给一个极短的精简版（_state_brief），而不是完全不给。
    blocks.append(Block("state", state, 20))
    if memory_text.strip():
        blocks.append(Block("memory", memory_text.strip(), 40, sticky=True))
    if opinion_text.strip():
        blocks.append(Block("opinion", opinion_text.strip(), 50, sticky=True))
    if person_text.strip():
        blocks.append(Block("person", person_text.strip(), 60, sticky=True))
    if life_text.strip():
        blocks.append(Block("life", life_text.strip(), 80, sticky=True))

    # 按优先级依次放行，剩余预算不够就截断这一块，后面的直接放空
    used = 0
    kept: list[Block] = []
    for b in sorted(blocks, key=lambda x: x.priority):
        sep = 2 if kept else 0
        room = budget - used - sep
        if room <= 20:
            break
        text = _head(b.text, room)
        if not text:
            break
        # 预算截断时**必须把 sticky 带过去** —— 否则被截过的那一块
        # 就退化成"增量"，下一轮不注入了（2026-10-29 实测踩过：
        # soul 明明标了 sticky=True，运行时却是 False，就是这里丢的）。
        kept.append(Block(b.name, text, b.priority, sticky=b.sticky))
        used += len(text) + sep
    # 输出仍按语义顺序，不按预算顺序
    order = {"soul": 0, "state": 1, "memory": 2, "opinion": 3, "person": 4, "life": 5}
    kept.sort(key=lambda x: order.get(x.name, 99))
    parts = [b.text for b in kept]
    return Context(text="\n\n".join(parts), blocks=kept, now=now)


def compose(
    home: Home,
    *,
    now: datetime | None = None,
    budget: int = DEFAULT_BUDGET,
    query: str = "",
    include_soul: bool = True,
) -> Context:
    """按默认材料来源拼一份上下文。CLI / MCP / HTTP / 钩子都走这里。

    为什么要有这一个函数：四处各写一遍六个 render 参数，只要有一处漏了，
    "同一份 store 拼出不同文本"就会发生，而且极难发现。契约测试只盯得住
    CLI 和 MCP——让其余入口物理上复用同一段代码，才不会有第五种拼法。
    """
    from . import life, memory, opinion, people   # 局部导入：这几个模块也依赖 store，避免环

    # life 给**全部**（含还没跟他讲过的），只按 shared 标注去重：
    #   之前一度只给 shared=True，结果她根本不知道自己刷过显卡评测——
    #   把"防重复讲"错做成了"不许知道"。
    # opinion 只给想定的：hunch 是"还没定型"的猜测，一次注入四条，
    #   模型会当成"这就是我的立场"说出去。
    # 2026-10-29 那轮真实对话：主人问"我做这个有啥用"，她答"我今天刷了六个显卡评测"。
    # 不是她记错，是上下文里**只有**六条流水、没有一条她自己的判断，
    # 她只能拿流水顶。所以 life.render 现在会把相关的立场一并带上。
    return build(
        home,
        now=now,
        budget=budget,
        include_soul=include_soul,
        memory_text=memory.render(home, query),
        opinion_text=opinion.render(home, query, limit=2, settled_only=True, recent_days=3),
        person_text=people.render(home, people.OWNER),
        life_text=life.render(home),
    )


@dataclass
class Cache:
    """按会话记住上次注入了什么，只返回变化的块名。"""

    seen: dict[str, dict[str, str]] = field(default_factory=dict)

    def changed(self, session: str, blocks: list[Block]) -> list[str]:
        prev = self.seen.get(session) or {}
        now = {b.name: b.hash for b in blocks}
        diff = [name for name, h in now.items() if prev.get(name) != h]
        gone = [name for name in prev if name not in now]
        self.seen[session] = now
        return diff + gone


# ── 增量投影（省 token 的关键）────────────────────────────────
#
# 为什么要做：每轮把同一段前缀重发一遍是这套东西最大的开销。
# 宿主只需要注入"变化的那几块"，状态没变就一个字都不发。
#
# 为什么放在 core 而不是各宿主各写一份：hermes 已经有一版（钩子里的 _render_delta），
# dsh 与 MCP 各写一份就会出现三种口径。同一份 store 必须只有一种投影逻辑。
#
# 两条不能省的规则：
#  1. 指纹要抹掉「【现在】周三 17:39」里的时间。不抹的话状态块每分钟都算"变了"，
#     去重等于没做。
#  2. 每 N 轮强制全量一次。会话历史会被压缩，压缩后她可能已经忘了自己是谁；
#     周期性全量是兜底，不是可选项。


def _fingerprint(text: str) -> str:
    """比对用的指纹：把「【现在】…」整行换成占位。"""
    return re.sub(r"(?m)^【现在】.*$", "【现在】", text or "")


def _load_projection(home: Home) -> dict[str, Any]:
    try:
        d = json.loads((home.root / PROJECTION_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_projection(home: Home, data: dict[str, Any]) -> None:
    p = home.root / PROJECTION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{id(data)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class Projection:
    """一次投影的结果。text 是**该注入的那部分**，不是全量。"""

    text: str
    full: bool
    changed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    rounds: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "full": self.full,
            "changed": self.changed,
            "dropped": self.dropped,
            "rounds": self.rounds,
            "reason": self.reason,
        }


def project(
    home: Home,
    session: str,
    ctx: Context,
    *,
    full_every: int = FULL_EVERY,
    force: bool = False,
    now: datetime | None = None,
) -> Projection:
    """按会话做增量投影。

    · 第一次、或每 full_every 轮 → 全量
    · **常驻块（sticky）每轮都注入** —— 人格、记忆、生活不能靠"上次说过"
    · 只有非常驻块（state）没变时才省：这一轮什么都不用注入

    2026-10-29 的教训：投影原本对所有块一视同仁"没变就不给"。
    但**模型不会跨轮记住注入内容** —— 上一轮给了她"今天看了三条显卡评测"，
    这一轮不给，她就真不知道了。实测第二轮注入为空，她一个字都没提自己的事。
    省 token 不能省掉"她是谁"。
    """
    now = now or datetime.now()
    sid = str(session or "default")
    state = _load_projection(home)
    prev_raw = state.get(sid) if isinstance(state.get(sid), dict) else {}
    seen = prev_raw.get("blocks") if isinstance(prev_raw.get("blocks"), dict) else {}
    count = int(prev_raw.get("count") or 0)

    now_map = {b.name: _fingerprint(b.text) for b in ctx.blocks}
    changed = [b.name for b in ctx.blocks if seen.get(b.name) != _fingerprint(b.text)]
    dropped = [name for name in seen if name not in now_map]
    sticky = [b.name for b in ctx.blocks if b.sticky]

    is_full = bool(force) or not seen or (count % max(1, int(full_every)) == 0)
    state[sid] = {"blocks": now_map, "count": count + 1,
                  "at": now.isoformat(timespec="seconds")}
    if len(state) > PROJECTION_MAX_SESSIONS:
        state = dict(list(state.items())[-PROJECTION_MAX_SESSIONS:])
    _save_projection(home, state)

    if is_full:
        reason = "强制全量" if force else ("首次注入" if not seen else "周期性全量恢复")
        return Projection(text=ctx.text, full=True, changed=[b.name for b in ctx.blocks],
                          dropped=dropped, rounds=count + 1, reason=reason)

    # 常驻块每次都带；变化的非常驻块（state）也带。
    # 顺序按 priority 走，别让【状态更新】跑到人格前面。
    keep = set(sticky) | set(changed)
    parts = [b.text for b in ctx.blocks if b.name in keep]
    if not parts:
        return Projection(text="", full=False, changed=[], dropped=dropped,
                          rounds=count + 1, reason="状态没变，不用重复注入")
    only_sticky = not changed
    return Projection(text="\n\n".join(parts), full=False,
                      changed=[b.name for b in ctx.blocks if b.name in keep],
                      dropped=dropped, rounds=count + 1,
                      reason="只注入变化的块" if not only_sticky else "常驻块照旧带上")
