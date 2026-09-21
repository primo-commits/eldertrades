"""
Regression tests for the two defects the first live session exposed.

Run: python -m tests.test_timeframes
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from elder.data import regular_session, session_anchored
from elder.structure import find_swings
from elder.zones import Zone, select_zone


def _rth_5min(days: int = 41, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    bd = pd.bdate_range(end="2026-09-18", periods=days)
    idx = pd.DatetimeIndex([t for d in bd for t in pd.date_range(
        d + pd.Timedelta("9h30m"), periods=78, freq="5min", tz="America/New_York")])
    px = 400 + np.cumsum(rng.normal(0, 0.25, len(idx)))
    return pd.DataFrame({"open": px, "high": px + 0.4, "low": px - 0.4,
                         "close": px, "volume": rng.integers(5_000, 20_000, len(idx))},
                        index=idx)


def test_rth_filter_destroys_native_4h_bars():
    """The original bug: a time-of-day filter keeps a 4H bar only if its START
    lands inside RTH. Alpaca's 4H grid is UTC-aligned, so one bar per day
    survived and the structure read could never resolve."""
    u = pd.date_range("2026-08-07", "2026-09-18", freq="4h", tz="UTC")
    px = 400 + np.cumsum(np.random.default_rng(1).normal(0, 2, len(u)))
    native = pd.DataFrame({"open": px, "high": px + 3, "low": px - 3,
                           "close": px, "volume": 1e5}, index=u)
    kept = regular_session(native, skip_first_minutes=5, skip_last_minutes=5)
    survivors = {t.strftime("%H:%M") for t in kept.index}
    per_day = len(kept) / max(len({t.date() for t in kept.index}), 1)
    print(f"  native 4H: {len(native)} bars -> {len(kept)} survive "
          f"({len(kept)/len(native):.0%}), start times {sorted(survivors)}")
    assert per_day <= 1.01, "the bug: at most one native 4H bar per day survives"
    assert len(survivors) == 1, "all survivors share a single start time"


def test_session_anchored_4h_gives_two_bars_per_day():
    """The fix: derive 4H from RTH-filtered 5-min bars, anchored to 09:30."""
    m5 = _rth_5min()
    h4 = session_anchored(m5, "4h")
    days = len({t.date() for t in h4.index})
    per_day = len(h4) / days
    hi, lo = find_swings(h4, 3)
    print(f"  session-anchored 4H: {len(h4)} bars over {days} days "
          f"({per_day:.1f}/day), {len(hi)} swing highs, {len(lo)} swing lows")
    assert 1.9 <= per_day <= 2.1, "RTH is 6.5h -> two 4H buckets per day"
    assert {t.strftime("%H:%M") for t in h4.index} == {"09:30", "13:30"}
    # Enough confirmed pivots for the classifier to have something to work with.
    assert len(hi) >= 3 and len(lo) >= 3, "must yield usable swings"


def test_no_bar_straddles_the_overnight_gap():
    m5 = _rth_5min()
    for rule in ("15min", "30min", "1h", "4h"):
        out = session_anchored(m5, rule)
        spans = {t.date() for t in out.index}
        assert len(spans) == len({t.date() for t in m5.index}), rule
    print("  15m/30m/1h/4h: every bar stays inside its own session")


def test_zone_proximity_scales_with_zone_timeframe():
    """A 4H zone must be judged against a 4H-sized tolerance, not the 5-min ATR."""
    price = 377.0
    z4 = Zone("demand", top=341.0, bottom=338.0, formed_at=None, impulse_at=None,
              timeframe="4Hour", impulse_atr_mult=3.0)
    z15 = Zone("demand", top=375.5, bottom=374.8, formed_at=None, impulse_at=None,
               timeframe="15Min", impulse_atr_mult=2.5)

    five_min_only = {"15Min": (1.7 * 2) / price, "4Hour": (1.7 * 2) / price}
    per_tf = {"15Min": (1.7 * 2) / price, "4Hour": (9.0 * 2) / price}

    print(f"  4H zone sits {z4.distance_pct(price):.1%} from price")
    print(f"    tolerance from 5-min ATR : {five_min_only['4Hour']:.2%}  -> rejected")
    print(f"    tolerance from 4H ATR    : {per_tf['4Hour']:.2%}  -> still rejected (correctly)")

    assert select_zone([z4], price, 1, max_distance_by_tf=five_min_only) is None
    # A genuinely distant zone stays rejected even with the right units --
    # the fix is about correct reasoning, not about forcing a trade.
    assert select_zone([z4], price, 1, max_distance_by_tf=per_tf) is None
    # A nearby 15-min zone is accepted.
    picked = select_zone([z4, z15], price, 1, max_distance_by_tf=per_tf)
    assert picked is z15, "the reachable zone should be chosen"
    print(f"    nearby 15m zone {z15.distance_pct(price):.2%} away -> selected")


if __name__ == "__main__":
    for fn in (test_rth_filter_destroys_native_4h_bars,
               test_session_anchored_4h_gives_two_bars_per_day,
               test_no_bar_straddles_the_overnight_gap,
               test_zone_proximity_scales_with_zone_timeframe):
        print(f"\n{fn.__name__}:")
        fn()
    print("\nALL TIMEFRAME TESTS PASS")
