# Runbook — first live morning

Everything below assumes the repo is cloned and you are in its directory.

## The night before (do this now)

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Get paper API keys

<https://app.alpaca.markets/paper/dashboard/overview> → **Home → API Keys → Generate**.

Paper and live keys are **not** interchangeable. Make sure you are on the paper
dashboard, not the live one.

```bash
cat > alpaca_keys.txt <<'KEYS'
ALPACA_API_KEY=PK...your_key...
ALPACA_SECRET_KEY=...your_secret...
KEYS
```

`alpaca_keys.txt` is gitignored. It will not be committed.

### 3. Set the paper account balance

In the Alpaca paper dashboard, **reset the account** and set the starting
balance to **$1,000,000**.

This matters: the bot reads live equity from the broker on every risk check.
The `nominal_equity` in `config.yaml` is documentation only. If the account is
still at the $100k default, every position will be sized for $100k.

### 4. Enable shorting

The strategy is symmetric — it takes bearish setups at supply zones. If the
paper account has no margin enabled, **every short is rejected by the broker**
and you silently run long-only. Preflight warns you if this is the case.

### 5. Run preflight

```bash
python -m elder.preflight --scan
```

This exercises every failure mode that could bite you at 09:30: dependencies,
config, credentials, account status and flags, market clock, carried-over
positions and orders, data availability per symbol per timeframe, filesystem
permissions, and a full dry strategy evaluation.

**Do not proceed while anything is `[FAIL]`.** Read every `[WARN]`.

### 6. Run the offline tests

```bash
python -m tests.test_pipeline
python -m tests.test_reconciler
```

Both should end in `PASS`.

---

## Morning of

### 7. Preflight again

```bash
python -m elder.preflight
```

Overnight things change — data goes stale, a position carries over, the account
gets flagged. Thirty seconds now saves an hour later.

### 8. Start in DRY RUN first

```bash
python -m elder.runner
```

No `--live`. It scans, evaluates, sizes and journals — and places nothing.

**Let this run for at least the first 30–60 minutes.** Watch:
- Does it fetch data for every symbol?
- Does the reconciler tick every 30s without errors?
- Does it produce setups, and do they look sane?

### 9. Then go live

```bash
python -m elder.runner --live
```

Orders go to the **paper** account (`account.paper: true` in config).

Windows: `scripts\run_bot.bat --live`

---

## While it runs

Three things to watch, in order of importance:

```bash
# 1. Anything the reconciler is unhappy about -- this is the dangerous stuff
grep -E "ORPHANED|STILL OPEN|HALTED" logs/elder_*.log

# 2. Why nothing is firing
column -s, -t < journal/rejections_$(date +%Y%m%d).csv | tail -40

# 3. What it actually took
column -s, -t < journal/setups_$(date +%Y%m%d).csv
```

### Reading the rejections file

This is the important one on day one. Count the `stage` column:

| Mostly stopping at | Means |
|---|---|
| `context` | No 4H bias in your universe today. Normal on a chop day. |
| `location` | Bias exists, price is not at a zone. Normal. |
| `confirmation` | Reaching zones but the trigger never fires. If this is 100% all day, `flip_atr_mult` or `exhaustion_bars` is too strict. |
| `exits` | Setups forming but failing R:R or the stop-width cap. Check `stop_atr_max_mult`. |
| `risk` | Blocked by position/bucket/exposure limits. Check which. |

**Zero trades on day one is a normal outcome, not a failure.** The strategy
requires three independent conditions to align. Read the stage counts before
changing anything.

---

## Stopping

`Ctrl-C` once. It finishes the current scan, stops the reconciler, logs a final
reconcile report, and exits cleanly. Do not `kill -9` — you want that final
report.

---

## If something goes wrong

| Symptom | Do this |
|---|---|
| `RISK ENGINE IS HALTED` in preflight | Read `state/risk_state.json` for the reason. Resolve it, then delete the file to clear. |
| Position will not close | The reconciler escalates automatically. If it logs `MANUAL INTERVENTION REQUIRED`, close it in the Alpaca dashboard. |
| Orphaned position warning | `auto_protect` flattens it. Verify in the dashboard. |
| Every short rejected | Shorting not enabled on the paper account. |
| No data for a symbol | IEX carries thin names poorly. Drop it from the tier or move to SIP. |
| Everything rejected at `context` | Expected on a rangebound day. Check again tomorrow before touching parameters. |

**Change one parameter at a time, and only after a full session of evidence.**
Tuning against a single morning is how you fit to noise.
