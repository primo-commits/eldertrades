"""
Risk engine. Reads LIVE equity from Alpaca on every evaluation.

This replaces the original's module-level globals, which were the single most
dangerous defect in the codebase. There, PAPER_BALANCE was rebound by --balance
but DAILY_HIGH, ALL_TIME_PEAK and every GFT_* constant stayed pinned at their
$100,000 import-time values. On a $1M account that made `daily_used` come out
at -$898,500, so every limit passed. You could lose $800,000 before a single
check fired. Verified by reproduction.

Here nothing is a global, every limit is a percentage of live equity, and state
that must survive a restart is persisted to disk.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "risk_state.json"


@dataclass
class RiskState:
    """Persisted across restarts -- a crashed bot must not forget its drawdown."""
    session_date: str = ""
    session_start_equity: float = 0.0
    peak_equity: float = 0.0
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "RiskState":
        try:
            return cls(**json.loads(path.read_text()))
        except Exception:
            return cls()

    def save(self, path: Path = STATE_PATH) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2))
        except OSError as e:
            log.warning("could not persist risk state: %s", e)


@dataclass
class SizingResult:
    qty: int
    notional: float
    risk_dollars: float
    risk_pct: float
    capped_by: str | None = None

    @property
    def tradeable(self) -> bool:
        return self.qty > 0


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    sizing: SizingResult | None = None


class RiskEngine:
    def __init__(self, cfg, trading_client, state: RiskState | None = None):
        self.cfg = cfg
        self.client = trading_client
        self.state = state or RiskState.load()

    # ── live account ────────────────────────────────────────────────────────
    def equity(self) -> float:
        """Live equity from the broker. Never a cached constant."""
        acct = self.client.get_account()
        return float(acct.equity)

    def buying_power(self) -> float:
        return float(self.client.get_account().buying_power)

    def roll_session(self, equity: float) -> None:
        """Start a new trading day, or resume today's."""
        today = dt.date.today().isoformat()
        if self.state.session_date != today:
            self.state = RiskState(
                session_date=today,
                session_start_equity=equity,
                peak_equity=max(equity, self.state.peak_equity or equity),
                realized_pnl_today=0.0,
                trades_today=0,
            )
            self.state.save()
            log.info("new session %s, start equity $%,.2f".replace(",", ""), today, equity)
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
            self.state.save()

    # ── limits ──────────────────────────────────────────────────────────────
    def daily_loss_used(self, equity: float) -> float:
        return max(0.0, self.state.session_start_equity - equity)

    def daily_loss_limit(self) -> float:
        return self.state.session_start_equity * self.cfg.risk.daily_loss_limit_pct

    def trailing_drawdown(self, equity: float) -> float:
        return max(0.0, self.state.peak_equity - equity)

    def trailing_limit(self) -> float:
        return self.state.peak_equity * self.cfg.risk.trailing_dd_pct

    # ── sizing ──────────────────────────────────────────────────────────────
    def size_position(self, equity: float, entry: float, stop: float, *,
                      adv_notional: float = 0.0,
                      bar_volume: float = 0.0) -> SizingResult:
        """
        Shares from risk budget, then trimmed by every applicable cap.

        Liquidity caps matter: a position you cannot exit is not a position, it
        is a hostage.
        """
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0 or entry <= 0:
            return SizingResult(0, 0.0, 0.0, 0.0, "invalid stop distance")

        budget = equity * self.cfg.risk.risk_per_trade_pct
        qty = int(budget // risk_per_share)
        capped: str | None = None

        max_notional = equity * self.cfg.risk.max_position_notional_pct
        if qty * entry > max_notional:
            qty = int(max_notional // entry)
            capped = "position notional"

        if adv_notional > 0:
            adv_cap = adv_notional * self.cfg.risk.max_pct_of_adv
            if qty * entry > adv_cap:
                qty = int(adv_cap // entry)
                capped = "ADV liquidity"

        if bar_volume > 0:
            vol_cap = int(bar_volume * self.cfg.risk.max_pct_of_bar_volume)
            if qty > vol_cap:
                qty = vol_cap
                capped = "bar volume liquidity"

        qty = max(0, qty)
        return SizingResult(
            qty=qty,
            notional=round(qty * entry, 2),
            risk_dollars=round(qty * risk_per_share, 2),
            risk_pct=round((qty * risk_per_share) / equity, 5) if equity else 0.0,
            capped_by=capped,
        )

    # ── pre-trade gate ──────────────────────────────────────────────────────
    def check(self, setup, *, equity: float, open_positions: list,
              bucket_of: dict, adv_notional: float = 0.0,
              bar_volume: float = 0.0) -> RiskDecision:
        """Every limit, evaluated against live equity."""
        r = self.cfg.risk

        if self.state.halted:
            return RiskDecision(False, f"halted: {self.state.halt_reason}")

        used, limit = self.daily_loss_used(equity), self.daily_loss_limit()
        if used >= limit:
            self._halt(f"daily loss ${used:,.0f} hit limit ${limit:,.0f}")
            return RiskDecision(False, self.state.halt_reason)

        tdd, tlimit = self.trailing_drawdown(equity), self.trailing_limit()
        if tdd >= tlimit:
            self._halt(f"trailing drawdown ${tdd:,.0f} hit limit ${tlimit:,.0f}")
            return RiskDecision(False, self.state.halt_reason)

        if len(open_positions) >= r.max_concurrent_positions:
            return RiskDecision(False,
                f"max concurrent positions ({r.max_concurrent_positions}) reached")

        # One bucket must not become the whole book. SPY + QQQ + NVDA long is
        # one trade at 3x size, not three trades.
        bucket = bucket_of.get(setup.symbol, "unknown")
        in_bucket = [p for p in open_positions
                     if bucket_of.get(getattr(p, "symbol", ""), "") == bucket]
        base_risk = equity * r.risk_per_trade_pct
        bucket_budget = base_risk * r.max_risk_per_bucket_mult
        bucket_risk = sum(abs(float(getattr(p, "market_value", 0.0))) for p in in_bucket)
        if len(in_bucket) * base_risk >= bucket_budget:
            return RiskDecision(False,
                f"correlation bucket '{bucket}' already holds "
                f"{len(in_bucket)} position(s), at its {r.max_risk_per_bucket_mult}x risk cap")

        gross = sum(abs(float(getattr(p, "market_value", 0.0))) for p in open_positions)
        if gross >= equity * r.max_gross_exposure_pct:
            return RiskDecision(False,
                f"gross exposure ${gross:,.0f} at {r.max_gross_exposure_pct:.0%} cap")

        sizing = self.size_position(equity, setup.entry, setup.stop,
                                    adv_notional=adv_notional, bar_volume=bar_volume)
        if not sizing.tradeable:
            return RiskDecision(False,
                f"position sizes to 0 shares ({sizing.capped_by or 'risk budget'})")

        if gross + sizing.notional > equity * r.max_gross_exposure_pct:
            return RiskDecision(False, "would breach gross exposure cap")

        return RiskDecision(True, "ok", sizing)

    def halt(self, reason: str) -> None:
        """Stop all new entries. Called by the reconciler when a position
        cannot be closed -- if we cannot exit, we must not enter."""
        self._halt(reason)

    def _halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.save()
        log.error("TRADING HALTED: %s", reason)

    def record_fill(self, realized_pnl: float = 0.0) -> None:
        self.state.trades_today += 1
        self.state.realized_pnl_today += realized_pnl
        self.state.save()

    def status(self, equity: float) -> dict:
        return {
            "equity": round(equity, 2),
            "session_start_equity": round(self.state.session_start_equity, 2),
            "peak_equity": round(self.state.peak_equity, 2),
            "daily_loss_used": round(self.daily_loss_used(equity), 2),
            "daily_loss_limit": round(self.daily_loss_limit(), 2),
            "daily_loss_remaining": round(self.daily_loss_limit() - self.daily_loss_used(equity), 2),
            "trailing_dd": round(self.trailing_drawdown(equity), 2),
            "trailing_dd_limit": round(self.trailing_limit(), 2),
            "trades_today": self.state.trades_today,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
        }
