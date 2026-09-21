"""
Order-flow proxy built from Alpaca trades and quotes.

WHAT THIS IS NOT
----------------
Elder Santis trades ES/NQ futures using footprint charts, DOM and Bookmap --
that is CME depth-of-book: resting limit orders at every price level, order
additions and cancellations, queue position. Alpaca does not sell that, and it
does not exist for equities in the same form (US equities are fragmented across
~16 venues with no consolidated book feed at this price point).

What we can honestly reconstruct from Alpaca's trade and quote streams:

  * trade-side classification  -- was the aggressor a buyer or a seller?
  * cumulative volume delta    -- net aggressive buying minus selling
  * volume-at-price            -- a footprint-like profile per price level
  * absorption                 -- heavy one-sided volume that fails to move price

That last one is the real signal. Absorption in the order-flow sense means
aggressive orders hitting resting size and price NOT moving. We cannot see the
resting size, but we can see the aggressive volume and the price response, and
the ratio of the two is a defensible proxy.

The original code's `detect_absorption()` used last-bar volume over a 20-bar
average plus a wick check. That is a volume-ratio heuristic on OHLCV bars; it
contains no directional information at all.

FEED WARNING
------------
On the IEX feed this module reads roughly 2% of consolidated volume, and that
2% is not a random sample. Delta computed from it is close to meaningless.
Use feed: sip.
"""
from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd

from alpaca.data.requests import StockQuotesRequest, StockTradesRequest

log = logging.getLogger(__name__)

BUY, SELL, UNKNOWN = 1, -1, 0


def fetch_trades_quotes(md, symbol: str, start: dt.datetime,
                        end: dt.datetime | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull raw trades and quotes for one symbol over a window."""
    end = end or dt.datetime.now(dt.timezone.utc)
    t_req = StockTradesRequest(symbol_or_symbols=[symbol], start=start, end=end, feed=md.feed)
    q_req = StockQuotesRequest(symbol_or_symbols=[symbol], start=start, end=end, feed=md.feed)
    trades = md.stock.get_stock_trades(t_req).df
    quotes = md.stock.get_stock_quotes(q_req).df

    def _prep(df):
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(md.tz)
        return df.sort_index()

    return _prep(trades), _prep(quotes)


def classify_trades(trades: pd.DataFrame, quotes: pd.DataFrame,
                    method: str = "quote_rule") -> pd.DataFrame:
    """
    Tag each trade with an aggressor side.

    quote_rule (Lee-Ready): a trade at or above the ask is buyer-initiated, at
    or below the bid is seller-initiated, and one inside the spread falls back
    to the tick rule (compare to the previous different price).

    Each trade is matched to the quote prevailing at or before its timestamp
    via merge_asof, which is the standard approach.
    """
    if trades.empty:
        return trades.assign(side=pd.Series(dtype=int))

    out = trades.copy()

    if method == "quote_rule" and not quotes.empty:
        q = quotes[["bid_price", "ask_price"]].reset_index()
        t = out.reset_index()
        ts_col, q_ts = t.columns[0], q.columns[0]
        merged = pd.merge_asof(
            t.sort_values(ts_col), q.sort_values(q_ts),
            left_on=ts_col, right_on=q_ts, direction="backward",
        )
        mid = (merged["bid_price"] + merged["ask_price"]) / 2.0
        side = np.where(merged["price"] > mid, BUY,
               np.where(merged["price"] < mid, SELL, UNKNOWN))
        merged["side"] = side
        # Inside-the-spread / at-mid trades fall back to the tick rule.
        need = merged["side"] == UNKNOWN
        if need.any():
            merged.loc[need, "side"] = _tick_rule(merged["price"])[need]
        out = merged.set_index(ts_col)
    else:
        out["side"] = _tick_rule(out["price"])

    out["signed_size"] = out["side"] * out["size"]
    return out


def _tick_rule(price: pd.Series) -> pd.Series:
    """Uptick -> buy, downtick -> sell, flat -> carry the last classification."""
    diff = price.diff()
    side = pd.Series(np.sign(diff).fillna(0).astype(int), index=price.index)
    return side.replace(0, np.nan).ffill().fillna(BUY).astype(int)


def cumulative_delta(classified: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """
    Per-bar and cumulative volume delta.

    delta      -- aggressive buy volume minus aggressive sell volume in the bar
    cum_delta  -- running total across the window
    """
    if classified.empty:
        return pd.DataFrame(columns=["delta", "volume", "cum_delta", "delta_pct"])
    g = classified.resample(freq)
    out = pd.DataFrame({
        "delta":  g["signed_size"].sum(),
        "volume": g["size"].sum(),
    })
    out["cum_delta"] = out["delta"].cumsum()
    out["delta_pct"] = out["delta"] / out["volume"].replace(0, np.nan)
    return out


def volume_at_price(classified: pd.DataFrame, bin_size: float) -> pd.DataFrame:
    """
    Footprint-style profile: traded volume split by aggressor side at each price
    level. `bin_size` is typically one tick ($0.01 for most US equities).
    """
    if classified.empty or bin_size <= 0:
        return pd.DataFrame(columns=["buy_volume", "sell_volume", "total", "delta"])
    level = (classified["price"] / bin_size).round() * bin_size
    grouped = classified.assign(level=level).groupby("level")
    buy = grouped.apply(lambda d: d.loc[d["side"] == BUY, "size"].sum(), include_groups=False)
    sell = grouped.apply(lambda d: d.loc[d["side"] == SELL, "size"].sum(), include_groups=False)
    prof = pd.DataFrame({"buy_volume": buy, "sell_volume": sell})
    prof["total"] = prof["buy_volume"] + prof["sell_volume"]
    prof["delta"] = prof["buy_volume"] - prof["sell_volume"]
    return prof.sort_index()


def point_of_control(profile: pd.DataFrame) -> float | None:
    """Price level with the most traded volume."""
    return None if profile.empty else float(profile["total"].idxmax())


def value_area(profile: pd.DataFrame, pct: float = 0.70) -> tuple[float, float] | None:
    """
    Value area: the contiguous price range around the POC holding `pct` of
    volume. Expands to whichever adjacent level has more volume, the standard
    construction.
    """
    if profile.empty:
        return None
    levels = profile.sort_index()
    target = levels["total"].sum() * pct
    poc_idx = int(np.argmax(levels["total"].values))
    lo = hi = poc_idx
    acc = levels["total"].iloc[poc_idx]
    vals = levels["total"].values
    while acc < target and (lo > 0 or hi < len(vals) - 1):
        below = vals[lo - 1] if lo > 0 else -1
        above = vals[hi + 1] if hi < len(vals) - 1 else -1
        if above >= below:
            hi += 1
            acc += above
        else:
            lo -= 1
            acc += below
    return float(levels.index[lo]), float(levels.index[hi])


def absorption_score(delta_df: pd.DataFrame, bars: pd.DataFrame,
                     lookback: int = 5) -> float:
    """
    Absorption proxy in [0, 1]: heavy, one-sided aggressive volume that fails to
    move price.

    Rationale: if aggressive sellers dump size and price barely falls, resting
    bids are absorbing it -- a demand signal. High |delta| with low price
    displacement per unit of volume scores high.

    Returns 0.0 when there is not enough data rather than guessing.
    """
    if delta_df.empty or bars.empty or len(delta_df) < lookback:
        return 0.0

    recent = delta_df.tail(lookback)
    total_vol = float(recent["volume"].sum())
    if total_vol <= 0:
        return 0.0

    # How one-sided was the flow?
    imbalance = abs(float(recent["delta"].sum())) / total_vol      # 0..1

    # How far did price actually travel, in ATR terms?
    px = bars["close"].tail(lookback)
    if len(px) < 2:
        return 0.0
    rng = float(px.max() - px.min())
    ref = float(bars["close"].iloc[-1]) * 0.001                    # 10bp reference
    displacement = min(1.0, rng / ref) if ref > 0 else 1.0

    # One-sided flow + little movement = absorption.
    return round(float(imbalance * (1.0 - displacement)), 4)


def delta_divergence(delta_df: pd.DataFrame, bars: pd.DataFrame, lookback: int = 10) -> str:
    """
    'bullish' when price makes a lower low but cumulative delta does not
    (sellers exhausted), 'bearish' for the mirror case, else 'none'.
    """
    if delta_df.empty or len(bars) < lookback or len(delta_df) < lookback:
        return "none"
    px, cd = bars["close"].tail(lookback), delta_df["cum_delta"].tail(lookback)
    price_down = px.iloc[-1] < px.iloc[0]
    delta_up = cd.iloc[-1] > cd.iloc[0]
    if price_down and delta_up:
        return "bullish"
    if (px.iloc[-1] > px.iloc[0]) and (cd.iloc[-1] < cd.iloc[0]):
        return "bearish"
    return "none"
