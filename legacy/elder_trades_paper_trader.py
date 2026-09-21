"""
Elder Trades — Multi-Timeframe Paper Trader
============================================
Implements Elder Trades' 6-step methodology on Alpaca paper trading.

GFT Instant GOAT Sizing ($100K account):
  Account: $100,000  |  Max risk/trade: $1,500 (1.5%)
  Daily DD limit: $3,000  |  Floating loss floor: $97,000
  Consistency cap: 15% of total profit per day  |  Unlimited trades/day

Strategy: 4H bias → 1H/30M zones → 5M entry trigger
  Longs only when 4H is bullish, shorts only when bearish
  Entries at S/D zones with 5M structure shift + absorption
  Target: VWAP  |  Stop: beyond zone invalidation

Platform: Alpaca Paper Trading
Symbols:  QQQ/SPY/IWM (stocks) or BTC/USD (crypto via --crypto)

Run:
    python elder_trades_paper_trader.py [--crypto --live --balance 100000] [--interval 5]
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Dependencies ──────────────────────────────────────────────────────────────
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    HAS_ALPACA = True
except ImportError:
    os.system("pip install alpaca-py -q")
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    HAS_ALPACA = True

# ── Keys ─────────────────────────────────────────────────────────────────────
def _load_keys() -> tuple:
    """Load Alpaca API keys from alpaca_keys.txt in the same folder."""
    keys_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alpaca_keys.txt")
    key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
    if key and secret:
        return key, secret
    if os.path.isfile(keys_file):
        with open(keys_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("API_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key or not secret:
        print("ERROR: No Alpaca API keys found.")
        print("  Set ALPACA_PAPER_KEY and ALPACA_PAPER_SECRET env vars, or")
        print("  create alpaca_keys.txt in this folder with:")
        print("    API_KEY=your_key_here")
        print("    API_SECRET=your_secret_here")
        sys.exit(1)
    return key, secret

_API_KEY, _API_SECRET = _load_keys()
API_KEY    = _API_KEY
API_SECRET = _API_SECRET
PAPER_BASE = "https://paper-api.alpaca.markets"

# ── GFT Account Constants ─────────────────────────────────────────────────────
GFT_ACCOUNT_SIZE    = 100_000.00   # $100K paper trading account
GFT_MAX_RISK_PCT    = 0.015    # 1.5% per trade → $1,500/trade max risk
GFT_MAX_RISK_DOLLAR = 1_500.00 # $1,500 max risk per trade (1.5% of $100K)
GFT_DAILY_DD_LIMIT  = 3_000.00  # $3,000 daily drawdown limit (3% of $100K)
GFT_FLOAT_FLOOR    = 97_000.00  # $97,000 hard floor (97% of $100K — trailing from peak)
GFT_POSITION_MAX    = 2_500.00  # $2,500 max notional per position (0.25% of $100K)
GFT_MAX_DD_PCT      = 0.05    # 5% — trailing drawdown cap (max total DD from peak)
CONSISTENCY_CAP     = 0.15    # 15% — no single day > 15% of total profit

# ── Strategy Constants ────────────────────────────────────────────────────────
EMA_4H_PERIOD    = 20    # 4H EMA for trend bias
EMA_1H_PERIOD    = 20    # 1H EMA
EMA_5M_PERIOD    = 9     # 5M EMA for short-term
ZONE_LOOKBACK    = 30    # candles back to find the impulse origin
ATR_PERIOD       = 14
VWAP_ATR_MULT    = 1.5   # TP = VWAP + 1.5 × ATR for longs
STOP_ATR_MULT    = 1.5   # SL = zone boundary + 1.5 × ATR

# ── Globals ──────────────────────────────────────────────────────────────────
DATA_CLIENT    = None
TRADING_CLIENT = None
USE_CRYPTO     = False   # Set True via --crypto flag to use BTC/USD
PAPER_BALANCE  = GFT_ACCOUNT_SIZE  # tracks simulated paper balance
DAILY_PNL      = 0.0
DAILY_HIGH     = PAPER_BALANCE
ALL_TIME_PEAK  = PAPER_BALANCE
TRADE_LOG      = []
CONSISTENCY_LOG = {}   # date -> pnl
DRY_RUN        = True

@dataclass
class Bar:
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass
class SupplyDemandZone:
    zone_type: str          # "supply" or "demand"
    top: float
    bottom: float
    origin_timestamp: dt.datetime
    impulse_candle_timestamp: dt.datetime
    strength: float        # 0–1, based on candle size vs ATR
    valid: bool = True

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.top - self.bottom


@dataclass
class Trade:
    id: int
    timestamp: dt.datetime
    symbol: str
    side: str               # "buy" or "sell"
    entry_price: float
    qty: int
    stop_loss: float
    take_profit: float
    zone_top: float
    zone_bottom: float
    pnl: float = 0.0
    status: str = "open"    # open / closed / cancelled
    exit_price: float = 0.0
    exit_time: Optional[dt.datetime] = None
    rr: float = 0.0
    session: str = "elder_trades"


# ─────────────────────────────────────────────────────────────────────────────
# DATAFetching
# ─────────────────────────────────────────────────────────────────────────────

def get_clients(use_crypto: bool = False):
    """Return (data_client, trading_client). Uses CryptoHistoricalDataClient for BTC."""
    global DATA_CLIENT, TRADING_CLIENT
    if DATA_CLIENT is None or (use_crypto and not hasattr(DATA_CLIENT, 'get_crypto_bars')):
        if use_crypto:
            from alpaca.data.historical import CryptoHistoricalDataClient
            DATA_CLIENT = CryptoHistoricalDataClient(API_KEY, API_SECRET)
        else:
            DATA_CLIENT = StockHistoricalDataClient(API_KEY, API_SECRET)
    if TRADING_CLIENT is None:
        TRADING_CLIENT = TradingClient(API_KEY, API_SECRET, paper=True)
    return DATA_CLIENT, TRADING_CLIENT


def fetch_bars(
    symbol: str,
    timeframe: TimeFrame,
    start: dt.datetime,
    end: dt.datetime,
    limit: int = 1000,
    use_crypto: bool = False,
) -> list[Bar]:
    """Fetch bars from Alpaca (stocks or crypto) and return as Bar dataclass list."""
    client, _ = get_clients(use_crypto=use_crypto)
    if use_crypto:
        req = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        resp = client.get_crypto_bars(req)
    else:
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            feed="iex",
        )
        resp = client.get_stock_bars(req)
    raw_bars = resp.data.get(symbol, [])
    out = []
    for b in raw_bars:
        # Handle both dict-style and object-style bars (different Alpaca versions)
        if isinstance(b, dict):
            ts, o, h, l, c, v = b["timestamp"], b["open"], b["high"], b["low"], b["close"], b["volume"]
        else:
            ts, o, h, l, c, v = b.timestamp, b.open, b.high, b.low, b.close, b.volume
        out.append(Bar(timestamp=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=int(v)))
    return out


def composite_to_timeframe(bars: list[Bar], period: int) -> list[Bar]:
    """Composite 1-min bars into N-minute bars."""
    if not bars:
        return []
    composite = []
    i = 0
    while i < len(bars):
        chunk = bars[i:i + period]
        if not chunk:
            break
        composite.append(Bar(
            timestamp=chunk[0].timestamp,
            open=chunk[0].open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=chunk[-1].close,
            volume=sum(c.volume for c in chunk),
        ))
        i += period
    return composite


def get_multi_timeframe_bars(symbol: str, days_back: int = 60, use_crypto: bool = False) -> dict:
    """
    Fetch multi-timeframe bars for Elder Trades strategy.
    Returns: { '5M': [...], '15M': [...], '1H': [...], '4H': [...], 'daily': [...] }
    """
    end   = dt.datetime.utcnow()
    start = end - dt.timedelta(days=days_back)

    # Alpaca IEX gives 1-min bars — composite to desired timeframes
    print(f"  Fetching 1-min bars for {symbol} ({days_back} days)...")
    raw = fetch_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), start, end,
                     limit=15000, use_crypto=use_crypto)
    print(f"  Got {len(raw)} raw 1-min bars")

    if len(raw) < 60:
        print(f"  WARNING: only {len(raw)} bars — check API key and data feed")
        return {}

    # 5M: composite every 5 bars
    m5  = composite_to_timeframe(raw, 5)
    # 15M: composite every 15 bars
    m15 = composite_to_timeframe(raw, 15)
    # 1H: composite every 60 bars
    h1  = composite_to_timeframe(raw, 60)
    # 4H: composite every 240 bars
    h4  = composite_to_timeframe(raw, 240)

    # Filter only the 5M for intraday session validity — 4H/1H need full history for trend
    # Crypto is 24/7 so skip the market-hours filter
    def market_hours(bars):
        if use_crypto:
            return bars
        return [b for b in bars if b.timestamp.hour >= 9 and b.timestamp.hour < 16
                and b.timestamp.weekday() < 5]

    return {
        "5M":    market_hours(m5),   # 5M: only during market hours (all bars for crypto)
        "15M":   m15,                # keep all — needed for zone context
        "1H":    h1,                # keep all — zone detection uses full history
        "4H":    h4,                # keep all — trend bias needs every bar
        "raw":   raw,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Technical Indicators
# ─────────────────────────────────────────────────────────────────────────────

def ema(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return [0.0] * len(closes)
    k = 2 / (period + 1)
    result = [0.0] * (period - 1)
    result.append(closes[period - 1])
    for i in range(period, len(closes)):
        result.append(result[-1] * (1 - k) + closes[i] * k)
    return result


def atr(bars: list[Bar], period: int = 14) -> list[float]:
    if len(bars) < period + 1:
        return [0.0] * len(bars)
    trs = [0.0] * (period + 1)
    trs[0] = bars[0].high - bars[0].low
    for i in range(1, min(period + 1, len(bars))):
        trs[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low  - bars[i - 1].close),
        )
    for i in range(period + 1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low  - bars[i - 1].close),
        )
        trs.append(tr)
    result = [0.0] * period
    result.append(sum(trs[1:period + 1]) / period)
    for i in range(period + 1, len(trs)):
        result.append((result[-1] * (period - 1) + trs[i]) / period)
    return result


def vwap_bars(bars: list[Bar]) -> list[float]:
    """Calculate VWAP from bars with HLC + volume."""
    if not bars:
        return []
    cum_pv = 0.0
    cum_vol = 0.0
    result = []
    for b in bars:
        typical = (b.high + b.low + b.close) / 3
        pv = typical * b.volume
        cum_pv += pv
        cum_vol += b.volume
        result.append(cum_pv / cum_vol if cum_vol > 0 else typical)
    return result


def highest(bars: list[Bar], period: int) -> list[float]:
    result = []
    for i in range(len(bars)):
        if i < period - 1:
            result.append(max(b.open, b.close) if i > 0 else bars[i].high)
        else:
            result.append(max(bars[i - period + 1:i + 1], key=lambda x: x.high).high)
    return result


def lowest(bars: list[Bar], period: int) -> list[float]:
    result = []
    for i in range(len(bars)):
        if i < period - 1:
            result.append(min(b.open, b.close) if i > 0 else bars[i].low)
        else:
            result.append(min(bars[i - period + 1:i + 1], key=lambda x: x.low).low)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Supply & Demand Zone Detection (Elder Trades rules)
# ─────────────────────────────────────────────────────────────────────────────

def find_supply_demand_zones(bars: list[Bar], lookback: int = 30,
                               atr_vals: list[float] = None,
                               min_bars: int = 3) -> list[SupplyDemandZone]:
    """
    Elder Trades zone rules:
    - Supply zone: consolidation before a bearish impulse candle
        Zone bottom = body bottom of consolidation candle
        Zone top    = wick top of consolidation candle
    - Demand zone: consolidation before a bullish impulse candle
        Zone bottom = wick bottom of consolidation candle
        Zone top    = body top of consolidation candle

    Also handles multi-candle consolidation:
        Zone top    = highest high of consolidation
        Zone bottom = lowest low of consolidation
    """
    zones = []
    if len(bars) < lookback + 5:
        return zones

    current_atr = atr_vals[-1] if atr_vals else bars[-1].high - bars[-1].low

    for i in range(lookback, len(bars) - min_bars):
        # Look for a strong impulse candle
        impulse = bars[i]
        impulse_body = abs(impulse.close - impulse.open)
        impulse_range = impulse.high - impulse.low

        # Strong impulse = body > 60% of range (most of candle moved in one direction)
        if impulse_range < 0.001 * impulse.close:  # too small
            continue

        body_ratio = impulse_body / impulse_range if impulse_range > 0 else 0
        if impulse_range < current_atr * 0.5:  # too small relative to ATR
            continue

        # Examine consolidation before the impulse
        for j in range(i - 1, max(i - lookback, i - 15), -1):
            consolidation = bars[j:i]  # candles before the impulse
            if len(consolidation) < min_bars:
                continue

            # Find the origin candle — the one that precedes the big move
            if impulse.is_bearish:
                # Supply zone: look for consolidation before bearish impulse
                # Strong bearish = close well below open
                if not impulse.is_bearish:
                    continue
                zone_top    = max(c.high for c in consolidation)   # top wick
                zone_bottom = min(c.body_bottom for c in consolidation)  # body bottom
                zone_top    = max(zone_top, impulse.high)         # also include impulse wick

                zone_height = zone_top - zone_bottom
                if zone_height < 0.001 * impulse.close:
                    continue
                # Zone is valid only if it hasn't been breached
                # Check bars after the impulse
                post_impulse = bars[i + 1:min(i + 20, len(bars))]
                breached = any(
                    (b.close > zone_top and b.low < zone_top)
                    for b in post_impulse
                )
                zones.append(SupplyDemandZone(
                    zone_type="supply",
                    top=zone_top,
                    bottom=zone_bottom,
                    origin_timestamp=consolidation[0].timestamp,
                    impulse_candle_timestamp=impulse.timestamp,
                    strength=min(1.0, impulse_range / (current_atr * 3)),
                    valid=not breached,
                ))

            elif impulse.is_bullish:
                # Demand zone: consolidation before bullish impulse
                if not impulse.is_bullish:
                    continue
                zone_bottom = min(c.low for c in consolidation)  # bottom wick
                zone_top    = max(c.body_top for c in consolidation)  # body top
                zone_bottom = min(zone_bottom, impulse.low)        # also include impulse wick

                zone_height = zone_top - zone_bottom
                if zone_height < 0.001 * impulse.close:
                    continue
                post_impulse = bars[i + 1:min(i + 20, len(bars))]
                breached = any(
                    (b.close < zone_bottom and b.high > zone_bottom)
                    for b in post_impulse
                )
                zones.append(SupplyDemandZone(
                    zone_type="demand",
                    top=zone_top,
                    bottom=zone_bottom,
                    origin_timestamp=consolidation[0].timestamp,
                    impulse_candle_timestamp=impulse.timestamp,
                    strength=min(1.0, impulse_range / (current_atr * 3)),
                    valid=not breached,
                ))

    # Deduplicate overlapping zones
    zones = dedup_zones(zones)
    return zones


def dedup_zones(zones: list[SupplyDemandZone]) -> list[SupplyDemandZone]:
    """Merge zones that overlap significantly."""
    if not zones:
        return []
    zones = sorted(zones, key=lambda z: z.origin_timestamp)
    merged = [zones[0]]
    for z in zones[1:]:
        prev = merged[-1]
        overlap_top    = min(prev.top, z.top)
        overlap_bottom = max(prev.bottom, z.bottom)
        overlap_size   = overlap_top - overlap_bottom
        prev_size = prev.top - prev.bottom
        z_size    = z.top - z.bottom
        if overlap_size > min(prev_size, z_size) * 0.6:
            # Merge: take wider boundaries
            merged[-1] = SupplyDemandZone(
                zone_type=prev.zone_type,
                top=max(prev.top, z.top),
                bottom=min(prev.bottom, z.bottom),
                origin_timestamp=prev.origin_timestamp,
                impulse_candle_timestamp=z.impulse_candle_timestamp,
                strength=max(prev.strength, z.strength),
                valid=prev.valid and z.valid,
            )
        else:
            merged.append(z)
    return merged


def get_live_zones_1H(h1_bars: list[Bar], atr_vals_1h: list[float]) -> list[SupplyDemandZone]:
    """Find zones on the 1H — Elder Trades steps 2-3."""
    zones = find_supply_demand_zones(h1_bars, lookback=ZONE_LOOKBACK,
                                      atr_vals=atr_vals_1h, min_bars=3)
    return [z for z in zones if z.valid]


# ─────────────────────────────────────────────────────────────────────────────
# Trend Bias (Step 1 — 4H)
# ─────────────────────────────────────────────────────────────────────────────

def get_4h_trend_bias(h4_bars: list[Bar]) -> str:
    """
    Elder Trades Step 1: Read 4H market structure.
    Bullish = price above 20 EMA, EMA sloping up
    Bearish = price below 20 EMA, EMA sloping down
    Balanced = price near EMA, no clear slope

    Uses a 5-bar EMA slope average to smooth out noise from 2-bar comparison.
    """
    if len(h4_bars) < EMA_4H_PERIOD + 10:
        return "neutral"

    closes = [b.close for b in h4_bars]
    ema_vals = ema(closes, EMA_4H_PERIOD)

    latest_close = closes[-1]
    latest_ema   = ema_vals[-1]
    price_vs_ema = (latest_close - latest_ema) / latest_ema if latest_ema > 0 else 0

    # Smooth slope: average over last 5 bars instead of just 2
    slope_vals = []
    for i in range(2, min(6, len(ema_vals))):
        if ema_vals[-i] > 0:
            slope_vals.append((ema_vals[-i+1] - ema_vals[-i]) / ema_vals[-i])
    ema_slope = sum(slope_vals) / len(slope_vals) if slope_vals else 0

    # DEBUG output
    print(f"  DEBUG 4H: price={latest_close:.2f}  EMA={latest_ema:.2f}  gap={price_vs_ema*100:.3f}%  slope={ema_slope*100:.4f}%")

    # Thresholds: 0.3% price gap, 0.01% avg slope per 4H bar (lenient — catches gentle trends)
    if price_vs_ema > 0.003 and ema_slope > 0.0001:
        return "bullish"
    elif price_vs_ema < -0.003 and ema_slope < -0.0001:
        return "bearish"
    else:
        return "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Entry Signals (Step 5 — 5M structure shift)
# ─────────────────────────────────────────────────────────────────────────────

def detect_structure_shift_5m(m5_bars: list[Bar], atr_vals_5m: list[float],
                               bias: str) -> Optional[dict]:
    """
    Elder Trades Step 5: Wait for 5M structure shift + absorption.

    Bullish structure shift:
      - Price is in a demand zone
      - 5M forms: higher low + higher high
      - Absorption: candle comes in, buyers step in, price holds

    Bearish structure shift:
      - Price is in a supply zone
      - 5M forms: lower high + lower low
      - Absorption: candle comes in, sellers step in, price rejects

    Returns dict with entry signal or None.
    """
    if len(m5_bars) < EMA_5M_PERIOD + 5:
        return None

    closes = [b.close for b in m5_bars]
    ema_5m = ema(closes, EMA_5M_PERIOD)

    # Last 10 bars for short-term structure
    recent = m5_bars[-10:]
    recent_ema = ema_5m[-10:]

    # Structure check
    if bias == "bullish":
        # Higher low: last low > previous low
        lows = [b.low for b in recent]
        if len(lows) >= 4:
            higher_low    = lows[-1]  > lows[-3]
            above_ema     = closes[-1] > recent_ema[-1]
            momentum_up   = closes[-1] > closes[-3]

            if higher_low and above_ema and momentum_up:
                current_atr = atr_vals_5m[-1] if atr_vals_5m else (recent[-1].high - recent[-1].low)
                return {
                    "direction": "long",
                    "entry_price": closes[-1],
                    "structure": "higher_low_confirmed",
                    "atr": current_atr,
                    "ema_5m": recent_ema[-1],
                    "confidence": 0.7,
                }

    elif bias == "bearish":
        highs = [b.high for b in recent]
        if len(highs) >= 4:
            lower_high    = highs[-1] < highs[-3]
            below_ema     = closes[-1] < recent_ema[-1]
            momentum_down = closes[-1] < closes[-3]

            if lower_high and below_ema and momentum_down:
                current_atr = atr_vals_5m[-1] if atr_vals_5m else (recent[-1].high - recent[-1].low)
                return {
                    "direction": "short",
                    "entry_price": closes[-1],
                    "structure": "lower_high_confirmed",
                    "atr": current_atr,
                    "ema_5m": recent_ema[-1],
                    "confidence": 0.7,
                }

    return None


def detect_absorption(m5_bars: list[Bar], zone: SupplyDemandZone) -> float:
    """
    Detect absorption at a zone.
    Score 0-1 based on:
    - Volume at zone: is it above average?
    - Wick rejection: does price reject from zone?
    - Candle compression: is candle smaller than prior ones?
    """
    if len(m5_bars) < 5:
        return 0.0

    recent = m5_bars[-5:]
    avg_vol = sum(b.volume for b in m5_bars[-20:]) / min(20, len(m5_bars))
    vol_ratio = recent[-1].volume / avg_vol if avg_vol > 0 else 1.0

    # Check for wick rejection
    if zone.zone_type == "demand":
        in_zone = any(zone.bottom <= b.close <= zone.top for b in recent)
        wick_rejection = recent[-1].low < zone.bottom * 1.001
    else:
        in_zone = any(zone.bottom <= b.close <= zone.top for b in recent)
        wick_rejection = recent[-1].high > zone.top * 1.001

    # Volume spike at zone = absorption signal
    vol_score = min(1.0, vol_ratio / 2.0)

    # Rejection candle: body smaller than wick
    last = recent[-1]
    rejection = (last.range_size > 0 and
                 ((zone.zone_type == "demand" and last.body_top > last.body_bottom
                   and last.low <= zone.bottom * 1.002) or
                  (zone.zone_type == "supply" and not (last.body_top > last.body_bottom)
                   and last.high >= zone.top * 0.998)))

    rejection_score = 0.5 if rejection else 0.0

    return min(1.0, vol_score * 0.6 + rejection_score * 0.4)


# ─────────────────────────────────────────────────────────────────────────────
# Position Sizing
# ─────────────────────────────────────────────────────────────────────────────

def calc_position_size(symbol: str, entry: float, stop: float,
                        risk_dollar: float = GFT_MAX_RISK_DOLLAR) -> dict:
    """
    Calculate position size respecting GFT risk rules.
    Returns: { qty, risk_dollar, risk_pct, notional }
    """
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return {"error": "Invalid stop distance", "qty": 0}

    qty_unrounded = risk_dollar / risk_per_share
    # Round to whole shares
    qty = max(1, round(qty_unrounded))
    actual_risk = qty * risk_per_share
    notional = qty * entry

    return {
        "qty":          qty,
        "entry":        entry,
        "stop":         stop,
        "risk_per_share": round(risk_per_share, 4),
        "risk_dollar":  round(actual_risk, 2),
        "risk_pct":     round(actual_risk / GFT_ACCOUNT_SIZE * 100, 2),
        "notional":     round(notional, 2),
        "notional_pct": round(notional / GFT_ACCOUNT_SIZE * 100, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# VWAP Target
# ─────────────────────────────────────────────────────────────────────────────

def calc_vwap_target(entry: float, direction: str,
                     vwap: float, atr: float) -> float:
    """
    Elder Trades Step 6: Target VWAP.
    If VWAP is in our favor, target it.
    Otherwise target last swing high/low.
    """
    if direction == "long":
        if vwap > entry:
            return round(vwap, 2)
        else:
            # Target last swing high (estimate as recent high)
            return round(entry + atr * VWAP_ATR_MULT, 2)
    else:
        if vwap < entry:
            return round(vwap, 2)
        else:
            return round(entry - atr * VWAP_ATR_MULT, 2)


# ─────────────────────────────────────────────────────────────────────────────
# GFT Risk Checks
# ─────────────────────────────────────────────────────────────────────────────

def check_gft_limits(proposed_loss: float, current_equity: float) -> dict:
    """Pre-trade risk check against GFT rules."""
    global DAILY_PNL, DAILY_HIGH, ALL_TIME_PEAK

    post_equity = current_equity - proposed_loss
    daily_used  = DAILY_HIGH - post_equity
    trailing    = ALL_TIME_PEAK - post_equity
    floating_ok = post_equity >= GFT_FLOAT_FLOOR
    daily_ok    = daily_used  <= GFT_DAILY_DD_LIMIT
    trailing_ok = trailing    <= GFT_ACCOUNT_SIZE * GFT_MAX_DD_PCT
    position_ok = proposed_loss <= GFT_MAX_RISK_DOLLAR

    all_ok = daily_ok and floating_ok and position_ok

    return {
        "all_ok":       all_ok,
        "proposed_loss": proposed_loss,
        "post_equity":  round(post_equity, 2),
        "daily_used":   round(daily_used, 2),
        "daily_limit":  GFT_DAILY_DD_LIMIT,
        "floating_floor": GFT_FLOAT_FLOOR,
        "floating_ok":  floating_ok,
        "daily_ok":     daily_ok,
        "position_ok":  position_ok,
        "blocking_rule": None if all_ok else (
            "DAILY_DD" if not daily_ok else
            "FLOATING_LOSS" if not floating_ok else
            "POSITION_SIZE"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Alpaca Order Execution
# ─────────────────────────────────────────────────────────────────────────────

def place_market_order(symbol: str, qty: int, side: str,
                       take_profit: float, stop_loss: float,
                       dry_run: bool = True) -> dict:
    """
    Place a market order with TP and SL via Alpaca.
    Returns order result dict.
    """
    if dry_run:
        return {
            "order_id":    f"DRY_RUN_{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "status":      "dry_run_accepted",
            "symbol":      symbol,
            "side":        side,
            "qty":         qty,
            "entry_price": None,
            "take_profit": take_profit,
            "stop_loss":   stop_loss,
            "timestamp":   dt.datetime.utcnow().isoformat(),
        }

    try:
        client = TRADING_CLIENT
        side_enum   = OrderSide.BUY if side == "buy" else OrderSide.SELL

        # Market order with TP/SL
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.GTC,
            order_class="bracket",
            take_profit=TakeProfitRequest(limit=take_profit),
            stop_loss=StopLossRequest(stop=stop_loss),
        )

        resp = client.submit_order(order)
        return {
            "order_id":    resp.id,
            "status":      "submitted",
            "symbol":      symbol,
            "side":        side,
            "qty":         qty,
            "take_profit": take_profit,
            "stop_loss":   stop_loss,
            "timestamp":   dt.datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"order_id": None, "status": "error", "error": str(e)}


def get_account_buying_power() -> float:
    """Get paper account buying power."""
    try:
        client = TRADING_CLIENT
        account = client.get_account()
        return float(account.buying_power)
    except Exception as e:
        print(f"  [WARN] Could not fetch buying power: {e}")
        return PAPER_BALANCE


def close_position_with_confirmation(symbol: str, qty: int, side: str,
                                     max_retries: int = 3,
                                     retry_delay: float = 30.0) -> dict:
    """
    Send a market close order then poll every `retry_delay` seconds
    until Alpaca confirms the position is closed (or max_retries hit).

    Returns:
        {"status": "confirmed", "attempts": N, "last_error": None}
      or {"status": "still_open", "attempts": N, "last_error": "...", "qty": N}
    """
    import time as _time

    client = TRADING_CLIENT
    close_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    qty_to_close = abs(qty)

    for attempt in range(1, max_retries + 1):
        # ── Step 1: send close order ──────────────────────────────────────
        try:
            order_req = MarketOrderRequest(
                symbol=symbol,
                qty=qty_to_close,
                side=close_side,
                time_in_force=TimeInForce.GTC,
            )
            resp = client.submit_order(order_req)
            print(f"  [CLOSE] Attempt {attempt}/{max_retries} — order submitted: {resp.id}")
        except Exception as e:
            print(f"  [CLOSE] Attempt {attempt}/{max_retries} — submit error: {e}")
            last_err = str(e)
        else:
            last_err = None

        # ── Step 2: wait then check position status ────────────────────────
        _time.sleep(retry_delay)

        try:
            pos = client.get_position(symbol)
            still_open = True
            pos_qty = abs(float(pos.qty))
            print(f"  [CLOSE] Attempt {attempt}/{max_retries} — position still open, qty={pos_qty}")
        except Exception:
            # No position found → consider it closed
            still_open = False
            pos_qty = 0
            print(f"  [CLOSE] Attempt {attempt}/{max_retries} — position closed confirmed.")

        if not still_open:
            return {"status": "confirmed", "attempts": attempt, "last_error": None}

        if attempt < max_retries:
            print(f"  [CLOSE] Re-sending close order in {retry_delay}s...")

    # All retries exhausted
    print(f"  [CLOSE] WARNING: position still open after {max_retries} attempts.")
    return {
        "status": "still_open",
        "attempts": max_retries,
        "last_error": last_err,
        "qty": pos_qty,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main Scanner / Strategy Runner
# ─────────────────────────────────────────────────────────────────────────────

def scan_for_setup(symbol: str, bias: str,
                   h1_zones: list[SupplyDemandZone],
                   m5_bars: list[Bar],
                   m15_bars: list[Bar],
                   h1_bars: list[Bar],
                   h4_bars: list[Bar],
                   atr_5m: list[float],
                   vwap_5m: list[float]) -> Optional[dict]:
    """
    Run the full Elder Trades setup check.
    Returns entry plan dict or None.
    """
    if bias == "neutral":
        return None

    # Get current price
    current_price = m5_bars[-1].close if m5_bars else 0
    current_vwap  = vwap_5m[-1]       if vwap_5m else current_price
    current_atr   = atr_5m[-1]         if atr_5m else (m5_bars[-1].high - m5_bars[-1].low) if m5_bars else 0

    # Filter zones by direction
    if bias == "bullish":
        zones = [z for z in h1_zones if z.zone_type == "demand" and z.valid]
    else:
        zones = [z for z in h1_zones if z.zone_type == "supply" and z.valid]

    if not zones:
        return None

    # Find zone nearest to current price
    nearest_zone = min(zones, key=lambda z: abs(current_price - z.midpoint))

    # Check if price is within zone or approaching it — LOOSENED: up to 1.5% from zone
    in_zone = (nearest_zone.bottom <= current_price <= nearest_zone.top)
    approaching = (abs(current_price - nearest_zone.bottom) / current_price < 0.015) or \
                   (abs(current_price - nearest_zone.top)    / current_price < 0.015)

    if not (in_zone or approaching):
        return None

    # Check 5M structure shift — relaxed: if no structure, use basic momentum bias
    structure = detect_structure_shift_5m(m5_bars, atr_5m, bias)
    if structure is None:
        # Fallback: basic directional bias if no clear structure
        structure = {"structure": "bias_only", "confidence": 0.4}

    # Check absorption at zone — LOOSENED: lower threshold, accept any non-zero score
    absorption_score = detect_absorption(m5_bars, nearest_zone)
    # Don't block on absorption — just reduce confidence weighting
    absorption_weight = 0.3 if absorption_score > 0.2 else 0.1

    # Calculate stop and target
    if bias == "bullish":
        stop_loss = round(nearest_zone.bottom - current_atr * STOP_ATR_MULT, 2)
        take_profit = calc_vwap_target(current_price, "long", current_vwap, current_atr)
    else:
        stop_loss = round(nearest_zone.top + current_atr * STOP_ATR_MULT, 2)
        take_profit = calc_vwap_target(current_price, "short", current_vwap, current_atr)

    # Check position size
    risk_dollar = GFT_MAX_RISK_DOLLAR
    pos = calc_position_size(symbol, current_price, stop_loss, risk_dollar)

    if "error" in pos:
        return None

    # GFT pre-trade check
    risk_check = check_gft_limits(pos["risk_dollar"], PAPER_BALANCE)
    if not risk_check["all_ok"]:
        return None

    confidence = (
        structure["confidence"] * 0.5 +
        nearest_zone.strength * 0.3 +
        absorption_score * absorption_weight
    )

    return {
        "symbol":       symbol,
        "direction":    "buy" if bias == "bullish" else "sell",
        "bias":         bias,
        "entry_price":  current_price,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "qty":          pos["qty"],
        "risk_dollar":  pos["risk_dollar"],
        "notional":     pos["notional"],
        "zone":         asdict(nearest_zone),
        "structure":    structure["structure"],
        "absorption":   round(absorption_score, 2),
        "confidence":   round(confidence, 2),
        "vwap":         round(current_vwap, 2),
        "atr":          round(current_atr, 4),
        "timestamp":    dt.datetime.utcnow().isoformat(),
    }


def run_strategy(symbol: str, dry_run: bool = True, max_trades: int = 9999):
    """
    Main Elder Trades strategy runner.
    Fetches data, finds setups, places paper trades.
    """
    global PAPER_BALANCE, DAILY_PNL, DAILY_HIGH, ALL_TIME_PEAK, TRADE_LOG, DRY_RUN

    DRY_RUN = dry_run
    trade_id = len(TRADE_LOG) + 1

    print(f"\n{'='*60}")
    print(f"ELDER TRADES PAPER TRADER  |  {symbol}  |  {'DRY RUN' if dry_run else 'LIVE PAPER'}")
    print(f"Account: ${PAPER_BALANCE:,.2f}  |  Max risk/trade: ${GFT_MAX_RISK_DOLLAR:.2f}")
    print(f"{'='*60}")

    # ── Step 1: Fetch data ────────────────────────────────────────────────
    print("\n[1] Fetching multi-timeframe data...")
    tf_bars = get_multi_timeframe_bars(symbol, days_back=60, use_crypto=USE_CRYPTO)

    if not tf_bars or not tf_bars.get("5M"):
        print("  ERROR: No data returned. Check API key and market open hours.")
        print("  Alpaca IEX free tier requires market to be open or recent data.")
        return None

    m5  = tf_bars["5M"]
    m15 = tf_bars["15M"]
    h1  = tf_bars["1H"]
    h4  = tf_bars["4H"]

    print(f"  5M bars:  {len(m5):>6}   latest: {m5[-1].timestamp.strftime('%Y-%m-%d %H:%M')}")
    print(f"  15M bars: {len(m15):>6}   latest: {m15[-1].timestamp.strftime('%Y-%m-%d %H:%M')}")
    print(f"  1H bars:  {len(h1):>6}   latest: {h1[-1].timestamp.strftime('%Y-%m-%d %H:%M')}")
    print(f"  4H bars:  {len(h4):>6}   latest: {h4[-1].timestamp.strftime('%Y-%m-%d %H:%M')}")

    # ── Step 2: 4H Trend Bias ───────────────────────────────────────────
    print("\n[2] Reading 4H trend bias...")
    bias = get_4h_trend_bias(h4)
    print(f"  4H Bias: {bias.upper()}")

    if bias == "neutral":
        print("  No clear 4H bias — skipping. Trade only with trend.")
        return None

    # ── Step 3: Find S/D Zones on 1H ────────────────────────────────────
    print("\n[3] Scanning 1H for supply/demand zones...")
    atr_h1 = atr(h1, ATR_PERIOD)
    atr_5m = atr(m5, ATR_PERIOD)
    vwap_5 = vwap_bars(m5)
    zones  = get_live_zones_1H(h1, atr_h1)

    valid_zones = [z for z in zones if z.valid]
    demand_zones = [z for z in valid_zones if z.zone_type == "demand"]
    supply_zones = [z for z in valid_zones if z.zone_type == "supply"]

    print(f"  Total zones found: {len(zones)}")
    print(f"  Valid demand zones: {len(demand_zones)}")
    print(f"  Valid supply zones: {len(supply_zones)}")

    for z in valid_zones[-5:]:
        print(f"    [{z.zone_type.upper():6}] {z.bottom:.2f} – {z.top:.2f} "
              f"(strength: {z.strength:.0%})  @{z.origin_timestamp.strftime('%m-%d %H:%M')}")

    # ── Step 4: Scan for setup ──────────────────────────────────────────
    print("\n[4] Scanning for A+ setup...")
    setup = scan_for_setup(
        symbol, bias, zones, m5, m15, h1, h4,
        atr_5m, vwap_5
    )

    if setup is None:
        print("  No A+ setup found right now.")
        print("  Reasons could be: price not at a zone, no 5M structure shift, absorption not confirmed.")
        print(f"\n  Current price: ${m5[-1].close:.2f}")
        print(f"  Current VWAP:  ${vwap_5[-1]:.2f}" if vwap_5 else "")
        print(f"  5M EMA({EMA_5M_PERIOD}): ${atr_5m[-1]:.2f}" if atr_5m else "")
        return None

    print(f"\n  *** A+ SETUP FOUND ***")
    print(f"  Direction:   {setup['direction'].upper()}")
    print(f"  Entry:       ${setup['entry_price']:.2f}")
    print(f"  Stop Loss:   ${setup['stop_loss']:.2f}  (risk: ${setup['risk_dollar']:.2f})")
    print(f"  Take Profit: ${setup['take_profit']:.2f}  (VWAP target)")
    print(f"  Qty:         {setup['qty']} shares  (notional: ${setup['notional']:.2f})")
    print(f"  Zone:        {setup['zone']['zone_type']} {setup['zone']['bottom']:.2f}-{setup['zone']['top']:.2f}")
    print(f"  Structure:  {setup['structure']}")
    print(f"  Absorption: {setup['absorption']:.0%}")
    print(f"  Confidence: {setup['confidence']:.0%}")

    # ── Step 5: Execute ──────────────────────────────────────────────────
    print("\n[5] Executing order...")

    # Update paper balance
    proposed_risk = setup["risk_dollar"]
    risk_check = check_gft_limits(proposed_risk, PAPER_BALANCE)

    if not risk_check["all_ok"]:
        print(f"  BLOCKED by GFT rule: {risk_check['blocking_rule']}")
        print(f"  {risk_check}")
        return None

    result = place_market_order(
        symbol       = symbol,
        qty          = setup["qty"],
        side         = setup["direction"],
        take_profit  = setup["take_profit"],
        stop_loss    = setup["stop_loss"],
        dry_run      = dry_run,
    )

    print(f"  Order: {result['status']} — {result.get('order_id', 'N/A')}")

    # Log the trade
    trade = Trade(
        id          = trade_id,
        timestamp    = dt.datetime.utcnow(),
        symbol      = symbol,
        side        = setup["direction"],
        entry_price = setup["entry_price"],
        qty         = setup["qty"],
        stop_loss   = setup["stop_loss"],
        take_profit = setup["take_profit"],
        zone_top    = setup["zone"]["top"],
        zone_bottom = setup["zone"]["bottom"],
        status      = "open",
    )
    TRADE_LOG.append(trade)

    print(f"\n  Trade #{trade.id} logged:")
    print(f"  {setup['direction'].upper()} {setup['qty']} {symbol} @ ${setup['entry_price']:.2f}")
    print(f"  SL: ${setup['stop_loss']:.2f}  TP: ${setup['take_profit']:.2f}")
    print(f"  Risk: ${setup['risk_dollar']:.2f} ({setup['risk_dollar']/GFT_ACCOUNT_SIZE*100:.2f}% of account)")

    return setup


# ─────────────────────────────────────────────────────────────────────────────
# Session / Risk Management
# ─────────────────────────────────────────────────────────────────────────────

def end_of_day_summary():
    """Print EOD risk report."""
    global PAPER_BALANCE, DAILY_PNL, DAILY_HIGH, ALL_TIME_PEAK, TRADE_LOG, CONSISTENCY_LOG

    today = dt.date.today().isoformat()
    CONSISTENCY_LOG[today] = DAILY_PNL

    print(f"\n{'='*60}")
    print(f"END OF DAY SUMMARY  |  {today}")
    print(f"{'='*60}")
    print(f"  Paper balance:  ${PAPER_BALANCE:>10,.2f}")
    print(f"  Daily PnL:      ${DAILY_PNL:>10,.2f}")
    print(f"  Daily DD used:  ${DAILY_HIGH - PAPER_BALANCE:>10,.2f} / ${GFT_DAILY_DD_LIMIT:.2f}")
    print(f"  All-time peak:  ${ALL_TIME_PEAK:>10,.2f}")
    print(f"  Trailing DD:    ${ALL_TIME_PEAK - PAPER_BALANCE:>10,.2f}")
    print(f"  Floating floor: ${GFT_FLOAT_FLOOR:>10,.2f}")
    print(f"  Trades today:   {len([t for t in TRADE_LOG if t.status == 'open'])}")

    # Consistency check
    total_pnl = sum(CONSISTENCY_LOG.values())
    if total_pnl > 0:
        violations = [
            (d, p, p/total_pnl*100)
            for d, p in CONSISTENCY_LOG.items()
            if p > 0 and p/total_pnl > CONSISTENCY_CAP
        ]
        if violations:
            print(f"\n  CONSISTENCY VIOLATIONS ({len(violations)}):")
            for d, p, pct in violations:
                print(f"    {d}: ${p:.2f} = {pct:.1f}% of total (cap: {CONSISTENCY_CAP*100:.0f}%)")
        else:
            print(f"\n  Consistency: OK — no violations")

    print(f"\n  Open trades: {len([t for t in TRADE_LOG if t.status == 'open'])}")

    return {
        "date": today,
        "balance": PAPER_BALANCE,
        "daily_pnl": DAILY_PNL,
        "total_pnl": total_pnl,
        "trades": len(TRADE_LOG),
    }


def export_trade_log(path: str = "elder_trades_journal.csv"):
    global TRADE_LOG
    if not TRADE_LOG:
        print("No trades to export.")
        return

    header = "id,date,symbol,side,entry_price,qty,stop_loss,take_profit,pnl,rr,status,exit_price,zone_top,zone_bottom"
    rows = [header]
    for t in TRADE_LOG:
        rr = abs((t.take_profit - t.entry_price) / (t.entry_price - t.stop_loss)) \
             if t.entry_price != t.stop_loss else 0
        rows.append(
            f"{t.id},{t.timestamp.date()},{t.symbol},{t.side},{t.entry_price:.2f},"
            f"{t.qty},{t.stop_loss:.2f},{t.take_profit:.2f},{t.pnl:.2f},{rr:.2f},"
            f"{t.status},{t.exit_price:.2f if t.exit_price else ''},"
            f"{t.zone_top:.2f},{t.zone_bottom:.2f}"
        )

    with open(path, "w") as f:
        f.write("\n".join(rows))
    print(f"Trade log saved to: {path}")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def get_market_session(use_crypto: bool = False) -> str:
    """Return 'pre', 'open', 'after', or 'closed' based on current ET time.
    Crypto is 24/7 so always returns 'open'.
    """
    if use_crypto:
        return "open"
    now_et = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))  # rough ET
    hour = now_et.hour
    minute = now_et.minute
    weekday = now_et.weekday()
    if weekday >= 5:
        return "closed"
    time_mins = hour * 60 + minute
    if time_mins < 9 * 60 + 30:       # before 9:30 AM ET
        return "pre"
    elif time_mins < 16 * 60:         # 9:30 AM - 4:00 PM ET
        return "open"
    else:
        return "after"


def print_dashboard(symbol: str, bias: str, price: float, session: str,
                    open_pos: int, daily_pnl: float, total_pnl: float,
                    trades_today: int, next_scan: str):
    """Print a clean fixed-width dashboard (ASCII-safe for Windows)."""
    bias_indicator = {"bullish": "[BULL]", "bearish": "[BEAR]", "neutral": "[NEUT]"}
    bias_label = bias_indicator.get(bias, "[????]")
    session_label = {"pre": "PRE-MKT", "open": "OPEN", "after": "AFTER-HRS", "closed": "CLOSED"}

    print(f"""
================================================================
  ELDER TRADES   |   {symbol}   |   GFT Instant GOAT $5K
================================================================
  Session : {session_label.get(session,'????'):<9}   {symbol}: ${price:<10.2f}   Bias: {bias_label} {bias.upper()}
----------------------------------------------------------------
  Account : ${PAPER_BALANCE:,.2f}
  Daily P&L: ${daily_pnl:>+8.2f}   Total P&L: ${total_pnl:>+9.2f}
  Trades today: {trades_today:<4}   Open positions: {open_pos:<4}
----------------------------------------------------------------
  GFT Rules: $1,500 max risk/trade   $3,000 daily DD
             $97,000 floating floor   15% consistency cap
================================================================
  Next scan: {next_scan}
================================================================""")


def main():
    parser = argparse.ArgumentParser(description="Elder Trades Paper Trader — GFT Instant GOAT $5K")
    parser.add_argument("--symbol",   default="QQQ",   help="Symbol to trade (default: QQQ)")
    parser.add_argument("--balance",  type=float, default=GFT_ACCOUNT_SIZE, help="Account size (default: $5,000)")
    parser.add_argument("--dry-run",  action="store_true", help="Dry run — no real paper orders")
    parser.add_argument("--live",     action="store_true", help="Live paper trading (default: ON)")
    parser.add_argument("--interval", type=int, default=5, help="Minutes between rescans (default: 5)")
    parser.add_argument("--max-trades", type=int, default=9999, help="Max open trades per session (default: unlimited)")
    parser.add_argument("--once",     action="store_true", help="Run once and exit (no loop)")
    parser.add_argument("--crypto",   action="store_true", help="Use BTC/USD (crypto, 24/7) instead of QQQ")
    args = parser.parse_args()

    global PAPER_BALANCE, USE_CRYPTO
    PAPER_BALANCE = args.balance
    dry_run = args.dry_run and not args.live
    USE_CRYPTO = args.crypto

    # Auto-switch symbol to BTC/USD when --crypto is set
    if USE_CRYPTO:
        args.symbol = "BTC/USD"

    mode = "DRY RUN" if dry_run else "LIVE PAPER"
    market_label = "CRYPTO 24/7" if USE_CRYPTO else "STOCKS (NYSE)"

    print(f"\n{'='*62}")
    print(f"  ELDER TRADES BOT  |  {mode}  |  GFT $5K Sizing")
    print(f"{'='*62}")
    print(f"  Symbol: {args.symbol}  |  Balance: ${PAPER_BALANCE:,.0f}  |  Rescan: {args.interval}min")
    print(f"  Market: {market_label}")
    print(f"  Keys: Paper trading endpoint (Alpaca)")
    print(f"{'='*62}\n")

    # Warm up clients
    get_clients(USE_CRYPTO)

    session = get_market_session(USE_CRYPTO)
    if session == "closed":
        print("[INFO] Market closed (weekend). Running in observe-only mode.")
        print("       Bot will scan 4H structure and sit ready for Monday open.\n")

    scan_count = 0
    open_positions = 0

    while True:
        scan_count += 1
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M ET")

        print(f"\n{'-'*62}")
        print(f"  SCAN #{scan_count}  |  {now}")

        # Fetch fresh data
        try:
            tf_bars = get_multi_timeframe_bars(args.symbol, days_back=30, use_crypto=USE_CRYPTO)
        except Exception as e:
            print(f"  [ERROR] Data fetch error: {e}")
            time.sleep(args.interval * 60)
            continue

        if not tf_bars or not tf_bars.get("5M"):
            print(f"  [ERROR] No data -- market may be closed or API limit hit.")
            print(f"          Waiting {args.interval} min before retry...")
            time.sleep(args.interval * 60)
            continue

        m5  = tf_bars["5M"]
        h1  = tf_bars["1H"]
        h4  = tf_bars["4H"]
        current_price = m5[-1].close
        session = get_market_session(USE_CRYPTO)

        # Step 1: 4H bias
        bias = get_4h_trend_bias(h4)

        # Calc daily/total P&L
        total_pnl = PAPER_BALANCE - GFT_ACCOUNT_SIZE
        daily_pnl = DAILY_PNL

        # Open positions
        open_pos = [t for t in TRADE_LOG if t.status == "open"]
        open_positions = len(open_pos)

        next_scan_time = (dt.datetime.now() + dt.timedelta(minutes=args.interval)).strftime("%H:%M ET")
        print_dashboard(args.symbol, bias, current_price, session,
                        open_positions, daily_pnl, total_pnl,
                        len(TRADE_LOG), next_scan_time)

        # ── Run the strategy ──────────────────────────────────────────────
        if bias == "neutral":
            print("\n  [WAIT] 4H bias NEUTRAL -- no trades without a trend.")
        else:
            print(f"\n  4H bias is {bias.upper()} — scanning for setup...")

            atr_h1 = atr(h1, ATR_PERIOD)
            atr_5m = atr(m5, ATR_PERIOD)
            vwap_5 = vwap_bars(m5)
            zones  = get_live_zones_1H(h1, atr_h1)

            setup = scan_for_setup(args.symbol, bias, zones, m5,
                                   tf_bars.get("15M", []), h1, h4, atr_5m, vwap_5)

            if setup:
                print(f"\n  [SETUP] A+ SETUP FOUND -- {setup['direction'].upper()}!")
                print(f"     Entry:    ${setup['entry_price']:.2f}")
                print(f"     Stop:     ${setup['stop_loss']:.2f}  (risk: ${setup['risk_dollar']:.2f})")
                print(f"     Target:   ${setup['take_profit']:.2f}  (VWAP)")
                print(f"     Zone:     {setup['zone']['zone_type']} {setup['zone']['bottom']:.2f}-{setup['zone']['top']:.2f}")
                print(f"     Confidence: {setup['confidence']:.0%}  |  Absorption: {setup['absorption']:.0%}")

                # Execute
                risk_check = check_gft_limits(setup["risk_dollar"], PAPER_BALANCE)
                if not risk_check["all_ok"]:
                    print(f"\n  [BLOCKED] GFT rule: {risk_check['blocking_rule']}")
                elif open_positions >= args.max_trades:
                    print(f"\n  [BLOCKED] Max open trades reached ({args.max_trades}).")
                else:
                    result = place_market_order(
                        symbol       = args.symbol,
                        qty          = setup["qty"],
                        side         = setup["direction"],
                        take_profit  = setup["take_profit"],
                        stop_loss    = setup["stop_loss"],
                        dry_run      = dry_run,
                    )
                    if result["status"] in ("submitted", "dry_run_accepted"):
                        print(f"\n  [ORDER] {result['status'].upper()} -- {result['order_id']}")
                        if not dry_run:
                            print(f"     TP: ${setup['take_profit']:.2f}  |  SL: ${setup['stop_loss']:.2f}")
                            # Verify position opened on Alpaca (common failure point)
                            time.sleep(5)
                            try:
                                pos = TRADING_CLIENT.get_position(args.symbol)
                                print(f"  [POSITION] Confirmed open: {pos.qty} shares {setup['direction']}")
                            except Exception:
                                print(f"  [WARN] Position not showing — will retry close on next scan.")
                    else:
                        print(f"\n  [ERROR] Order failed: {result.get('error', 'unknown')}")
            else:
                print("\n  [WAIT] No A+ setup -- price not at a valid zone right now.")

        # ── Once mode: exit after first scan ───────────────────────────────
        if args.once:
            print(f"\n{'='*62}")
            print("  Done. Run again to rescan.")
            print(f"{'='*62}")
            break

        # ── Daily reset check ─────────────────────────────────────────────
        current_session = get_market_session(USE_CRYPTO)
        if current_session == "closed" and scan_count > 1:
            print(f"\n{'='*62}")
            print("  [DONE] Market closed. Bot pausing until next session.")
            print(f"{'='*62}")
            export_trade_log()
            break

        print(f"\n  [SLEEP] Rescanning in {args.interval} min at {next_scan_time}...")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
