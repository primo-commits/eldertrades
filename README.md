# Elder Trades

Automated implementation of Elder Santis's "One and Done" model on Alpaca paper
trading: **context → location → confirmation**.

Strategy source and the full feasibility analysis: [`docs/strategy-spec.md`](docs/strategy-spec.md).
Original (broken) files kept for diff purposes in [`legacy/`](legacy/).

**First time running it? Follow [`RUNBOOK.md`](RUNBOOK.md).**

## Quick start

```bash
pip install -r requirements.txt

# credentials -- paper keys from app.alpaca.markets/paper/dashboard/overview
cat > alpaca_keys.txt <<'KEYS'
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
KEYS

# offline check -- no network, no credentials needed
python -m elder.preflight --scan   # check everything before the open
python -m tests.test_pipeline

# dry run: scans and logs setups, places NO orders
python -m elder.runner --once

# live paper trading
python -m elder.runner --live
```

Windows: `scripts\run_bot.bat --live`  ·  Linux/macOS: `scripts/run_bot.sh --live`

**Dry run is the default.** `--live` is required to submit orders (still to the
paper account while `account.paper: true`).

## Options

| Flag | Meaning |
|---|---|
| `--live` | Submit real orders. Omit for dry run |
| `--once` | Single scan then exit |
| `--tier` | Universe tier: `starter`, `etf_core`, `equity_large`, `intl_trial` |
| `--symbols` | Comma-separated override, e.g. `SPY,QQQ` |
| `--interval` | Minutes between scans |
| `--ignore-clock` | Scan with the market closed (inspection only) |
| `-v` | Debug logging |

## Which account am I on?

```bash
python -m elder.account
```

Prints the account number the API keys resolve to, what it holds, and the
position sizes that follow from it. API keys are tied to one specific paper
account — if the dashboard and the bot disagree about equity, compare the
account numbers first.

## Before you run it

1. **Reset your Alpaca paper account to $1,000,000** in the dashboard. The bot
   reads live equity from the broker — a command-line balance would not change
   what Alpaca thinks you have.
2. **Check `config.yaml`.** Risk is 0.25% per trade, 1.5% daily loss limit,
   5% max position, 10 concurrent positions.
3. **`feed: iex` is the free tier** and sees roughly 2% of consolidated volume.
   Volume profile and confirmation are both degraded on it. The bot warns at
   startup. `feed: sip` needs Algo Trader Plus.

## Output

| Path | Contents |
|---|---|
| `logs/elder_YYYY-MM-DD.log` | Full scan log |
| `journal/setups_YYYYMMDD.csv` | Every setup with entry, stop, target, size, zone, scores |
| `journal/rejections_YYYYMMDD.csv` | Every rejection with stage and reason |
| `state/risk_state.json` | Daily loss and peak equity, persisted across restarts |

The rejections file is the one to read first. It tells you *why* nothing fired,
which is what made the original bot impossible to debug.

## Layout

```
config.yaml            single source of truth
elder/
  config.py            typed config + startup warnings
  keys.py              credential loading
  universe.py          127 instruments, correlation buckets
  data.py              Alpaca bars, sessions, resampling
  indicators.py        session VWAP, Wilder ATR, swings
  volume_profile.py    POC, value area, HVN/LVN            CONTEXT
  structure.py         4H regime, invalidation levels      CONTEXT
  zones.py             supply/demand zones                 LOCATION
  orderflow.py         trade classification, delta         (needs SIP)
  confirmation.py      exhaustion + flip trigger           CONFIRMATION
  strategy.py          assembles the three pillars
  risk.py              live-equity risk engine
  broker.py            Alpaca orders
  journal.py           CSV journaling
  runner.py            scan loop / CLI
tests/
  test_pipeline.py     offline end-to-end check
  test_reconciler.py   close verification and orphan detection
  test_timeframes.py   session-anchoring and zone proximity regressions
```

## Position reconciliation

A close request to Alpaca is a market order, not a guarantee. `elder/reconciler.py`
runs on its own thread every 30s (configurable) and treats the broker as truth:

1. **Verifies requested closes actually closed.** If a close is rejected it
   escalates — from the second attempt it cancels the symbol's open orders
   first, because a bracket's TP/SL legs reserve the shares and cause a close
   order to be rejected for insufficient quantity. That single step resolves
   most stuck closes. After `max_close_attempts` it halts trading and logs
   MANUAL INTERVENTION REQUIRED.
2. **Detects orphaned positions** — open with no live stop order. This is the
   worst state the bot can be in: the bracket leg was rejected or cancelled and
   downside is unbounded. With `auto_protect: true` it flattens them.
3. **Cancels stale unfilled entries** older than `stale_order_minutes`.

Tuning is in `config.yaml` under `reconcile`. Tests: `python -m tests.test_reconciler`.

## Backtesting

```bash
python -m backtest.run --synthetic                 # engine self-test, no keys
python -m backtest.run --days 180                  # in-sample single pass
python -m backtest.run --walk-forward --days 365   # OUT-OF-SAMPLE -- the real answer
python -m backtest.run --sweep --days 180          # parameter grid (in-sample)
```

Or `6-BACKTEST.bat` on Windows.

**Each test window is given `data.lookback_days` of preceding history as
warm-up.** Bars before `test_start` are processed so structure, zones and
indicators build up, but no position may be opened until the test period
begins. Without this a window starts cold: the 4H bias needs roughly 14
sessions before a single pivot confirms, and `find_zones` needs 26 bars per
timeframe, so a bare 20-session window cannot produce context at all and
records zero trades regardless of the strategy.

**Only the walk-forward number means anything.** A single pass over all data is
in-sample: it tells you how well the parameters fit that period, not whether
there is an edge. Walk-forward optimises on one window, evaluates on the next
unseen one, rolls forward, and reports only out-of-sample results.

What this backtester fixes versus the original (`legacy/realistic_backtester.py`):

| | Original | Now |
|---|---|---|
| Strategy tested | an index MA crossover | **the actual Elder pipeline** |
| Entry fill | the signal bar's own close (look-ahead) | **next bar's open** |
| Exit fill | exactly at stop/target | **gaps fill at the open; slippage both sides** |
| Same-bar stop + target | target could win | **stop assumed (conservative)** |
| Costs | $0.003/share commission only | **spread + slippage + SEC + TAF** |
| RNG | global, seeded once for a 100-combo sweep | **local, reseeded per run** |
| Capital | 5% of initial per position, no ledger | **real cash accounting** |
| Validation | argmax over one year | **walk-forward, disjoint test windows** |
| "Profit factor" | avg win / avg loss (wrong) | **gross profit / gross loss** |

## Known limitations

- **No passive liquidity.** Elder reads resting orders on Bookmap. That needs
  CME depth-of-book and has no Alpaca equivalent. `confirmation.mode: bars`
  uses bar volume as a stand-in; `orderflow` mode uses classified trade delta
  (SIP) and is closer, but neither sees the book.
- **He trades ES/NQ futures.** SPY and QQQ are the nearest Alpaca instruments.
- **Crypto removed** — Alpaca sees too small a share of global crypto volume for
  a representative volume profile.
- **Not backtested yet.** Nothing here has been validated for profitability.
  Run it in dry run first and read the journal.
