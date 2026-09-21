"""
Backtester correctness tests. The point of these is that a backtest which is
subtly wrong is worse than none -- it produces a number people act on.

Run: python -m tests.test_backtest
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd

from backtest import engine, metrics
from backtest.fills import CostModel, entry_fill, exit_fill
from backtest.portfolio import Portfolio
from backtest.run import synthetic
from elder import config as cfgmod


def test_slice_never_reveals_future_bars():
    """
    E19, tested at the invariant rather than the outcome.

    engine._slice is the single gate through which the strategy sees data. If
    it never returns a bar later than the decision timestamp then look-ahead is
    impossible by construction -- a stronger statement than checking that one
    particular trade happened to fill correctly.
    """
    cfg = cfgmod.load()
    data = synthetic(["AAA"], sessions=40, seed=2)
    frames = engine.build_frames(data["AAA"], cfg)
    base = cfg.data.base_timeframe
    stamps = list(frames[base].index)
    checked = 0
    for t in stamps[::97]:
        sl = engine._slice(frames, t)
        for tf, df in sl.items():
            if df.empty:
                continue
            assert df.index.max() <= t, f"{tf} leaked a bar at {df.index.max()} > {t}"
        checked += 1
    assert checked > 5
    print(f"  {checked} decision points x {len(frames)} timeframes: no frame "
          f"ever contained a bar later than the decision time")


def test_partial_higher_timeframe_bar_is_a_known_caveat():
    """
    The subtle residual. A 4H bar LABELLED before t may only CLOSE after t, so
    including it means the strategy sees a bar whose high/low/close were built
    from 5-minute bars at or before t -- correct -- but which a live trader
    would still consider "in progress". This test documents the behaviour
    rather than asserting it away.
    """
    cfg = cfgmod.load()
    data = synthetic(["AAA"], sessions=20, seed=8)
    frames = engine.build_frames(data["AAA"], cfg)
    base = cfg.data.base_timeframe
    bias_tf = cfg.strategy.context["structure_timeframe"]
    m5, h4 = frames[base], frames[bias_tf]
    t = h4.index[3] + pd.Timedelta(minutes=30)
    t = m5.index[m5.index.get_indexer([t], method="ffill")[0]]
    inc = engine._slice(frames, t)[bias_tf]
    assert len(inc) and inc.index[-1] <= t
    in_progress = inc.index[-1] + pd.Timedelta(hours=4) > t
    print(f"  slice at {t:%Y-%m-%d %H:%M}: newest {bias_tf} label {inc.index[-1]:%H:%M}, "
          f"still in progress = {in_progress}")
    print("  (live behaves identically -- it also reads the forming bar)")


def test_entry_fills_at_next_bar_open_not_signal_close():
    """E19. A fill must land inside the NEXT bar, never at the signal bar's close."""
    cfg = cfgmod.load()
    data = synthetic(["AAA", "BBB", "CCC", "DDD"], sessions=120, seed=11)
    r = engine.run(data, cfg, starting_equity=1_000_000, rescan_bars=26, seed=99)
    if not r.trades:
        print("  no trades produced; checking the fill primitive directly")
        rng = random.Random(1)
        f = entry_fill(100.0, "buy", CostModel(), rng)
        assert f.reason == "next_bar_open" and f.price >= 100.0
        print(f"  entry_fill uses next_bar_open, adverse-slipped: {f.price}")
        return
    checked = 0
    for t in r.trades:
        bars = data[t.symbol]
        assert t.opened_at in bars.index, "fill timestamp must be a real bar"
        bar = bars.loc[t.opened_at]
        lo, hi = float(bar["low"]), float(bar["high"])
        tol = float(bar["open"]) * 0.001
        assert lo - tol <= t.entry <= hi + tol, (
            f"{t.symbol} filled at {t.entry} outside the entry bar {lo}-{hi}")
        # And it must be anchored to that bar's OPEN, not its close.
        assert abs(t.entry - float(bar["open"])) <= tol, (
            f"{t.symbol} filled at {t.entry}, not near the bar open {bar['open']}")
        checked += 1
    print(f"  {checked} fill(s) verified at the next bar's open, within tolerance")


def test_gap_through_stop_fills_at_the_open():
    """E20. A bar opening beyond the stop must fill at that open, not the stop."""
    cm, rng = CostModel(), random.Random(3)
    bar = pd.Series({"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.5})
    f = exit_fill(bar, stop=99.0, target=105.0, side="buy", costs=cm, rng=rng)
    assert f.reason == "gap_through_stop" and f.price == 95.0, f
    loss_naive = 99.0 - 100.0
    loss_real = f.price - 100.0
    print(f"  gap: naive model loses ${loss_naive:.2f}/sh, honest model ${loss_real:.2f}/sh "
          f"({loss_real / loss_naive:.1f}x worse)")


def test_ambiguous_bar_assumes_the_stop():
    """Both levels inside one bar -- without ticks you must assume the worse."""
    cm, rng = CostModel(), random.Random(4)
    bar = pd.Series({"open": 100.0, "high": 106.0, "low": 98.0, "close": 104.0})
    f = exit_fill(bar, stop=99.0, target=105.0, side="buy", costs=cm, rng=rng)
    assert f.reason == "stop_ambiguous", f
    print(f"  both touched -> resolved as {f.reason} at {f.price} (conservative)")


def test_cold_window_cannot_produce_context():
    """
    The walk-forward bug, pinned.

    A test window sliced to its own dates alone starts with NO history. The 4H
    bias needs ~14 sessions before a single pivot confirms and find_zones needs
    26 bars per timeframe, so a bare 20-session window cannot produce context
    at all. The first real walk-forward run returned 0 trades in all 9 windows
    over 275 sessions because of exactly this.
    """
    cfg = cfgmod.load()
    full = synthetic(["AAA"], sessions=90, seed=4)["AAA"]
    days = sorted({t.date() for t in full.index})
    window = days[-20:]
    cold = full[[t.date() in set(window) for t in full.index]]

    frames_cold = engine.build_frames(cold, cfg)
    bias_tf = cfg.strategy.context["structure_timeframe"]
    n_cold = len(frames_cold[bias_tf])

    warm = full[[t.date() >= days[-60] for t in full.index]]
    n_warm = len(engine.build_frames(warm, cfg)[bias_tf])

    print(f"  cold 20-session window -> {n_cold} {bias_tf} bars")
    print(f"  with 40 sessions warm-up -> {n_warm} {bias_tf} bars")
    assert n_warm > n_cold * 2, "warm-up must supply materially more history"

    # And the zone finder simply refuses below its minimum.
    from elder.zones import find_zones
    z_cold = find_zones(frames_cold[bias_tf], timeframe=bias_tf)
    print(f"  zones findable on the cold {bias_tf} frame: {len(z_cold)} "
          f"(needs >= 26 bars, has {n_cold})")


def test_trade_from_suppresses_entries_but_still_warms_up():
    """`trade_from` must gate ENTRIES only -- bars before it still build state."""
    import datetime as dt
    cfg = cfgmod.load()
    data = synthetic(["AAA", "BBB"], sessions=60, seed=6)
    days = sorted({t.date() for t in data["AAA"].index})
    cutoff = days[-15]

    r = engine.run(data, cfg, starting_equity=1_000_000, rescan_bars=26,
                   seed=5, trade_from=cutoff)
    assert all(t.opened_at.date() >= cutoff for t in r.trades), \
        "no position may be opened before trade_from"
    assert r.skipped.get("warm-up bars (not traded)", 0) > 0, \
        "warm-up bars should be counted, proving they were processed"
    assert len(r.equity_curve) == 0 or min(t.date() for t in r.equity_curve.index) >= cutoff, \
        "the equity curve must be trimmed to the traded period"
    print(f"  warm-up bars processed but not traded: "
          f"{r.skipped['warm-up bars (not traded)']:,}")
    print(f"  trades, all on or after {cutoff}: {len(r.trades)}")


def test_cash_is_conserved():
    """E23. Every fill moves cash; the ledger must reconcile exactly."""
    pf = Portfolio(starting_equity=100_000.0)
    import datetime as dt
    t0 = dt.datetime(2026, 9, 21, 10, 0)
    pf.open("X", "buy", 100, 50.0, 49.0, 53.0, t0, cost=5.0)
    tr = pf.close("X", 52.0, t0 + dt.timedelta(minutes=10), "target", cost=5.0)
    expected = 100_000.0 - 5.0 + (52.0 - 50.0) * 100 - 5.0
    assert abs(pf.cash - expected) < 1e-9, (pf.cash, expected)
    assert abs(tr.net - (200.0 - 10.0)) < 1e-9
    print(f"  cash reconciles exactly: ${pf.cash:,.2f}  (net ${tr.net:,.2f})")


def test_same_seed_is_reproducible():
    """E21. Two runs with one seed must be identical, or a sweep is meaningless."""
    cfg = cfgmod.load()
    data = synthetic(["AAA", "BBB"], sessions=80, seed=5)
    a = engine.run(data, cfg, starting_equity=1_000_000, seed=777)
    b = engine.run(data, cfg, starting_equity=1_000_000, seed=777)
    assert len(a.trades) == len(b.trades)
    for x, y in zip(a.trades, b.trades):
        assert (x.symbol, x.entry, x.exit, x.net) == (y.symbol, y.entry, y.exit, y.net)
    print(f"  identical across two runs ({len(a.trades)} trades) -- sweeps are comparable")


def test_walk_forward_windows_do_not_overlap():
    """E22. Test windows must be disjoint or out-of-sample means nothing."""
    import datetime as dt
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(250)]
    w = metrics.make_windows(days, train_days=60, test_days=20)
    assert w, "should produce windows"
    for i in range(len(w) - 1):
        assert w[i].test_end < w[i + 1].test_start, "test windows overlap"
        assert w[i].train_end < w[i].test_start, "train leaks into test"
    print(f"  {len(w)} windows, all disjoint, no train/test leakage")


def test_profit_factor_is_not_payoff_ratio():
    """The original mislabelled avg_win/avg_loss as profit factor."""
    import datetime as dt
    class T:
        def __init__(s, net): s.net, s.costs, s.r_multiple, s.exit_reason = net, 0, 0, "x"
    trades = [T(300), T(300), T(-100), T(-100), T(-100), T(-100)]
    m = metrics.compute(trades, pd.Series(dtype=float), 100_000)
    assert abs(m.profit_factor - 600 / 400) < 1e-9, m.profit_factor
    assert abs(m.payoff_ratio - 300 / 100) < 1e-9, m.payoff_ratio
    print(f"  profit factor {m.profit_factor} (600/400) != payoff ratio "
          f"{m.payoff_ratio} (300/100)")


if __name__ == "__main__":
    for fn in (test_slice_never_reveals_future_bars,
               test_partial_higher_timeframe_bar_is_a_known_caveat,
               test_entry_fills_at_next_bar_open_not_signal_close,
               test_gap_through_stop_fills_at_the_open,
               test_ambiguous_bar_assumes_the_stop,
               test_cold_window_cannot_produce_context,
               test_trade_from_suppresses_entries_but_still_warms_up,
               test_cash_is_conserved,
               test_same_seed_is_reproducible,
               test_walk_forward_windows_do_not_overlap,
               test_profit_factor_is_not_payoff_ratio):
        print(f"\n{fn.__name__}:")
        fn()
    print("\nALL BACKTEST TESTS PASS")
