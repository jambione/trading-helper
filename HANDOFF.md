# Desk state — 2026-08-26

Replaces `GROK_HANDOFF.md`. Written for someone with none of the chat that
produced it. **Challenge anything.** Every claim carries its anchor.

**Git:** `master-mac`. **Live book:** `desk_product=scalp_legacy` — the
ratchet, auto-buy **armed** for Monday's open. Paper Alpaca, Mac mini
(`trading.jbrasfield.com`). The MacBook is the git/deploy box and has no
Alpaca keys.

`docs/PROFIT_REDESIGN.md` is **superseded**. Read it only as the record of
why the scalp was stopped on 8/21; its H4 direction was measured and
falsified on 8/22 (§4).

---

## 0. One page

The desk trades an 86-second continuation scalp on a squeeze watchlist,
protected by a raise-only software shelf 0.10R under the print (the
"ratchet"). Between 8/04 and 8/21 it took 347 paper trades and lost.

Grok's 8/21 redesign answered that by stopping the scalp (`observe`) and
proposing H4 — liquid multi-day swings. On 8/22 that was measured over 16
months and failed on its own terms, so the redesign is retired and the book
is back on the ratchet.

What the measurement actually found is narrower and more useful than either
position:

- **The ratchet works.** It beats every other exit tested, including just
  holding, by +0.167 R/trade at ~2σ. It is not what is losing money.
- **The entries have no edge.** They lose to a random minute on the same
  name, same day. The gate the desk runs is anti-selective.
- **The field is neutral, not hostile.** After admission the watchlist is a
  driftless walk (MFE/MAE ≈ 1.0). Nothing is tilted against you; there is
  simply nothing to select.
- **The drift is real but early.** 71% of the momentum move is over before
  the name is admitted. The desk arrives at 10:37 with 3.0% left, then the
  gate deliberates for another 31.7 minutes.

On 8/22 that last point was read as the answer: the open problem is
admission latency. **8/23 falsified it.** `captured` does not predict
realized R (−1.03σ; 5/9 sessions, p=1.000), and the excursion available is
flat in how far a name has already run (+0.62σ). Arriving earlier would not
have helped.

What replaced it is harder. A universe screen over six candidate
watchlists — including the 804 name-days the gate **rejected** — found
**zero drift and zero playable tape in 18 cells.** The desk's own watchlist
offers a median favorable excursion of 0.49% at 15 minutes against a 0.79%
round trip: **62% of what it costs to trade.** The rejects are
statistically identical to the admissions.

So the open problem is not the exit, not the entry gate, and not latency.
**It is that the available tape does not clear its own costs**, and the
selection machinery is choosing between names that do not differ. See §5C
and §5D — they supersede the direction §5A implied.

---

## 1. Live config (mini `config/bot_config.json`)

Tunables live here. `load_config()` does **not** read `signal_engine.env`;
a knob set there is silently dead. The trader reloads config each poll, so
a product change needs no restart.

| knob | value | note |
|---|---|---|
| `desk_product` | **scalp_legacy** | arms the ratchet. `observe` = no new auto-arms |
| `ai_trade_style` | Day scalp | cosmetic |
| `ai_watch_start_time` | **04:00** ET | premarket shadow; buys still RTH |
| `ai_h4_paper` / `ai_h3_paper` / `ai_late_hold_paper` | false | keep them false, see §4 |
| `ai_local_trail_give_r` | 0.10 | ≈0.497% of price, since 1R ≈ 4.97% |
| `ai_local_trail_give_spread_k` | **1.0** | floors the give at 1×spread — the shelf can no longer sit inside the book |
| `ai_local_trail_give_spread_max_r` | **0.50** | caps that floor. RTH spread_r runs p90 **5.56R**, and uncapped k=1 would park the shelf 5.5R down, which is no stop at all |
| `ai_local_trail_be_at_spread_k` | **1.0** | won't protect a gain until it clears one round trip |
| `ai_max_spread_r` | 0.0 (off) | **open, and now blocked on the feed, not on n.** A book wider than the move is unwinnable, but every `spread_r` on the record is an IEX quote, which overstates the book 1.5x in RTH, 3.3x premarket and 200x on SDOT (§5G). Setting this from the current record would refuse the *tightest* names. More tape will not fix it |
| `ai_watch_open_seed_min_pct` | 0.0 | new, and 0.0 = shipped behaviour. See §5 |
| heat 40 / 1R 5% / 2 slots / IEX / flatten 15:50 | unchanged | |
| `ai_watch_min_pct_change` | 50.0 | **not the operative gate** — see §5 |

**Deploy:** commit on the MacBook → push → `git pull` on the mini. The mini
cannot push. A stack restart logs the Claude CLI out of Keychain; restore
from a Terminal *on the mini* (`scripts/session.command`), never
`claude /login`, never an API key. The dashboard is served from disk
(`FileResponse` + a `/static` mount), so HTML/CSS/JS changes need only a
hard refresh, not a restart.

---

## 2. What the ratchet actually does

Held the 347 real entries fixed and swept only the exit, on 1-minute IEX
bars — the same feed the shelf watches. Costs charged at the measured
median spread (0.287% of price). `scratchpad/ratchet_prior.py`.

| exit | stomp% | median R | mean R | sessions green |
|---|---:|---:|---:|---:|
| **ratchet 0.10R (live)** | 13% | −0.103 | −0.029 | 31% |
| ratchet 0.20R | 5% | −0.105 | −0.024 | 23% |
| ratchet 1.00R | 2% | −0.076 | −0.038 | 46% |
| time exit 30m | — | −0.090 | −0.185 | 23% |
| hold to 15:50 | — | −0.069 | −0.196 | 38% |

All eleven policies lose. But paired against hold-to-close on the identical
trade, **ratchet 0.10R is +0.167 R at +1.96σ** and beats every time exit.

**The ratchet protects; it does not capture.** Mean improvement +0.83%,
median improvement **+0.000%** — the entire benefit is truncating the left
tail. That is a stop doing a stop's job. It cannot make expectancy that the
entries do not have.

**Stomps are not where the money went.** Widening the give from 0.10R to
1.00R cuts stomps 13% → 2% and lifts the win rate 25% → 36%, and the mean
stays at −0.03 R. Do not expect a give change to fix P&L.

> **⚠ The "live" row above is mislabelled, and the whole section needs
> re-testing (8/24).** `ai_local_trail_give_max_pct` was set to **0.1** in
> `bot_config.json` — 0.1% of price — against a shipped default of 0.0 and
> an intended give of ~0.5% of price. It therefore capped the R-based give
> at roughly a fifth of its intent on **every** name, not just the wide-R
> outliers it was written for, after which the spread floor set the real
> cushion. Two live positions on 8/24 measured effective give_r of **0.067
> (BMNR)** and **0.243 (RUM)** — a 3.6× spread while the config read 0.10.
>
> The sweep itself is still sound *as a simulation*: `ratchet_prior.py`
> applied each give directly, so "0.10R vs 1.00R" is a real comparison of
> two modelled policies. What is wrong is the claim that the desk was
> *running* the 0.10R row, and the geometry note below it describes the
> configured give rather than the effective one. Fixed 8/24 by restoring
> `give_max_pct = 0.0`; re-run the sweep against clean tape before citing
> any of this again. Found by the `give_r` column added the same morning,
> on its first session.

Geometry, for the record: give = 0.497% of price; median 1-minute return sd
on these names = 0.245%. So the shelf is 2.03× one-minute noise, 1.44× at
the 2-minute median hold, and 0.77× by 7 minutes. Shelf/spread is 1.73 at
the median — but spread p75 is 0.283 R, nearly 3× the give, so **the shelf
does sit inside the spread on the widest quarter of trades.**

---

## 3. Why it loses: the entries

**Dart control.** Same 0.10R shelf, real entry vs a random minute on the
same name/day after that day's first watch, 20 darts each
(`scratchpad/ratchet_control.py`):

| | median | mean R | win% |
|---|---:|---:|---:|
| real entry | −0.514% | −0.029 | 25% |
| eligible-within dart | −0.176% | −0.014 | 28% |
| paired (real − dart) | **−0.275%** | −0.015 | −1.04σ |

A coin flip on the same name beats the desk's chosen minute, gross and net.
Not significant at −1.04σ, so read it as "no edge," not "reliably negative."

**What it would take.** A trade must clear give + spread = **0.158 R**
(0.78% of price) to pay. Sorting the 346 trades by outcome and keeping only
the best X% with the same shelf:

| keep top | mean R | sessions green |
|---:|---:|---:|
| 100% (today) | −0.029 | 31% |
| 30% | +0.193 | 91% |
| 20% | +0.288 | 100% |

That is perfect foresight — a ceiling, not a forecast. It says the exit
machinery banks good entries fine. Working backwards, **a gate breaks even
if it is ~1.3–1.4× better than chance** at picking the trades that run.
That is not an outrageous target, which is why the field question below
matters more than it sounds.

---

## 4. What was measured and closed

Do not reopen these without new evidence. Each is a screen you can rerun.

**H4 swing (Grok's redesign) — FAIL.** 16 months of daily bars, the shipped
`simulate_hold`, 9 parameter cells, two universes: zero PASS.
`scratchpad/h4_prior.py`, `scratchpad/h4_rs_prior.py`.
- H4 as coded has **no entry signal** — it buys the open of every eligible
  name every eligible day. Universe filter is liquidity + RS.
- At the designed config (hold 2d, stop 2%) on the RS universe: gross vs
  SPY **+0.116%/swing**, haircut 0.20%/swing → net **−0.084%** at −2.25σ.
  A 2-day hold is ~126 round trips/yr, so 20 bps is a **25%/yr** hurdle.
- The 2% stop is **0.73σ** of the 2-day noise band; 31% of 2-day windows
  touch it. Adding it to a 5-day hold moves the median from +0.58% to
  **−2.20%**. Same error as a shelf inside the spread, one timescale up.
- `rs_ratings.json` is `as_of 2026-07-27` — 22 names already known to have
  beaten SPY. Even with that hindsight it fails, and **out-of-sample
  (after 7/27) gross vs SPY is −0.057%**, negative at zero cost.
- The 20 bps haircut is also internally inconsistent: the universe gate
  caps spread at 0.10% of mid, so the worst round trip it permits is 10 bps.
  Correcting it does not rescue the design.

**Late 14:00–15:30 hold — FAIL** on the session test once 8/21 is included
(n=143, 4/6 sessions, one day 71% of sample). **Chase 0/6. Research list at
60m FAIL** after 20 bps.

**Loss by source — no source is the leak.** 344 trades, 31.22 R gross loss:
momentum 61% of loss on 56% of trades, trending 28%/31%, research 8%/12%.
Per trade −0.041 / −0.048 / −0.049 R against a pooled −0.045 —
indistinguishable. Note 72% of "momentum"-labelled rows also carry a
trending marker, so the split is not a partition, and the dollar version
(momentum 73%) is a sizing artifact: 56% of the $1,128 is eight oversized
trades from 8/05–8/13.

**Go-live bar not met.** 2/10 sessions; needs 7/10, n≥30, median
MFE − spread > 0. Paper undercharges the exit ~0.018 R/trade, so grade on
live-equivalent.

---

## 5. Where we are going

Three questions have been run. A and B were answered on 8/22 and still
stand as measurements. **C, run on 8/23, falsified the plan that was built
on A.** Read them in order; do not act on A without reading C.

### A. Is admission late? — **Yes, and it does not matter.** `tools/admission_latency.py`

| source | n | med admit ET | at admit | run banked | travel left | **captured** |
|---|---:|---:|---:|---:|---:|---:|
| momentum | 358 | 10:37 | +8.5% | 8.5% | 3.0% | **0.71** |
| trending | 181 | 10:13 | +6.0% | 6.0% | 2.2% | **0.73** |
| research | 65 | 09:32 | +2.0% | 2.0% | 3.0% | **0.51** |

The delay splits into three legs with three different fixes, and only two
of them are problems:

| source | admit -> first arm | arm -> fill (RTH) | arm -> fill (pre-mkt arm) |
|---|---:|---:|---:|
| momentum | **31.7 min** | 0.2 min | 64.1 min (n=2) |
| trending | 18.6 min | 0.2 min | 41.4 min (n=4) |
| research | **101.1 min** | 0.2 min | 64.1 min (n=2) |

**Execution is not the problem** — 12 seconds from arm to shares. Detection
and decision are. Pre-market arms wait for the open because the book places
market orders and pre-market takes limits only; that is a constraint, not a
bug, and it is reported apart so it cannot be averaged into the fixable
column.

`captured` is the share of the day's up-move spent before the desk could
act, computed in price space. Three-quarters of the momentum move is gone
by admission. Research arrives earliest with the most left — the only
source admitted mid-move, and the one with the smallest loss share.

**All of that is true and none of it predicts money. See C.**

**`ai_watch_min_pct_change=50` was not what admits names.** The median
admission sits at +8.5% vs the prior close, and the threshold-crossing
latency is *negative*: names are typically admitted before ever clearing
+50%. There are two momentum seed paths, and that knob gates only
`_big_mover_from_dashboard`; the soft open seed (`mom_open_soft`,
`bypass_inclusion`) had **no percent gate at all**, and it is where most
admissions come from.

Fixed 8/22 by giving the soft path its own knob,
**`ai_watch_open_seed_min_pct`**, defaulted to **0.0 = admit exactly as
before**. This is a truthful name for existing behaviour, not a behaviour
change.

**It was described here as "the dial for attacking latency." That was
wrong twice over.** Raising a *cumulative percent* floor makes the desk
wait for a **bigger** move before admitting, which mechanically **raises**
`captured` — it is a volume dial, not a freshness dial, and it pushes the
opposite way from what the text claimed. And per C, freshness does not pay
anyway. Leave it at 0.0.

### B. Does any gate select drift? — **No.** `tools/gate_screen.py`

14 candidate gates × 3 horizons, anchored at the instants each gate
actually fired in `shadow.jsonl`. **42 cells, zero DRIFT.**

| gate | horiz | n | med MFE/MAE | sigma | med net | green |
|---|---:|---:|---:|---:|---:|---:|
| `fresh_15m` | 60m | 524 | 0.90 | **2.31** | −0.183 | 5/12 |
| `all` (baseline) | 60m | 740 | 0.84 | 2.27 | −0.174 | 5/12 |
| `fresh_5m` | 60m | 506 | 0.88 | 2.22 | −0.175 | 5/12 |
| `rvol_5` | 30m | 288 | 1.07 | 2.25 | −0.324 | 5/11 |
| **`arm_ok`** (incumbent) | 30m | 377 | 1.07 | **−0.07** | −0.070 | 4/11 |
| `rvol_10` | 60m | 63 | 1.11 | 1.44 | **−5.517** | 1/9 |

Nothing clears the gates. Note what beats what: several cells now pass 2σ
on **mean** MFE−MAE while their **median** MFE/MAE is below 1.0 and median
net is negative. That is a right-skewed field — a few large up-moves drag
the mean positive while the typical sample goes against you — and the
session sign says the skew does not repeat week to week.

Two things follow. `arm_ok`, the gate the desk is actually running, is at
the null (−0.07σ) and **loses to no gate at all**. And a right-skewed field
is precisely the shape a trailing stop should harvest — but only with a
cushion wide enough to survive to the tail. A 0.10R shelf exits on the
first wiggle, so it collects the negative median and never reaches the
tail. TEM 2026-08-20 is that failure in one name: 1.40 R available after
the fill, +0.040 R banked, exited in 53 seconds.

### C. Does lateness cost money? — **No.** (8/23)

A and B produced a plan — drive `captured` down — that rested on one link
nobody had tested: that arriving late is *why* the desk loses. It is not.
`scratchpad/captured_vs_r.py`, all 344 fills with realized R:

| instrument | n | rho | sigma | |
|---|---:|---:|---:|---|
| pre-admission run-up % | 341 | −0.139 | −2.59 | clean — shares no term with R |
| **`captured` at admission** | 328 | −0.057 | **−1.03** | **coupled** — shares the day high with R |
| `captured` at fill | 326 | −0.047 | −0.85 | coupled |

**`captured` is null**, and the way it is null matters: its denominator
contains the post-admission day high, which is also what makes a trade
profitable, so the arithmetic was working *in its favour* and it still did
not show. Session-paired it is **5/9, p = 1.000, median delta +0.000 R**.

Run-up looked alive at −2.6σ, so `scratchpad/runup_excursion.py` chased the
mechanism — and killed it:

| run-up bucket | n | medMFE | medMAE | MFE/MAE | medR |
|---|---:|---:|---:|---:|---:|
| −20.8..1.9% | 77 | 0.042 | 0.040 | 1.06 | −0.030 |
| 2.1..6.2% | 77 | 0.051 | 0.035 | 1.46 | −0.022 |
| 6.5..10.6% | 77 | 0.030 | 0.056 | **0.53** | **−0.074** |
| 10.6..91.0% | 79 | **0.056** | 0.040 | 1.39 | −0.026 |

**Run-up vs MFE: rho +0.035, +0.62σ — flat.** Extended names have *as much*
favorable excursion available as fresh ones; the most extended bucket has
the **highest** median MFE. "The move is gone by the time we arrive" is
false. The relationship is also **not monotonic** — the worst bucket is the
middle one — and session-paired it fails (7/9, p=0.18), with momentum
running backwards at 2/6. Only `trending` holds (5/5, p=0.062, n=96).

So the five "the desk buys what has already moved" measurements in the
8/22 queue — captured 0.71, extension >30% worst, RVOL >10 worst, high
score worst, contention favouring the least-extended — were **bucket-edge
artifacts of a non-monotonic relationship with no mechanism underneath.**
Latency is not the leak. Do not spend another session on it.

Caveat kept honestly: `mfe_r` in `outcomes.jsonl` is measured over the
actual hold (88–239s median by bucket), so it understates what the names
offered. That weakens the absolute numbers, not the comparison across
buckets, which is what carries the conclusion.

### D. Does ANY universe hold playable moves? — **No.** `tools/universe_screen.py`

C left the real question exposed: every screen in this lab is anchored on
`shadow.jsonl`, so we had only ever measured **our own watchlist**. The
universe screen builds candidates from *causes*, resolves each
point-in-time, and grades on two separate bars — does it drift, and is the
drift big enough to pay. The pay bar is pre-registered in the tool:
**median MFE ≥ 2× the 0.79% round trip, MFE/MAE ≥ 1.2, ≥70% sessions
green.** Symbol pool is shadow ∪ rejects = 562 names, 509 with bars.

| universe | 15m payX | 30m payX | 60m payX | M/A @30m | verdict |
|---|---:|---:|---:|---:|---|
| **burst** (mention rate) | **3.79** | **5.90** | **10.27** | 0.94 | range, no direction |
| desk (incumbent) | 0.62 | 1.00 | 1.46 | 1.03 | unplayable |
| **rejects** (gate said no) | 0.59 | 0.91 | 1.33 | 0.99 | **identical to desk** |
| gap_hold | 0.61 | 0.91 | 1.43 | 1.02 | unplayable |
| early_rvol | 1.27 | 1.80 | 2.95 | **0.80** | anti-predictive |
| liquid (control) | 0.15 | 0.23 | 0.35 | 1.00 | clean null |

**18 cells, zero DRIFT, zero PLAYABLE.** Four things to take from it:

- **The desk's own watchlist is unplayable at its own horizon.** payX 0.62
  at 15m means the median name offers **62% of what the round trip costs**.
  No gate fixes that — there is nothing to gate.
- **`rejects` ≈ `desk` on 804 name-days vs 481.** The names the gate turned
  down are statistically indistinguishable from the ones it kept. This is
  `arm_ok`'s −0.07σ confirmed on a bigger, independent sample: **the gate
  is not selecting.**
- **`burst` is the only tape with real size** — 3.0% median MFE at 15m,
  6× the desk's, clearing the pay bar 3.8×. And it is **directionless**
  (M/A 0.91–0.94). A huge symmetric coin flip is the single worst shape for
  a ratchet, and the `flag` path feeding on it is 28% of admissions.
- **`liquid` behaves exactly as the null predicts** (M/A 1.00, sigma −0.23),
  which is the evidence that the measurement itself is not broken.

**Holding longer does not rescue it.** desk payX runs 0.62 → 1.00 → 1.46
across 15/30/60m, but √4 = 2.0, so the growth is the noise band widening
almost exactly as a driftless walk predicts — MFE/MAE only moves 0.97 →
1.07. A longer hold buys a bigger wiggle, not a better one.

### E. Is the cost lever real? — **Mostly no.** (8/23, same day)

D charged every universe a flat 0.79%, which quietly assumes every
watchlist costs what ours does. The obvious follow-up was that our cost
problem is *the names we pick*: one tick is 0.50% of a $2 stock and 0.02%
of a $50 one, a hundredfold structural difference. `universe_screen
--cost-model measured` charges each name-day `give + its own spread`,
estimated by Roll (1984) from bid-ask bounce and floored at one tick.

**The hypothesis was wrong, and it was wrong for a reason worth keeping:**

| universe | medPx | cost% | medMFE@30m | M/A@30m | payX@30m |
|---|---:|---:|---:|---:|---:|
| desk_px:0-10 | 4.83 | 0.693 | 1.059 | 0.97 | **1.53** |
| desk_px:10-50 | 17.27 | 0.574 | 0.649 | 1.03 | 1.13 |
| desk_px:50- | 71.02 | 0.549 | 0.608 | **1.31** | 1.11 |

Cheap names have the **best** payX, not the worst. Their spread is wider
but their moves are wider by more — cost differs 1.26× across the bands
while MFE differs 1.74×.

**Why: the desk's biggest trading cost is its own stop, not the market.**
The give is 0.10R and 1R is 5% of price, so the give is **0.50% of price on
every name by construction** and does not vary with price at all. Against
it the spread contributes 0.09–0.31%. Sorting the watchlist by price moves
the small term and leaves the big one untouched. Any real attack on cost
has to go at the give, or at the number of round trips, not at the ticker.

**The estimate is biased low, which strengthens the negatives.** Validated
against the 61 name-days with a trustworthy quote (<1.0 R), Roll reads
0.091% against a quoted 0.310% — a ratio of **0.32** — and 51% of
name-days fell back to the tick *floor* rather than a measurement. So
every cost above is a lower bound and every payX is an **upper** bound.
Corrected, `desk` lands back near the flat 0.79% it started from.
**Everything that failed here fails harder in reality.**

**One thing did move, and it is the opposite of what we trade.**
`desk_px:50-` is the only universe in 39 cells with MFE/MAE climbing
across horizons — 1.15 → 1.31 → 1.49 — at sigma 2.09/2.41/2.29 and
**8/10, 8/10, 7/9 sessions green**. It misses the DRIFT verdict on session
sign by p = **0.0547 against a 0.05 gate**, and misses PLAYABLE on payX
1.11 against a 2.0 bar. Read it as a candidate and nothing more: n=35
name-days, searched across nine universes, and 0.0547 is a miss rather
than a near-pass — this file has been burned five times by exactly this
shape. What makes it worth writing down at all is that it is the first
result pointing somewhere new rather than back at the null, and it points
at names the desk's watchlist (median price $9.79) almost never holds.

### F. The operator's setup — the first conjunction ever tested (8/23)

Everything above tests **marginal** gates: one condition at a time against
all 493 name-days. The operator's actual thesis is a **conjunction of
five**, and it fires on **25 of 493 name-days (5.1%)** before the float leg
and 7–15 after it. A 25-sample effect inside a 493-sample average does not
move the average. **"No gate selects drift" was only ever a statement about
single filters.** It was never a test of this.

The rule lives in `setup_rules.py` so live and lab import the same
thresholds. Stage 1: up ≥10%, RVOL ≥5, catalyst <24h, price $2–20, shares
outstanding <10M. Stage 2 (timing, untested): both %R lines rising together
toward overbought is the move, one turning down ends it, RSI entered at the
bottom of its oscillation.

| universe | horiz | names | n | sess | medMFE | M/A | payX | clear | green |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **setup ≤10M** | 15m | 7 | 42 | 4 | **5.77** | **2.02** | **4.65** | **83%** | 1/4 |
| **setup ≤30M** | 15m | 15 | 98 | 7 | **3.77** | 1.10 | **3.04** | **80%** | 3/7 |
| **setup ≤30M** | 30m | 15 | 45 | 6 | **7.25** | 1.17 | **5.85** | **82%** | 3/6 |
| desk | 15m | 494 | 4909 | 12 | 0.49 | 0.97 | 0.78 | 42% | 4/12 |

**On magnitude this is unlike anything else measured.** payX 3–6 against
the desk's 0.78; **80% of samples clear their own round trip** against the
desk's 42%. The unplayability that defeated all six earlier universes is
simply absent here.

**On direction it is still unproven.** M/A 1.10–1.17 sits under the 1.2
gate, sigma 1.26–1.65 under 2.0, sessions 3/7 and 3/6 under 70%. So stage 1
finds names that **move**; it does not yet show they move **up** reliably.

That is the correct division of labour and it is why stage 2 matters:
**the setup picks the name, the timing rule is supposed to supply the
direction — and stage 2 has never been tested at all**, because `pctr_slow`
only reached 31% of historical rows. It is logged live from 2026-08-24.

**Read all of this as exploratory, for four reasons.** n is 7–15 name-days.
Shares *outstanding* is a proxy for float (always ≥ float; Finnhub
publishes no float field, and `/stock/metric` has none either), so the cap
is an approximation and the 30M variant was chosen **after** seeing that
only ≥30M yields testable n — which is a real methodological compromise,
not a neutral choice. The screen applies today's share count to past
sessions, so a name that issued stock mid-window is scored post-issuance.
And the ≤10M cell looks *better* than ≤30M (M/A 2.02 vs 1.10) on 7
name-days, which could mean a tighter float genuinely carries more
direction or could be noise; n cannot separate those.

**The honest next step is more tape, not a tighter threshold.**

### G. Is the premarket book too wide to trade? — **No. The book is fine; the feed is not.** (8/26)

Asked because the operator wants the day to start at 04:00 — the setup in F
is a gap-up-on-news setup, and on those names the move is often over by
09:30. `tools/spread_coverage.py` appeared to answer it decisively:

```
RTH spread_r        median 0.065R
PRE-MARKET spread_r median 1.717R      <- 26x, and a round trip pays it twice
```

That reads as ~3.4R to scratch on a distribution that lives inside ±1R —
premarket entry as arithmetic you cannot win. **It is an artifact.** Every
`spread_r` on the record is built from `DataFeed.IEX` (`ai_trading.py:226`).
IEX is one venue with a couple of percent of consolidated volume and very
little of it before 09:30, so a wide premarket "spread" is substantially
measuring IEX's *absence*. Orders route against the NBBO, and the NBBO is
what prices an entry.

`scratchpad/sip_premarket_spread.py` re-prices the **same shadow rows**
against SIP historical quotes — same stops, matched strictly point-in-time
(last quote *before* the instant, never the one that closed it), and
self-validating: it reproduces each logged IEX value from the row's own
bid/ask/stop first, and aborts rather than compare an unpaired sample.
6452 paired premarket rows, 2026-08-20..26:

| | p10 | median | p90 | RTH-like (≤0.13R) |
|---|---:|---:|---:|---:|
| IEX — what the desk logs | 0.026 | **0.749** | 7.325 | 14% |
| SIP — what you actually cross | 0.029 | **0.229** | 3.050 | 37% |
| SIP, quote <60s old | 0.024 | **0.120** | 0.465 | 52% |

RTH, same method (10:00–11:00, 8 busiest symbol-days, n=1162): IEX 0.173R
vs SIP **0.117R**.

**The premarket book, measured properly, costs what the regular-session
book costs — 0.120R against 0.117R.** The 26x penalty was never in the
market.

Three prior claims die with it:

- *"09:00–09:30 is the worst book of the day."* Pure IEX artifact: IEX says
  2.280R there, SIP says 0.201R — among the *better* premarket half-hours.
  The SIP median is flat at 0.17–0.27R across all of 04:00–09:30. The book
  does not degrade toward the open.
- *"Only 2 of 21 names are crossable premarket."* On SIP it is 6 of 18.
- Per-name error is not a scale factor and cannot be calibrated away. SDOT:
  IEX 5.278R, SIP **0.026R** — a 200x error, on a name the desk would have
  refused as untradeable while its book was tighter than the RTH median.

**What the premarket book actually does wrong is go dark, not go wide.**
Matched-quote age is median 20.4s / p90 856s premarket, against 2.3s / 24s
in RTH; 37% of premarket rows matched a quote over a minute old, versus 2%
in RTH. Every name with a bad SIP median is a name with a stale quote (OI
378s, CADL 410s, AUPH 582s, IND 860s). That is a real untradeability
signal, but a *different* one, and it lands on the exact weakness in §8:
`ai_positions.py:4026` gates the blind-book flatten on `_rth_now()`, and
Alpaca will not hold a stop outside RTH. Premarket, a dark book means no
protection at all.

**Consequences.**

1. **`ai_max_spread_r` must not be enforced on IEX quotes.** It is 1.5x
   wrong in RTH and 3.3x wrong premarket, per-name up to 200x. Set from
   IEX it would refuse SDOT at 5.278R while its real book was 0.026R. The
   gate has been parked at `0.0` waiting for a record — this is why it must
   keep waiting for a *better feed*, not a longer sample.
2. **The blocker on trading at 04:00 is the live quote, not the economics.**
   SIP is delayed-only on this account (§10), so it can measure and cannot
   trade. Finnhub already holds the websocket in `signal_engine.py` and
   `stream_bars.py` landed 8/25 — the premarket feed problem and the
   EXH/RSI realtime problem are the same problem with the same fix.
3. **Validate Finnhub premarket quotes against SIP with this same script
   before building anything on top of them.** Do not swap one unvalidated
   feed for another.

#### G2. Finnhub is not the answer, and the premarket price is frozen (8/26)

Checked live at 06:00 ET (`scratchpad/finnhub_quote_probe.py`):

| endpoint | result |
|---|---|
| `/stock/bidask` (NBBO) | **HTTP 403** — not entitled on this plan |
| `/quote` | fields `c,d,dp,h,l,o,pc,t` — last trade only, **no book** |
| `/quote` timestamp, premarket | `t = 2026-08-25 16:00 ET` — **yesterday's close** |

So Finnhub cannot price a spread at any hour, and cannot price *anything*
premarket. Only its **websocket** carries live premarket trades, and that
socket lives in `signal_engine.py`, not in the `ai_trader` process where
`ai_entry_watch` runs.

**Which makes the 8/25 stream experiment unable to answer its question.**
`_stream_pctr_fields` (`ai_entry_watch.py:1422`) feeds the aggregator with
`stream_bars.observe(symbol, px, now)` — `px` is the shadow row's own
price, from the same IEX path. Its docstring says so plainly: "without a
trade stream in this process". `pctr_stream` is therefore **not** a denser
feed to compare against; it is the same feed resampled, and it answers
"would a denser feed fix the window" with a restatement of the old one.

What that produces premarket is degenerate. Of 770 paired rows on 8/26,
`pctr_stream` is pinned to an extreme (0 or −100) on **95%** — 590 at 0 and
144 at −100 — against 25% for the IEX-bar `pctr`. On most rows the two read
*exact opposites*: `pctr_stream` = 0 (maximally overbought) while `pctr` =
−100. `cm_rsi_stream` hits both rails (0.00 and 100.00) where `cm_rsi` runs
11.1–99.9. A windowed indicator over a window with no variation collapses
to its extremes; that is arithmetic, not signal.

**The cause is upstream of all of it. The premarket price never moves.**
RARE, 365 distinct samples, 04:00–06:05 ET, median gap 20.5s
(`scratchpad/premarket_frozen_check.py`):

```
  FROZE : price, ask, spread_r, pctr_stream, stop_price, pct_change, ...
  MOVED : bid, pctr, pctr_slow, cm_rsi, ...

  live IEX quote   bid 22.72   ask 0.00      age 50710s
  live IEX trade   26.45                     age 50712s
  shadow log last  26.45                     age     6s
```

**The IEX ask is literally `0.00`, the last IEX trade is 14 hours old — it
*is* yesterday's 16:00 close — and the desk logged that number six seconds
ago as the current price.** Zero price changes in ~363 samples, on all
eight names watched. The same symbol on the same field changes on 61% of
RTH samples with a real 26.12–27.08 range, so the machinery is fine; it is
specifically premarket that is dead.

`logs/engine.log` states it outright — it prints the price age, and **that
age equals time-since-subscription for every name**:

```
[RARE ] $26.45  age=7595s  #7528      [VVOS ] $0.26  age=51s   #52
[TEM  ] $68.74  age=7595s  #7528      [LGHL ] $0.54  age=369s  #367
```

Polls run ~1/s, so age ≈ poll count on all 13 subscribed names: each symbol
receives **one** price when it is subscribed and never another. The engine
subscribes "(Alpaca IEX, Finnhub)" and neither pushes a premarket update.

The market itself is not quiet — the SIP quotes in the table above have a
median age of **20.4s** across the same window. Premarket trading is
happening on these names; IEX and Finnhub simply cannot see it.

> **⚠ Corrected same day — this section overstated the blindness.** The
> sentence that stood here said *every* premarket number is computed on
> yesterday's close. That is true of the **price path** and false of the
> **indicators**, and the difference decides the whole feature. See G3.

#### G3. The desk is half-blind premarket: live tape, dead book (8/26)

`_engine_indicator_map()` read live at 07:10 ET, against `load_watch()`:

```
sym     bars_src    bars_age    pctr  cm_rsi   last_ask_src
AUPH    realtime         0.1   -18.7    56.0   stale_tape
CADL    realtime         0.6   -33.5    66.6   rest
IMMX    realtime         0.3    -0.1    99.9   stale_tape
IOVA    realtime         0.9   -10.3    95.8   stale_tape
PURR    realtime         0.7   -48.5    42.3   rest
RARE    realtime         0.7   -15.2    66.9   rest
VVOS    realtime         0.2   -96.7     0.6   None
BRNX    alpaca          None   -14.1    92.9   stale_tape
TEM     alpaca          None   -23.9    64.1   rest
```

**Seven of nine names are on realtime bars premarket with sub-second trade
age.** `rt_bars.on_trade` is wired to the Finnhub trade callback
(`signal_engine.py:1370`), and `age_seconds()` measures *the trade's own
timestamp*, written precisely to catch "connected but serving stale prints"
— so 0.1–0.9s is live premarket tape, not a poll artifact.

So the two halves diverge, and the frozen-field list in G2 says which is
which:

| | premarket | source |
|---|---|---|
| `pctr`, `cm_rsi` — **the arm levers** | **live, <1s** | Finnhub trades → `rt_bars` |
| `price`, `ask`, `spread_r`, `pct_change` | **frozen at yesterday's close** | IEX REST |
| bid/ask book | **absent** (`ask` reads `0.00`) | needs SIP |

The irony worth keeping: `pctr_stream`, built 8/25 to test "would a denser
feed fix the window", is degenerate *because it feeds on the frozen price*
— while production `pctr` was already running on the denser feed the
experiment was proposing. The experiment is both redundant and broken.

**What this does and does not change.** It does not revive premarket
trading: entry pricing needs an ask, a synthetic stop needs a live price to
trigger on, and neither exists without SIP — Finnhub carries trades, never
a book. What it changes is the *reason*, and therefore the cost of the fix:
the missing piece is a **quote**, not a feed. A live premarket last-trade
price is already in the process (`finnhub_stream.get_latest_price`) and
simply is not wired into the price path.

`_require_protective_exit` refusing premarket entries remains correct, and
should not be relaxed until a premarket **book** exists.

### The design criterion this produces

For a driftless walk, expected favorable excursion is ≈ **0.8σ√t** — which
is exactly what the tape measures at every horizon. So on a driftless
field, **MFE is always smaller than the noise band**, and the ratchet's
requirement `noise(t) < give < MFE(t)` has no solution at any horizon. That
is a fact about driftless tape, not about this watchlist.

Drift breaks it, because drift accumulates **linearly** (μt) while noise
grows as **√t**. The crossover is `t > (σ/μ)²`. With the measured
σ = 0.245%/√min, an intraday ratchet product needs roughly **μ ≥ 0.05%/min
— about 3%/hour, sustained.**

The names do exactly that — during the run-up, before admission. On 8/22
that produced the product question "can the desk be admitted during the run
instead of after it?", read as a latency problem.

**8/23 answers it: being admitted earlier would not have helped.** C shows
arriving fresher does not pay, and D shows the excursion available is flat
in run-up. The derivation above survives — it is arithmetic, and D confirms
it empirically with MFE/MAE ≈ 1.0 on every universe tried — but its
consequence is harsher than the latency reading:

> **No universe we can currently construct sustains μ ≥ 0.05%/min, so no
> intraday ratchet product has a solution on any of them.**

That is a statement about the tape available to this desk, not about
admission speed. The honest options it leaves are a different instrument
(bigger μ), a different holding period (where μt beats σ√t), or a cost
structure where the 0.79% round trip stops dominating — not another gate.

### The queue, and why it is ordered this way

**One live experiment at a time.** From 2026-08-24 the desk runs the
min-hold exit test and **nothing else live changes**. Entry and exit moved
together once already — 8/19 changed twelve knobs at once and that day
cannot be compared to anything. If entry and exit both move now, an
improvement has two authors and a regression has two suspects.

**Lab work runs in parallel.** Building a catalyst feed and grading it with
`gate_screen` happens entirely on shadow rows and never touches the book,
so it cannot contaminate the running test. Only *arming* waits.

**GATE 1 — the exit test (live now, ~10 sessions).**
`ai_exit_min_hold_sec=900`, everything else frozen. Read `eod.py` nightly.

| result after 10 sessions | conclusion |
|---|---|
| ≥7/10 live-positive **and** median CAPTURE > 0 | exit fixed. Unfreeze entry work. |
| 5–6/10 | underpowered — extend to 15, do not tune |
| ≤4/10 | the exit was not the problem. Back to the field. |

CAPTURE is the diagnostic, not just the P&L: if sessions improve while
capture stays negative, something other than the delay is doing the work
and the result will not survive.

**GATE 2 — entry, only after gate 1 passes.** In order — **but read the
8/23 annotations first: items 4, 5 and 6 are dead, and 1–3 are now the
whole list.** §5D reframes what is left: `rejects` matching `desk` means
the gate is not selecting, and the desk's own watchlist is unplayable at
its own horizon (payX 0.62 at 15m). Item 1 remains worth doing because it
removes trades the operator would never have taken by hand; items 2 and 3
remain *lab* questions. None of the three is an edge, and nothing in this
queue is now expected to make the desk profitable.

**1. Enforce the volume floor the operator already believes in.** This is
a defect, not a hypothesis, and it is first because it needs no discovery.
The operator does not trade a name without volume; `ai_watch_min_rvol=2.0`
encodes that. It is enforced on **one path of three**. Of 344 fills, 94
were below the floor:

| source | fills under RVOL 2.0 | why |
|---|---:|---|
| momentum | 1 | enforced at `ai_entry_watch.py:2313` — working |
| trending | 55 | separate lower floor, `ai_watch_trending_min_rvol=1.5` |
| research (xai+anthropic) | **38 of 43 fills** | **no RVOL check at all** |

Research is also the worst source in the book — `anthropic` at −0.055 R
with **0/7 sessions green**, the only source that never had a green day.
Two related defects fall out of the same check: the test is
`if rv is not None and rv < floor: skip`, so a **missing** reading passes
(19 fills had no RVOL at all — it must fail closed), and the field carries
garbage (max value **3144.09**; a relative volume of 3,144x is not a
number, and anything averaging RVOL has been eating it).

This is not a new edge. It is removing trades the operator would never
have taken by hand.

**2. RVOL as the shelf-width input.** RVOL is anti-predictive for
*returns* — monotonically worse: rvol<2 −0.027 R, 2–5 −0.020, 5–10 −0.108,
>10 −0.128, and `gate_screen`'s `rvol_10` had median net −3.12%. But it is
the strongest predictor of **range** measured anywhere in this work:

| bucket | median 30m range |
|---|---:|
| rvol < 3 | 2.15% |
| rvol 3–8 | 8.09% |
| rvol ≥ 8 | **28.90%** |

A 13x spread. That matters because a wide shelf on names with ≥1R of range
available returned **+0.53 R** where the tight shelf returned +0.22 R — the
problem was never the rule, it was identifying those names at entry. RVOL
identifies them, costs nothing, and is already wired.

**3. Catalyst — second order, and probably an interaction.**
`tools/catalyst_screen.py`, Alpaca news, no new credentials. It does **not**
predict direction: no gate cleared 2σ and `bullish_words` came back
NO_DRIFT with a negative mean. It predicts range —
no-catalyst 2.10 → has-catalyst 3.75 → news<60m 4.39 → news<15m 5.99, and
fresh news beat the no-news median range in **11 of 12 sessions (p≈0.003)**,
the most solid statistic in this file. But conditioned on RVOL the lift
mostly vanishes (1.06x / 0.50x / 1.14x), so **RVOL is the better range
input** and catalyst's value is likely the interaction:

| median 30m direction | no fresh news | news < 60m |
|---|---:|---:|
| rvol < 3 | +0.112 | −0.109 |
| rvol 3–8 | −1.016 | +0.167 |
| **rvol ≥ 8** | **−0.531** | **+6.075** |

Volume without a reason is a crowd; volume with a reason is a repricing.
**n=10 in that cell** — a hypothesis with a mechanism, not a finding. Watch
it as sessions accumulate.

**4. Detection latency — ~~queued~~ DEAD (8/23).** Was:
`ai_watch_open_seed_min_pct` as the dial, `captured` as the scoreboard.
§5C shows `captured` does not predict R (−1.03σ, 5/9 sessions, p=1.000)
and §5D shows available excursion is flat in run-up. There is nothing here
to win. Do not rebuild it under a new name.

**5. Decision latency — ~~queued~~ DEAD (8/23).** The 31.7-minute
admit-to-arm gap is real and costs nothing measurable, for the same
reason. Arming faster into a driftless name just reaches the same
distribution sooner.

**6. A slot ranker — ranked on freshness, never on strength.** Two slots
decide which candidates exist at all, and today the winner is whoever
arrived first. `tools/slot_contention.py` over 8/10–8/21: on 3,799 contested
moments the names the desk **turned away** beat the ones it kept by a median
**+0.08% over 15m, winning 53%**. Allocation is currently worse than
arbitrary, so there is something to capture.

The obvious ranking key would capture it backwards. Every "strength" proxy
is anti-predictive here — extension >30% is the worst bucket (−0.174 R),
RVOL >10 the worst (−0.128 R), and trending score runs ρ=−0.209 against
realized R. A ranker promoting the strongest name would concentrate two
scarce slots in the trades that lose most.

`tools/slot_ranker_signal.py` tested twelve rules against the base rate of
taking the skipped name every time (+0.076%, 53%). One separates:

| rule | swaps | median | win | vs base |
|---|---:|---:|---:|---:|
| **prefer LOWER cm_rsi** | 696 | +0.120% | **57%** | **+4.8pp** |
| prefer rising cm_rsi | 1330 | +0.125% | 54% | +1.3pp |
| prefer lower pctr_ok | 349 | −0.288% | 40% | −12.6pp |

Lower CM RSI is the *less* overbought, *less* extended name — earlier in
the move. At n=696 one standard error on the win rate is ~1.9pp, so +4.8pp
is roughly 2.5σ, searched across twelve rules. A candidate, not a result.

**The "freshness" reading of this is dead (8/23).** It was written as the
fifth of five measurements pointing at "the desk buys things that have
already moved" — and §5C shows all five were bucket-edge artifacts of a
non-monotonic relationship with no mechanism. `rejects` matching `desk` in
§5D says the same thing from the other side: allocation cannot be the leak
when the pool is homogeneous.

What survives is only the bare empirical rule — *prefer lower `cm_rsi`* —
at 2.5σ **searched across twelve rules**, with its explanation removed. A
searched candidate with no mechanism is the weakest thing in this file.
Do not build on it.

**Re-measure the ceiling before building it.** All of the above is from the
86-second regime, where a slot freed every minute or two. Under the
min-hold a slot is parked for 15, contention rises sharply, and the prize
grows with it. Re-run `slot_contention.py --days 10` once gate 1 has tape.

**Do not** arm anything from gate 2 while gate 1 is running, and do not
retune the min-hold delay mid-test because a week looks bad. Ten sessions,
then read it.

### What is actually left after 8/23

The entry queue is gone and it is worth being blunt about what that means:
**there is no longer a queued change that anyone expects to make this desk
profitable.** GATE 1 is the last live candidate, and it is an *exit* test
sitting at p=0.13 on a backtest of its own data.

§5D says the constraint is the tape, not the selection. That leaves three
honest directions, in the order their evidence supports:

**i. Finish gate 1 and read it.** Ten sessions, unchanged. It is the only
experiment running and the only new information arriving. Everything below
waits on it.

**ii. Attack the cost — but the give, not the ticker.** The round trip is
0.79% of price and the desk's own tape offers 0.49% median MFE at 15m.
Those two numbers are the whole problem, and only one of them is under our
control. §5E tested the obvious version of this and it failed: sorting the
watchlist by price barely moves total cost, because **0.50 of the 0.79 is
the ratchet's own give**, fixed at 0.10R on every name by construction.
What is genuinely untested is the give itself, the number of round trips,
and limit-order entry. §2 prices only the *widening* direction (more give
removes stomps and does not move the mean); the tightening direction, and
what it costs in stomps, has never been swept against a per-name spread.

Note the tension before acting on it: a smaller give lowers cost and
raises stomp rate, and §2 says stomps were never where the money went. So
this is arithmetic worth checking, not an obvious win.

**iii. Change the holding period, not the gate.** μt beats σ√t only for
`t > (σ/μ)²`. Every screen in this file lives inside one session because
the product does. That constraint has never been tested as a *variable* —
H4 tested a specific bad multi-day design, not the timescale itself, and
its failure was over-read as closing the whole direction.

**iv. The one candidate on the board.** `desk_px:50-` (§5E) — the desk's
own seeds filtered to names over $50 — is the only cell in 39 with
MFE/MAE rising across horizons and 8/10 sessions green. It missed DRIFT
on p=0.0547 and PLAYABLE on payX 1.11. It is thin (n=35 name-days) and
searched, so the correct next move is **more tape, not an arm**: it costs
nothing to re-run `universe_screen` weekly and see whether it survives.
If it does, it says the desk should be trading the names it currently
filters out on price.

**What is NOT left:** another indicator permutation, another latency fix,
another ranker. `gate_screen` covered the first (42 cells), §5C the second,
§5D the third. If a proposal is one of those three wearing a new name, it
has already been measured.

**And the outcome the operator pre-committed to.** The standing rule is
"stop if the data on the optimized system says there is no chance of a
profitable system." That threshold has not been crossed — gate 1 is still
running and (ii) and (iii) are genuinely untested — but it is closer than
it was on 8/22, and nobody should be told otherwise.

---

## 6. What not to do

- **Do not permute** RSI / %R / RVOL / heat / zone on the squeeze list as a
  timing overlay. That family is measured out — `gate_screen` covers it and
  `in_zone`/`pctr_ok`/`cm_ok` all sit at or below the null.
- **Do not enable** `ai_h4_paper`, `ai_h3_paper`, or `ai_late_hold_paper`.
- **Do not widen the give** expecting P&L. §2 prices that experiment: it
  removes stomps and does not move the mean. `give_spread_k` and
  `be_at_spread_k` are worth turning on for the wide-spread quarter of
  trades, but as risk hygiene, not as a profit change.
- **Do not build premarket trading until the desk has real-time SIP.**
  **Shelved 8/26 by the operator, on evidence, not preference.** The
  economics are fine (premarket crossing costs 0.120R against 0.117R in
  RTH) and the order plumbing already exists on both sides
  (`alpaca_trader.py:405` buy, `:955` sell) — so this *looks* buildable and
  is not. The desk cannot see the premarket market at all: price age equals
  time-since-subscription on every name, the IEX ask reads `0.00`, and the
  last IEX trade is yesterday's close (§5G2). A synthetic stop cannot fire
  on a frozen price, which is why `_require_protective_exit` refusing these
  entries is **correct** and must not be relaxed to unblock the feature.
  The single unshelving condition is a live premarket quote — SIP; Finnhub
  is out on entitlement (403) and IEX is structurally absent before 09:30.
  Until then, do not touch the entry gate, the spread gate, or the stream
  indicators *for premarket reasons*. Treat premarket `price`, `ask`,
  `spread_r` and `pct_change` as unevidenced — they are yesterday's close.
  Premarket `pctr` and `cm_rsi` are **not**: they run on live Finnhub tape
  at sub-second age (§5G3). The missing piece is a quote, not a feed.
- **Do not price anything off an IEX spread.** §5G: IEX overstates the book
  1.5x in RTH, 3.3x premarket, and up to 200x on a single name (SDOT). That
  includes setting `ai_max_spread_r`, ranking names by cost, and any
  premarket entry or exit rule. Measure with SIP historical; trade on a
  feed validated against it.
- **Do not change live knobs mid-session** without the operator.
- **Do not read a replay verdict without checking `desk_product` first.**
  `tools/sim_rstop_path.path_cfg()` calls `load_config()`, so the simulator
  inherits the **live** product knob. While the desk sat in `observe` the
  arm veto fired *inside the sim* and `walk_symbol` placed zero trades —
  four of its seven tests failed for exactly that reason and went green the
  moment the book went back to `scalp_legacy`, with no code change. Every
  "do not change config" verdict produced during observe is empty rather
  than informative. This is the likely explanation for the earlier note
  about vacuous `optimize_rstop` runs.
- **Do not run `drift_screen` without `--eligible-within`.** Without it the
  same universe reads DRIFT everywhere (MFE/MAE 1.13–2.83) purely from the
  pre-admission run-up. That gap is the finding, not a signal.
- **Do not reopen admission latency** in any form — earlier seeds, faster
  arming, a freshness ranker, a `captured` target. §5C tested the premise
  directly and it is null. It is a seductive story because the latency is
  genuinely large; the size of the delay is not evidence that closing it
  pays.
- **Do not grade a universe on drift alone.** `burst` has 6× the desk's
  range and would look magnificent on any MFE-only screen; its MFE/MAE is
  0.91. Range without direction is the worst possible tape for a ratchet,
  and it is what the desk is already 28% invested in via the `flag` path.
- **Do not read a −2σ trade-level correlation as a finding.** Run-up vs R
  was −2.59σ pooled and collapsed to 7/9 sessions (p=0.18) with the source
  that dominates it running backwards. Trades inside a session are not
  independent; the session test exists because pooling lies.

---

## 7. The lab

Mini only — the MacBook cannot fetch bars. Use the venv; system `python3`
has no Alpaca client.

```bash
.venv/bin/python tools/universe_screen.py --days 20 --horizons 15,30,60
.venv/bin/python tools/drift_screen.py --eligible-within --days 20
.venv/bin/python tools/gate_screen.py --days 20 --horizons 15,30,60
.venv/bin/python tools/admission_latency.py --days 20
.venv/bin/python tools/harvest_screen.py --days 10
.venv/bin/python tools/eod.py --days 10
```

`universe_screen` is the one to run first on any new idea. It asks whether
a *watchlist rule* produces tradeable tape, before anyone spends a week
building a gate to pick names off it. Its pay bar is pre-registered in the
source (median MFE ≥ 2× the 0.79% round trip, MFE/MAE ≥ 1.2, ≥70% sessions)
— **do not edit those constants to make a universe pass.**

**Verdict gates, unchanged and not to be weakened:** n ≥ 30, ≥ 5 sessions,
no session > 50% of the sample, paired ≥ 2σ, session sign p ≤ 0.05. The
session is the unit of independence, never the sample or the admission. A
green Thursday is not a pass. EMPTY RUN is not a verdict.

`drift_screen` and `gate_screen` charge **no cost** on purpose — they
measure the field and the gate. DRIFT is permission to look for a trade,
not a trade. Cost (0.287% of price round trip, measured) decides afterwards
whether a found edge survives.

---

## 8. Weaknesses of everything above

- **One tape.** 8/04–8/21, twelve sessions, 20 config fingerprints pooled.
  Everything here could be an August artifact.
- **Spread n=28.** The 0.287% round trip is the median of 28 quoted rows
  from 8/21 alone, applied uniformly. Wide-spread names are undercharged.
- **IEX minute bars** cannot resolve sub-minute prints, so simulated stomp
  rates are a floor (13% simulated vs 15% observed — close, but a floor).
- **The tape was not hostile.** The dart itself was gross-positive
  (+0.216% mean), so the names drifted up and the desk still lost on them.
- **`captured` needs a prior close**, so it is undefined for names admitted
  below it; those are reported as None rather than 0, which flatters
  nothing but does shrink n.
- **`arm_ok` in shadow is the signal, not the fill.** It reads mildly
  better (MFE/MAE 1.19) than the executed entries did (0.66–0.85). That gap
  is the execution path — slots, zone, entry pricing — and has not been
  isolated.
- **The oracle ceiling is tautological.** Sorting by outcome always looks
  good; it bounds the opportunity, it does not suggest it is reachable.
- **The universe screen still cannot see outside our own scanner.** Its
  pool is shadow ∪ rejects — 562 names, up from 314, but every one of them
  was surfaced by the desk's own momentum/trending feeds. "No universe is
  playable" therefore means *no universe we can currently construct*. A
  genuinely external symbol source (a full-market gap/volume scan) has
  never been tried and would be the honest way to close the question.
- **Its thin cells are thin.** `early_rvol` is 45 name-days and `burst`
  covers 9 sessions. Read those two rows as direction, not as verdicts.
- **The pay bar assumes 1R = 5% of price uniformly**, which is the desk's
  sizing rule rather than a property of each name. A universe of tighter
  names would have a different R and a different bar; payX is the number
  to compare across universes, not medMFE.
- **The Roll spread estimate reads ~3× low** against the 61 trustworthy
  quoted name-days, and 51% of name-days fall back to the tick floor,
  which is a bound rather than a measurement. Costs are therefore lower
  bounds and payX values are upper bounds. This makes the negative results
  safe and would make any future *positive* one suspect — a universe that
  passes under this model must be re-tested against real quotes before
  anyone believes it.
- **`gap_hold` and `liquid` sample from a fixed clock**, so time-of-day is
  confounded with the rule in those two rows. The log-derived universes
  each carry their own eligibility instant and do not have this problem.

---

## 9. Glossary

- **Ratchet / working shelf:** raise-only software stop ~0.10R under the
  print (`ai_local_trail_*`). Broker stops are off; this *is* the
  protection. Protects, does not capture (§2).
- **Stomp:** hold < 10s. ~13% of trades at the live give. Not the leak.
- **MFE / MAE:** max favorable / adverse excursion after entry. Their ratio
  is the drift test; a driftless walk gives 1.00.
- **captured:** share of a day's up-move already spent at admission.
- **Eligible-within:** sampling the same name only *after* its first watch
  that day. The non-hindsight control. The old WITHIN was run-up loaded.
- **Session:** the unit of independence. One afternoon is one observation.
- **1R:** 5% of price here, so 0.10R ≈ 0.497% and the pay bar is 0.158 R.

---

## 9B. Performance audit — where the latency and the coverage actually are (8/26)

Asked: is the desk optimised and performing — realtime coverage, decision
speed, ratchet responsiveness, admission efficiency, and whether selection
is weighted on the stated principles. Measured rather than reasoned;
scripts in `scratchpad/{cadence_cost,admission_audit,rth_freshness_audit}.py`.

### The cadences

| loop | cadence | what it decides |
|---|---:|---|
| shelf / ratchet | **0.25s** | raise the stop, flatten through it |
| book tick | 1.0s | fills, T1, dead-trade, EOD |
| positions poll | 3.0s | broker reconciliation |
| **arm gate** | **20.0s** | open a position |

`ai_shelf_tick_sec` is **not in `bot_config.json`** and runs on the 0.25
default; `ai_trader.py:1246` reads it with a fallback of `0.0`, which folds
the shelf back into the 1s book tick. It resolves correctly today only
because `load_config()` merges `DEFAULT_CONFIG` — an undeclared knob one
refactor away from silently quartering the ratchet's rate.

### 1. The single root cause: `price_age_sec` is None

Live on the wire, all nine names: **`price_age_sec = None`**. On RTH shadow
rows it is absent on **53%** (9,253/17,585). It is computed from the
trade's own timestamp and is None whenever the winning price source cannot
supply one — which is the normal case, because the dashboard's merge is
falling back to REST.

**Every freshness guard in the desk keys off that field, and every one
fails open:**

| guard | anchor | behaviour when age is None |
|---|---|---|
| ratchet staleness | `ai_positions.py:3035` | `return px` — accepts unconditionally |
| arm freshness, 8s | `ai_entry_watch.py:359` | `return False` — not stale |
| blind-book flatten, 15s | `ai_positions.py:4026` | cannot evaluate |

That is why `stale_quote` blocked **0 of 17,585** RTH rows while
`tape_age_sec` exceeded its own 8s threshold on 69% of the rows that
recorded it. `ai_watch_decision_max_age_sec` is effectively dead config.
**Fix this one thing and three guards come back to life.**

### 2. The ratchet's 0.25s tick is ~1.5s in practice

`tick_local_trail` → `_fresh_tape_px` → `ew.live_print` → `dashboard_state`,
which is **cached for `_DASH_CACHE_TTL` = 1.5s**. The new
`ind_snapshot_age_sec` column measures that cache directly and reads
**0.99–1.37s**. So the shelf ticks four times per second against a price
that cannot refresh faster than ~1.5s — a 6× gap between intended and
actual, and it is invisible without the column.

Worse, that price is the *weaker* of the two feeds in the building. The
engine holds Finnhub trades at **0.1–0.9s** for the same names; the price
path runs on Alpaca IEX because the dashboard's merge is not winning with a
timestamped trade. **The desk owns sub-second tape and does not stop on it.**

### 3. Decision speed: fresh data, slow gate

The 2s desk sync refreshes %R (`ensure_live_exhaustion`) and RSI
(`refresh_engine_rsi`) onto every record — but its own docstring says it is
"not a place to re-run the whole arm decision". So data refreshes every 2s
and the gate decides every 20s; roughly 18 of every 20 seconds of freshness
is discarded.

What that costs is real but **second-order**, and the honest numbers are
mixed: median blind-window drift is 0.000R (shadow samples repeat a stale
price ~44% of the time, so the median is uninformative), **p90 is 0.079R —
nearly 2× the median trade's entire MFE of 0.046R** — and 17% of gaps drift
further than that MFE. Flicker is only **0.3%** of gaps, so the armable
state is stable and little is opening and closing unseen. Fix §1 and §2
before touching this.

### 4. The levers are not reliably realtime *at the arm*

Of 422 armed rows over 8/20–26:

| | realtime | degraded | unrecorded |
|---|---:|---:|---:|
| `pctr_src` | live 314 (74%) | **clock_range 108 (26%)** | — |
| `cm_rsi_src` | realtime 279 (66%) | alpaca 22 (5%) | **None 121 (29%)** |

**About one arm in four fires on a non-live %R.** Guards for this exist
(`rsi_not_realtime_alpaca`, `pctr_not_live_clock_range`) but blocked **0**
of the 5,412 fallback-RSI rows evaluated — they are not on the path that
matters. Indicator coverage itself is good: 7 of 9 names on realtime bars.

### 5. Admission is fast; research arms far more often

| source | names | armed | rate | first seen ET |
|---|---:|---:|---:|---:|
| momentum | 69 | 14 | 20% | 09:26 |
| trending | 42 | 17 | 40% | 09:34 |
| anthropic | 14 | 9 | **64%** | 06:17 |
| xai | 8 | 5 | **63%** | 09:30 |
| bb_live | 1 | 0 | 0% | 08:11 |

Admission latency is **p50 9.6s / p90 17.0s / max 20.5s** — bounded by the
20s poll, i.e. admission is as fast as the gate allows and is not itself a
bottleneck. Research names arm at ~3× the rate of momentum names on ~1/5
the volume, and `anthropic` names are first seen at **06:17**, inside the
window where §5G2 says price and setup are unevaluable.

### 6. Selection is NOT weighted on the stated principles

The setup (§5F) needs five legs. What the book could actually see:

| leg | known | coverage | passing |
|---|---:|---:|---:|
| pct_change ≥ 10 | 48,502 | 100% | 24% |
| price $2–20 | 48,734 | 100% | 69% |
| **rvol ≥ 5** | 32,029 | **66%** | 13% |
| **catalyst** | 26,974 | **55%** | 48% |
| **float < 10M** | 27,396 | **56%** | 9% |

`setup_n_legs` is **None on 42%** of rows and all five legs are satisfied on
**1.3%**. And the block reasons say what really decided the book:
`rsi_extended` 51% + `rsi_not_rising` 37% = **88% RSI**. No setup leg
appears anywhere in the top ten.

**The desk selects on RSI. The stated principles are recorded, not
enforced.** That is not a bug — nothing ever wired them to the arm gate —
but it means §5F has never been the operative rule, and any claim that the
book trades the operator's setup is currently false.

### Ordered remedy

1. **Populate `price_age_sec`, and make the three guards fail closed.**
   One root cause, three dead safety checks, no new feed required.
2. **Point `_fresh_tape_px` at the engine's Finnhub tape** (0.1–0.9s)
   instead of the 1.5s-cached IEX wire, or shorten `_DASH_CACHE_TTL` for
   the shelf path. The ratchet is the one thing §2 proved works — it
   deserves the better feed.
3. **Declare `ai_shelf_tick_sec` in `bot_config.json`.** It is load-bearing
   and currently implicit.
4. Put the realtime guards on the arm path so a `clock_range` %R cannot
   fire an entry, or accept it deliberately and record the choice.
5. Only then consider the 20s arm cadence — it is a tail cost (p90 0.079R)
   and the cheapest of these to get wrong.
6. Decide whether §5F legs should gate. Today they do not, and with rvol,
   float and catalyst each known on ~half of rows, they *cannot* without
   the coverage first.

## 10. Operator constraints

- The operator owns the account and has more faith in the ratchet than in
  any redesign. §2 says they are right about the ratchet and §3 says the
  entries are the problem — lead with that, not with a new product.
- A dead watchlist is boring. That is a real constraint on any proposal
  that parks the desk in `observe` for weeks.
- Do not restore auto-buy, change a live knob, or arm a paper flag because
  a screen looked green once. Session majority or it did not happen.

```
Live:  desk_product=scalp_legacy · ratchet armed · 04:00 watch · paper
Open:  the tape does not clear its own costs — payX 0.62 at 15m
Next:  finish gate 1 (10 sessions), then cost or timescale — not gates
```

— 8/20 Claude: measurement, H1 dead, late candidate.
— 8/21 Grok: observe, H4 lab, harvest split. Retired 8/22.
— 8/22 Claude: ratchet vindicated as an exit, entries falsified, H4
  falsified, field measured driftless, latency identified as the problem.
— 8/23 Claude: **latency falsified** — `captured` does not predict R and
  available excursion is flat in run-up, so 8/22's five "buys what already
  moved" measurements were bucket-edge artifacts. Universe screen built
  (`tools/universe_screen.py`, pre-registered pay bar): 6 universes, 18
  cells, zero playable. `rejects` ≈ `desk`, so the gate is not selecting;
  `burst` has 6× the range and no direction. Entry queue items 4–6 retired.
  Then per-name cost added, which **falsified the cost-lever proposal on
  the same day**: cheap names have the best payX, because 0.50 of the
  0.79% round trip is the ratchet's own give and does not vary with price.
  One candidate survives — `desk_px:50-`, 8/10 sessions, p=0.0547 — thin,
  searched, and pointing at names the watchlist filters out.

**8/26 — the premarket book is not wide, the premarket feed is blind.**
`spread_coverage` said premarket costs 1.717R against 0.065R in RTH, which
would end the "start at 04:00" idea on arithmetic. Re-priced against SIP on
the same rows it is **0.229R**, and 0.120R when the quote is under a minute
old — indistinguishable from the RTH SIP median of 0.117R. Every spread on
the record comes from IEX, which barely quotes before 09:30, so the desk
has been measuring its own blindness and calling it the market. `SDOT` is
the case that settles it: logged 5.278R, actual 0.026R. The premarket
hazard is the quote going *dark* (37% of rows matched a quote over a minute
old, vs 2% in RTH) against a session where Alpaca holds no stop and
`_rth_now()` gates the blind-book flatten off. §5G.

**8/26 (later) — the premarket price is yesterday's close.** Finnhub is out
as a quote source (`/stock/bidask` 403, `/quote` returns the prior close
premarket), so only its websocket carries premarket tape — and that socket
is in `signal_engine`, not in the process that computes indicators. The
8/25 `pctr_stream` experiment feeds the aggregator the *same* IEX price it
was meant to be compared against, so it pins to an extreme on 95% of
premarket rows and reads the exact opposite of `pctr` on most of them.
Underneath: the live IEX ask is `0.00`, the last IEX trade is 14 hours old,
and the desk logs it as current every 20 seconds — zero price changes in
363 samples across all 8 names. Every premarket number the desk has ever
recorded is computed on yesterday's close. §5G2.
