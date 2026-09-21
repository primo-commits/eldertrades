"""
Reconciler tests against a fake broker that reproduces the real failure mode:
Alpaca accepting a close and the position staying open because the bracket's
exit legs are still reserving the shares.

Run: python -m tests.test_reconciler
"""
from __future__ import annotations

import datetime as dt

from elder import config as cfgmod
from elder.broker import OrderResult
from elder.reconciler import Reconciler


class FakeOrder:
    def __init__(self, oid, symbol, otype, status="new", age_min=0):
        self.id = oid
        self.symbol = symbol
        self.order_type = otype
        self.status = status
        self.submitted_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min)


class FakePosition:
    def __init__(self, symbol, qty): self.symbol, self.qty = symbol, str(qty)
    market_value = "10000"


class FakeClient:
    def __init__(self, outer): self.outer = outer
    def get_orders(self, req=None): return list(self.outer.orders)
    def cancel_order_by_id(self, oid):
        self.outer.orders = [o for o in self.outer.orders if o.id != oid]
        self.outer.cancelled.append(oid)


class FakeBroker:
    """Refuses to close while exit legs are live -- Alpaca's actual behaviour."""
    def __init__(self, positions, orders):
        self._pos = list(positions)
        self.orders = list(orders)
        self.cancelled: list[str] = []
        self.close_calls = 0
        self.client = FakeClient(self)

    def positions(self): return list(self._pos)

    def close(self, symbol, dry_run=True):
        self.close_calls += 1
        key = symbol.replace("/", "")
        blocking = [o for o in self.orders if o.symbol == key]
        if blocking:
            return OrderResult(False, None, "rejected", symbol, "close", 0,
                               error="insufficient qty: held by open orders")
        self._pos = [p for p in self._pos if p.symbol != key]
        return OrderResult(True, "CLOSE-1", "closing", symbol, "close", 0)


def test_stuck_close_escalates():
    cfg = cfgmod.load()
    legs = [FakeOrder("tp1", "SPY", "limit"), FakeOrder("sl1", "SPY", "stop")]
    b = FakeBroker([FakePosition("SPY", 100)], legs)
    r = Reconciler(cfg, b, None, dry_run=False, max_close_attempts=5)
    r.request_close("SPY")

    r.reconcile()
    assert b.close_calls == 1 and len(b.positions()) == 1, "attempt 1 blocked, as expected"
    print("  pass 1: close rejected (legs holding shares), position still open")

    rep = r.reconcile()
    print(f"  pass 2: escalated, cancelled {b.cancelled} -> close ok, "
          f"{len(b.positions())} position(s) left")
    assert b.cancelled == ["tp1", "sl1"], "should cancel the blocking legs"
    assert len(b.positions()) == 0, "should be flat after escalation"

    rep = r.reconcile()
    assert "SPY" in rep.confirmed_closes
    print(f"  pass 3: confirmed flat -> {rep.confirmed_closes}")


def test_halts_when_unclosable():
    cfg = cfgmod.load()
    class Immovable(FakeBroker):
        def close(self, symbol, dry_run=True):
            self.close_calls += 1
            return OrderResult(False, None, "rejected", symbol, "close", 0, error="nope")
    class FakeRisk:
        halted = False
        def halt(self, reason):
            self.halted, self.reason = True, reason
    b = Immovable([FakePosition("QQQ", 50)], [])
    risk = FakeRisk()
    r = Reconciler(cfg, b, risk, dry_run=False, max_close_attempts=3)
    r.request_close("QQQ")
    for _ in range(5):
        rep = r.reconcile()
    assert risk.halted, "must halt when a position cannot be closed"
    print(f"  halted after {b.close_calls} attempts: {risk.reason}")
    assert "QQQ" in rep.stuck_closes


def test_orphan_detection():
    cfg = cfgmod.load()
    # TSLA has only a take-profit leg -- no stop. That is the dangerous state.
    orders = [FakeOrder("tp", "TSLA", "limit"),
              FakeOrder("sl", "AAPL", "stop")]
    b = FakeBroker([FakePosition("TSLA", 10), FakePosition("AAPL", 10)], orders)
    r = Reconciler(cfg, b, None, dry_run=True, auto_protect=True)
    rep = r.reconcile()
    print(f"  protected: {rep.protected} | orphaned: {rep.orphaned}")
    assert rep.orphaned == ["TSLA"], "TSLA has no stop -> orphaned"
    assert rep.protected == ["AAPL"]


def test_stale_order_cancel():
    cfg = cfgmod.load()
    orders = [FakeOrder("old", "IWM", "limit", age_min=30),
              FakeOrder("fresh", "XLF", "limit", age_min=1)]
    b = FakeBroker([], orders)
    r = Reconciler(cfg, b, None, dry_run=False, stale_order_minutes=10)
    rep = r.reconcile()
    print(f"  cancelled {rep.stale_orders_cancelled} stale order(s): {b.cancelled}")
    assert b.cancelled == ["old"]


if __name__ == "__main__":
    for fn in (test_stuck_close_escalates, test_halts_when_unclosable,
               test_orphan_detection, test_stale_order_cancel):
        print(f"\n{fn.__name__}:")
        fn()
    print("\nALL RECONCILER TESTS PASS")
