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
    "intl_eu":     "European exposure via US-listed ETFs and ADRs",
    "intl_jp":     "Japanese exposure via US-listed ETFs and ADRs",
    "index":       "Broad index beta (ES/NQ proxies)",
    "semis":       "Semiconductors",
    "megatech":    "Mega-cap technology",
    "financials":  "Banks and brokers",
    "energy":      "Oil, gas, energy equities",
    "metals":      "Gold, silver, miners",
    "rates":       "Treasuries and credit",
    "biotech":     "Biotech and pharma",
    "highbeta":    "High-beta single names",
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
    _i("SMCI", "equity", "highbeta", 3),
    _i("HOOD", "equity", "financials", 3),
    _i("SOFI", "equity", "financials", 3),
    _i("AFRM", "equity", "highbeta", 3),
    _i("CVNA", "equity", "highbeta", 3),
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

# ── Tier 4: international exposure via US-listed proxies ─────────────────────
# Alpaca lists US securities only -- no TSE, LSE, Euronext or Xetra. These are
# US-listed ETFs and ADRs that TRACK those markets and trade on NYSE/NASDAQ.
#
# SESSION WARNING, and it matters for this strategy specifically:
#   Tokyo   09:00-15:00 JST = 20:00-02:00 ET -> ZERO overlap with NYSE hours
#   London  08:00-16:30 GMT = 03:00-11:30 ET -> overlaps only 09:30-11:30 ET
#   Europe  09:00-17:30 CET = 03:00-11:30 ET -> same two-hour window
#
# A Japan proxy traded during NYSE hours is priced off futures and NAV estimates
# with its underlying market shut. Volume clusters at the open and the close,
# which distorts the volume profile -- the core of Elder's context read. Europe
# is better: the 09:30-11:30 ET overlap is genuine two-sided liquidity.
INTL: list[Instrument] = [
    # Europe -- tradeable in the 09:30-11:30 ET overlap
    _i("FEZ",  "equity", "intl_eu", 2, "Euro Stoxx 50; closest thing to a EU index future"),
    _i("VGK",  "equity", "intl_eu", 2, "FTSE Europe, broad"),
    _i("EZU",  "equity", "intl_eu", 3, "Eurozone only"),
    _i("HEDJ", "equity", "intl_eu", 3, "Currency-hedged Europe"),
    _i("EWG",  "equity", "intl_eu", 3, "Germany / DAX proxy"),
    _i("EWU",  "equity", "intl_eu", 3, "UK / FTSE proxy"),
    _i("EWQ",  "equity", "intl_eu", 3, "France / CAC proxy"),
    _i("EWL",  "equity", "intl_eu", 3, "Switzerland"),
    # European ADRs -- deepest US-hours liquidity of the European names
    _i("ASML", "equity", "intl_eu", 2, "ADR; semis overlap -- correlates with SMH"),
    _i("SAP",  "equity", "intl_eu", 3, "ADR"),
    _i("NVO",  "equity", "intl_eu", 3, "ADR; Denmark"),
    _i("AZN",  "equity", "intl_eu", 3, "ADR; UK pharma"),
    _i("SHEL", "equity", "intl_eu", 3, "ADR; correlates with XLE"),
    _i("UL",   "equity", "intl_eu", 3, "ADR; defensive"),
    _i("STLA", "equity", "intl_eu", 3, "ADR; high beta"),
    _i("SPOT", "equity", "intl_eu", 3, "US-listed, Swedish; trades like a US growth name"),
    # Japan -- underlying market CLOSED during NYSE hours
    _i("EWJ",  "equity", "intl_jp", 3, "MSCI Japan; best JP volume but gappy intraday"),
    _i("DXJ",  "equity", "intl_jp", 3, "Yen-hedged Japan; cleaner when USDJPY moves"),
    _i("BBJP", "equity", "intl_jp", 3, "Broad Japan, thinner"),
    _i("TM",   "equity", "intl_jp", 3, "Toyota ADR; most liquid JP single name"),
    _i("SONY", "equity", "intl_jp", 3, "ADR; trades on US tech sentiment as much as Japan"),
    _i("MUFG", "equity", "intl_jp", 3, "ADR; JP banks, rate-sensitive"),
    _i("SMFG", "equity", "intl_jp", 3, "ADR; JP banks"),
    _i("HMC",  "equity", "intl_jp", 3, "Honda ADR; thin"),
]

# ── Recommended starting universe for a $1M book ─────────────────────────────
STARTER_SYMBOLS = [
    # SPY and QQQ lead: the direct ES/NQ analogues Elder actually trades.
    "SPY", "QQQ", "IWM", "SMH", "XLF", "XLE", "XLK", "XBI", "GDX", "TLT", "HYG", "SLV",
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "AMD", "JPM",
]

# Europe only, and only in the 09:30-11:30 ET overlap when the underlying market
# is genuinely open. Japan is deliberately excluded -- see the INTL note.
INTL_TRIAL_SYMBOLS = ["FEZ", "VGK", "ASML", "EWG", "EWU"]

ALL: list[Instrument] = ETF_CORE + EQUITY_LARGE + EQUITY_HIGHBETA + INTL
BY_SYMBOL: dict[str, Instrument] = {x.symbol: x for x in ALL}

TIERS: dict[str, list[Instrument]] = {
    "etf_core":     ETF_CORE,
    "equity_large": EQUITY_LARGE,
    "equity_highbeta": EQUITY_HIGHBETA,
    "intl":         INTL,
    "intl_eu":      [x for x in INTL if x.bucket == "intl_eu"],
    "intl_jp":      [x for x in INTL if x.bucket == "intl_jp"],
    "starter":      [BY_SYMBOL[s] for s in STARTER_SYMBOLS],
    "intl_trial":   [BY_SYMBOL[s] for s in INTL_TRIAL_SYMBOLS],
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
