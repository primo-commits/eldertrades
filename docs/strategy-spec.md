# Strategy spec — Elder Santis, "One and Done" model

Source: *My Entire Day Trading Strategy Explained In 30mins* (full transcript in
`docs/transcript-one-and-done.txt`). Instrument in the video: **ES futures**.

> **Supersedes the PO3/TTrades draft.** That was a wrong lead. This transcript
> matches the original research: supply/demand + volume profile + order flow.

## The three pillars

> "For any good setup, you need three things. You need context, you need
> location, and then you need confirmation."

Nothing fires unless all three line up. He is explicit that any one alone is a
losing trade: *"Don't just be looking at book map and being like, oh, big bubble,
I'm shorting."*

---

## 1. CONTEXT — where is the market going?

### 1a. Market structure (4H timeframe)

Classified from swing sequence:

| Regime | Definition (his words) |
|---|---|
| **Bullish** | "two higher lows and three higher highs" |
| **Bearish** | "two lower highs and then three new lows" |
| **Balanced** | "three equal highs and three equal lows" |
| **Unclear** | No clean structure → **no predetermined bias**; react after the open |

Rationale given: *"any timeframe tends to stay in a trend than it does breaking
or forming a new one"* — he is leaning on trend persistence, not prediction.

**He is never married to the bias.** News or an open-drive can invalidate it.

### 1b. Volume profile (built on 30-minute bars)

Two profiles, both in use through the day:

| Profile | Window |
|---|---|
| **Session / overnight** | Futures session: **18:00 ET → 17:00 ET next day** ("closes at five, opens back up at six") |
| **RTH** | NY open → NY close |

Components:
- **Value Area** — "68% of the total transaction volume of the given session"
- **POC** — price with the most transactions; *"a magnet, fair value reference"*

Reads:

| Observation | Meaning |
|---|---|
| Price **inside** VA | Balanced / neutral — rotates VAH ↔ VAL |
| Break **above** VA + volume | Market finding value higher → favour longs |
| Break **below** VA + volume | Market finding value lower → favour shorts |
| **POC rising** session over session | Bullish continuation |
| **POC falling** session over session | Bearish continuation |
| New high/low on **thin** volume, snap back into VA | Rejection — fade it |

Key insight he stresses: candles alone mislead. A new low with no volume that
immediately returns to value means *lower prices were rejected*, even though the
candle chart shows a lower low.

---

## 2. LOCATION — where do I engage?

### 2a. Supply & demand zones

Drawn on 4H, 2H, 1H, 30m, 15m.

**Rule:** find an **obvious** large expansion move, then mark the
**consolidation/balance immediately before it**. That consolidation is the zone.

> "Mark out the consolidation, which again is going to be the value area before
> the expansion."

- **If the move is not obvious, skip it.** *"If the move down or up isn't
  obvious, I suggest you just simply stay away."*
- Zone strength = how many times price has since respected it (hold, or
  break-and-retest).
- Logic: institutions cannot fill size in one go, so they accumulate in the
  consolidation before driving price. The zone is where their fills sit.

### 2b. Volume profile levels

| Level | Use |
|---|---|
| **POC** | Target / magnet. **Never enter blindly at POC** — price chops there |
| **HVN** (high volume node) | Acceptance, consolidation, slow price. **Do not enter in the middle.** Use as a target where price stalls |
| **LVN** (low volume node) | Thin — price moves through fast. Either sharp rejection or fast breakout |

**The highest-quality setup is confluence**: an S/D zone that is *also* an LVN.
Expect a quick, decisive reaction. Conversely, a supply zone with an LVN *above*
it means: if it breaks, expect an immediate run through the thin area.

---

## 3. CONFIRMATION — the trigger

> "which is gonna be our confirmation part later in this video, which is
> probably the most important part"

Tools: **Bookmap heatmap**, **footprint chart**, **DOM**.

Two object types on the heatmap:

| Visual | What it is |
|---|---|
| Green / red **bubbles** | **Aggressive** market orders being filled. Bigger bubble = more volume |
| Orange / red **lines** | **Passive** resting limit orders — the millions/billions waiting |

### The entry trigger (this is the precise rule)

**Both sides must turn. One is not enough.**

At a **supply** zone, to go short:
1. Price pushes up into the zone
2. Aggressive **buyers get progressively smaller** — exhaustion
3. Aggressive **sellers step up** and start moving price down

At a **demand** zone, to go long:
1. Price pushes down into the zone
2. Aggressive **sellers shrink or disappear**
3. Aggressive **buyers step up** and start moving price up

> "You're not only waiting for one side to die out or get absorbed, you're
> waiting for the other side to step back up and then fully take control. This
> is what you have to use as an entry."

**Disqualifier:** if price enters supply and buyers stay aggressive and blast
through — no trade. *"Assuming makes a fool out of you and me."*

---

## 4. Stop loss

At **thesis invalidation**, not at a fixed distance.

> "I want to put my stop loss where my thesis is invalid. If I'm looking for
> longs, I think this is going to be a higher low. I'm going to put my stop loss
> underneath that higher low because if we make a new low, my thesis is
> incorrect."

## 5. Targets

- **POC** as the primary magnet
- **HVN** where price is expected to stall
- Opposing value area edge

## 6. Trade philosophy

- Wants **fast** moves — *"I don't want to just sit and chop"*
- Never married to a bias
- Spread ignored on ES (too liquid to matter) — **this does not hold for
  equities or Alpaca crypto**

---

# Alpaca feasibility

| Component | Alpaca | Notes |
|---|---|---|
| 4H market structure | ✅ **Full** | Swing sequence from bars |
| Volume profile, VA, POC | ✅ **Full** | From 30-min bars, better from SIP ticks |
| POC session-over-session trend | ✅ **Full** | |
| HVN / LVN detection | ✅ **Full** | |
| S/D zone marking | ✅ **Full** | "Obvious expansion" needs a numeric threshold |
| Zone × LVN confluence | ✅ **Full** | |
| Invalidation-based stops | ✅ **Full** | Swing-based |
| POC/HVN targets | ✅ **Full** | |
| **Aggressive flow** (the bubbles) | ⚠️ **Approximate** | `elder/orderflow.py` classifies trades buy/sell from SIP quotes. Needs `feed: sip`. On IEX it is noise |
| **Passive liquidity** (the lines) | ❌ **Not available** | Requires CME depth-of-book. No equities equivalent exists at this price point |

**Verdict:** Context and Location — the majority of the framework — are fully
automatable. The Confirmation trigger as he *states* it is about **aggressive**
participants shrinking and flipping, and that is approximable from SIP trade
data. The passive heatmap lines inform his read but are not the stated trigger.

**`feed: sip` is now mandatory, not optional.** On IEX the confirmation step
cannot be built at all.

---

# Instrument ranking (revised again)

Volume profile and order flow both require the venue to see a representative
share of volume.

| Rank | Instrument | Why |
|---|---|---|
| 1 | **SPY, QQQ** | Closest to ES/NQ. Deep, well-distributed volume; SIP ticks give usable profiles and flow |
| 2 | **IWM, SMH, XLF, XLE, TLT, GLD** | Liquid ETFs, clean profiles |
| 3 | **NVDA, AAPL, MSFT, TSLA, AMZN, META** | Workable; single-name headline risk |
| ❌ | **Crypto (BTC/ETH/SOL)** | **Drops out.** Alpaca sees a small slice of global crypto volume, so the volume profile is not representative — the core of the method breaks. (Under the PO3 draft crypto ranked first; that reverses here.) |

---

# Open questions — need a number before coding

1. **"Obvious" expansion** — what multiple of ATR (or consecutive-bar range)
   qualifies a move as obvious enough to mark its origin?
2. **Equal highs/lows tolerance** — within what % are three highs "equal"?
3. **Swing definition** — what fractal width defines the HH/HL sequence?
4. **Acceptance outside VA** — how many bars, or how much volume, before a
   break counts as acceptance rather than a poke?
5. **HVN / LVN thresholds** — what percentile of the volume-at-price
   distribution marks a node high or low?
6. **"Getting smaller"** — over how many bars/trades must aggressive size
   decline monotonically to count as exhaustion?
7. **"Step up and take control"** — what delta flip magnitude confirms it?
8. **Equity session mapping** — the futures 18:00→17:00 profile has no exact
   equity analogue. Use 04:00→20:00 extended hours, add Alpaca's `overnight`
   feed, or RTH only?
9. **Zone timeframe priority** — when 4H and 15m zones conflict, which wins?

---

# Implementation parameters (supplied 2026-09-21)

| Parameter | Value | Where |
|---|---|---|
| Equal HH/LL tolerance | 0.5% | `strategy.context.equal_level_tolerance` |
| Swing fractal half-width | 3 bars | `strategy.context.swing_bars` |
| VA breakout confirmation | 2 closes outside + vol > 1.5x avg | `strategy.volume_profile.va_breakout_*` |
| HVN threshold | ≥ 60th pct volume-at-price | `strategy.volume_profile.hvn_percentile` |
| LVN threshold | ≤ 30th pct (complement, chosen) | `strategy.volume_profile.lvn_percentile` |
| Exhaustion | 3 consecutive pushes, declining volume | `strategy.confirmation.exhaustion_*` |
| Momentum flip | > 1 ATR move + vol > 1.5x avg | `strategy.confirmation.flip_*` |
| Session | NYSE 09:30–16:00 ET weekdays | `data.rth_open` / `rth_close` |
| ATR stop multiple | 1.5 | `strategy.exits.stop_atr_max_mult` |

## Two places the implementation differs from the literal wording

**1. ATR stop is a cap, not the basis.** The transcript is explicit that the stop
goes where the thesis breaks, not at a fixed distance:

> "I want to put my stop loss where my thesis is invalid... I'm going to put my
> stop loss underneath that higher low because if we make a new low, my thesis
> is incorrect."

So `stop_basis: invalidation` is primary — the stop sits beyond the last
confirmed swing (`structure.invalidation_level`) plus a 0.25×ATR cushion — and
**1.5×ATR acts as a maximum**. If invalidation is further than 1.5×ATR away, the
setup is rejected for being too wide rather than the stop being pulled in tight,
which would place it inside the noise the zone is supposed to absorb.

**2. Exhaustion direction.** The supplied wording was "3 consecutive bars of
lower closes + lower volume". At a **supply** zone price is pushing *up* into
it, so lower closes would already be the reversal, not the exhaustion that
precedes it. Per the transcript:

> "we see the market push up higher and higher and higher and we see the
> aggressive buyers getting smaller and smaller and smaller"

Implemented as: **continued pushes toward the zone, each on declining
volume/aggressive size.** Mirrored for demand. `exhaustion_bars: 3` and the
declining-volume requirement are kept as given.

## Measured behaviour

`structure.classify` on 300 random walks: **25% produce a directional bias**
(38 bullish, 36 bearish, 224 unclear, 2 balanced). Structure alone is not an
edge — which is precisely why the method requires location and confirmation on
top. Useful as a baseline when the backtester reports a win rate.
