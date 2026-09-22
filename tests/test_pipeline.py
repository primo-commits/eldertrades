"""
Offline end-to-end check. Builds synthetic bars engineered to satisfy all three
pillars, then runs the full path: context -> location -> confirmation -> risk ->
order. No network, no credentials.

Run: python -m tests.test_pipeline
"""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from elder import config as cfgmod
from elder import journal, risk, strategy, universe
from elder.broker import Broker


def bars_from(prices, *, freq, volumes=None, start="2026-09-01 09:30"):
    px = np.asarray(prices, dtype=float)
    idx = pd.date_range(start, periods=len(px), freq=freq, tz="America/New_York")
    o = np.r_[px[0], px[:-1]]
    v = np.asarray(volumes, float) if volumes is not None else np.full(len(px), 10_000.0)
    return pd.DataFrame({
        "open": o,
        "high": np.maximum(o, px) + 0.10,
        "low": np.minimum(o, px) - 0.10,
        "close": px,
        "volume": v,
    }, index=idx)


def build_bullish_case():
    """
    Uptrend 4H, a demand zone below price, exhaustion then a buyer flip.

    Sized so every zone timeframe has enough bars: find_zones needs at least
    atr_period + consolidation_max_bars + 2 = 26. Live, a 45-day lookback gives
    roughly 217 1H bars and 403 30M bars, so this is not a live constraint.
    """
    t = np.arange(160)
    h4 = bars_from(100 + t * 0.5 + np.sin(t / 4.0) * 6, freq="4h")

    rng = np.random.default_rng(5)
    px: list[float] = []
    vol: list[float] = []

    # Exactly two 24h days of quiet base (288 5-min bars each), so the pattern
    # below starts on a clean session boundary and the final session's volume
    # profile covers the whole balance -> expansion -> retrace range. Without
    # that alignment the expansion lands on a previous day and no target level
    # sits high enough to pay the minimum R:R.
    for _ in range(2):
        px += list(100 + rng.normal(0, 0.25, 288))
        vol += list(rng.uniform(9_000, 11_000, 288))

    # Balance: 18 bars (90 min) tight -> survives the 30-min resample.
    px += list(101.0 + rng.normal(0, 0.03, 18))
    vol += [9_000.0] * 18

    # Obvious expansion up over 9 bars (45 min) -> creates the demand zone.
    px += list(np.linspace(101.6, 105.0, 9))
    vol += [32_000.0] * 9

    # Drift back down into the zone.
    px += list(np.linspace(104.8, 101.4, 36))
    vol += list(rng.uniform(10_000, 12_000, 36))

    # Three selling pushes on fading volume -> exhaustion.
    px += [101.20, 101.05, 100.90]
    vol += [9_000.0, 7_000.0, 5_000.0]

    # The flip, now evaluated over flip_window_bars (3). Buyers take control
    # across three bars rather than one outsized candle -- which is both what
    # Elder describes and what the single-bar encoding was throwing away.
    px += [101.15, 101.35, 101.55]
    vol += [30_000.0, 33_000.0, 36_000.0]

    ex = bars_from(px, freq="5min", volumes=vol)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    m15 = ex.resample("15min").agg(agg).dropna()
    m30 = ex.resample("30min").agg(agg).dropna()
    h1 = ex.resample("1h").agg(agg).dropna()
    return {"4Hour": h4, "1Hour": h1, "30Min": m30, "15Min": m15, "5Min": ex}


class FakeAccount:
    account_number = "PA-TEST"
    equity = "1000000"
    buying_power = "4000000"


class FakeClient:
    def get_account(self): return FakeAccount()
    def get_all_positions(self): return []
    def get_clock(self):
        class C: is_open = True
        return C()


def main() -> int:
    cfg = cfgmod.load()
    tmp = Path(tempfile.mkdtemp())
    risk.STATE_PATH = tmp / "risk.json"
    journal.JOURNAL_DIR = tmp / "journal"

    frames = build_bullish_case()
    print("bars built:", {k: len(v) for k, v in frames.items()})

    setup, rejects = strategy.evaluate("SPY", bars_by_tf=frames, cfg=cfg)
    for r in rejects:
        print(f"  reject [{r.stage}] {r.reason}")
    if setup is None:
        print("\nNo setup produced. Pipeline ran cleanly but the synthetic case "
              "did not satisfy all three pillars.")
        return 1

    print("\nSETUP:", setup.summary())
    for n in setup.notes:
        print("  note:", n)

    client = FakeClient()
    eng = risk.RiskEngine(cfg, client, risk.RiskState())
    equity = eng.equity()
    eng.roll_session(equity)
    decision = eng.check(setup, equity=equity, open_positions=[],
                         bucket_of={i.symbol: i.bucket for i in universe.ALL},
                         bar_volume=float(frames["5Min"]["volume"].tail(20).mean()))
    print(f"\nrisk: allowed={decision.allowed} ({decision.reason})")
    if not decision.allowed:
        return 1
    s = decision.sizing
    print(f"  qty {s.qty} | notional ${s.notional:,.0f} | risk ${s.risk_dollars:,.0f} "
          f"({s.risk_pct:.3%}) | capped_by={s.capped_by}")

    broker = Broker(cfg, client)
    order = broker.submit_bracket("SPY", s.qty, setup.side, setup.target,
                                  setup.stop, dry_run=True)
    print(f"\norder: ok={order.ok} status={order.status} id={order.order_id}")

    journal.log_setup(setup, s, order)
    journal.log_rejections(rejects)
    written = sorted(p.name for p in (tmp / "journal").glob("*.csv"))
    print("journal written:", written)

    print("\nEND-TO-END PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
