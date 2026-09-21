"""
GFT Instant GOAT Risk Dashboard
================================
Position Size Calculator + Daily DD Monitor + Consistency Tracker
for Goat Funded Trader Instant GOAT ($5,000 account)

Account Rules (confirmed with GFT):
  - Daily DD:     3% of starting balance → $150/day max loss
  - Trailing Max DD: 6% of highest equity peak → hard floor at $4,700
  - Floating Loss: -2% of current equity → hard breach if equity drops to $4,900
  - Consistency:  15% rule → no single day > 15% of total profit
  - Min Days:     5 trading days before first payout
  - Profit Split: 80% to you, bi-weekly payouts
  - Daily Cap:    $3,000/day profit cap (excess deducted, not a breach)

Platforms: MT5, cTrader, TradeLocker, Match-Trader
"""

import datetime
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

# ─────────────────────────────────────────────
#  ACCOUNT CONFIG
# ─────────────────────────────────────────────

ACCOUNT_BALANCE: float        = 5_000.00
DAILY_DD_PCT: float           = 0.03    # 3% of STARTING BALANCE per day
MAX_DD_PCT: float             = 0.06    # 6% trailing max DD from PEAK equity
FLOATING_LOSS_PCT: float      = 0.02    # -2% hard breach from CURRENT equity
CONSISTENCY_CAP_PCT: float    = 0.15    # no single day > 15% of total profit
MIN_PAYOUT_DAYS: int          = 5       # min trading days before payout
SPLIT: float                  = 0.80    # 80% to you
DAILY_PROFIT_CAP: float       = 3_000.00

@dataclass
class AccountLimits:
    daily_loss_limit: float    # $ hard limit per day
    floating_loss_floor: float # $ equity floor (2% below current peak)
    max_position_loss: float   # conservative single-trade loss limit (1.5% risk)
    daily_profit_target_cap: float

    @classmethod
    def from_balance(cls, balance: float = ACCOUNT_BALANCE) -> "AccountLimits":
        daily_loss_limit     = balance * DAILY_DD_PCT       # $150
        floating_loss_floor  = balance * (1 - FLOATING_LOSS_PCT)  # $4,900
        max_position_loss    = balance * 0.015              # $75 (1.5% risk — conservative for prop)
        daily_profit_cap     = DAILY_PROFIT_CAP
        return cls(
            daily_loss_limit    = daily_loss_limit,
            floating_loss_floor = floating_loss_floor,
            max_position_loss   = max_position_loss,
            daily_profit_target_cap = daily_profit_cap,
        )

ACCOUNT_LIMITS = AccountLimits.from_balance()

# ─────────────────────────────────────────────
#  LOT SIZE / POSITION SIZE CALCULATOR
# ─────────────────────────────────────────────

# Common CFD index contract sizes (lots → contracts/units)
CONTRACT_SIZE = {
    "GER40":     1.0,   # 1 lot = 1 contract, value = price × 1
    "NAS100":    1.0,   # 1 lot = 1 contract
    "US30":      1.0,
    "US100":     1.0,
    "USDX":      100,  # 1 lot = $100 per point
    "XAUUSD":    100,  # 1 lot = 100 oz (gold)
    "XTIUSD":    100,  # 1 lot = 100 barrels (oil)
    "EURUSD":    100_000,  # standard lot in base currency
    "GBPUSD":    100_000,
    "USDJPY":    100_000,
}

PIP_SIZE = {
    # For forex — 1 pip in quote currency
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    # For indices — 1 point
    "GER40":  1.0,
    "NAS100": 1.0,
    "US30":   1.0,
    "US100":  1.0,
    "USDX":   0.005,
    # For commodities
    "XAUUSD": 0.01,   # gold quoted in USD, 1 pip = $1 per lot
    "XTIUSD": 0.01,   # oil quoted in USD, $1 per lot per 0.01 move
}

def calc_lot_size(
    symbol: str,
    entry_price: float,
    stop_loss_pips: float,
    risk_dollars: float,
    account_currency: str = "USD",
) -> dict:
    """
    Calculate safe lot size given a risk dollar amount and stop-loss distance.

    Parameters
    ----------
    symbol         : Trading symbol (e.g. "NAS100", "GER40", "EURUSD", "XAUUSD")
    entry_price    : Planned entry price
    stop_loss_pips : Distance to stop loss in pips/points
    risk_dollars   : Amount to risk in account currency (e.g. 75 for 1.5%)
    account_currency: Account denom — USD assumed

    Returns dict with lots, notional_value, risk_pct_of_account
    """
    if stop_loss_pips <= 0:
        return {"error": "Stop loss pips must be positive"}

    pip_val = PIP_SIZE.get(symbol, 0.0001)
    contract = CONTRACT_SIZE.get(symbol, 100_000)

    # Pip value per standard lot in account currency
    if symbol in ("EURUSD", "GBPUSD"):
        pip_value_per_lot = contract * pip_val  # e.g. 100000 × 0.0001 = $10/pip/lot
    elif symbol in ("XAUUSD",):
        pip_value_per_lot = contract * pip_val  # e.g. 100 × 0.01 = $1/pip/lot
    elif symbol in ("XTIUSD",):
        pip_value_per_lot = contract * pip_val
    elif symbol in ("USDJPY",):
        # Need quote conversion — use 1.0 approximation or fetch rate
        pip_value_per_lot = contract * pip_val  # approximate
    else:
        # Indices: 1 point = $1 per contract
        pip_value_per_lot = contract * pip_val  # e.g. 1 × 1.0 = $1/point/lot

    if pip_value_per_lot <= 0:
        return {"error": f"Cannot calculate pip value for {symbol}"}

    lots = risk_dollars / (stop_loss_pips * pip_value_per_lot)

    notional = lots * contract * entry_price if symbol not in ("XAUUSD", "XTIUSD", "USDX") else lots * contract
    risk_pct = (risk_dollars / ACCOUNT_BALANCE) * 100

    return {
        "symbol":            symbol,
        "entry_price":       entry_price,
        "stop_loss_pips":    stop_loss_pips,
        "risk_dollars":      risk_dollars,
        "lots":              round(lots, 2),
        "notional_value":   round(notional, 2),
        "risk_pct_of_account": round(risk_pct, 2),
        "pip_value_per_lot": round(pip_value_per_lot, 4),
        "account_limits":   asdict(ACCOUNT_LIMITS),
    }


def calc_position_from_risk_pct(
    symbol: str,
    entry_price: float,
    stop_loss_pips: float,
    risk_pct: float = 1.5,
) -> dict:
    """Calculate position size from a risk-percentage of account."""
    risk_dollars = ACCOUNT_BALANCE * (risk_pct / 100)
    result = calc_lot_size(symbol, entry_price, stop_loss_pips, risk_dollars)
    result["risk_pct_input"] = risk_pct
    return result


# ─────────────────────────────────────────────
#  DAILY DRAW DOWN MONITOR
# ─────────────────────────────────────────────

@dataclass
class DailySession:
    date: str
    starting_equity: float
    closing_equity: float
    trades: int = 0
    pnl: float = 0.0
    floating_pnl: float = 0.0
    peak_equity: float = 0.0
    max_dd_dollars: float = 0.0
    max_dd_pct: float = 0.0
    warning_level: str = "GREEN"  # GREEN / YELLOW / ORANGE / RED / BREACH
    breaching_trade_id: Optional[int] = None

    @property
    def closed_pnl(self) -> float:
        return self.pnl

    @property
    def net_pnl(self) -> float:
        return self.pnl + self.floating_pnl


class DailyDDMonitor:
    """
    Tracks daily drawdown in real time. Call update() after each trade.
    """

    def __init__(self, account_balance: float = ACCOUNT_BALANCE):
        self.account_balance = account_balance
        self.limits = AccountLimits.from_balance(account_balance)
        self.sessions: dict[str, DailySession] = {}
        self.peak_equity = account_balance
        self.all_time_peak = account_balance

    def _get_today(self) -> str:
        return datetime.date.today().isoformat()

    def start_session(self, date: Optional[str] = None) -> DailySession:
        d = date or self._get_today()
        if d in self.sessions:
            session = self.sessions[d]
        else:
            session = DailySession(
                date=d,
                starting_equity=self.all_time_peak,
                closing_equity=self.all_time_peak,
                peak_equity=self.all_time_peak,
            )
            self.sessions[d] = session
        return session

    def update(self, equity: float, closed_pnl: float = 0.0,
               floating_pnl: float = 0.0, trade_id: Optional[int] = None) -> DailySession:
        today = self._get_today()
        session = self.start_session(today)

        session.closing_equity = equity
        session.pnl += closed_pnl
        session.floating_pnl = floating_pnl

        # Track peak equity today
        if equity > session.peak_equity:
            session.peak_equity = equity

        # Track all-time peak
        if equity > self.all_time_peak:
            self.all_time_peak = equity

        # Calculate drawdowns
        session.max_dd_dollars = session.peak_equity - equity
        session.max_dd_pct = (session.max_dd_dollars / session.peak_equity) * 100

        # Trailing max DD from all-time peak
        trailing_mdd = self.all_time_peak - equity
        trailing_mdd_pct = (trailing_mdd / self.all_time_peak) * 100

        # Warning level
        session.warning_level = self._calc_warning_level(session, trailing_mdd_pct)
        session.trades += 1

        return session

    def _calc_warning_level(self, session: DailySession, trailing_mdd_pct: float) -> str:
        daily_pct = (session.max_dd_dollars / session.peak_equity) * 100

        if daily_pct >= DAILY_DD_PCT * 100 or trailing_mdd_pct >= MAX_DD_PCT * 100:
            return "BREACH"
        elif daily_pct >= 0.85 * DAILY_DD_PCT * 100 or trailing_mdd_pct >= 0.85 * MAX_DD_PCT * 100:
            return "RED"
        elif daily_pct >= 0.70 * DAILY_DD_PCT * 100 or trailing_mdd_pct >= 0.70 * MAX_DD_PCT * 100:
            return "ORANGE"
        elif daily_pct >= 0.50 * DAILY_DD_PCT * 100 or trailing_mdd_pct >= 0.50 * MAX_DD_PCT * 100:
            return "YELLOW"
        else:
            return "GREEN"

    def status(self) -> dict:
        today = self._get_today()
        session = self.sessions.get(today)
        if session is None:
            session = self.start_session(today)

        daily_used   = session.max_dd_dollars
        daily_pct    = session.max_dd_pct
        daily_remain = self.limits.daily_loss_limit - daily_used

        trailing_mdd = self.all_time_peak - session.closing_equity
        trailing_pct = (trailing_mdd / self.all_time_peak) * 100

        # Floating loss floor
        floating_floor_equity = session.closing_equity - (session.closing_equity * FLOATING_LOSS_PCT)
        # Actually GFT says floating loss floor is 2% BELOW current equity, not absolute
        # Re-read: "2% floating loss hard breach" = if you're down 2% from peak, hard stop
        floating_breach_equity = self.all_time_peak * (1 - FLOATING_LOSS_PCT)  # $4,900 absolute

        return {
            "account_balance":    ACCOUNT_BALANCE,
            "all_time_peak":      round(self.all_time_peak, 2),
            "current_equity":     round(session.closing_equity, 2),
            "today_pnl":          round(session.pnl, 2),
            "today_floating":     round(session.floating_pnl, 2),
            "today_net":          round(session.net_pnl, 2),
            "daily_loss_used":    round(daily_used, 2),
            "daily_loss_limit":   round(self.limits.daily_loss_limit, 2),
            "daily_loss_remain":  round(daily_remain, 2),
            "daily_loss_pct":     round(daily_pct, 2),
            "trailing_mdd":       round(trailing_mdd, 2),
            "trailing_mdd_pct":   round(trailing_pct, 2),
            "floating_breach_eq":round(floating_breach_equity, 2),
            "warning_level":      session.warning_level,
            "max_position_risk":  round(self.limits.max_position_loss, 2),
            "can_trade":          session.warning_level not in ("BREACH", "RED"),
        }

    def quick_check(self, proposed_loss: float) -> dict:
        """Check if a proposed losing trade would breach limits."""
        today = self._get_today()
        session = self.start_session(today)
        current_equity = session.closing_equity

        post_trade_equity = current_equity - proposed_loss
        post_trade_daily  = session.peak_equity - post_trade_equity
        post_trade_daily_pct = (post_trade_daily / session.peak_equity) * 100
        post_trade_trailing = self.all_time_peak - post_trade_equity
        post_trade_trailing_pct = (post_trade_trailing / self.all_time_peak) * 100

        daily_ok     = post_trade_daily_pct  < DAILY_DD_PCT * 100
        trailing_ok  = post_trade_trailing_pct < MAX_DD_PCT * 100
        floating_ok  = post_trade_equity     >= self.all_time_peak * (1 - FLOATING_LOSS_PCT)
        position_ok  = proposed_loss         <= self.limits.max_position_loss

        all_ok = daily_ok and trailing_ok and floating_ok and position_ok

        return {
            "proposed_loss":         round(proposed_loss, 2),
            "post_trade_equity":     round(post_trade_equity, 2),
            "post_trade_daily_dd":   round(post_trade_daily_pct, 2),
            "post_trade_trailing_dd":round(post_trade_trailing_pct, 2),
            "daily_ok":     daily_ok,
            "trailing_ok":  trailing_ok,
            "floating_ok":  floating_ok,
            "position_ok":  position_ok,
            "all_ok":       all_ok,
            "blocking_rule": None if all_ok else (
                "DAILY_DD" if not daily_ok else
                "TRAILING_MDD" if not trailing_ok else
                "FLOATING_LOSS" if not floating_ok else
                "POSITION_SIZE"
            ),
        }


# ─────────────────────────────────────────────
#  CONSISTENCY TRACKER
# ─────────────────────────────────────────────

@dataclass
class Trade:
    id: int
    date: str
    symbol: str
    pnl: float
    pips: float = 0.0
    direction: str = "LONG"
    rr: float = 0.0
    session: str = "S"  # S = scalpel, D = day, S = swing

@dataclass
class ConsistencyTracker:
    """
    Tracks 15% consistency rule.
    Rule: No single trading day may account for more than 15% of total profit.
    Payout eligibility requires all days to be ≤ 15% of cumulative profit.
    """
    trades: list[Trade] = field(default_factory=list)
    sessions: dict[str, DailySession] = field(default_factory=dict)
    payout_started: bool = False
    payout_date: Optional[str] = None

    def add_trade(self, trade: Trade):
        self.trades.append(trade)
        # Update session
        if trade.date not in self.sessions:
            self.sessions[trade.date] = DailySession(
                date=trade.date,
                starting_equity=ACCOUNT_BALANCE,
                closing_equity=ACCOUNT_BALANCE,
            )
        s = self.sessions[trade.date]
        s.pnl += trade.pnl
        s.trades += 1
        s.closing_equity += trade.pnl

    def get_day_summary(self, date: Optional[str] = None) -> dict:
        d = date or datetime.date.today().isoformat()
        session = self.sessions.get(d)
        if session is None:
            return {"date": d, "pnl": 0.0, "trades": 0, "eligible": True}
        return self._day_summary(session)

    def _day_summary(self, session: DailySession) -> dict:
        pnl = session.pnl
        return {
            "date":    session.date,
            "pnl":     round(pnl, 2),
            "trades":  session.trades,
            "closed_eq":round(session.closing_equity, 2),
            "eligible": True,  # computed in full_check
        }

    def full_check(self) -> dict:
        """Run full consistency check across all trading days."""
        if not self.sessions:
            return {
                "total_pnl": 0.0,
                "trading_days": 0,
                "payout_eligible": False,
                "reason": "No trading days recorded",
                "daily_breakdown": [],
            }

        total_pnl = sum(s.pnl for s in self.sessions.values())
        profitable_days = [s for s in self.sessions.values() if s.pnl > 0]
        trading_days = len(self.sessions)

        # Sort days by absolute PnL (largest first)
        sorted_days = sorted(
            self.sessions.values(),
            key=lambda s: abs(s.pnl),
            reverse=True
        )

        daily_breakdown = []
        violations = []

        for s in sorted_days:
            if total_pnl > 0 and s.pnl > 0:
                pct_of_profit = (s.pnl / total_pnl) * 100
            else:
                pct_of_profit = 0.0

            is_violation = (
                s.pnl > 0 and
                total_pnl > 0 and
                pct_of_profit > CONSISTENCY_CAP_PCT * 100
            )

            daily_breakdown.append({
                "date":         s.date,
                "pnl":          round(s.pnl, 2),
                "pct_of_profit":round(pct_of_profit, 1),
                "violation":    is_violation,
                "eligible":     not is_violation,
                "trades":       s.trades,
            })

            if is_violation:
                violations.append({
                    "date": s.date,
                    "pnl": round(s.pnl, 2),
                    "pct_of_profit": round(pct_of_profit, 1),
                    "limit_pct":     CONSISTENCY_CAP_PCT * 100,
                })

        # Payout eligibility
        payout_eligible = (
            trading_days >= MIN_PAYOUT_DAYS
            and total_pnl > 0
            and len(violations) == 0
        )

        reason = ""
        if trading_days < MIN_PAYOUT_DAYS:
            reason = f"Need {MIN_PAYOUT_DAYS - trading_days} more trading days"
        elif len(violations) > 0:
            reason = f"{len(violations)} consistency violation(s)"
        elif total_pnl <= 0:
            reason = "No profitable days yet"
        else:
            reason = "Eligible for payout"

        estimated_payout = total_pnl * SPLIT if payout_eligible else 0.0

        return {
            "total_pnl":          round(total_pnl, 2),
            "trading_days":       trading_days,
            "profitable_days":    len(profitable_days),
            "payout_eligible":    payout_eligible,
            "estimated_payout":   round(estimated_payout, 2),
            "split_pct":          SPLIT * 100,
            "min_payout_days":    MIN_PAYOUT_DAYS,
            "consistency_limit":  f"{CONSISTENCY_CAP_PCT * 100:.0f}%",
            "reason":             reason,
            "violations":        violations,
            "daily_breakdown":    daily_breakdown,
            "next_payout_date":   self._next_payout_date(),
        }

    def _next_payout_date(self) -> str:
        """Bi-weekly — next 1st or 15th of month."""
        today = datetime.date.today()
        y, m = today.year, today.month
        candidates = [
            datetime.date(y, m, 1),
            datetime.date(y, m, 15),
        ]
        if today.day < 15:
            return candidates[0].isoformat()
        else:
            next_month = m + 1 if m < 12 else 1
            next_year  = y if m < 12 else y + 1
            return datetime.date(next_year, next_month, 1).isoformat()

    def export_journal(self, path: str = "GFT_Trade_Journal.csv") -> str:
        rows = ["date,symbol,pnl,pips,direction,rr,session"]
        for t in self.trades:
            rows.append(f"{t.date},{t.symbol},{t.pnl:.2f},{t.pips:.1f},{t.direction},{t.rr:.2f},{t.session}")
        with open(path, "w") as f:
            f.write("\n".join(rows))
        return path


# ─────────────────────────────────────────────
#  EXAMPLE USAGE / QUICK DEMO
# ─────────────────────────────────────────────

def demo():
    print("=" * 60)
    print("GFT INSTANT GOAT — RISK DASHBOARD DEMO")
    print("=" * 60)

    # 1. Position Size
    print("\n[1] POSITION SIZE — NAS100")
    result = calc_position_from_risk_pct(
        symbol="NAS100",
        entry_price=20_500,
        stop_loss_pips=20,   # 20 points SL
        risk_pct=1.5,       # risk 1.5% of $5,000 = $75
    )
    for k, v in result.items():
        if k != "account_limits":
            print(f"  {k}: {v}")

    print("\n[2] POSITION SIZE — GER40")
    result2 = calc_position_from_risk_pct(
        symbol="GER40",
        entry_price=18_500,
        stop_loss_pips=30,   # 30 points
        risk_pct=1.5,
    )
    for k, v in result2.items():
        if k != "account_limits":
            print(f"  {k}: {v}")

    # 2. DD Monitor
    print("\n[3] DAILY DD MONITOR")
    monitor = DailyDDMonitor()

    # Simulate a losing trade
    session1 = monitor.update(equity=4_970, closed_pnl=-30.0, floating_pnl=0.0)
    print(f"  After trade 1 — Equity: $4,970 | DD: ${session1.max_dd_dollars:.2f} ({session1.max_dd_pct:.2f}%) | Level: {session1.warning_level}")
    status = monitor.status()
    print(f"  Daily used: ${status['daily_loss_used']:.2f} / ${status['daily_loss_limit']:.2f}")
    print(f"  Trailing MDD: ${status['trailing_mdd']:.2f} ({status['trailing_mdd_pct']:.2f}%)")

    # Check a proposed trade
    print("\n[4] PRE-TRADE RISK CHECK — $100 proposed loss")
    check = monitor.quick_check(100.0)
    print(f"  Post-trade equity: ${check['post_trade_equity']:.2f}")
    print(f"  Daily DD post: {check['post_trade_daily_dd']:.2f}%")
    print(f"  Trailing DD post: {check['post_trade_trailing_dd']:.2f}%")
    print(f"  All OK: {check['all_ok']}")
    if not check['all_ok']:
        print(f"  BLOCKING RULE: {check['blocking_rule']}")

    # 3. Consistency Tracker
    print("\n[5] CONSISTENCY TRACKER")
    tracker = ConsistencyTracker()

    # Simulate 6 trading days
    sim_days = [
        ("2026-09-15", 85.00,  "NAS100"),
        ("2026-09-16", 62.00,  "GER40"),
        ("2026-09-17", 120.00, "NAS100"),  # > 15% of total — violation if total is small
        ("2026-09-18", 45.00,  "US30"),
        ("2026-09-19", 38.00,  "GER40"),
        ("2026-09-22", 55.00,  "NAS100"),
    ]

    trade_id = 1
    for date, pnl, symbol in sim_days:
        t = Trade(id=trade_id, date=date, symbol=symbol, pnl=pnl)
        tracker.add_trade(t)
        trade_id += 1

    report = tracker.full_check()
    print(f"  Total PnL: ${report['total_pnl']:.2f}")
    print(f"  Trading days: {report['trading_days']}")
    print(f"  Payout eligible: {report['payout_eligible']}")
    print(f"  Reason: {report['reason']}")
    print(f"  Estimated payout: ${report['estimated_payout']:.2f}")
    print(f"  Next payout date: {report['next_payout_date']}")
    print(f"  Consistency violations: {len(report['violations'])}")
    for v in report['violations']:
        print(f"    — {v['date']}: ${v['pnl']:.2f} = {v['pct_of_profit']:.1f}% of profit (limit: {v['limit_pct']:.0f}%)")

    print("\n  Daily Breakdown:")
    for d in report['daily_breakdown']:
        flag = " [VIOLATION]" if d['violation'] else ""
        print(f"    {d['date']}: ${d['pnl']:>8.2f} — {d['pct_of_profit']:>5.1f}% of profit{flag}")

    print("\n[6] ACCOUNT LIMITS REFERENCE")
    limits = ACCOUNT_LIMITS
    print(f"  Daily loss limit:      ${limits.daily_loss_limit:.2f}")
    print(f"  Floating loss floor:   ${limits.floating_loss_floor:.2f}")
    print(f"  Max single-trade risk: ${limits.max_position_loss:.2f}")
    print(f"  Trailing MDD floor:    ${ACCOUNT_BALANCE * (1-MAX_DD_PCT):.2f} (6% below peak)")
    print(f"  Consistency cap:       {CONSISTENCY_CAP_PCT*100:.0f}% per day")
    print(f"  Min payout days:       {MIN_PAYOUT_DAYS}")
    print(f"  Profit split:          {SPLIT*100:.0f}% to you")

    # Export journal
    journal_path = tracker.export_journal()
    print(f"\n[7] Journal exported to: {journal_path}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    demo()
