"""
Portfolio with a real cash constraint (E23).

The original backtester sized every position at 5% of INITIAL capital with no
ledger at all -- ten tickers per signal meant 50% deployed, multiple concurrent
signals could exceed 100% invested, and nothing ever checked. Here every fill
moves cash, exposure is capped, and the equity curve is marked to market.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    side: str                 # "buy" | "sell"
    qty: int
    entry: float
    stop: float
    target: float
    opened_at: dt.datetime
    entry_cost: float = 0.0

    @property
    def long(self) -> bool:
        return self.side == "buy"

    def unrealised(self, price: float) -> float:
        d = price - self.entry
        return d * self.qty if self.long else -d * self.qty

    def notional(self, price: float) -> float:
        return abs(self.qty * price)


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    qty: int
    entry: float
    exit: float
    opened_at: dt.datetime
    closed_at: dt.datetime
    stop: float
    target: float
    exit_reason: str
    gross: float
    costs: float
    net: float
    r_multiple: float
    regime: str = ""
    zone_tf: str = ""
    session: str = ""

    @property
    def win(self) -> bool:
        return self.net > 0

    @property
    def bars_held(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 60.0


@dataclass
class Portfolio:
    starting_equity: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[tuple[dt.datetime, float]] = field(default_factory=list)
    session_start_equity: float = 0.0
    halted_until: dt.date | None = None

    def __post_init__(self):
        if self.cash == 0.0:
            self.cash = self.starting_equity
        self.session_start_equity = self.starting_equity

    # ── valuation ───────────────────────────────────────────────────────────
    def equity(self, prices: dict[str, float]) -> float:
        eq = self.cash
        for s, p in self.positions.items():
            px = prices.get(s, p.entry)
            eq += p.unrealised(px) + (p.qty * p.entry if p.long else 0.0)
            if p.long:
                eq -= p.qty * p.entry      # cash already reduced at entry
        return eq

    def mark(self, prices: dict[str, float]) -> float:
        """Equity marked to market: cash + open-position P&L."""
        return self.cash + sum(
            p.unrealised(prices.get(s, p.entry)) for s, p in self.positions.items())

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(p.notional(prices.get(s, p.entry)) for s, p in self.positions.items())

    # ── lifecycle ───────────────────────────────────────────────────────────
    def can_open(self, symbol: str, qty: int, price: float, *, equity: float,
                 max_positions: int, max_gross_pct: float,
                 prices: dict[str, float]) -> tuple[bool, str]:
        if symbol in self.positions:
            return False, "already holding"
        if len(self.positions) >= max_positions:
            return False, "max concurrent positions"
        if qty < 1:
            return False, "zero quantity"
        if self.gross_exposure(prices) + qty * price > equity * max_gross_pct:
            return False, "gross exposure cap"
        return True, ""

    def open(self, symbol: str, side: str, qty: int, price: float, stop: float,
             target: float, when: dt.datetime, cost: float, **meta) -> Position:
        pos = Position(symbol, side, qty, price, stop, target, when, entry_cost=cost)
        self.positions[symbol] = pos
        self.cash -= cost
        self._meta = getattr(self, "_meta", {})
        self._meta[symbol] = meta
        return pos

    def close(self, symbol: str, price: float, when: dt.datetime, reason: str,
              cost: float) -> ClosedTrade:
        p = self.positions.pop(symbol)
        gross = p.unrealised(price)
        costs = p.entry_cost + cost
        net = gross - costs
        risk = abs(p.entry - p.stop) * p.qty
        meta = getattr(self, "_meta", {}).pop(symbol, {})
        t = ClosedTrade(
            symbol=symbol, side=p.side, qty=p.qty, entry=p.entry, exit=price,
            opened_at=p.opened_at, closed_at=when, stop=p.stop, target=p.target,
            exit_reason=reason, gross=round(gross, 2), costs=round(costs, 2),
            net=round(net, 2), r_multiple=round(net / risk, 3) if risk > 0 else 0.0,
            regime=meta.get("regime", ""), zone_tf=meta.get("zone_tf", ""),
            session=str(when.date()),
        )
        self.cash += gross - cost
        self.closed.append(t)
        return t

    # ── daily risk ──────────────────────────────────────────────────────────
    def start_session(self, day: dt.date, prices: dict[str, float]) -> None:
        self.session_start_equity = self.mark(prices)

    def daily_loss(self, prices: dict[str, float]) -> float:
        return max(0.0, self.session_start_equity - self.mark(prices))

    def is_halted(self, day: dt.date) -> bool:
        return self.halted_until is not None and day <= self.halted_until

    def halt_for_day(self, day: dt.date) -> None:
        self.halted_until = day
