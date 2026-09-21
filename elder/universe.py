"""
Tradeable universe for Alpaca, with correlation buckets.

Elder Santis trades ES/NQ futures, which Alpaca does not offer. SPY and QQQ are
the closest liquid proxies (same underlying indices, cash-settled ETF form).
Everything here is an instrument you can actually place an Alpaca order against.

Correlation buckets exist because SPY + QQQ + NVDA + AAPL long at the same time
is one trade at 4x size, not four trades. The risk engine caps concurrent
positions and risk per bucket.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str          # "equity" | "crypto"
    bucket: str               # correlation group
    tier: int                 # 1 = deepest book, 3 = size-capped
    note: str = ""

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"

    @property
    def shortable(self) -> bool:
        # Alpaca does not support short selling of crypto.
        return not self.is_crypto

    @property
    def supports_bracket(self) -> bool:
        # Alpaca crypto does not support bracket/OCO orders -- stops must be
        # managed by the bot itself. See broker.py.
        return not self.is_crypto


# ── Correlation buckets ──────────────────────────────────────────────────────
BUCKETS = {
    "index":       "Broad index beta (ES/NQ proxies)",
    "semis":       "Semiconductors",
    "megatech":    "Mega-cap technology",
    "financials":  "Banks and brokers",
    "energy":      "Oil, gas, energy equities",
    "metals":      "Gold, silver, miners",
    "rates":       "Treasuries and credit",
    "biotech":     "Biotech and pharma",
    "highbeta":    "High-beta single names",
    "crypto":      "Digital assets",
    "intl":        "International equity",
    "defensive":   "Staples, utilities, healthcare",
    "industrial":  "Industrials and materials",
    "consumer":    "Consumer discretionary",
}


def _i(sym, ac, bucket, tier, note=""):
    return Instrument(sym, ac, bucket, tier, note)


# ── Tier 1: index and sector ETFs ────────────────────────────────────────────
# Best structural fit for a zone/VWAP system: deepest books, tightest spreads,
# no single-name earnings gaps, no headline risk.
ETF_CORE: list[Instrument] = [
    _i("SPY",  "equity", "index",      1, "ES proxy -- closest Alpaca instrument to Elder's ES"),
    _i("QQQ",  "equity", "index",      1, "NQ proxy -- closest Alpaca instrument to Elder's NQ"),
    _i("IWM",  "equity", "index",      1, "Russell 2000"),
    _i("DIA",  "equity", "index",      2, "Dow 30"),
    _i("MDY",  "equity", "index",      3, "Mid-cap; thinner"),
    _i("SMH",  "equity", "semis",      1, "Semis, very high ATR"),
    _i("SOXX", "equity", "semis",      2, "Semis alt; overlaps SMH"),
    _i("XLK",  "equity", "megatech",   1),
    _i("XLF",  "equity", "financials", 1),
    _i("XLE",  "equity", "energy",     1),
    _i("XLV",  "equity", "biotech",    2),
    _i("XLI",  "equity", "industrial", 2),
    _i("XLY",  "equity", "consumer",   2),
    _i("XLP",  "equity", "defensive",  2),
    _i("XLU",  "equity", "defensive",  2),
    _i("XLB",  "equity", "industrial", 3),
    _i("XLRE", "equity", "financials", 3),
    _i("XLC",  "equity", "megatech",   3),
    _i("KRE",  "equity", "financials", 2, "Regional banks; high beta"),
    _i("XBI",  "equity", "biotech",    2, "Equal-weight biotech; clean zones"),
    _i("IBB",  "equity", "biotech",    3),
    _i("XOP",  "equity", "energy",     2),
    _i("XME",  "equity", "metals",     3),
    _i("ITB",  "equity", "consumer",   3, "Homebuilders; rate-sensitive"),
    _i("GDX",  "equity", "metals",     2, "Gold miners; high ATR"),
    _i("GDXJ", "equity", "metals",     3),
    _i("TLT",  "equity", "rates",      1, "20yr Treasury"),
    _i("IEF",  "equity", "rates",      3),
    _i("HYG",  "equity", "rates",      2, "High yield credit"),
    _i("LQD",  "equity", "rates",      3),
    _i("GLD",  "equity", "metals",     1),
    _i("SLV",  "equity", "metals",     2, "Higher ATR than GLD"),
    _i("USO",  "equity", "energy",     3, "Contango drag; intraday only"),
    _i("EEM",  "equity", "intl",       2),
    _i("EFA",  "equity", "intl",       3),
    _i("FXI",  "equity", "intl",       3, "Gaps hard on China headlines"),
    _i("EWZ",  "equity", "intl",       3),
    _i("IBIT", "equity", "crypto",     2, "Spot BTC ETF -- shortable, unlike Alpaca crypto"),
    _i("ETHA", "equity", "crypto",     3, "Spot ETH ETF"),
]

# ── Tier 2: mega-cap equities, ADV > $1B ─────────────────────────────────────
EQUITY_LARGE: list[Instrument] = [
    _i("NVDA",  "equity", "semis",      1),
    _i("AMD",   "equity", "semis",      2),
    _i("AVGO",  "equity", "semis",      2),
    _i("MU",    "equity", "semis",      2),
    _i("QCOM",  "equity", "semis",      3),
    _i("TXN",   "equity", "semis",      3),
    _i("ARM",   "equity", "semis",      3),
    _i("TSM",   "equity", "semis",      3, "ADR; gaps on Taiwan session"),
    _i("INTC",  "equity", "semis",      3),
    _i("AAPL",  "equity", "megatech",   1),
    _i("MSFT",  "equity", "megatech",   1),
    _i("AMZN",  "equity", "megatech",   1),
    _i("META",  "equity", "megatech",   1),
    _i("GOOGL", "equity", "megatech",   2),
    _i("NFLX",  "equity", "megatech",   2, "Wide spread; size down"),
    _i("ORCL",  "equity", "megatech",   3),
    _i("CRM",   "equity", "megatech",   3),
    _i("ADBE",  "equity", "megatech",   3),
    _i("NOW",   "equity", "megatech",   3, "High price, thin book"),
    _i("TSLA",  "equity", "highbeta",   1, "Highest ATR mega-cap"),
    _i("JPM",   "equity", "financials", 1),
    _i("BAC",   "equity", "financials", 2),
    _i("WFC",   "equity", "financials", 3),
    _i("GS",    "equity", "financials", 3),
    _i("V",     "equity", "financials", 3),
    _i("MA",    "equity", "financials", 3),
    _i("XOM",   "equity", "energy",     1),
    _i("CVX",   "equity", "energy",     2),
    _i("UNH",   "equity", "biotech",    2),
    _i("LLY",   "equity", "biotech",    2),
    _i("JNJ",   "equity", "biotech",    3),
    _i("ABBV",  "equity", "biotech",    3),
    _i("MRK",   "equity", "biotech",    3),
    _i("PFE",   "equity", "biotech",    3),
    _i("COST",  "equity", "defensive",  3),
    _i("WMT",   "equity", "defensive",  2),
    _i("PG",    "equity", "defensive",  3),
    _i("KO",    "equity", "defensive",  3),
    _i("PEP",   "equity", "defensive",  3),
    _i("MCD",   "equity", "consumer",   3),
    _i("HD",    "equity", "consumer",   3),
    _i("DIS",   "equity", "consumer",   2),
    _i("UBER",  "equity", "consumer",   2),
    _i("BA",    "equity", "industrial", 2),
    _i("CAT",   "equity", "industrial", 3),
    _i("GE",    "equity", "industrial", 3),
    _i("T",     "equity", "defensive",  3),
    _i("VZ",    "equity", "defensive",  3),
]

# ── Tier 3: high-beta movers ─────────────────────────────────────────────────
# Excellent ATR for zone setups, but cap size and hard-skip earnings.
EQUITY_HIGHBETA: list[Instrument] = [
    _i("PLTR", "equity", "highbeta", 2),
    _i("COIN", "equity", "crypto",   2, "Crypto beta, shortable"),
    _i("MSTR", "equity", "crypto",   2, "Extreme ATR; size way down"),
    _i("SMCI", "equity", "highbeta", 3),
    _i("HOOD", "equity", "financials", 3),
    _i("SOFI", "equity", "financials", 3),
    _i("AFRM", "equity", "highbeta", 3),
    _i("CVNA", "equity", "highbeta", 3),
    _i("MARA", "equity", "crypto",   3),
    _i("RIOT", "equity", "crypto",   3),
    _i("CLSK", "equity", "crypto",   3),
    _i("IONQ", "equity", "highbeta", 3),
    _i("RKLB", "equity", "highbeta", 3),
    _i("ASTS", "equity", "highbeta", 3),
    _i("APP",  "equity", "highbeta", 3),
    _i("CELH", "equity", "consumer", 3),
    _i("DKNG", "equity", "consumer", 3),
    _i("ROKU", "equity", "highbeta", 3),
    _i("SNAP", "equity", "highbeta", 3),
    _i("RIVN", "equity", "highbeta", 3),
    _i("LCID", "equity", "highbeta", 3),
    _i("ONON", "equity", "consumer", 3),
    _i("NIO",  "equity", "highbeta", 3),
]

# ── Leveraged ETFs: deliberately excluded ────────────────────────────────────
# Daily-reset compounding and path dependency break zone logic drawn on a
# multi-day chart. Listed so the exclusion is a decision, not an oversight.
LEVERAGED_EXCLUDED = ["TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
                      "LABU", "LABD", "SPXU", "UVXY", "VIXY", "TSLL"]

# ── Tier 5: Alpaca crypto ────────────────────────────────────────────────────
# LONG ONLY (no shorting) and NO bracket orders. The bot manages these stops.
CRYPTO: list[Instrument] = [
    _i("BTC/USD",  "crypto", "crypto", 1),
    _i("ETH/USD",  "crypto", "crypto", 1),
    _i("SOL/USD",  "crypto", "crypto", 2),
    _i("LINK/USD", "crypto", "crypto", 2),
    _i("AVAX/USD", "crypto", "crypto", 3),
    _i("LTC/USD",  "crypto", "crypto", 3),
    _i("DOGE/USD", "crypto", "crypto", 3),
    _i("XRP/USD",  "crypto", "crypto", 3),
    _i("BCH/USD",  "crypto", "crypto", 3, "Thin on Alpaca"),
    _i("DOT/USD",  "crypto", "crypto", 3, "Thin on Alpaca"),
    _i("UNI/USD",  "crypto", "crypto", 3, "Thin on Alpaca"),
    _i("AAVE/USD", "crypto", "crypto", 3, "Thin on Alpaca"),
]

# ── Recommended starting universe for a $1M book ─────────────────────────────
STARTER_SYMBOLS = [
    "SPY", "QQQ", "IWM", "SMH", "XLF", "XLE", "XLK", "XBI", "GDX", "TLT", "HYG", "SLV",
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "AMD", "JPM",
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD",
]

ALL: list[Instrument] = ETF_CORE + EQUITY_LARGE + EQUITY_HIGHBETA + CRYPTO
BY_SYMBOL: dict[str, Instrument] = {x.symbol: x for x in ALL}

TIERS: dict[str, list[Instrument]] = {
    "etf_core":     ETF_CORE,
    "equity_large": EQUITY_LARGE,
    "equity_highbeta": EQUITY_HIGHBETA,
    "crypto":       CRYPTO,
    "starter":      [BY_SYMBOL[s] for s in STARTER_SYMBOLS],
}


def get_tier(name: str) -> list[Instrument]:
    if name not in TIERS:
        raise KeyError(f"Unknown universe tier {name!r}. Options: {sorted(TIERS)}")
    return TIERS[name]


def by_bucket(instruments: list[Instrument]) -> dict[str, list[Instrument]]:
    out: dict[str, list[Instrument]] = {}
    for x in instruments:
        out.setdefault(x.bucket, []).append(x)
    return out


def split_asset_classes(instruments: list[Instrument]) -> tuple[list[Instrument], list[Instrument]]:
    """Return (equities, crypto) -- they need different data clients and order rules."""
    eq = [x for x in instruments if not x.is_crypto]
    cx = [x for x in instruments if x.is_crypto]
    return eq, cx
