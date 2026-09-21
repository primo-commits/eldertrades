"""
Trade journal. Every scan decision is recorded, not just the fills.

    "track results through journaling and metrics"

E6 fixed: the original's CSV writer used an invalid f-string format spec,
f"{t.exit_price:.2f if t.exit_price else ''}", which raises ValueError at
runtime. It was called on market close, so the log was destroyed at exactly the
moment it mattered. Verified.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field, asdict
from pathlib import Path

JOURNAL_DIR = Path(__file__).resolve().parent.parent / "journal"

SETUP_FIELDS = ["timestamp", "symbol", "side", "entry", "stop", "target",
                "reward_risk", "qty", "notional", "risk_dollars", "regime",
                "zone_kind", "zone_low", "zone_high", "zone_strength",
                "lvn_confluence", "confirmation_score", "poc", "poc_trend",
                "profile_position", "atr", "order_id", "order_status", "dry_run"]

REJECT_FIELDS = ["timestamp", "symbol", "stage", "reason"]


def _writer(path: Path, fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    f = path.open("a", newline="")
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    if new:
        w.writeheader()
    return f, w


def log_setup(setup, sizing, order, *, directory: Path | None = None) -> None:
    # Resolved at call time, not bound as a default at import -- otherwise the
    # destination cannot be redirected for tests or alternate runs.
    directory = directory or JOURNAL_DIR
    path = directory / f"setups_{dt.date.today():%Y%m%d}.csv"
    f, w = _writer(path, SETUP_FIELDS)
    with f:
        w.writerow({
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "symbol": setup.symbol, "side": setup.side,
            "entry": f"{setup.entry:.2f}", "stop": f"{setup.stop:.2f}",
            "target": f"{setup.target:.2f}", "reward_risk": setup.reward_risk,
            "qty": getattr(sizing, "qty", ""),
            "notional": getattr(sizing, "notional", ""),
            "risk_dollars": getattr(sizing, "risk_dollars", ""),
            "regime": setup.regime,
            "zone_kind": setup.zone.kind,
            "zone_low": f"{setup.zone.bottom:.2f}",
            "zone_high": f"{setup.zone.top:.2f}",
            "zone_strength": setup.zone.strength,
            "lvn_confluence": setup.zone.lvn_confluence,
            "confirmation_score": setup.confirmation.score,
            "poc": setup.poc, "poc_trend": setup.poc_trend,
            "profile_position": setup.profile_position, "atr": setup.atr,
            "order_id": getattr(order, "order_id", ""),
            "order_status": getattr(order, "status", ""),
            "dry_run": getattr(order, "dry_run", ""),
        })


def log_rejections(rejections, *, directory: Path | None = None) -> None:
    """Why nothing fired is as informative as why something did."""
    if not rejections:
        return
    directory = directory or JOURNAL_DIR
    path = directory / f"rejections_{dt.date.today():%Y%m%d}.csv"
    f, w = _writer(path, REJECT_FIELDS)
    with f:
        ts = dt.datetime.now().isoformat(timespec="seconds")
        for r in rejections:
            w.writerow({"timestamp": ts, "symbol": r.symbol,
                        "stage": r.stage, "reason": r.reason})
