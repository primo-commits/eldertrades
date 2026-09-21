"""
Alpaca market data, done correctly.

Fixes four defects from the original implementation:

E2  No `limit=` on bar requests. Alpaca returns bars ascending from `start`, so
    a limit truncates the NEWEST data. The original asked for 30 days of 1-min
    crypto bars with limit=15000 and got the oldest ~10.4 days -- meaning the
    bot's "current price" was roughly 20 days stale. The SDK paginates; bound
    the window with start/end instead.

E7  Session filtering is done in America/New_York, not on raw UTC hours. The
    original kept UTC 09:00-15:59, which is 05:00-11:59 ET -- four hours of
    pre-market plus the first 2.5 hours of RTH, and zero afternoon data.

E13 Timeframes are requested natively from Alpaca (or resampled with real
    timestamps) instead of chunking N consecutive available 1-min bars. Alpaca
    omits no-trade minutes, so the original's "4H bar" was 240 arbitrary bars
    that could silently span two days and straddle the overnight gap.

E14 VWAP is anchored to each RTH session open, not accumulated from the first
    bar of the entire fetch.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

import pandas as pd

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import Adjustment, DataFeed

from .keys import load_keys

log = logging.getLogger(__name__)

OHLCV = ["open", "high", "low", "close", "volume"]

_UNITS = {
    "min": TimeFrameUnit.Minute, "minute": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour, "h": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day, "d": TimeFrameUnit.Day,
    "week": TimeFrameUnit.Week, "month": TimeFrameUnit.Month,
}


def parse_timeframe(spec: str) -> TimeFrame:
    """'5Min' -> TimeFrame(5, Minute). '1Hour' -> TimeFrame(1, Hour)."""
    s = spec.strip()
    digits = "".join(c for c in s if c.isdigit())
    unit = "".join(c for c in s if c.isalpha()).lower()
    if not digits or unit not in _UNITS:
        raise ValueError(f"Bad timeframe {spec!r}. Try '5Min', '15Min', '1Hour', '1Day'.")
    return TimeFrame(int(digits), _UNITS[unit])


def pandas_rule(spec: str) -> str:
    """'4Hour' -> '4h' for pandas resample."""
    digits = "".join(c for c in spec if c.isdigit()) or "1"
    unit = "".join(c for c in spec if c.isalpha()).lower()
    return f"{digits}{'min' if unit.startswith('min') else 'h' if unit.startswith('h') else 'D'}"


class MarketData:
    """Thin wrapper over the two Alpaca data clients."""

    def __init__(self, feed: str = "iex", session_tz: str = "America/New_York",
                 keyfile: str | None = None):
        key, secret = load_keys(keyfile)
        self.stock = StockHistoricalDataClient(key, secret)
        self.crypto = CryptoHistoricalDataClient(key, secret)
        self.feed = DataFeed(feed.lower())
        self.tz = session_tz

    # ── bars ────────────────────────────────────────────────────────────────
    def bars(self, symbols: Iterable[str], timeframe: str, *,
             start: dt.datetime, end: dt.datetime | None = None,
             crypto: bool = False) -> dict[str, pd.DataFrame]:
        """
        Fetch bars for many symbols in one paginated request.

        Deliberately passes no `limit` -- see E2 in the module docstring.
        Returns {symbol: DataFrame} indexed by tz-aware timestamps in session_tz.
        """
        symbols = list(symbols)
        if not symbols:
            return {}
        tf = parse_timeframe(timeframe)
        end = end or dt.datetime.now(dt.timezone.utc)

        if crypto:
            req = CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=tf,
                                    start=start, end=end)
            raw = self.crypto.get_crypto_bars(req).df
        else:
            req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf,
                                   start=start, end=end, feed=self.feed,
                                   adjustment=Adjustment.SPLIT)
            raw = self.stock.get_stock_bars(req).df

        return self._split_by_symbol(raw, symbols)

    def _split_by_symbol(self, raw: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return {s: pd.DataFrame(columns=OHLCV) for s in symbols}
        for sym in symbols:
            try:
                df = raw.xs(sym, level="symbol").copy()
            except (KeyError, TypeError):
                out[sym] = pd.DataFrame(columns=OHLCV)
                continue
            idx = pd.to_datetime(df.index, utc=True)
            df.index = idx.tz_convert(self.tz)
            df.index.name = "timestamp"
            keep = [c for c in OHLCV if c in df.columns]
            out[sym] = df[keep].sort_index()
        return out

    # ── daily ADV for liquidity-aware sizing ────────────────────────────────
    def adv_notional(self, symbols: Iterable[str], days: int = 20,
                     crypto: bool = False) -> dict[str, float]:
        """20-day average daily volume expressed in dollars."""
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=days * 2 + 10)   # padding for holidays
        daily = self.bars(symbols, "1Day", start=start, end=end, crypto=crypto)
        out = {}
        for sym, df in daily.items():
            if df.empty:
                out[sym] = 0.0
                continue
            tail = df.tail(days)
            out[sym] = float((tail["close"] * tail["volume"]).mean())
        return out


# ── session handling ────────────────────────────────────────────────────────

def regular_session(df: pd.DataFrame, *, tz: str = "America/New_York",
                    open_time: str = "09:30", close_time: str = "16:00",
                    skip_first_minutes: int = 0,
                    skip_last_minutes: int = 0) -> pd.DataFrame:
    """
    Keep only regular-hours bars, evaluated in exchange-local time so DST is
    handled automatically. Optionally trim the open and close.
    """
    if df.empty:
        return df
    local = df.tz_convert(tz) if df.index.tz is not None else df.tz_localize("UTC").tz_convert(tz)
    o = dt.time.fromisoformat(open_time)
    c = dt.time.fromisoformat(close_time)

    def _shift(t: dt.time, minutes: int) -> dt.time:
        base = dt.datetime.combine(dt.date(2000, 1, 1), t) + dt.timedelta(minutes=minutes)
        return base.time()

    lo = _shift(o, skip_first_minutes)
    hi = _shift(c, -skip_last_minutes)
    times = local.index.time
    mask = (times >= lo) & (times < hi) & (local.index.dayofweek < 5)
    return local[mask]


def session_id(index: pd.DatetimeIndex, tz: str = "America/New_York") -> pd.Series:
    """Trading-day label for each bar -- the anchor for session VWAP."""
    local = index.tz_convert(tz) if index.tz is not None else index.tz_localize("UTC").tz_convert(tz)
    return pd.Series(local.date, index=index, name="session")


def resample_ohlcv(df: pd.DataFrame, rule: str, *, origin: str | pd.Timestamp = "start_day",
                   offset: str | None = None) -> pd.DataFrame:
    """
    Resample with real timestamps, so gaps stay gaps.

    `origin`/`offset` let a 4H bar be anchored to the 09:30 RTH open rather than
    UTC midnight -- without this, a "4H" bar straddles the overnight break.
    """
    if df.empty:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    agg = {k: v for k, v in agg.items() if k in df.columns}
    out = df.resample(rule, origin=origin, offset=offset, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


def session_anchored(df: pd.DataFrame, rule: str, *, tz: str = "America/New_York",
                     open_time: str = "09:30") -> pd.DataFrame:
    """
    Resample each trading day independently, anchored to that day's RTH open.
    This is how a session-aligned 4H bias chart is built for equities.
    """
    if df.empty:
        return df
    local = df.tz_convert(tz) if df.index.tz is not None else df.tz_localize("UTC").tz_convert(tz)
    o = dt.time.fromisoformat(open_time)
    frames = []
    for day, chunk in local.groupby(local.index.date):
        anchor = pd.Timestamp(dt.datetime.combine(day, o), tz=tz)
        frames.append(resample_ohlcv(chunk, rule, origin=anchor))
    return pd.concat(frames).sort_index() if frames else local.iloc[0:0]
