"""
Pre-open checks. Run this the night before, and again before you start the bot.

Every check that can fail at 09:30 is exercised here at a time when you can
still do something about it. Exits non-zero if anything blocking fails.

    python -m elder.preflight
    python -m elder.preflight --tier starter --scan
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

PASS, FAIL, WARN, INFO = "  [OK]  ", "  [FAIL]", "  [WARN]", "  [--]  "


class Checks:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def ok(self, msg):   print(f"{PASS} {msg}")
    def info(self, msg): print(f"{INFO} {msg}")
    def warn(self, msg):
        self.warned += 1
        print(f"{WARN} {msg}")
    def fail(self, msg):
        self.failed += 1
        print(f"{FAIL} {msg}")

    def section(self, title):
        print(f"\n{title}\n{'-' * len(title)}")


def run(tier: str | None = None, do_scan: bool = False,
        config_path: str | None = None) -> int:
    c = Checks()
    print("=" * 66)
    print("  ELDER TRADES -- PREFLIGHT")
    print(f"  {dt.datetime.now():%Y-%m-%d %H:%M:%S} local")
    print("=" * 66)

    # ── 1. dependencies ─────────────────────────────────────────────────────
    c.section("1. Dependencies")
    try:
        import pandas, numpy, yaml, alpaca
        c.ok(f"pandas {pandas.__version__}, numpy {numpy.__version__}, "
             f"alpaca-py {getattr(alpaca, '__version__', '?')}")
    except ImportError as e:
        c.fail(f"missing dependency: {e}. Run: pip install -r requirements.txt")
        return _finish(c)

    # ── 2. config ───────────────────────────────────────────────────────────
    c.section("2. Configuration")
    try:
        from . import config as cfgmod
        cfg = cfgmod.load(config_path)
        c.ok(f"config.yaml loaded | feed={cfg.data.feed} | paper={cfg.paper}")
        c.info(f"risk/trade {cfg.risk.risk_per_trade_pct:.2%} | "
               f"daily loss {cfg.risk.daily_loss_limit_pct:.2%} | "
               f"max positions {cfg.risk.max_concurrent_positions}")
        c.info(f"confirmation mode: {cfg.strategy.confirmation.get('mode')} | "
               f"reconcile every {cfg.reconcile.interval_seconds}s")
        for w in cfg.warnings():
            c.warn(w)
    except Exception as e:
        c.fail(f"config failed to load: {e}")
        return _finish(c)

    # ── 3. credentials ──────────────────────────────────────────────────────
    c.section("3. Credentials")
    try:
        from .keys import load_keys
        key, _secret = load_keys()
        c.ok(f"API key found (…{key[-4:]})")
    except Exception as e:
        c.fail(str(e).split("\n")[0])
        print("\n".join("        " + ln for ln in str(e).split("\n")[1:]))
        return _finish(c)

    # ── 4. trading account ──────────────────────────────────────────────────
    c.section("4. Trading account")
    try:
        from .broker import Broker, make_trading_client
        client = make_trading_client(cfg)
        broker = Broker(cfg, client)
        acct = client.get_account()
        equity = float(acct.equity)
        c.ok(f"account {acct.account_number} reachable | equity ${equity:,.2f}")
        c.info(f"buying power ${float(acct.buying_power):,.2f} | "
               f"cash ${float(acct.cash):,.2f}")

        status = str(getattr(acct, "status", "")).split(".")[-1]
        if status.upper() != "ACTIVE":
            c.fail(f"account status is {status}, not ACTIVE -- it will not trade")
        else:
            c.ok("account status ACTIVE")

        for flag, label in (("trading_blocked", "trading"),
                            ("account_blocked", "account"),
                            ("transfers_blocked", "transfers")):
            if bool(getattr(acct, flag, False)):
                c.fail(f"{label} is BLOCKED on this account")

        if bool(getattr(acct, "shorting_enabled", False)):
            c.ok("shorting enabled -- bearish setups can be taken")
        else:
            c.warn("shorting DISABLED -- every bearish setup will be rejected by "
                   "the broker. Enable margin on the paper account, or expect "
                   "long-only behaviour.")

        # Equity vs the size the config assumes.
        if equity < cfg.nominal_equity * 0.5:
            c.warn(f"live equity ${equity:,.0f} is far below config nominal "
                   f"${cfg.nominal_equity:,.0f}. Sizing follows LIVE equity, so "
                   f"positions will be much smaller than you may expect. Reset "
                   f"the paper account balance in the Alpaca dashboard.")
        if float(getattr(acct, "daytrade_count", 0) or 0) >= 3 and equity < 25_000:
            c.warn("day-trade count at PDT threshold with equity under $25k")
    except Exception as e:
        c.fail(f"cannot reach Alpaca trading API: {e}")
        return _finish(c)

    # ── 5. market clock ─────────────────────────────────────────────────────
    c.section("5. Market clock")
    try:
        clock = broker.clock()
        c.ok(f"market is {'OPEN' if clock.is_open else 'CLOSED'} right now")
        c.info(f"next open:  {clock.next_open}")
        c.info(f"next close: {clock.next_close}")
        if not clock.is_open:
            delta = clock.next_open - clock.timestamp
            c.info(f"opens in {delta.total_seconds() / 3600:.1f} hours")
    except Exception as e:
        c.fail(f"clock unavailable: {e}")

    # ── 6. positions and orders ─────────────────────────────────────────────
    c.section("6. Existing state")
    try:
        positions = broker.positions()
        if positions:
            c.warn(f"{len(positions)} position(s) already open -- the bot will "
                   f"skip these symbols:")
            for p in positions:
                c.info(f"    {p.symbol} qty={p.qty} mv=${float(p.market_value):,.2f}")
        else:
            c.ok("no open positions")

        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = list(client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)))
        if orders:
            c.warn(f"{len(orders)} open order(s) carried over")
            for o in orders[:10]:
                c.info(f"    {o.symbol} {o.side} {o.qty} {o.order_type} [{o.status}]")
        else:
            c.ok("no open orders")

        from .risk import RiskState
        st = RiskState.load()
        if st.halted:
            c.fail(f"RISK ENGINE IS HALTED: {st.halt_reason}. "
                   f"Delete state/risk_state.json to clear once resolved.")
        elif st.session_date:
            c.info(f"risk state from {st.session_date}: "
                   f"{st.trades_today} trade(s), peak ${st.peak_equity:,.2f}")
    except Exception as e:
        c.warn(f"could not read existing state: {e}")

    # ── 7. market data ──────────────────────────────────────────────────────
    c.section("7. Market data")
    from . import universe
    syms = [i.symbol for i in universe.get_tier(tier or cfg.universe_tier)]
    c.info(f"universe '{tier or cfg.universe_tier}': {len(syms)} symbols")

    frames_by_symbol = {}
    try:
        from .data import MarketData, regular_session, session_anchored
        md = MarketData(feed=cfg.data.feed, session_tz=cfg.data.session_tz)
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=cfg.data.lookback_days)

        # Every timeframe the strategy actually reads, with the minimum bar
        # count each consumer needs (find_zones needs atr_period + lookback + 2).
        needed = {
            "5Min": 60,
            "15Min": 26,
            "30Min": 26,
            "1Hour": 26,
        }
        for tf, minimum in needed.items():
            got = md.bars(syms, tf, start=start, end=end)
            counts, stale = {}, []
            for s, df in got.items():
                rth = regular_session(df, tz=cfg.data.session_tz,
                                      open_time=cfg.data.rth_open,
                                      close_time=cfg.data.rth_close)
                counts[s] = len(rth)
                frames_by_symbol.setdefault(s, {})[tf] = rth
                if not rth.empty:
                    age_h = (end - rth.index[-1].tz_convert("UTC")).total_seconds() / 3600
                    if age_h > 96:
                        stale.append(f"{s} ({age_h:.0f}h)")
            thin = [s for s, n in counts.items() if n < minimum]
            if thin:
                c.fail(f"{tf}: {len(thin)} symbol(s) under {minimum} bars -- "
                       f"{', '.join(thin[:8])}{'…' if len(thin) > 8 else ''}")
            else:
                c.ok(f"{tf}: all {len(syms)} symbols have >= {minimum} bars "
                     f"(median {sorted(counts.values())[len(counts)//2]})")
            if stale:
                c.warn(f"{tf}: stale data for {', '.join(stale[:5])}")

        # 4H bias frame is resampled from 1H
        bias_tf = cfg.strategy.context["structure_timeframe"]
        thin4h = []
        for s, frames in frames_by_symbol.items():
            if "1Hour" in frames and not frames["1Hour"].empty:
                h4 = session_anchored(frames["1Hour"], "4h", tz=cfg.data.session_tz,
                                      open_time=cfg.data.rth_open)
                frames[bias_tf] = h4
                if len(h4) < 2 * cfg.strategy.context["swing_bars"] + 1:
                    thin4h.append(s)
        if thin4h:
            c.fail(f"{bias_tf}: too few bars to find swings for {', '.join(thin4h[:8])}")
        else:
            c.ok(f"{bias_tf}: resampled from 1Hour for all symbols")
    except Exception as e:
        c.fail(f"market data failed: {e}")

    # ── 8. filesystem ───────────────────────────────────────────────────────
    c.section("8. Filesystem")
    for d in ("logs", "journal", "state"):
        p = Path(d)
        try:
            p.mkdir(exist_ok=True)
            probe = p / ".preflight"
            probe.write_text("ok")
            probe.unlink()
            c.ok(f"{d}/ writable")
        except OSError as e:
            c.fail(f"{d}/ not writable: {e}")

    # ── 9. dry scan ─────────────────────────────────────────────────────────
    if do_scan and frames_by_symbol:
        c.section("9. Dry scan (no orders)")
        from . import strategy
        stages: dict[str, int] = {}
        setups = 0
        for s in syms:
            frames = {k: v for k, v in frames_by_symbol.get(s, {}).items()
                      if v is not None and not v.empty}
            if not frames:
                continue
            try:
                setup, rejects = strategy.evaluate(s, bars_by_tf=frames, cfg=cfg)
            except Exception as e:
                c.warn(f"{s}: evaluate raised {type(e).__name__}: {e}")
                continue
            for r in rejects:
                stages[r.stage] = stages.get(r.stage, 0) + 1
            if setup:
                setups += 1
                c.ok(f"SETUP {setup.summary()}")
        c.info(f"{setups} setup(s); rejections by stage: {stages or 'none'}")
        if setups == 0:
            c.info("no setups is normal -- this is one snapshot, and the market "
                   "is likely closed. Check the stage counts above: if everything "
                   "stops at 'context', your universe simply has no 4H bias today.")

    return _finish(c)


def _finish(c: Checks) -> int:
    print("\n" + "=" * 66)
    if c.failed:
        print(f"  PREFLIGHT FAILED -- {c.failed} blocking issue(s), {c.warned} warning(s)")
        print("  Fix the [FAIL] lines above before starting the bot.")
    elif c.warned:
        print(f"  PREFLIGHT PASSED with {c.warned} warning(s)")
        print("  Safe to start. Read the [WARN] lines so nothing surprises you.")
    else:
        print("  PREFLIGHT PASSED -- clean")
    print("=" * 66)
    return 1 if c.failed else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="elder.preflight")
    p.add_argument("--tier", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--scan", action="store_true",
                   help="also run a full strategy evaluation against live data")
    a = p.parse_args(argv)
    return run(tier=a.tier, do_scan=a.scan, config_path=a.config)


if __name__ == "__main__":
    sys.exit(main())
