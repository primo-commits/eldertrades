"""
Position reconciliation. Runs faster than the scan loop and treats the broker,
never local memory, as the truth.

WHY THIS EXISTS
---------------
A close request to Alpaca is a market order, not a guarantee. It can be
rejected, partially filled, or accepted and then go nowhere. The common root
cause of "I closed it and it's still open" is worth stating plainly:

    A bracket order's take-profit and stop-loss legs RESERVE the shares. While
    those legs are live, a separate close order for the full quantity can be
    rejected for insufficient quantity -- the shares are already spoken for.

So the escalation below cancels the symbol's open orders before retrying the
close. That single step resolves most stuck closes.

Three jobs, every `reconcile_interval_seconds`:
  1. verify requested closes actually closed, and escalate if not
  2. find ORPHANED positions -- open with no protective stop. This is the
     genuinely dangerous state: unbounded downside with nothing attached.
  3. cancel stale unfilled entry orders

Closes the E16 audit item.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest,
                                     StopLossRequest, TakeProfitRequest)

log = logging.getLogger(__name__)

# Order states that still hold shares / buying power.
LIVE_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding",
               "partially_filled", "held", "calculated", "pending_review",
               "replaced", "pending_replace", "pending_cancel", "stopped",
               "done_for_day", "suspended"}


@dataclass
class CloseIntent:
    symbol: str
    requested_at: dt.datetime
    attempts: int = 0
    last_error: str | None = None
    resolved: bool = False
    escalated: bool = False


@dataclass
class ReconcileReport:
    checked_at: dt.datetime
    open_positions: int = 0
    pending_closes: int = 0
    confirmed_closes: list[str] = field(default_factory=list)
    stuck_closes: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    stale_orders_cancelled: int = 0


class Reconciler:
    """
    Verifies broker state. Safe to run alongside the scan loop -- all mutating
    calls are serialised through `_lock`.
    """

    def __init__(self, cfg, broker, risk=None, *,
                 interval_seconds: int = 30,
                 max_close_attempts: int = 5,
                 stale_order_minutes: int = 10,
                 auto_protect: bool = True,
                 dry_run: bool = True):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.interval = interval_seconds
        self.max_close_attempts = max_close_attempts
        self.stale_order_minutes = stale_order_minutes
        self.auto_protect = auto_protect
        self.dry_run = dry_run

        self._intents: dict[str, CloseIntent] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_report: ReconcileReport | None = None

    # ── public API ──────────────────────────────────────────────────────────
    def request_close(self, symbol: str) -> None:
        """Register intent to close. The loop verifies it actually happened."""
        with self._lock:
            self._intents[symbol] = CloseIntent(symbol, dt.datetime.now(dt.timezone.utc))
        log.info("[reconcile] close requested for %s", symbol)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reconciler", daemon=True)
        self._thread.start()
        log.info("[reconcile] started, interval %ds", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 5)
        log.info("[reconcile] stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("[reconcile] pass failed; continuing")
            self._stop.wait(self.interval)

    # ── one pass ────────────────────────────────────────────────────────────
    def reconcile(self) -> ReconcileReport:
        with self._lock:
            rep = ReconcileReport(checked_at=dt.datetime.now(dt.timezone.utc))
            positions = self.broker.positions()
            held = {p.symbol: p for p in positions}
            rep.open_positions = len(positions)

            orders = self._open_orders()
            by_symbol: dict[str, list] = {}
            for o in orders:
                by_symbol.setdefault(o.symbol, []).append(o)

            self._verify_closes(held, by_symbol, rep)
            self._check_protection(held, by_symbol, rep)
            self._cancel_stale(orders, held, rep)

            self.last_report = rep
            if rep.stuck_closes or rep.orphaned:
                log.warning("[reconcile] %d open | stuck closes %s | ORPHANED %s",
                            rep.open_positions, rep.stuck_closes or "-", rep.orphaned or "-")
            else:
                log.debug("[reconcile] %d open, %d pending close, all protected",
                          rep.open_positions, rep.pending_closes)
            return rep

    # ── job 1: did the close actually happen? ───────────────────────────────
    def _verify_closes(self, held: dict, by_symbol: dict, rep: ReconcileReport) -> None:
        for symbol, intent in list(self._intents.items()):
            if intent.resolved:
                continue
            key = symbol.replace("/", "")

            if key not in held:
                intent.resolved = True
                rep.confirmed_closes.append(symbol)
                log.info("[reconcile] %s confirmed flat after %d attempt(s)",
                         symbol, intent.attempts)
                continue

            rep.pending_closes += 1
            intent.attempts += 1

            if intent.attempts > self.max_close_attempts:
                rep.stuck_closes.append(symbol)
                msg = (f"{symbol} STILL OPEN after {intent.attempts} close attempts "
                       f"(qty {getattr(held[key], 'qty', '?')}). Last error: "
                       f"{intent.last_error}. MANUAL INTERVENTION REQUIRED.")
                log.error("[reconcile] %s", msg)
                if self.risk is not None:
                    self.risk.halt(f"cannot close {symbol}")
                continue

            # Escalation: from attempt 2 onward, clear the orders holding the
            # shares first. Bracket legs reserve quantity and will cause a
            # close order to be rejected for insufficient shares.
            if intent.attempts >= 2 and not intent.escalated:
                legs = by_symbol.get(key, [])
                if legs:
                    log.warning("[reconcile] %s: cancelling %d open order(s) that may be "
                                "reserving the shares, then retrying close",
                                symbol, len(legs))
                    self._cancel_for(key, legs)
                    intent.escalated = True

            res = self.broker.close(symbol, dry_run=self.dry_run)
            if not res.ok:
                intent.last_error = res.error
                log.error("[reconcile] close attempt %d for %s failed: %s",
                          intent.attempts, symbol, res.error)
            else:
                log.info("[reconcile] close attempt %d submitted for %s",
                         intent.attempts, symbol)

        # Forget resolved intents once they are old enough to be uninteresting.
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
        for s, i in list(self._intents.items()):
            if i.resolved and i.requested_at < cutoff:
                del self._intents[s]

    # ── job 2: is every position protected? ─────────────────────────────────
    def _check_protection(self, held: dict, by_symbol: dict, rep: ReconcileReport) -> None:
        """
        A position with no live stop is the worst state this bot can be in: the
        bracket leg was rejected or cancelled and downside is now unbounded.
        """
        for key, pos in held.items():
            if key in {s.replace("/", "") for s, i in self._intents.items() if not i.resolved}:
                continue                      # being closed anyway
            legs = by_symbol.get(key, [])
            has_stop = any(self._is_stop(o) for o in legs)
            if has_stop:
                rep.protected.append(key)
                continue

            rep.orphaned.append(key)
            log.error("[reconcile] ORPHANED POSITION %s qty=%s: no protective stop order",
                      key, getattr(pos, "qty", "?"))
            if self.auto_protect:
                self._protect(key, pos)

    @staticmethod
    def _is_stop(order) -> bool:
        t = str(getattr(order, "order_type", getattr(order, "type", ""))).lower()
        status = str(getattr(order, "status", "")).lower().split(".")[-1]
        return ("stop" in t) and (status in LIVE_STATES)

    def _protect(self, symbol: str, pos) -> None:
        """
        Flatten an unprotected position rather than guess a stop level.

        Attaching a stop after the fact requires a price the bot no longer has
        context for -- the zone that justified it may be long gone. Flat is a
        known state; a guessed stop is not.
        """
        log.warning("[reconcile] auto-protect: flattening unprotected %s", symbol)
        if self.dry_run:
            log.info("[reconcile] (dry run) would flatten %s", symbol)
            return
        self.request_close(symbol)
        res = self.broker.close(symbol, dry_run=False)
        if not res.ok:
            log.error("[reconcile] auto-protect close failed for %s: %s", symbol, res.error)

    # ── job 3: stale unfilled entries ───────────────────────────────────────
    def _cancel_stale(self, orders: list, held: dict, rep: ReconcileReport) -> None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=self.stale_order_minutes)
        for o in orders:
            submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            if submitted is None or submitted > cutoff:
                continue
            if o.symbol in held:
                continue          # exit legs on a live position: leave alone
            status = str(getattr(o, "status", "")).lower().split(".")[-1]
            if status not in {"new", "accepted", "pending_new"}:
                continue
            log.info("[reconcile] cancelling stale order %s on %s (age > %dmin)",
                     o.id, o.symbol, self.stale_order_minutes)
            if not self.dry_run:
                try:
                    self.broker.client.cancel_order_by_id(o.id)
                except Exception as e:
                    log.warning("[reconcile] cancel failed for %s: %s", o.id, e)
            rep.stale_orders_cancelled += 1

    # ── helpers ─────────────────────────────────────────────────────────────
    def _open_orders(self) -> list:
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
            return list(self.broker.client.get_orders(req))
        except Exception as e:
            log.error("[reconcile] could not fetch open orders: %s", e)
            return []

    def _cancel_for(self, symbol: str, legs: list) -> None:
        if self.dry_run:
            return
        for o in legs:
            try:
                self.broker.client.cancel_order_by_id(o.id)
            except Exception as e:
                log.warning("[reconcile] could not cancel %s: %s", o.id, e)
        time.sleep(1.0)      # let the cancels settle before re-closing
