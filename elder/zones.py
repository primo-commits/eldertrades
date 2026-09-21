"""
Supply & demand zones -- Elder's location layer.

    "What I like to do with these levels is mark out the consolidation, which
     again is going to be the value area before the expansion."

So a zone is NOT the impulse candle. It is the balance/consolidation that came
immediately BEFORE the impulse -- where institutions filled before driving
price:

    "They will get into their positions right here... they can't get in all at
     once. They have to break their orders slowly and slowly, which is why you
     start to see that absorption when you get to these key levels."

The "obvious" gate is explicit and is the main filter:

    "If the move down or up isn't obvious, I suggest you just simply stay away."

Implemented as: the expansion must exceed `expansion_atr_mult` x ATR.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr

SUPPLY, DEMAND = "supply", "demand"


@dataclass
class Zone:
    kind: str                   # "supply" | "demand"
    top: float
    bottom: float
    formed_at: pd.Timestamp     # start of the consolidation
    impulse_at: pd.Timestamp    # the bar that expanded away
    timeframe: str
    impulse_atr_mult: float     # how "obvious" the move was
    touches: int = 0
    mitigated: bool = False
    lvn_confluence: bool = False

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def distance_pct(self, price: float) -> float:
        """Fractional distance from price to the nearest zone edge (0 if inside)."""
        if self.contains(price):
            return 0.0
        edge = self.bottom if price < self.bottom else self.top
        return abs(price - edge) / price if price else float("inf")

    @property
    def strength(self) -> float:
        """
        0-1. Rewards an obvious impulse and LVN confluence; penalises a zone that
        has already been worked repeatedly ("these levels get respected... or
        get held and invalidated").
        """
        s = min(1.0, self.impulse_atr_mult / 4.0) * 0.6
        s += 0.25 if self.lvn_confluence else 0.0
        s += max(0.0, 0.15 - 0.05 * self.touches)
        return round(min(1.0, s), 3)


def find_zones(bars: pd.DataFrame, *, timeframe: str = "1Hour",
               expansion_atr_mult: float = 2.0,
               consolidation_max_bars: int = 10,
               consolidation_atr_mult: float = 0.75,
               atr_period: int = 14,
               max_touches: int = 3) -> list[Zone]:
    """
    Locate consolidation-before-expansion zones.

    1. Find bars whose displacement exceeds `expansion_atr_mult` x ATR -- the
       "obvious" move.
    2. Walk backwards to collect the tight bars preceding it (each with a range
       under `consolidation_atr_mult` x ATR) -- the balance.
    3. Zone = that consolidation's high/low.
    4. Count later touches and mark mitigation.
    """
    if bars is None or len(bars) < atr_period + consolidation_max_bars + 2:
        return []

    a = atr(bars, atr_period)
    highs, lows = bars["high"].values, bars["low"].values
    opens, closes = bars["open"].values, bars["close"].values
    idx = bars.index
    n = len(bars)

    zones: list[Zone] = []

    for i in range(atr_period + consolidation_max_bars, n):
        cur_atr = a.iloc[i]
        if not np.isfinite(cur_atr) or cur_atr <= 0:
            continue

        displacement = closes[i] - opens[i]
        mult = abs(displacement) / cur_atr
        if mult < expansion_atr_mult:
            continue                                    # not obvious -> skip

        kind = DEMAND if displacement > 0 else SUPPLY

        # Walk back over the tight bars that formed the balance.
        tight_max = cur_atr * consolidation_atr_mult
        start = i
        for j in range(i - 1, max(i - consolidation_max_bars - 1, 0), -1):
            if (highs[j] - lows[j]) <= tight_max:
                start = j
            else:
                break
        if start >= i:
            continue                                    # no balance before the move

        seg_h = float(highs[start:i].max())
        seg_l = float(lows[start:i].min())
        if seg_h <= seg_l:
            continue

        z = Zone(kind=kind, top=seg_h, bottom=seg_l,
                 formed_at=idx[start], impulse_at=idx[i],
                 timeframe=timeframe, impulse_atr_mult=round(float(mult), 2))

        # How has price treated it since?
        after = bars.iloc[i + 1:]
        if not after.empty:
            touched = ((after["low"] <= z.top) & (after["high"] >= z.bottom))
            z.touches = int(touched.sum())
            # Mitigated once price closes clean through the far side.
            if kind == DEMAND:
                z.mitigated = bool((after["close"] < z.bottom).any())
            else:
                z.mitigated = bool((after["close"] > z.top).any())

        zones.append(z)

    return _dedupe(zones, max_touches)


def _dedupe(zones: list[Zone], max_touches: int) -> list[Zone]:
    """Merge heavily overlapping same-kind zones; drop worn-out ones."""
    live = [z for z in zones if not z.mitigated and z.touches <= max_touches]
    live.sort(key=lambda z: (z.kind, z.bottom))
    out: list[Zone] = []
    for z in live:
        if out and out[-1].kind == z.kind:
            prev = out[-1]
            overlap = min(prev.top, z.top) - max(prev.bottom, z.bottom)
            if overlap > 0 and overlap > min(prev.height, z.height) * 0.6:
                prev.top = max(prev.top, z.top)
                prev.bottom = min(prev.bottom, z.bottom)
                prev.impulse_atr_mult = max(prev.impulse_atr_mult, z.impulse_atr_mult)
                prev.touches = max(prev.touches, z.touches)
                continue
        out.append(z)
    return sorted(out, key=lambda z: z.formed_at)


def tag_lvn_confluence(zones: list[Zone], lvn_levels: list[float]) -> list[Zone]:
    """
    Flag zones sitting on a low-volume node -- his highest-quality location:

        "if we have a demand zone down here, and it's also a low volume node...
         that's when you can expect a quick breakout, a quick bounce."
    """
    for z in zones:
        z.lvn_confluence = any(z.contains(p) for p in lvn_levels)
    return zones


def select_zone(zones: list[Zone], price: float, direction: int, *,
                max_distance_pct: float = 0.01,
                max_distance_by_tf: dict[str, float] | None = None,
                prefer_higher_timeframe: bool = True,
                timeframe_rank: dict[str, int] | None = None) -> Zone | None:
    """
    Pick the zone to engage: right side of the market, near enough to price.

    `max_distance_by_tf` gives each zone a proximity budget scaled to the ATR of
    the timeframe it was drawn on. A single global tolerance is a units
    mismatch: a 4-hour zone is a wider structure than a 15-minute one, and
    judging it against the 5-minute ATR makes every higher-timeframe zone look
    absurdly far away.

    Ties break toward the higher timeframe (a 4H zone beats a conflicting 15m
    zone), then toward strength.
    """
    want = DEMAND if direction > 0 else SUPPLY
    rank = timeframe_rank or {"4Hour": 4, "1Hour": 3, "30Min": 2, "15Min": 1}
    limits = max_distance_by_tf or {}

    def _limit(z: Zone) -> float:
        return limits.get(z.timeframe, max_distance_pct)

    cands = [z for z in zones
             if z.kind == want and not z.mitigated
             and z.distance_pct(price) <= _limit(z)]
    if not cands:
        return None
    return max(cands, key=lambda z: (
        rank.get(z.timeframe, 0) if prefer_higher_timeframe else 0,
        z.strength,
        -z.distance_pct(price),
    ))
