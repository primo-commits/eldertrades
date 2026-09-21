"""
Indicators, vectorised on pandas.

The headline fix is session-anchored VWAP (E14). The original accumulated
price*volume from the first bar of a 10-30 day fetch, producing a multi-week
cumulative average that it then used as an intraday profit target.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import session_id


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return atr(df, period) / df["close"]


def vwap(df: pd.DataFrame, tz: str = "America/New_York") -> pd.Series:
    """
    Session-anchored VWAP -- resets at each trading day's first bar.

    Typical price is (H+L+C)/3 weighted by bar volume. On the IEX feed this is
    computed from a small share of consolidated volume and will not match a
    charting platform's VWAP; see Config.warnings().
    """
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    sess = session_id(df.index, tz)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).groupby(sess.values).cumsum()
    vol = df["volume"].groupby(sess.values).cumsum()
    return (pv / vol.replace(0, np.nan)).ffill()


def rolling_vwap(df: pd.DataFrame, window: int) -> pd.Series:
    """Anchorless VWAP over a trailing window -- useful for 24/7 crypto."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).rolling(window).sum()
    vol = df["volume"].rolling(window).sum()
    return pv / vol.replace(0, np.nan)


def swing_highs(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.Series:
    """Fractal swing highs. Uses only confirmed pivots (shifted by `right`)."""
    h = df["high"]
    cond = pd.Series(True, index=df.index)
    for i in range(1, left + 1):
        cond &= h > h.shift(i)
    for i in range(1, right + 1):
        cond &= h > h.shift(-i)
    return h.where(cond).shift(right)


def swing_lows(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.Series:
    low = df["low"]
    cond = pd.Series(True, index=df.index)
    for i in range(1, left + 1):
        cond &= low < low.shift(i)
    for i in range(1, right + 1):
        cond &= low < low.shift(-i)
    return low.where(cond).shift(right)


def relative_volume(df: pd.DataFrame, window: int = 20) -> pd.Series:
    avg = df["volume"].rolling(window).mean()
    return df["volume"] / avg.replace(0, np.nan)
