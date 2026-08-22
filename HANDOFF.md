# Desk state — 2026-08-22

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

So the open problem is **admission latency**, not exit geometry and not
another indicator permutation.

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
| `ai_max_spread_r` | 0.0 (off) | **open.** A book wider than the move is unwinnable, but n=28 fills cannot pick a threshold — kept and refused means are indistinguishable at every cap tried. Revisit when coverage gives real n |
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

Two questions were run on 8/22, in order, and both are answered.

### A. Is admission too late? — **Yes.** `tools/admission_latency.py`

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
change — and it is the dial to raise when attacking latency, since making
the desk wait for a bigger move is the one lever that trades admissions for
freshness. Raising it will cut volume; measure `captured` before and after.

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

The names do exactly that — during the run-up, before admission. So the
whole product question is now one sentence:

> **Can the desk be admitted during the run instead of after it?**

That is a latency and detection problem, not a signal problem.

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

**GATE 2 — entry, only after gate 1 passes.** In order:

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

**4. Detection latency.** `ai_watch_open_seed_min_pct` is the dial;
`captured` is the scoreboard. 71% of the move is gone at admission.

**5. Decision latency.** The 31.7-minute admit-to-arm gap. Only worth
attacking after the above — arming faster into a name chosen badly just
loses money sooner.

**Do not** arm anything from gate 2 while gate 1 is running, and do not
retune the min-hold delay mid-test because a week looks bad. Ten sessions,
then read it.

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

---

## 7. The lab

Mini only — the MacBook cannot fetch bars. Use the venv; system `python3`
has no Alpaca client.

```bash
.venv/bin/python tools/drift_screen.py --eligible-within --days 20
.venv/bin/python tools/gate_screen.py --days 20 --horizons 15,30,60
.venv/bin/python tools/admission_latency.py --days 20
.venv/bin/python tools/harvest_screen.py --days 10
.venv/bin/python tools/eod.py --days 10
```

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
Open:  71% of the move gone at admit, then 31.7m to arm
Next:  drive captured down, then re-run gate_screen
```

— 8/20 Claude: measurement, H1 dead, late candidate.
— 8/21 Grok: observe, H4 lab, harvest split. Retired 8/22.
— 8/22 Claude: ratchet vindicated as an exit, entries falsified, H4
  falsified, field measured driftless, latency identified as the problem.
