"""
Live scan loop.

Safe by default: placing real orders requires --live. The original inverted
this -- `dry_run = args.dry_run and not args.live` meant running with no flags
at all submitted real orders (E17).
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from pathlib import Path

import pandas as pd

from . import config as cfgmod
from . import journal, strategy, universe
from .broker import Broker, make_trading_client
from .data import MarketData, regular_session
from .keys import MissingCredentials
from .reconciler import Reconciler
from .risk import RiskEngine

log = logging.getLogger("elder")
_STOP = False


def _setup_logging(verbose: bool) -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"logs/elder_{dt.date.today():%Y-%m-%d}.log"),
        ],
    )


def _handle_sigint(signum, frame):
    global _STOP
    _STOP = True
    log.info("interrupt received -- finishing this scan then stopping")


def fetch_all(md: MarketData, symbols: list[str], cfg) -> dict[str, dict[str, pd.DataFrame]]:
    """Fetch every timeframe for every symbol; one paginated request per timeframe."""
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=cfg.data.lookback_days)
    wanted = sorted({*cfg.data.timeframes,
                     cfg.strategy.volume_profile["timeframe"],
                     *cfg.strategy.zones["timeframes"]},
                    key=lambda s: s)

    out: dict[str, dict[str, pd.DataFrame]] = {s: {} for s in symbols}
    for tf in wanted:
        try:
            got = md.bars(symbols, tf, start=start, end=end)
        except Exception as e:
            log.error("bar fetch failed for %s: %s", tf, e)
            continue
        for sym, df in got.items():
            if df.empty:
                continue
            out[sym][tf] = regular_session(
                df, tz=cfg.data.session_tz,
                open_time=cfg.data.rth_open, close_time=cfg.data.rth_close,
                skip_first_minutes=cfg.data.skip_first_minutes,
                skip_last_minutes=cfg.data.skip_last_minutes)

    # The 4H bias chart is resampled from 1H, anchored to the RTH open.
    bias_tf = cfg.strategy.context["structure_timeframe"]
    if bias_tf not in wanted:
        from .data import session_anchored
        for sym, frames in out.items():
            if "1Hour" in frames and not frames["1Hour"].empty:
                frames[bias_tf] = session_anchored(frames["1Hour"], "4h",
                                                   tz=cfg.data.session_tz,
                                                   open_time=cfg.data.rth_open)
    return out


def scan_once(md, broker, risk, cfg, symbols, *, dry_run: bool) -> dict:
    """One full pass over the universe. Returns a summary dict."""
    equity = risk.equity()
    risk.roll_session(equity)
    positions = broker.positions()
    held = {p.symbol for p in positions}
    bucket_of = {i.symbol: i.bucket for i in universe.ALL}

    log.info("scan: equity $%s | %d open position(s) | %s",
             f"{equity:,.2f}", len(positions), "DRY RUN" if dry_run else "LIVE")

    data = fetch_all(md, symbols, cfg)
    setups, rejections, orders = [], [], []

    for sym in symbols:
        frames = data.get(sym, {})
        if not frames:
            rejections.append(strategy.Rejection(sym, "context", "no data returned"))
            continue
        if sym.replace("/", "") in held:
            rejections.append(strategy.Rejection(sym, "location", "already holding"))
            continue

        try:
            setup, rejects = strategy.evaluate(sym, bars_by_tf=frames, cfg=cfg)
        except Exception as e:
            log.exception("evaluate failed for %s", sym)
            rejections.append(strategy.Rejection(sym, "error", str(e)))
            continue

        rejections.extend(rejects)
        if setup is None:
            continue

        exec_tf = min(frames, key=lambda k: strategy._tf_minutes(k))
        bar_vol = float(frames[exec_tf]["volume"].tail(20).mean())
        decision = risk.check(setup, equity=equity, open_positions=positions,
                              bucket_of=bucket_of, bar_volume=bar_vol)
        if not decision.allowed:
            rejections.append(strategy.Rejection(sym, "risk", decision.reason))
            log.info("  %s BLOCKED by risk: %s", sym, decision.reason)
            continue

        log.info("  SETUP %s", setup.summary())
        for n in setup.notes:
            log.info("        note: %s", n)

        order = broker.submit_bracket(sym, decision.sizing.qty, setup.side,
                                      setup.target, setup.stop, dry_run=dry_run)
        if order.ok:
            risk.record_fill()
            positions = broker.positions()
        else:
            log.error("  %s order failed: %s", sym, order.error)

        journal.log_setup(setup, decision.sizing, order)
        setups.append(setup)
        orders.append(order)

    journal.log_rejections(rejections)

    by_stage: dict[str, int] = {}
    for r in rejections:
        by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
    log.info("scan complete: %d setup(s), %d rejection(s) %s",
             len(setups), len(rejections), by_stage or "")
    return {"equity": equity, "setups": setups, "orders": orders,
            "rejections": rejections, "risk": risk.status(equity)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="elder", description="Elder Trades scanner")
    p.add_argument("--live", action="store_true",
                   help="submit real orders (paper account). Omit for dry run.")
    p.add_argument("--once", action="store_true", help="single scan, then exit")
    p.add_argument("--tier", default=None, help="universe tier (default from config)")
    p.add_argument("--symbols", default=None, help="comma-separated override")
    p.add_argument("--interval", type=int, default=None, help="minutes between scans")
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--ignore-clock", action="store_true",
                   help="scan even when the market is closed (data inspection only)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)
    signal.signal(signal.SIGINT, _handle_sigint)

    cfg = cfgmod.load(args.config)
    for w in cfg.warnings():
        log.warning("config: %s", w)

    dry_run = not args.live
    interval = args.interval or cfg.execution.scan_interval_minutes

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [i.symbol for i in universe.get_tier(args.tier or cfg.universe_tier)]

    log.info("=" * 68)
    log.info("ELDER TRADES  |  %s  |  %d symbols  |  feed=%s",
             "DRY RUN" if dry_run else "LIVE (paper)", len(symbols), cfg.data.feed)
    log.info("universe: %s", ", ".join(symbols))
    log.info("=" * 68)

    try:
        md = MarketData(feed=cfg.data.feed, session_tz=cfg.data.session_tz,
                        prefer=cfg.prefer_credentials)
        client = make_trading_client(cfg)
        broker = Broker(cfg, client)
        risk = RiskEngine(cfg, client)
    except MissingCredentials as e:
        log.error("%s", e)
        return 1
    except Exception as e:
        log.error("startup failed: %s", e)
        return 1

    try:
        acct = client.get_account()
        log.info("account %s | equity $%s | buying power $%s | paper=%s",
                 acct.account_number, f"{float(acct.equity):,.2f}",
                 f"{float(acct.buying_power):,.2f}", cfg.paper)
    except Exception as e:
        log.error("cannot reach Alpaca: %s", e)
        return 1

    recon = None
    if cfg.reconcile.enabled:
        recon = Reconciler(cfg, broker, risk,
                           interval_seconds=cfg.reconcile.interval_seconds,
                           max_close_attempts=cfg.reconcile.max_close_attempts,
                           stale_order_minutes=cfg.reconcile.stale_order_minutes,
                           auto_protect=cfg.reconcile.auto_protect,
                           dry_run=dry_run)
        recon.start()

    while not _STOP:
        try:
            if not args.ignore_clock and not broker.is_open():
                log.info("market closed -- next check in %d min", interval)
            else:
                scan_once(md, broker, risk, cfg, symbols, dry_run=dry_run)
        except Exception:
            log.exception("scan failed; continuing")

        if args.once or _STOP:
            break
        for _ in range(interval * 60):
            if _STOP:
                break
            time.sleep(1)

    if recon is not None:
        recon.stop()
        if recon.last_report is not None:
            r = recon.last_report
            log.info("final reconcile: %d open | orphaned %s | stuck %s",
                     r.open_positions, r.orphaned or "-", r.stuck_closes or "-")
    log.info("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
