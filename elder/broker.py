"""
Alpaca order placement.

E1 fixed: the original used TakeProfitRequest(limit=...) and
StopLossRequest(stop=...). The real field names are `limit_price` and
`stop_price`. Both raised pydantic ValidationError, which the original swallowed
in a bare `except Exception`, so EVERY live order failed silently and the bot
never placed a single trade. Verified against alpaca-py 0.44.0.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (MarketOrderRequest, StopLossRequest,
                                     TakeProfitRequest)

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    status: str
    symbol: str
    side: str
    qty: int
    error: str | None = None
    dry_run: bool = False


class Broker:
    def __init__(self, cfg, client: TradingClient):
        self.cfg = cfg
        self.client = client

    # ── market state ────────────────────────────────────────────────────────
    def clock(self):
        """Authoritative market clock -- handles DST and holidays, unlike the
        original's hard-coded UTC-5 offset (E18)."""
        return self.client.get_clock()

    def is_open(self) -> bool:
        try:
            return bool(self.clock().is_open)
        except Exception as e:
            log.warning("clock unavailable: %s", e)
            return False

    # ── positions ───────────────────────────────────────────────────────────
    def positions(self) -> list:
        """Broker positions are the source of truth, not an in-memory list."""
        try:
            return list(self.client.get_all_positions())
        except Exception as e:
            log.error("could not fetch positions: %s", e)
            return []

    def position_for(self, symbol: str):
        for p in self.positions():
            if p.symbol == symbol.replace("/", ""):
                return p
        return None

    # ── orders ──────────────────────────────────────────────────────────────
    def submit_bracket(self, symbol: str, qty: int, side: str,
                       take_profit: float, stop_loss: float,
                       dry_run: bool = True) -> OrderResult:
        """
        Market entry with attached TP and SL as a bracket.

        Bracket orders require time_in_force DAY or GTC. Config default is DAY,
        which also means the broker flattens the legs at the close rather than
        carrying an unmanaged position overnight.
        """
        if dry_run:
            return OrderResult(True, f"DRY-{dt.datetime.now():%H%M%S}", "dry_run",
                               symbol, side, qty, dry_run=True)

        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce(self.cfg.execution.time_in_force),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
            )
            resp = self.client.submit_order(req)
            log.info("order accepted %s %s %s x%s id=%s",
                     resp.status, side, symbol, qty, resp.id)
            return OrderResult(True, str(resp.id), str(resp.status), symbol, side, qty)
        except Exception as e:
            # Surfaced, never swallowed -- the original's silent failure is
            # exactly what hid the field-name bug for so long.
            log.error("ORDER REJECTED %s %s x%s: %s", side, symbol, qty, e)
            return OrderResult(False, None, "rejected", symbol, side, qty, error=str(e))

    def close(self, symbol: str, dry_run: bool = True) -> OrderResult:
        if dry_run:
            return OrderResult(True, None, "dry_run_close", symbol, "close", 0, dry_run=True)
        try:
            resp = self.client.close_position(symbol.replace("/", ""))
            return OrderResult(True, str(getattr(resp, "id", "")), "closing", symbol, "close", 0)
        except Exception as e:
            log.error("close failed for %s: %s", symbol, e)
            return OrderResult(False, None, "error", symbol, "close", 0, error=str(e))

    def cancel_all(self) -> None:
        try:
            self.client.cancel_orders()
        except Exception as e:
            log.warning("cancel_orders failed: %s", e)


def make_trading_client(cfg, keyfile: str | None = None) -> TradingClient:
    from .keys import load_keys
    key, secret = load_keys(keyfile, prefer=getattr(cfg, "prefer_credentials", "file"))
    return TradingClient(key, secret, paper=cfg.paper)
