"""
Volume profile -- Elder's context layer.

    "The next thing is going to be even more important, which is going to be the
     volume profile. And this is what's going to give me the overall arching
     theme or where the value is sitting for the current session."

Built on 30-minute bars per the transcript. Each bar's volume is distributed
uniformly across its high-low range into price bins; that is the standard
bar-based approximation. With `feed: sip` you can instead pass classified ticks
from `orderflow.volume_at_price`, which is exact.

Concepts implemented, in his terms:
  POC  -- "the fair value is going to be the magnet"
  VA   -- "68% of the total transaction volume of the given session"
  HVN  -- "acceptance, heavy business... price often is going to slow down"
  LVN  -- "price often moves through these areas faster"
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import session_id


@dataclass
class Profile:
    """Volume distribution across price for one window."""
    levels: pd.Series          # index = price level, value = volume
    poc: float
    vah: float
    val: float
    hvn: list[float]
    lvn: list[float]
    total_volume: float

    @property
    def value_area(self) -> tuple[float, float]:
        return self.val, self.vah

    def contains(self, price: float) -> bool:
        """Is price inside the value area? -> balanced/rotational regime."""
        return self.val <= price <= self.vah

    def position_of(self, price: float) -> str:
        if price > self.vah:
            return "above_value"
        if price < self.val:
            return "below_value"
        return "in_value"

    def nearest_lvn(self, price: float) -> float | None:
        return min(self.lvn, key=lambda x: abs(x - price)) if self.lvn else None

    def nearest_hvn(self, price: float) -> float | None:
        return min(self.hvn, key=lambda x: abs(x - price)) if self.hvn else None


def build_profile(bars: pd.DataFrame, *, bins: int = 100, value_area_pct: float = 0.68,
                  hvn_percentile: float = 60, lvn_percentile: float = 30) -> Profile | None:
    """
    Distribute bar volume across price to produce a profile.

    Returns None when there is not enough data rather than a misleading profile.
    """
    if bars is None or bars.empty or len(bars) < 2:
        return None
    lo = float(bars["low"].min())
    hi = float(bars["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    acc = np.zeros(bins)

    # Spread each bar's volume evenly over the bins its range covers.
    for low, high, vol in zip(bars["low"].values, bars["high"].values, bars["volume"].values):
        if not np.isfinite(vol) or vol <= 0:
            continue
        i0 = max(0, int(np.searchsorted(edges, low, side="right") - 1))
        i1 = min(bins - 1, int(np.searchsorted(edges, high, side="left")))
        if i1 < i0:
            i0 = i1 = min(max(i0, 0), bins - 1)
        acc[i0:i1 + 1] += vol / (i1 - i0 + 1)

    levels = pd.Series(acc, index=centers, name="volume")
    total = float(levels.sum())
    if total <= 0:
        return None

    poc_i = int(np.argmax(acc))
    poc = float(centers[poc_i])
    val, vah = _value_area(centers, acc, poc_i, value_area_pct)

    nonzero = acc[acc > 0]
    hvn_cut = float(np.percentile(nonzero, hvn_percentile)) if nonzero.size else 0.0
    lvn_cut = float(np.percentile(nonzero, lvn_percentile)) if nonzero.size else 0.0

    hvn = _cluster_peaks(centers, acc, acc >= hvn_cut)
    lvn = _cluster_peaks(centers, acc, (acc <= lvn_cut) & (acc > 0), pick="min")

    return Profile(levels=levels, poc=poc, vah=vah, val=val,
                   hvn=hvn, lvn=lvn, total_volume=total)


def _value_area(centers: np.ndarray, acc: np.ndarray, poc_i: int,
                pct: float) -> tuple[float, float]:
    """Expand from the POC toward whichever neighbour holds more volume."""
    target = acc.sum() * pct
    lo = hi = poc_i
    got = acc[poc_i]
    n = len(acc)
    while got < target and (lo > 0 or hi < n - 1):
        below = acc[lo - 1] if lo > 0 else -1.0
        above = acc[hi + 1] if hi < n - 1 else -1.0
        if above >= below:
            hi += 1
            got += above
        else:
            lo -= 1
            got += below
    return float(centers[lo]), float(centers[hi])


def _cluster_peaks(centers: np.ndarray, acc: np.ndarray, mask: np.ndarray,
                   pick: str = "max") -> list[float]:
    """
    Collapse each contiguous run of qualifying bins to one representative price,
    so a wide shelf reports one node rather than twenty adjacent ones.
    """
    out: list[float] = []
    i = 0
    n = len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        seg = acc[i:j + 1]
        k = int(np.argmax(seg)) if pick == "max" else int(np.argmin(seg))
        out.append(float(centers[i + k]))
        i = j + 1
    return out


# ── session profiles ────────────────────────────────────────────────────────

def session_profiles(bars: pd.DataFrame, *, tz: str = "America/New_York",
                     **kw) -> dict:
    """One profile per trading day, in chronological order."""
    if bars.empty:
        return {}
    sess = session_id(bars.index, tz)
    out = {}
    for day, chunk in bars.groupby(sess.values):
        prof = build_profile(chunk, **kw)
        if prof is not None:
            out[day] = prof
    return out


def poc_trend(profiles: dict, lookback: int = 3) -> str:
    """
    Elder's session-over-session POC read:

        "every single day we had lower and lower points of control... it
         indicates a great continuation of a shift down"

    Returns "bullish" | "bearish" | "neutral".
    """
    if len(profiles) < 2:
        return "neutral"
    days = sorted(profiles)[-lookback:]
    pocs = [profiles[d].poc for d in days]
    if len(pocs) < 2:
        return "neutral"
    if all(b > a for a, b in zip(pocs, pocs[1:])):
        return "bullish"
    if all(b < a for a, b in zip(pocs, pocs[1:])):
        return "bearish"
    return "neutral"


def value_area_breakout(bars: pd.DataFrame, profile: Profile, *,
                        bars_required: int = 2, volume_mult: float = 1.5,
                        avg_window: int = 20) -> str:
    """
    Acceptance outside value, not just a poke.

        "breaking out of these value areas will create a very, very high
         probable setup"

    Requires `bars_required` consecutive closes beyond the edge AND elevated
    volume, so a single spike through does not qualify.

    Returns "bullish" | "bearish" | "none".
    """
    if bars.empty or len(bars) < bars_required:
        return "none"
    recent = bars.tail(bars_required)
    avg_vol = float(bars["volume"].tail(avg_window).mean())
    if avg_vol <= 0:
        return "none"
    vol_ok = float(recent["volume"].mean()) > avg_vol * volume_mult
    if not vol_ok:
        return "none"
    if (recent["close"] > profile.vah).all():
        return "bullish"
    if (recent["close"] < profile.val).all():
        return "bearish"
    return "none"
