"""
Typed access to config.yaml. One file drives bot, risk engine and backtester.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    trailing_dd_pct: float
    max_position_notional_pct: float
    max_concurrent_positions: int
    max_gross_exposure_pct: float
    max_risk_per_bucket_mult: float
    min_reward_risk: float
    max_pct_of_adv: float
    max_pct_of_bar_volume: float


@dataclass(frozen=True)
class DataConfig:
    feed: str
    timeframes: list[str]
    bias_timeframe: str
    lookback_days: int
    session_tz: str
    rth_open: str
    rth_close: str
    skip_first_minutes: int
    skip_last_minutes: int


@dataclass(frozen=True)
class OrderFlowConfig:
    enabled: bool
    classify: str
    delta_window_minutes: int
    price_bin_ticks: int


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool
    scan_interval_minutes: int
    order_type: str
    time_in_force: str


@dataclass(frozen=True)
class ScreeningConfig:
    min_price: float
    min_adv_notional: float
    full_size_adv_notional: float
    max_spread_bps: float
    min_atr_pct: float
    max_atr_pct: float
    skip_earnings_window_days: int


@dataclass(frozen=True)
class ReconcileConfig:
    enabled: bool
    interval_seconds: int
    max_close_attempts: int
    stale_order_minutes: int
    auto_protect: bool


@dataclass(frozen=True)
class StrategyConfig:
    context: dict[str, Any]
    volume_profile: dict[str, Any]
    zones: dict[str, Any]
    confirmation: dict[str, Any]
    exits: dict[str, Any]


@dataclass(frozen=True)
class Config:
    nominal_equity: float
    paper: bool
    risk: RiskConfig
    data: DataConfig
    orderflow: OrderFlowConfig
    execution: ExecutionConfig
    screening: ScreeningConfig
    strategy: StrategyConfig
    reconcile: ReconcileConfig
    universe_tier: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def feed_is_iex(self) -> bool:
        return self.data.feed.lower() == "iex"

    def warnings(self) -> list[str]:
        """Config combinations that will quietly produce garbage."""
        out = []
        if self.feed_is_iex and self.orderflow.enabled:
            out.append(
                "data.feed=iex with orderflow.enabled=true: IEX carries roughly 2% of "
                "consolidated volume, so delta, volume-at-price and absorption are "
                "computed on a non-representative sample. Use feed=sip (Algo Trader "
                "Plus) or treat order-flow output as unreliable."
            )
        if self.feed_is_iex:
            out.append(
                "data.feed=iex: session VWAP is derived from ~2% of market volume and "
                "will diverge from the VWAP your charts show."
            )
        if self.risk.risk_per_trade_pct > 0.01:
            out.append(
                f"risk.risk_per_trade_pct={self.risk.risk_per_trade_pct:.2%} is high for "
                "an automated system. 0.25-0.5% is the recommended band."
            )
        return out


def load(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_PATH
    raw = yaml.safe_load(p.read_text())
    return Config(
        nominal_equity = float(raw["account"]["nominal_equity"]),
        paper          = bool(raw["account"].get("paper", True)),
        risk           = RiskConfig(**raw["risk"]),
        data           = DataConfig(**raw["data"]),
        orderflow      = OrderFlowConfig(**raw["orderflow"]),
        execution      = ExecutionConfig(**raw["execution"]),
        screening      = ScreeningConfig(**raw["screening"]),
        strategy       = StrategyConfig(**raw["strategy"]),
        reconcile      = ReconcileConfig(**raw["reconcile"]),
        universe_tier  = raw["universe"]["active_tier"],
        raw            = raw,
    )
