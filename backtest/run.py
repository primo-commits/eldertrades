"""
Backtest CLI.

    python -m backtest.run --symbols SPY,QQQ,TSLA --days 180
    python -m backtest.run --walk-forward --train 60 --test 20
    python -m backtest.run --sweep --days 365
    python -m backtest.run --synthetic          # no API keys needed
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import itertools
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from elder import config as cfgmod
from elder import universe
from elder.data import MarketData, regular_session

from . import engine, metrics

log = logging.getLogger("backtest")
OUT = Path("backtest_results")


def fetch(cfg, symbols: list[str], days: int) -> dict[str, pd.DataFrame]:
    md = MarketData(feed=cfg.data.feed, session_tz=cfg.data.session_tz,
                    prefer=cfg.prefer_credentials)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    print(f"fetching {cfg.data.base_timeframe} bars for {len(symbols)} symbols, "
          f"{days} days ({cfg.data.feed} feed)...")
    raw = md.bars(symbols, cfg.data.base_timeframe, start=start, end=end)
    out = {}
    for s, df in raw.items():
        if df.empty:
            print(f"  {s}: no data")
            continue
        rth = regular_session(df, tz=cfg.data.session_tz,
                              open_time=cfg.data.rth_open, close_time=cfg.data.rth_close,
                              skip_first_minutes=cfg.data.skip_first_minutes,
                              skip_last_minutes=cfg.data.skip_last_minutes)
        out[s] = rth
        print(f"  {s}: {len(rth):,} bars  {rth.index[0].date()} -> {rth.index[-1].date()}")
    return out


def synthetic(symbols: list[str], sessions: int = 120, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Trending/ranging synthetic bars, for verifying the engine without keys."""
    rng = np.random.default_rng(seed)
    bd = pd.bdate_range(end="2026-09-18", periods=sessions)
    idx = pd.DatetimeIndex([t for d in bd for t in pd.date_range(
        d + pd.Timedelta("9h30m"), periods=78, freq="5min", tz="America/New_York")])
    out = {}
    for i, s in enumerate(symbols):
        px0 = 100 + i * 50
        drift = rng.normal(0, 0.00002, len(idx))
        shock = rng.normal(0, 0.0012, len(idx))
        # occasional impulse moves so zones actually form
        for _ in range(sessions // 4):
            k = rng.integers(0, len(idx) - 20)
            shock[k:k + 6] += rng.choice([-1, 1]) * 0.004
        c = px0 * np.exp(np.cumsum(drift + shock))
        o = np.r_[c[0], c[:-1]]
        hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0006, len(idx))))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0006, len(idx))))
        v = rng.integers(3_000, 40_000, len(idx)).astype(float)
        out[s] = pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c,
                               "volume": v}, index=idx)
    return out


def _with(cfg, **over):
    """Config clone with strategy overrides -- for sweeps."""
    c = copy.deepcopy(cfg)
    for k, v in over.items():
        sec, key = k.split(".", 1)
        getattr(c.strategy, sec)[key] = v
    return c


SWEEP_GRID = {
    "confirmation.flip_atr_mult": [0.6, 0.8, 1.0, 1.3],
    "confirmation.exhaustion_bars": [2, 3, 4],
    "zones.expansion_atr_mult": [1.5, 2.0, 2.5],
    "exits.min_reward_risk": [1.2, 1.5, 2.0],
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backtest.run")
    p.add_argument("--symbols", default=None)
    p.add_argument("--tier", default=None)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--equity", type=float, default=None)
    p.add_argument("--rescan-bars", type=int, default=26)
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--walk-forward", action="store_true")
    p.add_argument("--train", type=int, default=60)
    p.add_argument("--test", type=int, default=20)
    p.add_argument("--sweep", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)

    cfg = cfgmod.load()
    equity = a.equity or cfg.nominal_equity
    symbols = ([s.strip().upper() for s in a.symbols.split(",")] if a.symbols
               else [i.symbol for i in universe.get_tier(a.tier or cfg.universe_tier)])

    data = synthetic(symbols) if a.synthetic else fetch(cfg, symbols, a.days)
    data = {s: d for s, d in data.items() if len(d) > 500}
    if not data:
        print("no usable data")
        return 1
    OUT.mkdir(exist_ok=True)

    if a.sweep:
        return _sweep(cfg, data, equity, a)
    if a.walk_forward:
        return _walk_forward(cfg, data, equity, a)

    r = engine.run(data, cfg, starting_equity=equity,
                   rescan_bars=a.rescan_bars, seed=a.seed)
    m = metrics.compute(r.trades, r.equity_curve, equity)
    print()
    print(metrics.report(m, "IN-SAMPLE (single pass -- NOT out of sample)"))
    print(f"\n  why nothing fired: {dict(sorted(r.skipped.items(), key=lambda x: -x[1])[:8])}")
    _save(r, "single")
    if m.trades:
        print("\n  NOTE: a single pass over all data is in-sample. Use")
        print("        --walk-forward before believing any of it.")
    return 0


def _walk_forward(cfg, data, equity, a) -> int:
    base = cfg.data.base_timeframe
    sessions = sorted({t.date() for d in data.values() for t in d.index})
    windows = metrics.make_windows(sessions, a.train, a.test)
    if not windows:
        print(f"not enough sessions ({len(sessions)}) for train={a.train} test={a.test}")
        return 1
    print(f"\nWALK-FORWARD: {len(windows)} windows, "
          f"train {a.train}d / test {a.test}d, over {len(sessions)} sessions\n")

    results, rows = [], []
    for i, w in enumerate(windows, 1):
        test = {s: d[(d.index.date >= w.test_start) & (d.index.date <= w.test_end)]
                for s, d in data.items()}
        test = {s: d for s, d in test.items() if len(d) > 100}
        if not test:
            continue
        r = engine.run(test, cfg, starting_equity=equity,
                       rescan_bars=a.rescan_bars, seed=a.seed + i)
        m = metrics.compute(r.trades, r.equity_curve, equity)
        results.append(r)
        rows.append({"window": i, "test_start": w.test_start, "test_end": w.test_end,
                     "trades": m.trades, "win_rate": m.win_rate,
                     "net": m.net, "return_pct": m.return_pct,
                     "profit_factor": m.profit_factor, "max_dd_pct": m.max_drawdown_pct})
        print(f"  win {i:>2}  {w.test_start} -> {w.test_end}  "
              f"trades {m.trades:>4}  wr {m.win_rate:>6.1%}  net ${m.net:>10,.0f}  "
              f"PF {m.profit_factor:>5.2f}")

    trades, eq = metrics.combine(results)
    m = metrics.compute(trades, eq, equity)
    print()
    print(metrics.report(m, "OUT-OF-SAMPLE (all test windows combined)"))
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "walk_forward.csv", index=False)
    if len(df):
        pos = (df["net"] > 0).sum()
        print(f"\n  profitable windows: {pos}/{len(df)}  ({pos/len(df):.0%})")
        print(f"  saved -> {OUT / 'walk_forward.csv'}")
    return 0


def _sweep(cfg, data, equity, a) -> int:
    keys = list(SWEEP_GRID)
    combos = list(itertools.product(*(SWEEP_GRID[k] for k in keys)))
    print(f"\nSWEEP: {len(combos)} combinations, identical seed per run "
          f"so they are comparable\n")
    rows = []
    for n, combo in enumerate(combos, 1):
        over = dict(zip(keys, combo))
        r = engine.run(data, _with(cfg, **over), starting_equity=equity,
                       rescan_bars=a.rescan_bars, seed=a.seed)
        m = metrics.compute(r.trades, r.equity_curve, equity)
        rows.append({**over, "trades": m.trades, "win_rate": m.win_rate,
                     "net": m.net, "profit_factor": m.profit_factor,
                     "expectancy": m.expectancy, "max_dd_pct": m.max_drawdown_pct,
                     "sharpe": m.sharpe})
        if n % 10 == 0:
            print(f"  {n}/{len(combos)}")
    df = pd.DataFrame(rows).sort_values("expectancy", ascending=False)
    df.to_csv(OUT / "sweep.csv", index=False)
    print("\nTOP 10 BY EXPECTANCY (in-sample -- see the warning below)")
    print(df.head(10).to_string(index=False))
    print(f"\n  saved -> {OUT / 'sweep.csv'}")
    print("\n  WARNING: these are IN-SAMPLE. The best row here is the one that")
    print("  fits this period's noise most closely. Re-run the winner under")
    print("  --walk-forward before trusting it.")
    return 0


def _save(r, name: str) -> None:
    if r.trades:
        pd.DataFrame([vars(t) for t in r.trades]).to_csv(OUT / f"trades_{name}.csv", index=False)
    if len(r.equity_curve):
        r.equity_curve.to_csv(OUT / f"equity_{name}.csv")
    print(f"  saved -> {OUT}/trades_{name}.csv")


if __name__ == "__main__":
    sys.exit(main())
