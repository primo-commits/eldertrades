"""
Fill model.

Fixes E19, E20 and E24 from the audit:

E19 no look-ahead. A signal computed from bars up to and including time t can
    only be filled at the OPEN of bar t+1. The original filled at the signal
    bar's own close, which is information you do not have until that bar has
    closed -- up to 5 minutes of free foresight on every trade.

E20 symmetric, gap-aware exits. The original applied slippage to entries and
    then filled exits at exactly the stop or target price. Real stops gap
    through. Here a bar that OPENS beyond the stop fills at that open, and
    slippage is applied to both sides.

E24 realistic costs. The original charged $0.003/share round trip, roughly 20x
    reality. Alpaca US equities are commission-free; the real costs are the
    spread you cross, slippage, and on sells the SEC fee plus FINRA TAF.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    spread_per_share: float = 0.01     # half-spread crossed on a market order
    slippage_bps: float = 1.0          # each way
    sec_fee_rate: float = 0.0000278    # sells only, on notional
    taf_per_share: float = 0.000166    # sells only
    taf_max: float = 8.30

    def entry_cost(self, qty: int, price: float) -> float:
        return qty * (self.spread_per_share / 2 + price * self.slippage_bps / 10_000)

    def exit_cost(self, qty: int, price: float) -> float:
        base = qty * (self.spread_per_share / 2 + price * self.slippage_bps / 10_000)
        sec = qty * price * self.sec_fee_rate
        taf = min(qty * self.taf_per_share, self.taf_max)
        return base + sec + taf

    def round_trip(self, qty: int, entry: float, exit_: float) -> float:
        return self.entry_cost(qty, entry) + self.exit_cost(qty, exit_)


@dataclass(frozen=True)
class Fill:
    price: float
    reason: str


def entry_fill(next_open: float, side: str, costs: CostModel,
               rng: random.Random) -> Fill:
    """
    Fill at the next bar's open, adverse-slipped.

    `rng` is passed in rather than using module state so a parameter sweep can
    reseed per run and compare configurations on identical random draws (E21).
    """
    slip = next_open * (costs.slippage_bps / 10_000) * rng.uniform(0.5, 1.5)
    px = next_open + slip if side == "buy" else next_open - slip
    return Fill(round(px, 4), "next_bar_open")


def exit_fill(bar, stop: float, target: float, side: str, costs: CostModel,
              rng: random.Random) -> Fill | None:
    """
    Resolve an exit within one bar, conservatively.

    Order of checks matters and is deliberately pessimistic:
      1. GAP -- the bar opens beyond the stop. Fill at the open, not the stop.
         This is the single biggest source of optimism in naive backtests.
      2. STOP before TARGET when both are inside the bar's range. Without tick
         data you cannot know which came first, so assume the worse one.
      3. TARGET.
    """
    o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
    long_ = side == "buy"

    # 1. gap through the stop
    if (long_ and o <= stop) or (not long_ and o >= stop):
        return Fill(round(o, 4), "gap_through_stop")

    hit_stop = (l <= stop) if long_ else (h >= stop)
    hit_target = (h >= target) if long_ else (l <= target)

    # 2. both touched in the same bar -> assume the stop
    if hit_stop:
        slip = stop * (costs.slippage_bps / 10_000) * rng.uniform(0.5, 2.0)
        px = stop - slip if long_ else stop + slip
        return Fill(round(px, 4), "stop" if not hit_target else "stop_ambiguous")

    # 3. target -- limit orders do not slip in your favour, but can miss
    if hit_target:
        return Fill(round(target, 4), "target")

    return None


def eod_fill(bar, side: str, costs: CostModel, rng: random.Random) -> Fill:
    """Flat at the close. Day orders do not carry overnight."""
    c = float(bar["close"])
    slip = c * (costs.slippage_bps / 10_000) * rng.uniform(0.5, 1.5)
    px = c - slip if side == "buy" else c + slip
    return Fill(round(px, 4), "eod_flat")
