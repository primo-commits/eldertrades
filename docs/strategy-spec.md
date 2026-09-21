# Strategy spec — 4-Hour Power of Three (PO3) / AMD

> **Status: DRAFT — awaiting transcripts.** Recorded from the user's brief on
> 2026-09-21. Nothing here is implemented yet. Rules marked **[AMBIGUOUS]**
> need a precise definition before they can be coded.

## Correction to earlier research

The strategy is **not** Elder Santis's (@tradingelder) order-flow method. It is
the **4-Hour Power of Three** model associated with **TTrades**, derived from
ICT / Smart Money Concepts. Earlier research in this repo targeted the wrong
trader. Consequence: the order-flow layer (`elder/orderflow.py`) is **no longer
required** — PO3 is derivable from OHLC plus time alone.

## Core cycle: AMD

A 4H candle is treated as a miniature daily chart:

| Phase | Behaviour |
|---|---|
| **Accumulation** | Candle opens, price ranges tightly |
| **Manipulation** | Price pushes the *wrong* way, sweeping liquidity and trapping traders. This is the wick. **Do not trade into it.** |
| **Distribution** | Price reverses and expands in the true direction, forming the body |

"Fading the 4-hour" = refusing the manipulation push, waiting for it to fail.

## Execution

1. **Anchor (4H)** — bias from high-impact 4H candles, commonly the **10:00 ET**
   and **06:00 ET** candles.
2. **Sweep** — wait for the candle to take out a key level (prior day high/low,
   session high/low).
3. **Shift (15m / 5m / 1m)** — require a **Market Structure Shift (MSS)** or
   **displacement** proving the fakeout is done.
4. **Entry** — on return to a newly formed **FVG**, **Order Block**, or
   **Inversion FVG** left by the reversal.
5. **Stop** — strictly beyond the manipulation wick.
6. **Target** — Fibonacci extension, commonly **-2 to -2.5 standard deviations**
   of the initial range, or the opposing liquidity pool.

## Filters

- **No volume, no trade.** Favour London/NY session overlap.
- **Shallow wick exception** — a shallow wick means manipulation resolved fast;
  trade the body expansion.
- **Deep push exception** — if price pushes very deep against bias, structure is
  broken: stand aside. **[AMBIGUOUS]** what depth qualifies.

## Open questions for the transcripts

1. **4H anchoring.** PO3 4H candles conventionally anchor to the **18:00 ET**
   CME open (18:00 / 22:00 / 02:00 / 06:00 / 10:00 / 14:00), not the 09:30
   equity open. Needs confirming. *(The current `data.session_anchored()`
   anchors to 09:30 and is wrong for this model.)*
2. **FVG definition** — 3-candle gap; does it need displacement to qualify?
   What counts as mitigated/filled: touch, 50%, or full fill?
3. **Order Block** — last opposing candle before displacement: body only, or
   body+wick? Must it be unmitigated?
4. **MSS vs BOS** — which swing qualifies, and does it need a *body* close
   through, or is a wick enough?
5. **Displacement threshold** — how large, relative to what? (ATR multiple?
   Average body size?)
6. **Liquidity pools** — which specifically: PDH/PDL, session H/L, equal
   highs/lows, relative equal highs/lows?
7. **Deep-push invalidation** — the percentage or level that says stand aside.
8. **Fib extension** — anchored to what exact two points?

## Instrument implications

| Instrument | PO3 fit |
|---|---|
| **BTC/USD, ETH/USD** | **Best fit on Alpaca.** 24/7, so all six 4H candles are real, no gaps. Long-only and no bracket orders (see `universe.py`). |
| **SPY, QQQ** | Only the 06:00-10:00, 10:00-14:00 and 14:00-18:00 candles carry meaningful volume. Overnight candles are near-empty. Alpaca's `overnight` feed may partially help. |
| **Futures (ES/NQ)** | The model's native habitat — not available on Alpaca. |

## Caveat to keep in view

SMC/ICT constructs (FVGs, order blocks, liquidity sweeps) are easy to identify
in hindsight and hard to define unambiguously in real time. Every rule above has
to be pinned down precisely enough that code produces one answer, not a judgment
call — that is what the transcripts are for, and what the backtester is for
after that.
