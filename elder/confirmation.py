"""
Confirmation -- Elder's entry trigger, the third pillar.

    "You're not only waiting for one side to die out or get absorbed, you're
     waiting for the other side to step back up and then fully take control.
     This is what you have to use as an entry."

BOTH legs are required:
  1. EXHAUSTION -- the side driving price into the zone runs out of size
  2. FLIP       -- the other side steps up and moves price away

Two modes:

  mode="bars"       Works on the free IEX feed. Uses bar volume and range as a
                    stand-in for aggressive participation. Degraded but honest:
                    it cannot distinguish aggressive from passive flow.

  mode="orderflow"  Requires feed: sip. Uses classified trade delta from
                    orderflow.py, which is much closer to the bubbles Elder
                    actually reads on Bookmap.

Neither mode sees passive resting liquidity (the heatmap lines) -- that needs
CME depth and has no Alpaca equivalent. See docs/strategy-spec.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import atr
from .zones import DEMAND, SUPPLY, Zone


@dataclass
class ConfirmationRead:
    confirmed: bool
    exhaustion: bool
    flip: bool
    mode: str
    detail: str
    exhaustion_strength: float = 0.0   # 0-1
    flip_strength: float = 0.0         # 0-1

    @property
    def score(self) -> float:
        if not self.confirmed:
            return 0.0
        return round((self.exhaustion_strength + self.flip_strength) / 2.0, 3)


def _avg_volume(bars: pd.DataFrame, window: int = 20) -> float:
    v = float(bars["volume"].tail(window).mean())
    return v if np.isfinite(v) and v > 0 else 0.0


def detect_exhaustion(bars: pd.DataFrame, zone: Zone, *, n_bars: int = 3,
                      require_declining_volume: bool = True,
                      mode: str = "fading",
                      flow: pd.DataFrame | None = None) -> tuple[bool, float, str]:
    """
    The side pushing into the zone is running out.

    At a SUPPLY zone price is pushing UP, so exhaustion means continued upward
    pushes on shrinking participation -- not down bars, which would already be
    the reversal.

        "we see the market push up higher and higher and higher and we see the
         aggressive buyers getting smaller and smaller and smaller"

    mode="strict"  every bar makes a higher high AND volume falls every bar.
                   A perfect monotonic staircase. Measured end to end on a real
                   walk-forward: the full confirmation passed 5 times out of
                   3,052 setups, and once the flip moved to a window it passed
                   ZERO times out of 6,099. One bar that fails to extend, or a
                   single volume uptick, resets the whole sequence.

    mode="fading"  (default) price is still PRESSING the zone -- the latest
                   extreme sits near the window's extreme -- and participation
                   is FADING: mean volume over the later half of the window is
                   below the earlier half. Same idea, no staircase requirement.
    """
    if len(bars) < n_bars + 1:
        return False, 0.0, "not enough bars"

    if mode == "fading":
        return _exhaustion_fading(bars, zone, n_bars, require_declining_volume, flow)

    recent = bars.tail(n_bars)
    pushing_up = zone.kind == SUPPLY

    # Leg 1: price is still being driven toward the zone.
    if pushing_up:
        advancing = bool((recent["high"].diff().dropna() > 0).all())
        direction = "buyers"
    else:
        advancing = bool((recent["low"].diff().dropna() < 0).all())
        direction = "sellers"
    if not advancing:
        return False, 0.0, f"{direction} not still pushing into the zone"

    # Leg 2: participation on each push is shrinking.
    if flow is not None and not flow.empty and len(flow) >= n_bars:
        # Aggressive size on the pushing side (SIP path).
        d = flow["delta"].tail(n_bars)
        series = d.abs() if pushing_up else d.abs()
        label = "aggressive size"
    else:
        series = recent["volume"]
        label = "volume"

    if require_declining_volume:
        declining = bool((series.diff().dropna() < 0).all())
        if not declining:
            return False, 0.0, f"{label} not declining across {n_bars} pushes"
    else:
        declining = True

    first, last = float(series.iloc[0]), float(series.iloc[-1])
    decay = (first - last) / first if first > 0 else 0.0
    strength = float(np.clip(decay, 0.0, 1.0))
    return True, round(strength, 3), (
        f"{direction} exhausting: {n_bars} pushes, {label} -{decay:.0%}"
    )


def _exhaustion_fading(bars: pd.DataFrame, zone: Zone, n_bars: int,
                       require_declining_volume: bool,
                       flow: pd.DataFrame | None) -> tuple[bool, float, str]:
    """Non-monotonic exhaustion: still pressing, participation fading."""
    w = bars.tail(max(n_bars * 2, 4))
    pushing_up = zone.kind == SUPPLY
    direction = "buyers" if pushing_up else "sellers"

    # Still pressing the zone: the latest extreme is at or near the window's.
    if pushing_up:
        extreme, latest = float(w["high"].max()), float(w["high"].tail(2).max())
    else:
        extreme, latest = float(w["low"].min()), float(w["low"].tail(2).min())
    span = float(w["high"].max() - w["low"].min())
    if span <= 0:
        return False, 0.0, "flat window"
    pressing = abs(latest - extreme) <= span * 0.25
    if not pressing:
        return False, 0.0, f"{direction} no longer pressing the zone"

    half = max(1, len(w) // 2)
    early, late = float(w["volume"].head(half).mean()), float(w["volume"].tail(half).mean())
    if require_declining_volume and not (late < early):
        return False, 0.0, f"participation not fading ({late/early:.2f}x)"

    decay = (early - late) / early if early > 0 else 0.0
    return True, round(float(np.clip(decay, 0.0, 1.0)), 3), (
        f"{direction} fading: volume {decay:+.0%} over {len(w)} bars while "
        f"still pressing")


def detect_flip(bars: pd.DataFrame, zone: Zone, *, atr_mult: float = 1.0,
                volume_mult: float = 1.5, atr_period: int = 14,
                avg_window: int = 20, window_bars: int = 3,
                flow: pd.DataFrame | None = None) -> tuple[bool, float, str]:
    """
    The other side steps up and takes control.

        "then big buyers step up and start moving the market up"

    Evaluated over a WINDOW of `window_bars`, not a single bar.

    The original required displacement greater than atr_mult x ATR AND volume
    above volume_mult x average, both on ONE 5-minute bar. Measured on a real
    backtest that fired 5 times out of 3,052 setups -- 0.16%. Taking control is
    not the same event as one outsized candle: a zone that gets rejected over
    three bars on rising participation is exactly what Elder describes, and the
    single-bar encoding threw all of those away.

    Cumulative displacement across the window is compared to atr_mult x ATR,
    and mean window volume to volume_mult x average. Set window_bars=1 to
    restore the old single-bar behaviour.
    """
    if len(bars) < atr_period + 2:
        return False, 0.0, "not enough bars for ATR"

    a = atr(bars, atr_period)
    cur_atr = float(a.iloc[-1])
    if not np.isfinite(cur_atr) or cur_atr <= 0:
        return False, 0.0, "ATR unavailable"

    w = bars.tail(max(1, window_bars))
    displacement = float(w["close"].iloc[-1] - w["open"].iloc[0])
    want_up = zone.kind == DEMAND          # demand -> we want buyers taking over

    if want_up and displacement <= 0:
        return False, 0.0, "no upward displacement"
    if not want_up and displacement >= 0:
        return False, 0.0, "no downward displacement"

    move_mult = abs(displacement) / cur_atr
    if move_mult < atr_mult:
        return False, 0.0, f"move {move_mult:.2f}x ATR below {atr_mult}x threshold"

    avg_v = _avg_volume(bars, avg_window)
    vol_ratio = float(w["volume"].mean()) / avg_v if avg_v > 0 else 0.0
    if vol_ratio < volume_mult:
        return False, 0.0, f"volume {vol_ratio:.2f}x avg below {volume_mult}x threshold"

    last = w.iloc[-1]

    # On the SIP path, also require aggressive delta to agree with the direction.
    if flow is not None and not flow.empty:
        d = float(flow["delta"].iloc[-1])
        if want_up and d <= 0:
            return False, 0.0, "delta still net-selling"
        if not want_up and d >= 0:
            return False, 0.0, "delta still net-buying"

    strength = float(np.clip((move_mult / (atr_mult * 2)) * 0.5
                             + (vol_ratio / (volume_mult * 2)) * 0.5, 0.0, 1.0))
    side = "buyers" if want_up else "sellers"
    return True, round(strength, 3), (
        f"{side} took control: {move_mult:.2f}x ATR on {vol_ratio:.2f}x volume"
    )


def confirm(bars: pd.DataFrame, zone: Zone, *, mode: str = "bars",
            exhaustion_bars: int = 3, require_declining_volume: bool = True,
            exhaustion_mode: str = "fading",
            flip_atr_mult: float = 1.0, flip_volume_mult: float = 1.5,
            flip_window_bars: int = 3,
            flow: pd.DataFrame | None = None) -> ConfirmationRead:
    """
    Full trigger: exhaustion THEN flip. Both, or no trade.

    Exhaustion is evaluated on the bars leading up to the flip bar, so the two
    legs do not overlap -- the flip bar itself is excluded from the exhaustion
    window.
    """
    if mode == "orderflow" and (flow is None or flow.empty):
        return ConfirmationRead(False, False, False, mode,
                                "orderflow mode requested but no flow data "
                                "(needs feed: sip)")

    use_flow = flow if mode == "orderflow" else None

    # Exhaustion looks at the window BEFORE the most recent bar.
    prior = bars.iloc[:-max(1, flip_window_bars)]
    prior_flow = use_flow.iloc[:-1] if use_flow is not None and len(use_flow) > 1 else None

    exh, exh_s, exh_why = detect_exhaustion(
        prior, zone, n_bars=exhaustion_bars, mode=exhaustion_mode,
        require_declining_volume=require_declining_volume, flow=prior_flow)

    flip, flip_s, flip_why = detect_flip(
        bars, zone, atr_mult=flip_atr_mult, volume_mult=flip_volume_mult,
        window_bars=flip_window_bars, flow=use_flow)

    confirmed = exh and flip
    detail = f"exhaustion: {exh_why} | flip: {flip_why}"
    return ConfirmationRead(confirmed, exh, flip, mode, detail,
                            exhaustion_strength=exh_s, flip_strength=flip_s)
