"""
Market structure -- Elder's first context read, on the 4H.

    "The market will either be making higher highs, higher lows, lower lows,
     lower highs or in a consolidation period. It doesn't do anything else."

Classification, in his words:
  bullish   "two higher lows and three higher highs"
  bearish   "two lower highs and then three new lows"
  balanced  "three equal highs and three equal lows"
  unclear   -> NO predetermined bias; react after the open

The "unclear" branch is not a failure mode, it is part of the method:

    "Sometimes you will see the market behave in weird ways in the higher
     timeframe where you don't have a clear structure. And at that point... I'm
     mostly going to be looking to reacting to the market... versus having one
     already predetermined in my head before the market opens."
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

BULLISH, BEARISH, BALANCED, UNCLEAR = "bullish", "bearish", "balanced", "unclear"


@dataclass
class Swing:
    timestamp: pd.Timestamp
    price: float
    kind: str        # "high" | "low"


@dataclass
class StructureRead:
    regime: str
    swing_highs: list[Swing]
    swing_lows: list[Swing]
    detail: str

    @property
    def has_bias(self) -> bool:
        return self.regime in (BULLISH, BEARISH)

    @property
    def direction(self) -> int:
        return {BULLISH: 1, BEARISH: -1}.get(self.regime, 0)


def find_swings(bars: pd.DataFrame, bars_each_side: int = 3) -> tuple[list[Swing], list[Swing]]:
    """
    Fractal pivots: a swing high is strictly higher than `bars_each_side` bars
    on both sides. Only confirmed pivots are returned -- the last
    `bars_each_side` bars cannot yet be classified, which avoids using a pivot
    that has not formed.
    """
    highs: list[Swing] = []
    lows: list[Swing] = []
    n = len(bars)
    k = bars_each_side
    if n < 2 * k + 1:
        return highs, lows

    h = bars["high"].values
    l = bars["low"].values
    idx = bars.index

    for i in range(k, n - k):
        window_h = h[i - k:i + k + 1]
        if h[i] == window_h.max() and (window_h.argmax() == k):
            highs.append(Swing(idx[i], float(h[i]), "high"))
        window_l = l[i - k:i + k + 1]
        if l[i] == window_l.min() and (window_l.argmin() == k):
            lows.append(Swing(idx[i], float(l[i]), "low"))
    return highs, lows


def _all_increasing(vals: list[float], min_step: float = 0.0) -> bool:
    """
    Strictly rising, and each step must exceed `min_step` (fractional).

    Without the min_step gate a sequence drifting by 0.01% reads as "higher
    highs" when the same tolerance would call those levels equal -- which
    silently turns a balanced range into a trend. Trend and equality must be
    judged against the same threshold.
    """
    if len(vals) < 2:
        return False
    return all(b > a * (1.0 + min_step) for a, b in zip(vals, vals[1:]))


def _all_decreasing(vals: list[float], min_step: float = 0.0) -> bool:
    if len(vals) < 2:
        return False
    return all(b < a * (1.0 - min_step) for a, b in zip(vals, vals[1:]))


def _all_equal(vals: list[float], tolerance: float) -> bool:
    """Within `tolerance` (fractional) of their mean -- his 'three equal highs'."""
    if len(vals) < 2:
        return False
    m = float(np.mean(vals))
    return m > 0 and all(abs(v - m) / m <= tolerance for v in vals)


def classify(bars: pd.DataFrame, *, bars_each_side: int = 3,
             required_highs: int = 3, required_lows: int = 2,
             equal_tolerance: float = 0.005) -> StructureRead:
    """
    Read structure from the most recent confirmed swings.

    Bullish needs `required_lows` rising lows AND `required_highs` rising highs
    (his 2 and 3). Bearish is the mirror: 2 falling highs and 3 falling lows --
    note the counts swap, matching the transcript exactly.
    """
    highs, lows = find_swings(bars, bars_each_side)
    if not highs or not lows:
        return StructureRead(UNCLEAR, highs, lows, "not enough confirmed swings")

    hi_recent = [s.price for s in highs[-max(required_highs, required_lows):]]
    lo_recent = [s.price for s in lows[-max(required_highs, required_lows):]]

    # Balanced is tested FIRST: levels within `equal_tolerance` are equal by
    # definition, so they must not also be eligible to count as a trend.
    eq_h = _all_equal([s.price for s in highs[-required_highs:]], equal_tolerance)
    eq_l = _all_equal([s.price for s in lows[-required_highs:]], equal_tolerance)
    if eq_h and eq_l:
        return StructureRead(BALANCED, highs, lows,
                             f"{required_highs} equal highs + {required_highs} equal lows "
                             f"(within {equal_tolerance:.2%})")

    tol = equal_tolerance
    bull_h = _all_increasing([s.price for s in highs[-required_highs:]], tol)
    bull_l = _all_increasing([s.price for s in lows[-required_lows:]], tol)
    bear_h = _all_decreasing([s.price for s in highs[-required_lows:]], tol)
    bear_l = _all_decreasing([s.price for s in lows[-required_highs:]], tol)

    if bull_h and bull_l:
        return StructureRead(BULLISH, highs, lows,
                             f"{required_lows} higher lows + {required_highs} higher highs")
    if bear_h and bear_l:
        return StructureRead(BEARISH, highs, lows,
                             f"{required_lows} lower highs + {required_highs} lower lows")

    return StructureRead(UNCLEAR, highs, lows,
                         "no clean higher-high/higher-low or equal-level sequence")


def invalidation_level(bars: pd.DataFrame, direction: int, *,
                       bars_each_side: int = 3) -> float | None:
    """
    Where the thesis is wrong -- Elder's stop logic, not an ATR distance:

        "If I'm looking for longs, I think this is going to be a higher low. I'm
         going to put my stop loss underneath that higher low because if we make
         a new low, my thesis is incorrect."

    Long  -> the most recent confirmed swing low.
    Short -> the most recent confirmed swing high.
    """
    highs, lows = find_swings(bars, bars_each_side)
    if direction > 0:
        return lows[-1].price if lows else None
    if direction < 0:
        return highs[-1].price if highs else None
    return None
