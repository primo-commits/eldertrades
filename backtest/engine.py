"""
Event-driven replay of the Elder Trades strategy.

The original backtester tested something else entirely -- an index MA crossover
that bought a basket. This one runs the ACTUAL pipeline: 4H structure, volume
profile, supply/demand zones, exhaustion-then-flip confirmation, invalidation
stops and POC/HVN targets.

NO LOOK-AHEAD (E19)
-------------------
At decision bar i only bars[:i+1] are visible, and a fill happens at the OPEN
of bar i+1. Every timeframe is precomputed once for the whole period and then
SLICED by timestamp at each decision point -- never recomputed from future data.

Heavy context (structure, profile, zones) is recomputed every `rescan_bars`
rather than every bar. That is both a performance decision and a faithful one:
Elder plans context and location in pre-market and then watches for triggers
intraday.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
from dataclasses import dataclass, field

import pandas as pd

from elder import structure as st
from elder import volume_profile as vp
from elder import zones as zn
from elder.confirmation import confirm
from elder.data import pandas_rule, session_anchored
from elder.indicators import atr

from .fills import CostModel, entry_fill, eod_fill, exit_fill
from .portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    trades: list
    equity_curve: pd.Series
    starting_equity: float
    params: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1]) if len(self.equity_curve) else self.starting_equity


def build_frames(m5: pd.DataFrame, cfg) -> dict[str, pd.DataFrame]:
    """Precompute every timeframe ONCE. Sliced later, never rebuilt."""
    out = {cfg.data.base_timeframe: m5}
    wanted = {*cfg.data.timeframes, cfg.strategy.volume_profile["timeframe"],
              cfg.strategy.context["structure_timeframe"],
              *cfg.strategy.zones["timeframes"]} - {cfg.data.base_timeframe}
    for tf in wanted:
        out[tf] = session_anchored(m5, pandas_rule(tf), tz=cfg.data.session_tz,
                                   open_time=cfg.data.rth_open)
    return out


def _slice(frames: dict[str, pd.DataFrame], upto: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Everything strictly at or before `upto`. This is the no-look-ahead gate."""
    return {tf: df.loc[:upto] for tf, df in frames.items()}


def _size(equity: float, entry: float, stop: float, cfg) -> int:
    r = cfg.risk
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return 0
    q_risk = (equity * r.risk_per_trade_pct) / per_share
    q_cap = (equity * r.max_position_notional_pct) / entry
    return max(0, int(min(q_risk, q_cap)))


def _context_and_zones(sliced: dict[str, pd.DataFrame], cfg):
    """Returns (regime_read, profile, poc_trend, zones) or None if unusable."""
    s = cfg.strategy
    sb = sliced.get(s.context["structure_timeframe"], pd.DataFrame())
    if sb.empty:
        return None
    read = st.classify(sb, bars_each_side=s.context["swing_bars"],
                       required_highs=s.context["bullish_higher_highs"],
                       required_lows=s.context["bullish_higher_lows"],
                       equal_tolerance=s.context["equal_level_tolerance"])
    if not read.has_bias:
        return ("no_bias", read.regime, None, None, None)

    vb = sliced.get(s.volume_profile["timeframe"], pd.DataFrame())
    profiles = vp.session_profiles(
        vb, bins=s.volume_profile["price_bins"],
        value_area_pct=s.volume_profile["value_area_pct"],
        hvn_percentile=s.volume_profile["hvn_percentile"],
        lvn_percentile=s.volume_profile["lvn_percentile"])
    if not profiles:
        return ("no_profile", read.regime, None, None, None)
    profile = profiles[sorted(profiles)[-1]]
    trend = vp.poc_trend(profiles)

    if trend != "neutral" and ((trend == "bullish") != (read.direction > 0)) \
            and s.context.get("require_poc_alignment", True):
        return ("poc_conflict", read.regime, None, None, None)

    found: list[zn.Zone] = []
    for tf in s.zones["timeframes"]:
        b = sliced.get(tf)
        if b is None or b.empty:
            continue
        found += zn.find_zones(b, timeframe=tf,
                               expansion_atr_mult=s.zones["expansion_atr_mult"],
                               consolidation_max_bars=s.zones["consolidation_max_bars"],
                               consolidation_atr_mult=s.zones["consolidation_atr_mult"],
                               max_touches=s.zones["max_touches"],
                               max_age_bars=s.zones["max_age_bars"])
    zn.tag_lvn_confluence(found, profile.lvn)
    return ("ok", read, profile, trend, found)


def run(bars_by_symbol: dict[str, pd.DataFrame], cfg, *,
        starting_equity: float = 1_000_000.0,
        rescan_bars: int = 26,
        seed: int = 12345,
        costs: CostModel | None = None,
        params: dict | None = None) -> BacktestResult:
    """
    Replay the strategy bar by bar.

    `seed` is applied to a LOCAL Random, so two parameter sets see identical
    random draws and are genuinely comparable (E21). The original seeded the
    global RNG once and let a 100-combination sweep consume different slices of
    the stream, making combos incomparable.
    """
    rng = random.Random(seed)
    costs = costs or CostModel(
        spread_per_share=cfg.screening.est_spread_per_share,
        slippage_bps=cfg.screening.est_slippage_bps)

    frames = {s: build_frames(df, cfg) for s, df in bars_by_symbol.items() if not df.empty}
    base = cfg.data.base_timeframe
    if not frames:
        return BacktestResult([], pd.Series(dtype=float), starting_equity, params or {})

    all_ts = sorted({t for f in frames.values() for t in f[base].index})
    sessions = sorted({t.date() for t in all_ts})
    pf = Portfolio(starting_equity=starting_equity)
    curve: list[tuple[dt.datetime, float]] = []
    skipped: dict[str, int] = {}
    pending: list[tuple[str, dict]] = []

    for day in sessions:
        day_ts = [t for t in all_ts if t.date() == day]
        if not day_ts:
            continue
        prices = {s: float(f[base].loc[:day_ts[0]]["close"].iloc[-1])
                  for s, f in frames.items() if len(f[base].loc[:day_ts[0]])}
        pf.start_session(day, prices)
        ctx_cache: dict[str, tuple] = {}
        bars_since_rescan = rescan_bars   # force a rescan at the open

        for bi, ts in enumerate(day_ts):
            # ---- current prices -------------------------------------------
            for s, f in frames.items():
                if ts in f[base].index:
                    prices[s] = float(f[base].at[ts, "close"])

            # ---- 1. fill anything queued on the PREVIOUS bar --------------
            equity = pf.mark(prices)
            for sym, plan in pending:
                f = frames[sym][base]
                if ts not in f.index:
                    continue
                bar = f.loc[ts]
                fill = entry_fill(float(bar["open"]), plan["side"], costs, rng)
                qty = _size(equity, fill.price, plan["stop"], cfg)
                ok, why = pf.can_open(sym, qty, fill.price, equity=equity,
                                      max_positions=cfg.risk.max_concurrent_positions,
                                      max_gross_pct=cfg.risk.max_gross_exposure_pct,
                                      prices=prices)
                if not ok:
                    skipped[why] = skipped.get(why, 0) + 1
                    continue
                pf.open(sym, plan["side"], qty, fill.price, plan["stop"],
                        plan["target"], ts, costs.entry_cost(qty, fill.price),
                        regime=plan.get("regime", ""), zone_tf=plan.get("zone_tf", ""))
            pending = []

            # ---- 2. manage open positions on THIS bar ---------------------
            last_bar = bi == len(day_ts) - 1
            for sym in list(pf.positions):
                f = frames[sym][base]
                if ts not in f.index:
                    continue
                pos = pf.positions[sym]
                bar = f.loc[ts]
                fill = exit_fill(bar, pos.stop, pos.target, pos.side, costs, rng)
                if fill is None and last_bar:
                    fill = eod_fill(bar, pos.side, costs, rng)
                if fill is not None:
                    pf.close(sym, fill.price, ts, fill.reason,
                             costs.exit_cost(pos.qty, fill.price))

            equity = pf.mark(prices)
            curve.append((ts, equity))

            if last_bar:
                continue

            # ---- 3. daily risk gate ---------------------------------------
            if pf.is_halted(day):
                continue
            if pf.daily_loss(prices) >= pf.session_start_equity * cfg.risk.daily_loss_limit_pct:
                pf.halt_for_day(day)
                skipped["daily loss limit"] = skipped.get("daily loss limit", 0) + 1
                continue

            # ---- 4. refresh context / zones periodically ------------------
            bars_since_rescan += 1
            if bars_since_rescan >= rescan_bars:
                bars_since_rescan = 0
                for sym, f in frames.items():
                    ctx_cache[sym] = _context_and_zones(_slice(f, ts), cfg)

            # ---- 5. look for an entry trigger -----------------------------
            for sym, f in frames.items():
                if sym in pf.positions or any(p[0] == sym for p in pending):
                    continue
                cached = ctx_cache.get(sym)
                if not cached or cached[0] != "ok":
                    if cached:
                        skipped[cached[0]] = skipped.get(cached[0], 0) + 1
                    continue
                _, read, profile, trend, found = cached

                m5 = f[base].loc[:ts]
                if len(m5) < 30:
                    continue
                price = float(m5["close"].iloc[-1])
                cur_atr = float(atr(m5, 14).iloc[-1])
                if not (cur_atr > 0):
                    continue

                s = cfg.strategy
                mult = s.zones["max_distance_atr"]
                by_tf = {}
                for tf, b in _slice(f, ts).items():
                    if len(b) < 15:
                        continue
                    a = float(atr(b, 14).iloc[-1])
                    if a > 0:
                        by_tf[tf] = (a * mult) / price
                zone = zn.select_zone(found, price, read.direction,
                                      max_distance_pct=(cur_atr * mult) / price,
                                      max_distance_by_tf=by_tf,
                                      prefer_higher_timeframe=s.zones["timeframe_priority_high_wins"])
                if zone is None:
                    skipped["not at a zone"] = skipped.get("not at a zone", 0) + 1
                    continue

                c = confirm(m5, zone, mode=s.confirmation.get("mode", "bars"),
                            exhaustion_bars=s.confirmation["exhaustion_bars"],
                            require_declining_volume=s.confirmation["exhaustion_require_declining_volume"],
                            flip_atr_mult=s.confirmation["flip_atr_mult"],
                            flip_volume_mult=s.confirmation["flip_volume_mult"])
                if not c.confirmed:
                    skipped["no confirmation"] = skipped.get("no confirmation", 0) + 1
                    continue

                interaction = s.confirmation["exhaustion_bars"] + 1
                win = m5.tail(max(interaction, 2))
                buf = cur_atr * s.exits["stop_atr_buffer"]
                stop = (float(win["low"].min()) - buf if read.direction > 0
                        else float(win["high"].max()) + buf)
                risk = abs(price - stop)
                if risk <= 0 or risk > cur_atr * s.exits["stop_atr_max_mult"]:
                    skipped["stop too wide"] = skipped.get("stop too wide", 0) + 1
                    continue

                from elder.strategy import _target_price
                target = _target_price(profile, price, read.direction,
                                       risk=risk, min_rr=s.exits["min_reward_risk"])
                if target is None:
                    skipped["no target at min R"] = skipped.get("no target at min R", 0) + 1
                    continue

                pending.append((sym, {
                    "side": "buy" if read.direction > 0 else "sell",
                    "stop": round(stop, 4), "target": round(target, 4),
                    "regime": read.regime, "zone_tf": zone.timeframe,
                }))

        # flat at the end of every session -- day orders do not carry over
        for sym in list(pf.positions):
            f = frames[sym][base]
            if day_ts[-1] in f.index:
                pos = pf.positions[sym]
                fill = eod_fill(f.loc[day_ts[-1]], pos.side, costs, rng)
                pf.close(sym, fill.price, day_ts[-1], "eod_flat",
                         costs.exit_cost(pos.qty, fill.price))
        pending = []
        pf.halted_until = None

    eq = pd.Series({t: v for t, v in curve}).sort_index()
    return BacktestResult(pf.closed, eq, starting_equity, params or {}, skipped)
