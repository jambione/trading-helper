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
- **The drift is real but early.** 74% of the momentum move is over before
  the name is admitted. The desk arrives at 10:33 with 2.9% left.

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
| `ai_local_trail_give_spread_k` | 0 (absent) | floors the give at k×spread. See §6 |
| `ai_local_trail_be_at_spread_k` | 0 | won't protect a gain until it clears k round trips |
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
| momentum | 161 | 10:33 | +8.2% | 8.2% | 2.9% | **0.74** |
| trending | 97 | 10:08 | +5.8% | 5.8% | 2.3% | **0.69** |
| research | 31 | 09:33 | +3.6% | 3.6% | 3.7% | **0.48** |

`captured` is the share of the day's up-move spent before the desk could
act, computed in price space. Three-quarters of the momentum move is gone
by admission. Research arrives earliest with the most left — the only
source admitted mid-move, and the one with the smallest loss share.

**`ai_watch_min_pct_change=50` is not what admits names.** The median
admission sits at +8.2% vs the prior close, and the threshold-crossing
latency is *negative*: names are typically admitted before ever clearing
+50%. The `mom_open_soft` seed path in `ai_entry_watch.py` sets
`bypass_inclusion` and skips inclusion scoring. **The config knob does not
describe the live gate.** Anyone tuning that number is tuning nothing.

### B. Does any gate select drift? — **No.** `tools/gate_screen.py`

14 candidate gates × 3 horizons, anchored at the instants each gate
actually fired in `shadow.jsonl`. **42 cells, zero DRIFT.**

| gate | horiz | n | MFE/MAE | sigma | med net | green |
|---|---:|---:|---:|---:|---:|---:|
| `fresh_5m` | 60m | 243 | 0.78 | **1.69** | 0.000 | 6/12 |
| `arm_ok` (incumbent) | 15m | 266 | **1.19** | 1.19 | +0.020 | 6/11 |
| `arm_ok` | 30m | 183 | 1.20 | 0.44 | +0.048 | 6/11 |
| `all` (baseline) | 15m | 1272 | 0.93 | −0.10 | −0.054 | 4/12 |
| `rvol_10` | 15m | 82 | 0.79 | −0.02 | **−2.977** | 1/9 |

Nothing clears 2σ; every session sign is a coin flip. High RVOL is actively
harmful. `in_zone` and `pctr_ok` are below the null at every horizon.

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

That is a latency and detection problem, not a signal problem. Next work,
in order:

1. **Instrument admission latency live.** Log, per admission, the time the
   move began vs `admit_ts`, so the 0.74 becomes a number that can be
   driven down and watched.
2. **Attack the seed path, not the indicators.** `mom_open_soft` and the
   feed cadence decide when a name lands. That is where the 74% lives.
3. **Re-run `gate_screen` after any latency change.** If `captured` drops
   and `fresh_5m` starts clearing 2σ with a session majority, that is the
   first real candidate this desk has had.
4. Only then discuss arming anything new, and only with a cost test on top.

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
- **Do not trust `optimize_rstop` right now.** Four tests in
  `tests/test_sim_rstop_path.py` fail because `walk_symbol` places **zero
  trades**. Any "do not change config" verdict it produced is empty, not
  informative. Fix before using it to judge ratchet knobs.
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
Open:  admission arrives after 74% of the move
Next:  drive captured down, then re-run gate_screen
```

— 8/20 Claude: measurement, H1 dead, late candidate.
— 8/21 Grok: observe, H4 lab, harvest split. Retired 8/22.
— 8/22 Claude: ratchet vindicated as an exit, entries falsified, H4
  falsified, field measured driftless, latency identified as the problem.
