"""
Performance metrics and walk-forward validation.

E22: the original swept 100 parameter combinations over one year, picked the
argmax, and reported it. With no train/test split that number is a measure of
how well the parameters fit that year's noise, not of edge. Walk-forward fixes
it: optimise on a window, evaluate on the NEXT unseen window, roll forward, and
report only the out-of-sample results.

E22 also: "profit_factor" in the original was avg_win/avg_loss, which is the
payoff ratio. Profit factor is gross profit / gross loss. Both are reported
here, named correctly.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net: float
    costs: float
    profit_factor: float        # gross profit / gross loss
    payoff_ratio: float         # avg win / avg loss
    expectancy: float           # net per trade
    avg_win: float
    avg_loss: float
    avg_r: float
    max_drawdown_pct: float
    max_drawdown_abs: float
    return_pct: float
    sharpe: float
    exposure_pct: float
    exit_reasons: dict

    def as_dict(self) -> dict:
        return asdict(self)


def compute(trades: list, equity: pd.Series, starting_equity: float) -> Metrics:
    n = len(trades)
    if n == 0:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, {})

    nets = np.array([t.net for t in trades], dtype=float)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    gp = float(wins.sum())
    gl = float(-losses.sum())

    final = float(equity.iloc[-1]) if len(equity) else starting_equity + nets.sum()
    peak = equity.cummax() if len(equity) else pd.Series([starting_equity])
    dd = (equity - peak) if len(equity) else pd.Series([0.0])
    dd_pct = (dd / peak.replace(0, np.nan)) if len(equity) else pd.Series([0.0])

    # Sharpe from the daily equity curve, annualised on 252 sessions.
    if len(equity) > 2:
        daily = equity.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    return Metrics(
        trades=n,
        wins=int(len(wins)),
        losses=int(len(losses)),
        win_rate=round(len(wins) / n, 4),
        gross_profit=round(gp, 2),
        gross_loss=round(gl, 2),
        net=round(float(nets.sum()), 2),
        costs=round(sum(t.costs for t in trades), 2),
        profit_factor=round(gp / gl, 3) if gl > 0 else float("inf"),
        payoff_ratio=round(float(wins.mean() / -losses.mean()), 3)
                     if len(wins) and len(losses) and losses.mean() != 0 else 0.0,
        expectancy=round(float(nets.mean()), 2),
        avg_win=round(float(wins.mean()), 2) if len(wins) else 0.0,
        avg_loss=round(float(losses.mean()), 2) if len(losses) else 0.0,
        avg_r=round(float(np.mean([t.r_multiple for t in trades])), 3),
        max_drawdown_pct=round(float(-dd_pct.min()) * 100, 2) if len(equity) else 0.0,
        max_drawdown_abs=round(float(-dd.min()), 2) if len(equity) else 0.0,
        return_pct=round((final - starting_equity) / starting_equity * 100, 3),
        sharpe=round(sharpe, 3),
        exposure_pct=0.0,
        exit_reasons=reasons,
    )


def report(m: Metrics, title: str = "RESULTS") -> str:
    if m.trades == 0:
        return f"{title}\n  NO TRADES"
    L = [f"{title}", "=" * 58]
    L.append(f"  trades {m.trades:>6}   wins {m.wins:>5}   losses {m.losses:>5}")
    L.append(f"  WIN RATE            {m.win_rate:>10.1%}")
    L.append(f"  expectancy / trade  ${m.expectancy:>9,.2f}")
    L.append(f"  avg win / avg loss  ${m.avg_win:>9,.2f} / ${m.avg_loss:,.2f}")
    L.append(f"  avg R multiple      {m.avg_r:>10.2f}")
    L.append(f"  profit factor       {m.profit_factor:>10.2f}   (gross profit / gross loss)")
    L.append(f"  payoff ratio        {m.payoff_ratio:>10.2f}   (avg win / avg loss)")
    L.append("-" * 58)
    L.append(f"  net P&L             ${m.net:>9,.2f}")
    L.append(f"  total costs         ${m.costs:>9,.2f}   ({m.costs / max(abs(m.net) + m.costs, 1):.0%} of gross)")
    L.append(f"  return              {m.return_pct:>10.2f}%")
    L.append(f"  max drawdown        {m.max_drawdown_pct:>10.2f}%  (${m.max_drawdown_abs:,.0f})")
    L.append(f"  sharpe (annualised) {m.sharpe:>10.2f}")
    L.append(f"  exits: {m.exit_reasons}")
    return "\n".join(L)


# ── walk-forward ────────────────────────────────────────────────────────────

@dataclass
class Window:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def make_windows(sessions: list[dt.date], train_days: int = 60,
                 test_days: int = 20, step: int | None = None) -> list[Window]:
    """
    Rolling anchored windows. Optimise on `train_days`, evaluate on the NEXT
    `test_days`, then step forward. Test windows never overlap, so concatenating
    them gives a continuous out-of-sample record.
    """
    step = step or test_days
    out: list[Window] = []
    i = 0
    while i + train_days + test_days <= len(sessions):
        out.append(Window(sessions[i], sessions[i + train_days - 1],
                          sessions[i + train_days],
                          sessions[i + train_days + test_days - 1]))
        i += step
    return out


def combine(results: list) -> tuple[list, pd.Series]:
    """Stitch out-of-sample windows into one continuous record."""
    trades = [t for r in results for t in r.trades]
    curves = [r.equity_curve for r in results if len(r.equity_curve)]
    if not curves:
        return trades, pd.Series(dtype=float)
    # Chain each window's returns onto the running equity.
    eq = curves[0].copy()
    for c in curves[1:]:
        if len(c) < 2:
            continue
        scaled = c / float(c.iloc[0]) * float(eq.iloc[-1])
        eq = pd.concat([eq, scaled.iloc[1:]])
    return trades, eq.sort_index()
