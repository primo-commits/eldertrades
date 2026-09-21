"""
Assembles the three pillars into a tradeable setup.

    "Context, location, confirmation is everything."

No setup is produced unless all three agree. Each rejection is recorded with a
reason so the live log tells you WHY nothing fired, rather than going silent --
which is what made the original bot impossible to debug.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

from . import structure as st
from . import volume_profile as vp
from . import zones as zn
from .confirmation import ConfirmationRead, confirm
from .indicators import atr


@dataclass
class Setup:
    symbol: str
    side: str                  # "buy" | "sell"
    entry: float
    stop: float
    target: float
    reward_risk: float
    regime: str
    zone: zn.Zone
    confirmation: ConfirmationRead
    profile_position: str
    poc: float
    poc_trend: str
    atr: float
    notes: list[str] = field(default_factory=list)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    def summary(self) -> str:
        return (f"{self.side.upper()} {self.symbol} @ {self.entry:.2f} "
                f"stop {self.stop:.2f} target {self.target:.2f} "
                f"({self.reward_risk:.2f}R) | {self.regime} | "
                f"zone {self.zone.kind} {self.zone.bottom:.2f}-{self.zone.top:.2f} "
                f"str {self.zone.strength} | conf {self.confirmation.score}")


@dataclass
class Rejection:
    symbol: str
    stage: str                 # "context" | "location" | "confirmation" | "exits"
    reason: str


def evaluate(symbol: str, *, bars_by_tf: dict[str, pd.DataFrame],
             cfg: Any) -> tuple[Setup | None, list[Rejection]]:
    """
    Run one symbol through context -> location -> confirmation.

    bars_by_tf must contain the structure timeframe, the volume-profile
    timeframe, and an execution timeframe (the shortest supplied).
    """
    s = cfg.strategy
    rejects: list[Rejection] = []

    tf_struct = s.context["structure_timeframe"]
    tf_vp = s.volume_profile["timeframe"]
    exec_tf = min(bars_by_tf, key=lambda k: _tf_minutes(k))
    exec_bars = bars_by_tf.get(exec_tf, pd.DataFrame())

    if exec_bars.empty:
        return None, [Rejection(symbol, "context", "no execution-timeframe bars")]

    # ── 1. CONTEXT ──────────────────────────────────────────────────────────
    struct_bars = bars_by_tf.get(tf_struct, pd.DataFrame())
    if struct_bars.empty:
        return None, [Rejection(symbol, "context", f"no {tf_struct} bars")]

    read = st.classify(
        struct_bars,
        bars_each_side=s.context["swing_bars"],
        required_highs=s.context["bullish_higher_highs"],
        required_lows=s.context["bullish_higher_lows"],
        equal_tolerance=s.context["equal_level_tolerance"],
    )
    if not read.has_bias:
        # Not a failure -- this is the method. No bias, no predetermined trade.
        return None, [Rejection(symbol, "context",
                                f"structure {read.regime}: {read.detail}")]
    direction = read.direction

    vp_bars = bars_by_tf.get(tf_vp, exec_bars)
    profiles = vp.session_profiles(
        vp_bars,
        bins=s.volume_profile["price_bins"],
        value_area_pct=s.volume_profile["value_area_pct"],
        hvn_percentile=s.volume_profile["hvn_percentile"],
        lvn_percentile=s.volume_profile["lvn_percentile"],
    )
    if not profiles:
        return None, [Rejection(symbol, "context", "volume profile unavailable")]

    today = sorted(profiles)[-1]
    profile = profiles[today]
    trend = vp.poc_trend(profiles)
    price = float(exec_bars["close"].iloc[-1])
    position = profile.position_of(price)

    # Elder does not fade his own context: if the POC trend disagrees with the
    # structural bias, stand down rather than guess.
    if trend != "neutral" and ((trend == "bullish") != (direction > 0)):
        return None, [Rejection(symbol, "context",
                                f"POC trend {trend} conflicts with structure {read.regime}")]

    # ── 2. LOCATION ─────────────────────────────────────────────────────────
    all_zones: list[zn.Zone] = []
    for tf in s.zones["timeframes"]:
        b = bars_by_tf.get(tf)
        if b is None or b.empty:
            continue
        all_zones += zn.find_zones(
            b, timeframe=tf,
            expansion_atr_mult=s.zones["expansion_atr_mult"],
            consolidation_max_bars=s.zones["consolidation_max_bars"],
            consolidation_atr_mult=s.zones["consolidation_atr_mult"],
            max_touches=s.zones["max_touches"],
        )
    if not all_zones:
        return None, [Rejection(symbol, "location", "no valid zones on any timeframe")]

    zn.tag_lvn_confluence(all_zones, profile.lvn)
    cur_atr = float(atr(exec_bars, 14).iloc[-1])
    if not (cur_atr > 0):
        return None, [Rejection(symbol, "location", "ATR unavailable for proximity test")]

    # Proximity budget per zone timeframe. A 4H zone gets a 4H-sized tolerance;
    # scoring it against the 5-minute ATR made every higher-timeframe zone read
    # as tens of percent away.
    mult = s.zones["max_distance_atr"]
    max_dist_by_tf: dict[str, float] = {}
    for tf, b in bars_by_tf.items():
        if b is None or len(b) < 15:
            continue
        a = float(atr(b, 14).iloc[-1])
        if a > 0:
            max_dist_by_tf[tf] = (a * mult) / price
    max_dist = (cur_atr * mult) / price

    zone = zn.select_zone(
        all_zones, price, direction,
        max_distance_pct=max_dist,
        max_distance_by_tf=max_dist_by_tf,
        prefer_higher_timeframe=s.zones["timeframe_priority_high_wins"],
    )
    if zone is None:
        near = [z for z in all_zones
                if z.kind == (zn.DEMAND if direction > 0 else zn.SUPPLY) and not z.mitigated]
        if near:
            z0 = min(near, key=lambda z: z.distance_pct(price))
            lim = max_dist_by_tf.get(z0.timeframe, max_dist)
            detail = (f"closest is {z0.distance_pct(price):.2%} away on {z0.timeframe}, "
                      f"limit {lim:.2%}")
        else:
            detail = "no zones on the required side"
        return None, [Rejection(symbol, "location",
                                f"price {price:.2f} not at a "
                                f"{'demand' if direction > 0 else 'supply'} zone ({detail})")]

    # ── 3. CONFIRMATION ─────────────────────────────────────────────────────
    c = confirm(
        exec_bars, zone,
        mode=s.confirmation.get("mode", "bars"),
        exhaustion_bars=s.confirmation["exhaustion_bars"],
        require_declining_volume=s.confirmation["exhaustion_require_declining_volume"],
        flip_atr_mult=s.confirmation["flip_atr_mult"],
        flip_volume_mult=s.confirmation["flip_volume_mult"],
    )
    if not c.confirmed:
        return None, [Rejection(symbol, "confirmation", c.detail)]

    # ── 4. EXITS ────────────────────────────────────────────────────────────
    interaction = s.confirmation["exhaustion_bars"] + 1   # the pushes plus the flip bar
    stop = _stop_price(exec_bars, zone, direction, cur_atr, s, interaction)
    if stop is None:
        return None, [Rejection(symbol, "exits", "no invalidation level available")]

    risk = abs(price - stop)
    if risk <= 0:
        return None, [Rejection(symbol, "exits", "zero stop distance")]
    if risk > cur_atr * s.exits["stop_atr_max_mult"]:
        return None, [Rejection(symbol, "exits",
                                f"invalidation {risk / cur_atr:.2f}x ATR exceeds "
                                f"{s.exits['stop_atr_max_mult']}x cap -- setup too wide")]

    target = _target_price(profile, price, direction,
                           risk=risk, min_rr=s.exits["min_reward_risk"])
    if target is None:
        return None, [Rejection(symbol, "exits",
                                f"no target ahead of price paying "
                                f"{s.exits['min_reward_risk']}R (risk ${risk:.2f})")]

    rr = abs(target - price) / risk
    if rr < s.exits["min_reward_risk"]:
        return None, [Rejection(symbol, "exits",
                                f"reward:risk {rr:.2f} below {s.exits['min_reward_risk']}")]

    setup = Setup(
        symbol=symbol, side="buy" if direction > 0 else "sell",
        entry=price, stop=round(stop, 2), target=round(target, 2),
        reward_risk=round(rr, 2), regime=read.regime, zone=zone,
        confirmation=c, profile_position=position, poc=round(profile.poc, 2),
        poc_trend=trend, atr=round(cur_atr, 4),
    )
    if zone.lvn_confluence:
        setup.notes.append("zone sits on a low-volume node -- expect a fast reaction")
    if position == "in_value":
        setup.notes.append("price inside value area: rotational, lower conviction")
    return setup, rejects


def _stop_price(bars: pd.DataFrame, zone: zn.Zone, direction: int,
                cur_atr: float, s: Any, interaction_bars: int) -> float | None:
    """
    Invalidation-based, per the transcript:

        "I think this is going to be a higher low. I'm going to put my stop loss
         underneath that higher low."

    The low he means is the one JUST formed at the zone -- the low of the
    exhaustion pushes plus the flip bar. A fractal swing cannot be used here:
    `find_swings` only returns pivots confirmed by N bars on both sides, so at
    the instant of entry the relevant low has not been confirmed yet and the
    function returns a much older, far-away pivot. Using that made almost every
    setup fail the ATR width cap.

    So: primary = the extreme of the interaction window. The confirmed swing is
    kept only as a wider fallback when that window is degenerate.
    """
    buf = cur_atr * s.exits["stop_atr_buffer"]
    window = bars.tail(max(interaction_bars, 2))

    if s.exits["stop_basis"] == "invalidation" and not window.empty:
        level = float(window["low"].min()) if direction > 0 else float(window["high"].max())
        if np.isfinite(level):
            return level - buf if direction > 0 else level + buf

    level = st.invalidation_level(bars, direction, bars_each_side=s.context["swing_bars"])
    if level is not None:
        return level - buf if direction > 0 else level + buf
    return zone.bottom - buf if direction > 0 else zone.top + buf


def _target_price(profile: vp.Profile, price: float, direction: int,
                  risk: float = 0.0, min_rr: float = 0.0) -> float | None:
    """
    POC first ("the fair value is going to be the magnet"), then HVNs where
    price is expected to stall, then the opposing value-area edge.

    Among the candidates ahead of price, take the NEAREST ONE THAT STILL PAYS
    at least `min_rr`. Taking the nearest candidate outright is wrong: a node
    three cents away is noise, and it produced setups at 0.04R. He wants fast
    trades, but not free ones.
    """
    candidates = []
    if (direction > 0 and profile.poc > price) or (direction < 0 and profile.poc < price):
        candidates.append(profile.poc)
    for h in profile.hvn:
        if (direction > 0 and h > price) or (direction < 0 and h < price):
            candidates.append(h)
    edge = profile.vah if direction > 0 else profile.val
    if (direction > 0 and edge > price) or (direction < 0 and edge < price):
        candidates.append(edge)
    if not candidates:
        return None
    if risk > 0 and min_rr > 0:
        viable = [c for c in candidates if abs(c - price) / risk >= min_rr]
        if viable:
            return min(viable, key=lambda x: abs(x - price))
        return None
    return min(candidates, key=lambda x: abs(x - price))


def _tf_minutes(spec: str) -> int:
    digits = int("".join(c for c in spec if c.isdigit()) or 1)
    unit = "".join(c for c in spec if c.isalpha()).lower()
    if unit.startswith("min"):
        return digits
    if unit.startswith("h"):
        return digits * 60
    return digits * 60 * 24
