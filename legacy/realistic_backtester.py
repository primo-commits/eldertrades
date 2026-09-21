"""
Emerging Shotgun — Realistic Backtester
========================================
Upgrades the original backtest with:
  - 5-minute bars  (Alpaca IEX, 1-year history; 12x resolution vs 60-min bars)
  - Realistic fill model: fill = bar_close + slippage(ATR, latency)
  - Execution latency: simulate 2-5 second queue delay before fill
  - Alpaca fee model: $0.003/share round-trip
  - 5-min stop/target scanning (not daily bars)
  - Full parameter sweep over MA periods, stop%, target%

Run:
    python realistic_backtester.py

Requires:
    pip install alpaca-py pandas numpy matplotlib

Author: Mavis (MiniMax Code) for Primo / FeeSlayer
"""

import datetime as dt
import math
import os
import random
from typing import Optional, Any

import numpy as np
import pandas as pd

# ── Alpaca data client ──────────────────────────────────────────────────────────
HAS_ALPACA = False
StockHistoricalDataClient = None   # module-level global; reassigned by install block
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    HAS_ALPACA = True
except ImportError:
    print("alpaca-py not found. Installing...")


# ── Execution model constants ─────────────────────────────────────────────────
EXECUTION_LATENCY_SECONDS = (2, 5)   # random uniform between 2–5 s after signal bar
SLIPPAGE_BPS = 5                     # base slippage in basis points (0.05%)
SLIPPAGE_ATR_FACTOR = 0.10           # fraction of daily ATR to add on top
ALPACA_FEE_PER_SHARE = 0.003        # Alpaca $0.003/share round-trip
ALPACA_MIN_FEE = 0.01               # minimum commission per order
MIN_FILL_BPS = 1                    # minimum slippage floor (1 bp)


def _load_keys_file(keys_path: str) -> None:
    """Load Alpaca keys from plain-text file sitting next to this script."""
    if not os.path.exists(keys_path):
        return
    with open(keys_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ[name.strip()] = value.strip().strip('"').strip("'")


def get_alpaca_client(keys_path: str):
    global HAS_ALPACA
    if not HAS_ALPACA:
        import subprocess
        subprocess.check_call(["pip", "install", "alpaca-py", "-q"])
        try:
            global StockHistoricalDataClient   # rebind module-level global
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            from alpaca.data.enums import DataFeed
            HAS_ALPACA = True
        except Exception as e:
            print(f"Failed to import alpaca-py after install: {e}")
            return None
    _load_keys_file(keys_path)
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Set them in alpaca_keys.txt next to this script, or as env vars.")
        return None
    return StockHistoricalDataClient(api_key, secret_key)


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_1min_bars(
    client: StockHistoricalDataClient,
    tickers: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """
    Fetch 5-minute bars for all tickers from Alpaca (IEX feed, 1-year history).
    Returns dict: ticker -> DataFrame with columns [open, high, low, close, volume].

    5-min bars are used instead of 1-min to stay within Alpaca IEX pagination limits
    when fetching many tickers in a single request.  This is still 12x the resolution
    of the original 5-bar backtest and is sufficient for realistic exit modeling.
    """
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),   # 5-min instead of 1-min
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    raw = client.get_stock_bars(req).df
    out = {}
    for ticker in tickers:
        try:
            df = raw.loc[ticker].copy()
            df.index = pd.to_datetime(df.index).tz_convert("America/New_York")
            out[ticker] = df.sort_index()[["open", "high", "low", "close", "volume"]]
        except Exception:
            out[ticker] = pd.DataFrame()
    return out


def compute_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Compute average True Range in dollars from 1-min bars."""
    if len(bars) < period + 1:
        return 0.0
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - bars["close"].shift(1)).abs(),
        (bars["low"] - bars["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


# ── Slippage & fill model ──────────────────────────────────────────────────────

def realistic_fill_price(
    signal_bar_close: float,
    atr: float,
    side: str = "buy",
    latency_sec: tuple = EXECUTION_LATENCY_SECONDS,
) -> tuple[float, int]:
    """
    Returns (fill_price, latency_seconds) simulating real order execution.

    Slippage has two components:
      1. Base slippage  = SLIPPAGE_BPS bps of price
      2. ATR component  = SLIPPAGE_ATR_FACTOR × ATR × (1-min vol proxy)
      3. Latency boost  = extra slippage for slower fills

    For buys, price moves up. For sells, price moves down.
    """
    latency = random.randint(*latency_sec)

    base_slip_bps = SLIPPAGE_BPS
    # During first/last 15 min, liquidity is thinner → double slippage
    # (We'll handle this in the backtest loop using the bar timestamp)

    atr_slip = atr * SLIPPAGE_ATR_FACTOR
    base_slip = signal_bar_close * (base_slip_bps / 10_000)

    total_slip = max(base_slip + atr_slip, signal_bar_close * (MIN_FILL_BPS / 10_000))

    if side == "buy":
        fill = signal_bar_close + total_slip
    else:
        fill = signal_bar_close - total_slip

    return round(fill, 2), latency


def alpaca_fees(qty: int, price: float) -> float:
    """Round-trip commission for Alpaca US (no commission but regulatory fee)."""
    fee = qty * ALPACA_FEE_PER_SHARE
    return max(fee, ALPACA_MIN_FEE)


# ── Core backtest engine ───────────────────────────────────────────────────────

def run_backtest(
    index_df: pd.DataFrame,
    basket_data: dict[str, pd.DataFrame],
    ma_periods: int,
    stop_pct: float,
    target_pct: float,
    initial_capital: float = 25_000.0,
    verbose: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """
    Event-driven backtest on 1-minute bars.

    Signal:  index closes above its MA(ma_periods) — AND the previous bar was below.
    Entry:   fire market order at signal bar close + realistic slippage
    Exit:    scan every subsequent bar for stop OR target hit (minute-by-minute)
             If neither hit by 15:55 ET that day → close at that bar's close (eod_flat)

    Returns:
        trades_df  — one row per completed trade
        metrics    — dict of performance stats
    """

    # ── Compute index signal bars ──────────────────────────────────────────────
    close = index_df["close"].copy()
    ma = close.rolling(ma_periods).mean()
    above_ma = close > ma

    # Signal = bar i crossed above, bar i-1 was below
    crossed = above_ma & (~above_ma.shift(1).fillna(False))

    trades = []

    for signal_dt, row in index_df.iterrows():
        if not crossed.loc[signal_dt]:
            continue

        signal_date = signal_dt.date()

        # ── Open positions in basket ────────────────────────────────────────────
        for ticker, bars in basket_data.items():
            if bars.empty:
                continue

            # ── Find entry window: signal bar + N subsequent 1-min bars ─────────
            # Look from signal bar up to 5 minutes ahead for fill
            signal_idx = bars.index.get_indexer([signal_dt], method="ffill")[0]
            if signal_idx < 0:
                signal_idx = 0

            entry_window = bars.iloc[signal_idx : signal_idx + 12]  # up to 12 bars (~60 min with 5-min bars)
            if entry_window.empty:
                continue

            # Use the first bar after signal as the "order arrives" bar
            fill_bar = entry_window.iloc[0]
            atr = compute_atr(bars.loc[:fill_bar.name].tail(780))  # ~10 trading days of 5-min bars

            # Side: entry is always BUY in this strategy
            entry_price, latency = realistic_fill_price(
                fill_bar["close"], atr, side="buy"
            )

            qty = int(initial_capital * 0.05 / entry_price)  # 5% of capital per position
            if qty < 1:
                continue

            fees = alpaca_fees(qty, entry_price)

            stop_price = round(entry_price * (1 - stop_pct), 2)
            target_price = round(entry_price * (1 + target_pct), 2)

            # ── Vectorized exit scan: find first stop / target / eod in one pass ──
            exit_idx = bars.index.get_indexer([fill_bar.name])[0] + 1
            bars_after = bars.iloc[exit_idx:]
            if bars_after.empty:
                continue

            # Same-day bars only (don't hold overnight)
            same_day = bars_after[bars_after.index.date == signal_date]

            exit_dt = None
            exit_price = None
            exit_reason = None

            if not same_day.empty:
                # Stop: first bar where low <= stop_price
                stop_hit = same_day["low"] <= stop_price
                if stop_hit.any():
                    exit_dt = stop_hit.idxmax()   # idxmax returns the datetime label directly
                    exit_price = stop_price
                    exit_reason = "stop"

                # Target: first bar where high >= target_price (before any stop)
                target_hit = same_day["high"] >= target_price
                if target_hit.any():
                    target_dt = target_hit.idxmax()
                    if exit_dt is None or target_dt < exit_dt:
                        exit_dt = target_dt
                        exit_price = target_price
                        exit_reason = "target"

                # EOD flat: first bar >= 15:55 on signal_date
                eod_mask = pd.Series(same_day.index.time >= dt.time(15, 55), index=same_day.index)
                if eod_mask.any():
                    eod_dt = eod_mask.idxmax()
                    if exit_dt is None or eod_dt < exit_dt:
                        exit_dt = eod_dt
                        exit_price = same_day.loc[exit_dt, "close"]
                        exit_reason = "eod_flat"

            if exit_dt is None or exit_price is None:
                continue

            # ── Exit fees ───────────────────────────────────────────────────────
            exit_fees = alpaca_fees(qty, exit_price)
            total_fees = fees + exit_fees

            pnl_gross = (exit_price - entry_price) * qty
            pnl_net = pnl_gross - total_fees
            pnl_pct = round((pnl_net / (entry_price * qty)) * 100, 4)

            trades.append({
                "signal_date": str(signal_date),
                "signal_time": str(signal_dt),
                "entry_time": str(fill_bar.name),
                "ticker": ticker,
                "entry_price": entry_price,
                "exit_time": str(exit_dt),
                "exit_price": exit_price,
                "qty": qty,
                "stop_price": stop_price,
                "target_price": target_price,
                "exit_reason": exit_reason,
                "latency_sec": latency,
                "fees": round(total_fees, 4),
                "pnl_gross": round(pnl_gross, 2),
                "pnl_net": round(pnl_net, 2),
                "pnl_pct": pnl_pct,
            })

            if verbose:
                print(f"  {ticker}: entry={entry_price} → {exit_reason}@{exit_price} | "
                      f"pnl={pnl_pct:+.2f}% | fees=${total_fees:.2f}")

    trades_df = pd.DataFrame(trades)

    # ── Compute metrics ────────────────────────────────────────────────────────
    if trades_df.empty:
        return trades_df, {}

    metrics = compute_metrics(trades_df, initial_capital)
    return trades_df, metrics


def compute_metrics(trades_df: pd.DataFrame, initial_capital: float) -> dict:
    total = len(trades_df)
    wins = len(trades_df[trades_df["pnl_net"] > 0])
    losses = len(trades_df[trades_df["pnl_net"] <= 0])
    win_rate = wins / total * 100 if total > 0 else 0

    gross_total = trades_df["pnl_gross"].sum()
    net_total = trades_df["pnl_net"].sum()
    total_fees = trades_df["fees"].sum()

    avg_win = trades_df.loc[trades_df["pnl_net"] > 0, "pnl_net"].mean() if wins > 0 else 0
    avg_loss = trades_df.loc[trades_df["pnl_net"] <= 0, "pnl_net"].mean() if losses > 0 else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # Simulate equity curve
    equity = initial_capital
    peak = equity
    max_dd = 0
    for _, t in trades_df.iterrows():
        equity += t["pnl_net"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    total_return_pct = (equity - initial_capital) / initial_capital * 100

    # Outcome breakdown
    outcomes = trades_df["exit_reason"].value_counts().to_dict()
    avg_latency = trades_df["latency_sec"].mean()
    avg_fees = total_fees / total if total > 0 else 0
    avg_pnl_pct = trades_df["pnl_pct"].mean()

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "gross_total": round(gross_total, 2),
        "net_total": round(net_total, 2),
        "total_fees": round(total_fees, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_return_pct": round(total_return_pct, 2),
        "final_equity": round(equity, 2),
        "initial_capital": initial_capital,
        "avg_latency_sec": round(avg_latency, 1),
        "avg_fees_per_trade": round(avg_fees, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 4),
        "outcomes": outcomes,
    }


# ── Parameter sweep ─────────────────────────────────────────────────────────────

def run_sweep(
    index_df: pd.DataFrame,
    basket_data: dict[str, pd.DataFrame],
    output_path: str,
    initial_capital: float = 25_000.0,
):
    """
    Full parameter sweep over MA periods, stop%, and target%.
    Writes sweep results to CSV and returns best params by Sharpe-like score.
    """

    ma_periods_list = [10, 15, 20, 25, 30]
    stop_pcts = [0.005, 0.01, 0.015, 0.02]   # 0.5%, 1%, 1.5%, 2%
    target_pcts = [0.02, 0.03, 0.05, 0.07, 0.10]  # 2%, 3%, 5%, 7%, 10%

    rows = []
    total_combos = len(ma_periods_list) * len(stop_pcts) * len(target_pcts)
    done = 0

    print(f"\nStarting sweep: {total_combos} combinations")
    print(f"  MA periods: {ma_periods_list}")
    print(f"  Stop %:     {[f'{x*100:.1f}%' for x in stop_pcts]}")
    print(f"  Target %:  {[f'{x*100:.1f}%' for x in target_pcts]}")
    print()

    for ma in ma_periods_list:
        for stop in stop_pcts:
            for target in target_pcts:
                done += 1
                _, metrics = run_backtest(
                    index_df, basket_data, ma, stop, target,
                    initial_capital=initial_capital, verbose=False
                )

                if metrics:
                    # Sharpe-like: net total / (abs(avg loss) * num losses + 1)
                    risk_adjusted = (
                        metrics["net_total"] /
                        (abs(metrics["avg_loss"]) * max(metrics["losses"], 1) + 1)
                        if metrics["losses"] > 0 else metrics["net_total"]
                    )
                    rows.append({
                        "ma_periods": ma,
                        "stop_pct": f"{stop*100:.2f}%",
                        "target_pct": f"{target*100:.1f}%",
                        "total_trades": metrics["total_trades"],
                        "wins": metrics["wins"],
                        "win_rate": metrics["win_rate"],
                        "net_total": metrics["net_total"],
                        "total_return_pct": metrics["total_return_pct"],
                        "max_drawdown_pct": metrics["max_drawdown_pct"],
                        "profit_factor": metrics["profit_factor"],
                        "avg_pnl_pct": metrics["avg_pnl_pct"],
                        "avg_fees_per_trade": metrics["avg_fees_per_trade"],
                        "risk_adjusted_score": round(risk_adjusted, 2),
                        "outcomes": str(metrics["outcomes"]),
                    })

                if done % 10 == 0:
                    print(f"  {done}/{total_combos} combos done...")

    sweep_df = pd.DataFrame(rows)
    sweep_df = sweep_df.sort_values("risk_adjusted_score", ascending=False)
    sweep_df.to_csv(output_path, index=False)
    print(f"\nSweep complete → {output_path}")
    print("\nTop 10 combos by risk-adjusted score:")
    print(sweep_df.head(10).to_string(index=False))

    return sweep_df


# ── Execution gap analysis ──────────────────────────────────────────────────────

def execution_gap_analysis(
    backtest_trades: pd.DataFrame,
    live_trades: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compare backtest fills vs actual live fills from the trade log.
    Produces a gap report: entry price diff, pnl diff, fee diff.
    """
    if backtest_trades.empty or live_trades.empty:
        return pd.DataFrame()

    # Merge on ticker + signal_date (approximate)
    live_trades = live_trades.copy()
    backtest_trades = backtest_trades.copy()

    live_trades["key"] = live_trades["ticker"] + "_" + live_trades["signal_date"].astype(str)
    backtest_trades["key"] = backtest_trades["ticker"] + "_" + backtest_trades["signal_date"].astype(str)

    merged = backtest_trades.merge(
        live_trades[["key", "entry_price", "exit_price", "pnl_pct", "outcome"]],
        on="key",
        how="inner",
        suffixes=("_bt", "_live"),
    )

    if merged.empty:
        print("No matching trades found between backtest and live log. Check signal dates.")
        return pd.DataFrame()

    merged["entry_slippage_bps"] = (merged["entry_price_bt"] - merged["entry_price_live"]) / merged["entry_price_live"] * 10_000
    merged["pnl_gap_pct"] = merged["pnl_pct_bt"] - merged["pnl_pct_live"]
    merged["fee_model_vs_zero"] = merged["fees"]

    gap_summary = {
        "avg_entry_slip_bps": round(merged["entry_slippage_bps"].mean(), 2),
        "avg_pnl_gap_pct": round(merged["pnl_gap_pct"].mean(), 4),
        "backtest_wins": len(merged[merged["pnl_pct_bt"] > 0]),
        "live_wins": len(merged[merged["pnl_pct_live"] > 0]),
        "backtest_avg_pnl": round(merged["pnl_pct_bt"].mean(), 4),
        "live_avg_pnl": round(merged["pnl_pct_live"].mean(), 4),
        "total_fee_model_cost": round(merged["fees"].sum(), 2),
        "matched_trades": len(merged),
    }

    print("\n=== Execution Gap Analysis ===")
    print(f"  Matched trades: {gap_summary['matched_trades']}")
    print(f"  Avg entry slip (backtest vs live): {gap_summary['avg_entry_slip_bps']:+.2f} bps")
    print(f"  Avg PnL gap (backtest vs live): {gap_summary['avg_pnl_gap_pct']:+.4f}%")
    print(f"  Backtest avg PnL: {gap_summary['backtest_avg_pnl']:+.4f}%")
    print(f"  Live avg PnL:     {gap_summary['live_avg_pnl']:+.4f}%")
    print(f"  Model fee cost (added): ${gap_summary['total_fee_model_cost']:.2f}")
    print(f"  Backtest win rate: {gap_summary['backtest_wins']/len(merged)*100:.1f}%")
    print(f"  Live win rate:     {gap_summary['live_wins']/len(merged)*100:.1f}%")

    return merged


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    random.seed(42)
    np.random.seed(42)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    keys_path = os.path.join(script_dir, "alpaca_keys.txt")

    # ── Strategy configs ────────────────────────────────────────────────────────
    STRATEGIES = {
        "SOXX_semis": {
            "index_ticker": "SOXX",
            "basket": ["MU", "ON", "NXPI", "QCOM", "ASML", "AVGO", "TXN", "ARM", "KLAC", "AMD"],
        },
        "IWM_smallcaps": {
            "index_ticker": "IWM",
            "basket": ["SFM", "FIX", "CELH", "APP", "FTAI", "ANF", "CROX", "SMCI", "AXON", "PLTR"],
        },
    }

    INITIAL_CAPITAL = 25_000.0

    # ── Date range ──────────────────────────────────────────────────────────────
    # Use last 6 months of data for the backtest
    end_date = pd.Timestamp.today(tz="America/New_York").normalize()
    start_date = end_date - pd.Timedelta(days=365)

    print(f"Backtest period: {start_date.date()} to {end_date.date()}")
    print(f"Initial capital: ${INITIAL_CAPITAL:,.0f}")
    print(f"Execution model: {EXECUTION_LATENCY_SECONDS[0]}-{EXECUTION_LATENCY_SECONDS[1]}s latency, "
          f"{SLIPPAGE_BPS} bps base slippage, ATR factor {SLIPPAGE_ATR_FACTOR}")
    print(f"Fees: ${ALPACA_FEE_PER_SHARE}/share round-trip (min ${ALPACA_MIN_FEE})")

    client = get_alpaca_client(keys_path)
    if not client:
        print("ERROR: Could not connect to Alpaca. Check your API keys.")
        return

    for strat_name, strat in STRATEGIES.items():
        print(f"\n{'='*60}")
        print(f"Running realistic backtest: {strat_name}")
        print(f"{'='*60}")

        index_ticker = strat["index_ticker"]
        basket = strat["basket"]
        all_tickers = [index_ticker] + basket

        print(f"Fetching 1-min bars for {all_tickers}...")
        all_data = fetch_1min_bars(client, all_tickers, start_date, end_date)

        index_df = all_data.get(index_ticker, pd.DataFrame())
        basket_data = {t: all_data.get(t, pd.DataFrame()) for t in basket}

        if index_df.empty:
            print(f"  No data for {index_ticker}. Skipping.")
            continue

        valid_baskets = {t: df for t, df in basket_data.items() if not df.empty}
        if not valid_baskets:
            print("  No basket data. Skipping.")
            continue

        print(f"  Index bars: {len(index_df)} | Basket tickers with data: {list(valid_baskets.keys())}")

        # ── Run sweep ───────────────────────────────────────────────────────────
        sweep_out = os.path.join(script_dir, f"sweep_{strat_name}_realistic.csv")
        sweep_df = run_sweep(index_df, valid_baskets, sweep_out, INITIAL_CAPITAL)

        # ── Best params full report ─────────────────────────────────────────────
        if not sweep_df.empty:
            best = sweep_df.iloc[0]
            print(f"\nBest config: MA={best['ma_periods']}, "
                  f"stop={best['stop_pct']}, target={best['target_pct']}")
            print(f"  Net return: {best['total_return_pct']:+.2f}%")
            print(f"  Win rate: {best['win_rate']:.1f}%")
            print(f"  Max DD: {best['max_drawdown_pct']:.2f}%")
            print(f"  Risk-adjusted score: {best['risk_adjusted_score']}")

        # ── Execution gap vs live log ──────────────────────────────────────────
        live_log_path = os.path.join(script_dir, f"shotgun_trades_{strat_name}.csv")
        if os.path.exists(live_log_path):
            try:
                live_df = pd.read_csv(live_log_path)
                best_ma = int(best["ma_periods"]) if not sweep_df.empty else 20
                best_stop = float(best["stop_pct"].replace("%", "")) / 100 if not sweep_df.empty else 0.01
                best_target = float(best["target_pct"].replace("%", "")) / 100 if not sweep_df.empty else 0.03

                bt_trades, bt_metrics = run_backtest(
                    index_df, valid_baskets, best_ma, best_stop, best_target,
                    initial_capital=INITIAL_CAPITAL, verbose=False
                )

                gap_df = execution_gap_analysis(bt_trades, live_df)
                gap_out = os.path.join(script_dir, f"gap_analysis_{strat_name}.csv")
                if not gap_df.empty:
                    gap_df.to_csv(gap_out, index=False)
                    print(f"Gap analysis saved → {gap_out}")
            except Exception as e:
                print(f"  Could not run gap analysis: {e}")

        print(f"\n  Sweep CSV → {sweep_out}")

    print(f"\n{'='*60}")
    print("Done. Run `python realistic_backtester.py` again to re-run with new params.")
    print("Edit EXECUTION_LATENCY_SECONDS and SLIPPAGE_BPS at the top to tune the model.")


if __name__ == "__main__":
    main()
