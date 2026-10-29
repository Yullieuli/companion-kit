"""typing：她"打字"要花多久。

为什么在 core 里：这是**她是谁**的一部分，不是通道的事。
一条 12 个字的消息秒回，像机器人；隔 3 秒回，像在忙。
通道只负责"等多久再发"，这个"多久"由 core 算。

三档：
  · 短消息（≤6 字）   → 800–1800ms，像秒回
  · 中等（7–30 字）   → 1500–3500ms
  · 长消息（>30 字）  → 3000–6500ms，长句子本来就要打一会儿

另外两个真实感细节：
  · 她正在忙（刚回了话 / 刚做完一件事）→ 整体乘 1.4
  · 凌晨（她本来就不该秒回）→ 整体乘 1.6

**不做**逐字流式输出——那是通道的能力，core 只给一个总的等待时间。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 每档的 (最小毫秒, 最大毫秒)
BANDS: tuple[tuple[int, int, int], ...] = (
    # (上限字数, 最小毫秒, 最大毫秒)
    (6, 800, 1800),
    (30, 1500, 3500),
    (10**9, 3000, 6500),
)
BUSY_FACTOR = 1.4
NIGHT_FACTOR = 1.6
NIGHT_HOURS = (23, 6)
MAX_MS = 8000


def band_of(chars: int) -> tuple[int, int]:
    """按字数落在哪一档。"""
    n = max(0, int(chars))
    for limit, lo, hi in BANDS:
        if n <= limit:
            return lo, hi
    return BANDS[-1][1], BANDS[-1][2]


def delay_ms(
    text: str,
    *,
    busy: bool = False,
    now: datetime | None = None,
    rnd: Any | None = None,
) -> int:
    """算这条消息该等多久再发（毫秒）。

    `rnd` 传一个有 `.random()` 的对象（像 `random.Random`），便于测试注入。
    """
    lo, hi = band_of(len(text or ""))
    r = rnd
    if r is None:
        import random

        r = random
    ms = lo + int(r.random() * (hi - lo))

    if busy:
        ms = int(ms * BUSY_FACTOR)
    now = now or datetime.now()
    if NIGHT_HOURS[0] <= now.hour or now.hour < NIGHT_HOURS[1]:
        ms = int(ms * NIGHT_FACTOR)
    return min(MAX_MS, ms)


def seconds(text: str, **kw: Any) -> float:
    """同一个口径，但单位是秒（宿主 sleep 用这个）。"""
    return round(delay_ms(text, **kw) / 1000.0, 2)
