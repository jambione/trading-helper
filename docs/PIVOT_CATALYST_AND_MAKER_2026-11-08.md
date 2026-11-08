# Strategic Pivot Handoff: Catalyst Thrust (#3) & Liquidity Shock Maker (#4)

**Date:** 2026-11-08  
**Goal:** Build a genuinely positive-expectancy engine capable of compounding a $10,000 account at +0.75% to +1.5% per day ($75–$150/day initially) toward $35k–$50k ($500/day).  
**Directive for Next Agent (Gemini Flash / Pro):** **STOP** running 1–2 bp parameter tweaks on `%R`, `pre-square`, and RTH indicator filters/exits. Over 120 studies (`docs/EDGE_SEARCH_MAP_2026-11-06.md`, `docs/EXPERIMENTS.md`) proved that taking liquidity via market orders on 1-to-15 minute technical patterns during RTH yields `0 to +2 bp` gross and `-6 to -8 bp` net after the 4–6 bp spread.

We are pivoting exclusively to **two structural, high-magnitude paradigms** where gross moves are large enough (+30 to +300 bp) or execution is flipped from *paying* spread to *earning* spread.

---

## Rules Kept Strict (Do Not Bypass)
Even though we are pivoting to big-mover strategies, **keep the anti-overfitting protections (`docs/TEST_STANDARD.md`)**:
1. **Pre-registration (`docs/studies/*_prereg.json`)** before reading outcomes.
2. **Code-review gate (`P4`) + planted-edge canary** + outcome-blind sample check before the single read.
3. **Fair P2 Controls:** Primary control = same-minute cross-name (or matched non-catalyst control); secondary = same-name strictly *after* the hold window (`t + hold + 2 .. t + hold + 62`).
4. **Realistic Fill Model:** Never assume passive limits fill merely on price touch; require trade-through (e.g., $\ge 2\text{ bp}$ through limit) or bid-cross on historical SIP NBBO quotes.

---

## Track #3: Immediate Hard Catalysts (Open Thrust & Breaking Intraday News)

> **CORRECTIONS (Claude, 11/08, checked against the repo):** (1) Row 70's +15.14 bp (gap >= 8%) has **t = +1.34, below the 2.0 bar**, and its "RESEARCH PROVEN" label was the author's own; running_list Row 71 records that **no result-skeptic review ran on it**. Treat it as an unreviewed lead, not a finding. (2) The Benzinga/catalyst cache is real (ai_reports/catalyst_pullback on the mini, ~70 MB) but there is **no NBBO quote warehouse**: the warehouse holds 1-minute bars (plus 12 ETFs' bars and a sampled ETF spread file). Track #4 fills must be judged from bars and prints, or quotes must be fetched per event. (3) Sizing: even a real +15 bp on roughly one or two qualifying events a day is about $15-30 a day on $10,000, not $500. (4) The "+1% a day compounding to $500 a day" roadmap is a target, not a result; nothing measured supports +35 to +60 bp net per trade.

### Why "We Already Tested Catalysts" Is Only Half True
- **What failed (`docs/studies/CATALYST_PULLBACK_2026-10-31.md`, Row 66):** Waiting until `09:40–10:30 ET` for an overnight gapper to pull back to VWAP. It lost `-31.4 bp/trade`. Crucially, Line 52 of that report proved *why*: **true catalyst winners run immediately from the open and never pull back to VWAP; the ones that pull back to VWAP at 10:00 AM are institutional distribution fades.**
- **What succeeded (`docs/running_list.md`, Row 70):** `tools/studies/open_thrust_deep_dive.py` (currently on git tag `archive/desk-2026-11-03`) tested buying the **09:30 Open Thrust** on verified news catalysts (`Gap >= 8%`) with a **1-Minute Prior-Low Trailing Stop (max 5m hold)** and netted **+15.14 bp/trade after 12 bp of costs/slippage** (`+16.6 bp` H1, `+13.7 bp` H2 across 417 sessions). It was wiped during the 11/03 branch reset before undergoing a formal P2/P4 skeptic audit.

### Action Items for Track #3
1. **Audit & Formalize Row 70 (`open_thrust_deep_dive.py`):**
   - Extract `tools/studies/open_thrust_deep_dive.py` from tag `archive/desk-2026-11-03` (`git show archive/desk-2026-11-03:tools/studies/open_thrust_deep_dive.py`).
   - Check existing cached data in `ai_reports/catalyst_pullback/` (684 sessions, 1,112 verified Benzinga fundamental catalysts: Earnings Beats, Guidance Raises, FDA Approvals).
   - Write a strict pre-registration (`docs/studies/catalyst_open_thrust_prereg.json`) testing the 09:30:00–09:35:00 opening drive on verified fundamental catalysts (`Gap >= 5%` and `Gap >= 8%`) with a 1-minute prior-low trailing stop and P2 controls.
   - Pass the P4 code-review + planted canary gate and run the one read.
2. **Intraday Breaking-News Shock (Second-0 / Minute-0 Execution):**
   - Previous studies only looked at *overnight* news (`16:00 D-1 to 09:25 D0`).
   - Using the cached Benzinga news timestamps (`created_at`), identify **intraday RTH breaking catalyst headlines** (`09:35 to 15:30 ET`: M&A, FDA, guidance, contract wins, 8-K).
   - Measure the markout and trailing-stop return of entering at the first bar/tick following the headline timestamp, before retail indicators even form.

---

## Track #4: Passive Liquidity Air-Pocket Maker (Flipping the Spread)

### Why "We Already Tested Limit Orders" Is False
- **What failed (`docs/studies/limit_below_prereg.json`, Row 7):** Placing a limit order 10 bp below the desk's `%R` *momentum breakout* signal. When you try to buy a momentum breakout with a limit order below market, you only fill when the breakout fails and crashes (`-65.6 bp`).

### The Real Strategy to Test (`liquidity_shock_maker`)
Instead of buying momentum with a limit order, **provide liquidity at the bottom of a violent, non-news micro-liquidation sweep ("air pocket")** on liquid stocks (`P >= $25`, `ADV20 >= $20M`):
1. **Trigger (Exhaustion Air-Pocket):**
   - Stock is in a neutral-to-positive intraday regime (e.g., above day's open or SPY non-crashing) with no negative news headline.
   - Suffers a sudden 1-to-3 minute panic sweep (`drop >= 60 to 100 bp` on `>= 3x` rolling 15-minute average volume) that stretches price below short-term equilibrium.
2. **Execution (Maker-Maker or Maker-Taker):**
   - **Entry:** Passive resting BUY limit placed at the exhausted bid (or `k bp` below the panic bar's close). Must trade through by $\ge 2\text{ bp}$ on SIP prints/quotes to count as filled (strict adverse-selection honesty).
   - **Exit:** Passive resting SELL limit at `+35 to +60 bp` reversion target, or time-stop at `5–10 minutes`, with a catastrophic backstop.
3. **Why the Math Works:**
   - Instead of paying `-6 bp` round-trip spread to chase a `+2 bp` momentum move, you **earn `+4 to +8 bp` of spread** while capturing a `+25 to +50 bp` mean-reversion snapback as the order book refills.

### Action Items for Track #4
1. Draft `docs/studies/liquidity_shock_maker_prereg.json` using the 120-day SIP 1-minute bar + NBBO quote warehouse (`ai_reports/warehouse/`).
2. Build `tools/studies/liquidity_shock_maker.py` with the strict 2 bp trade-through fill rule, P2 controls, and planted-edge canary tests.
3. Run outcome-blind sample count first; once cleared, execute the single read.

---

## How to Start in Your New Session
Copy and paste this prompt after switching models:
> *"Read `docs/PIVOT_CATALYST_AND_MAKER_2026-11-08.md` and `docs/RESULTS_2026-11-07.md` (if finished). We are stopping 1-2 bp indicator tweaks and executing Track #3 (Auditing Row 70 `open_thrust_deep_dive.py` from `archive/desk-2026-11-03` + Intraday Breaking Catalysts) and Track #4 (Passive Liquidity Air-Pocket Maker). Start by inspecting the archived `open_thrust_deep_dive.py` and `ai_reports/catalyst_pullback/` cache, and draft the pre-registration for Track #3."*
