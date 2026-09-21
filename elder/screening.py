"""
Volatility / economics screen.

An instrument earns its place only if a WINNING trade clears a minimum net
dollar figure at the sizing you actually run. That is not a preference, it is
arithmetic:

    profit per winning trade  ~  notional x ATR% x stop_mult x R  -  costs

A low-volatility instrument cannot produce meaningful dollars no matter how
good the setup. Measured from the first live sessions: HYG (ATR 0.055%) nets
$55 on a winner and gives back 23% of that in spread and slippage, while META
(ATR 0.805%) nets $1,040 on identical capital at identical risk.

The screen runs on LIVE data every scan, so it adapts as volatility regimes
change rather than relying on a hardcoded list.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .indicators import atr


@dataclass
class Economics:
    symbol: str
    price: float
    atr: float
    atr_pct: float
    qty: int
    notional: float
    stop: float
    risk: float
    gross_win: float
    costs: float
    net_win: float
    passes: bool
    reason: str = ""
    exempt: bool = False

    @property
    def cost_drag(self) -> float:
        return self.costs / self.gross_win if self.gross_win > 0 else 1.0


def estimate(symbol: str, bars: pd.DataFrame, *, equity: float, cfg,
             atr_period: int = 14) -> Economics | None:
    """
    Expected economics of one winning trade at current sizing.

    Deliberately uses the EXECUTION timeframe ATR, because that is what sets
    the stop distance and therefore both the share count and the target.
    """
    if bars is None or len(bars) < atr_period + 2:
        return None
    price = float(bars["close"].iloc[-1])
    a = float(atr(bars, atr_period).iloc[-1])
    if not (price > 0 and a > 0):
        return None

    s, r, sc = cfg.strategy, cfg.risk, cfg.screening
    stop_mult = s.confirmation["flip_atr_mult"] + 0.5 + s.exits["stop_atr_buffer"]
    stop = a * stop_mult
    rr = s.exits["min_reward_risk"]

    qty_risk = (equity * r.risk_per_trade_pct) / stop
    qty_cap = (equity * r.max_position_notional_pct) / price
    qty = int(min(qty_risk, qty_cap))
    if qty < 1:
        return Economics(symbol, price, a, a / price, 0, 0, stop, 0, 0, 0, 0,
                         False, "sizes to zero shares")

    gross = qty * stop * rr
    costs = qty * (sc.est_spread_per_share + 2 * price * sc.est_slippage_bps / 10_000)
    net = gross - costs

    clears = net >= sc.min_net_per_trade
    exempt = symbol in set(sc.always_include)
    ok = clears or exempt

    if clears:
        reason = ""
    elif exempt:
        # Kept deliberately -- say so, and keep the cost visible.
        reason = (f"below the ${sc.min_net_per_trade:,.0f} floor at ${net:,.0f} "
                  f"but exempt (always_include)")
    else:
        reason = (f"winning trade nets ${net:,.0f}, below the "
                  f"${sc.min_net_per_trade:,.0f} floor (ATR {a / price:.3%}, "
                  f"{costs / gross:.0%} cost drag)" if gross > 0
                  else "no expected profit")

    return Economics(symbol, price, a, a / price, qty, qty * price, stop,
                     qty * stop, gross, costs, net, ok, reason, exempt)


def screen(bars_by_symbol: dict[str, pd.DataFrame], *, equity: float,
           cfg) -> tuple[list[str], list[Economics]]:
    """Returns (symbols that pass, every economics record)."""
    records: list[Economics] = []
    for sym, bars in bars_by_symbol.items():
        e = estimate(sym, bars, equity=equity, cfg=cfg)
        if e is not None:
            records.append(e)
    return [e.symbol for e in records if e.passes], records


def report(records: list[Economics], *, min_net: float) -> str:
    """Readable table, worst first -- the ones to consider dropping."""
    if not records:
        return "no economics computed"
    lines = [f"{'sym':<7}{'price':>9}{'ATR%':>8}{'qty':>7}{'notional':>11}"
             f"{'risk$':>9}{'net win':>9}{'cost%':>7}  verdict",
             "-" * 78]
    for e in sorted(records, key=lambda x: x.net_win):
        v = "EXEMPT" if (e.exempt and e.net_win < min_net) else ("ok" if e.passes else "DROP")
        lines.append(f"{e.symbol:<7}{e.price:>9.2f}{e.atr_pct:>7.3%}{e.qty:>7}"
                     f"${e.notional:>10,.0f}${e.risk:>8,.0f}${e.net_win:>8,.0f}"
                     f"{e.cost_drag:>6.0%}  {v}")
    n_drop = sum(1 for e in records if not e.passes)
    n_exempt = sum(1 for e in records if e.exempt and e.net_win < min_net)
    lines.append("-" * 78)
    lines.append(f"{len(records) - n_drop - n_exempt}/{len(records)} clear the "
                 f"${min_net:,.0f} floor"
                 + (f"; {n_exempt} kept by always_include" if n_exempt else "")
                 + (f"; {n_drop} dropped" if n_drop else ""))
    return "\n".join(lines)
