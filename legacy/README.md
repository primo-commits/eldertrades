# Legacy / baseline

The original files as supplied, committed unmodified so the rewrite is a readable diff.
**Do not run anything in this folder** — see the audit in the repo root README for the
blocking defects (no live order ever succeeds; crypto data is ~20 days stale).

| File | Status |
|---|---|
| `elder_trades_paper_trader.py` | superseded by the `elder/` package |
| `GFT_Risk_Dashboard.py` | models MT5/cTrader CFDs (GER40, XAUUSD, NAS100) — not tradeable on Alpaca |
| `realistic_backtester.py` | superseded by `backtest/` (look-ahead bias, asymmetric slippage) |
| `run_bot.bat` | bash syntax in a .bat; broken date slicing |
| `elder_trades_9-19-0-1.log` | evidence: malformed filename + bare `time /t` output |
