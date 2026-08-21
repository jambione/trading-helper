# Handoff: state of the desk, 2026-08-21 (evening)

Audience: next agent. Challenge anything; claims carry evidence.

**Product contract (read this first):** [docs/PROFIT_REDESIGN.md](docs/PROFIT_REDESIGN.md).
The 86s scalp is falsified. Default book is `desk_product=observe` (no new
auto-arms). Profit path is H4 (liquid, multi-day) via `tools/h4_screen.py`,
paper off until gate 1 PASS.

**Git at the close:** `master-mac` @ `4d71ef6` plus the profit-redesign
commits after it. Mini runs the book.

**8/21 data (mini, venv):** paper 49 trades −1.56 R; live-eq −3.98 R;
median MFE−spread −0.032 R; go-live 2/10. Late 60m 8/14–21 **FAIL** on
the session test (4/6, one day 71% of n). Spread-k grid inert.

**Who built what**

| Who | When | What |
|---|---|---|
| Claude (Opus) | 2026-08-20 day | First honest measurement of the scalp. Simulator-never-traded bug. 1m-bar scoring. `entry_rule_screen`. Original §§2–6 below. |
| Grok | 2026-08-20 night → 21 | Independent read of that evidence. Measurement kernel (`desk_null`). `thesis_screen`. Late-hold sweep. Late-hold *code* (flag **off**). |
| Grok | 2026-08-21 day | Crossing-cost, EOD live-eq, spread-k grid (null), sounds. |
| Grok | 2026-08-21 evening | Late 60m re-screen FAIL. Profit redesign: observe default, H4 lab, fill_truth/instrumentation/watchdog. |

Historical §§2–3 below are still the scalp baseline. They are **not** a
license to retune it.

---

## 0. Where we are, in one page

**Do not trade the daytime momentum scalp.** Heat ≥ 40, 5% 1R, 0.10R
shelf, ~86s, IEX — frozen and **not the live money path**. Default
`desk_product=observe`. See `docs/PROFIT_REDESIGN.md`.

What remains from the 8/20–21 lab, and is still the kill-gate kernel:

1. Every new entry claim is graded against `tools/desk_null.py` (eligible-WITHIN, IWM residual, vol-matched outside, 20 bps vs cash). PASS = n≥30, median net of haircut > 0, **and** eligible-within > 0 at ≥2σ. A green day is not a pass.
2. Screens look at **different information**, not another RSI permutation.
3. One slice cleared gate 1+2: **names admitted 14:00–15:30, hold to 15:50, trail off, 2% hard stop, 30m dead-trade.** Thin after spread (~0.04%/trade in the sweep). Wired as `ai_late_hold_paper` and **left false** — the operator does not want morning arms blocked to collect that evidence.

**Next step (do this, not more overlays):** after each close, on the mini,
score yesterday's 14:00–15:30 *admissions* off bars. Accumulate sessions.
If that slice stays PASS with more days, *then* discuss a paper book that
does not sit out 9:30–14:00 (shadow-only, or late fills only when a slot is
free). If it dies out of sample, fork to H3 (overnight) or H4 (slower swing,
different universe).

```bash
# mini only (Alpaca keys + live shadow). MacBook cannot fetch bars.
python3 tools/thesis_screen.py --days 1 --horizon-min 60 --slices late --flatten-et 15:50
python3 tools/thesis_screen.py --days 5 --horizon-min 60 --slices late,chase,research --flatten-et 15:50
```

Do **not** set `ai_late_hold_paper=true` unless the operator explicitly
accepts no auto-arms before 14:00.

---

## 1. Live config that matters (`config/bot_config.json` on the mini)

| knob | value | note |
|---|---|---|
| `ai_late_hold_paper` | **false** | Late-hold *code* is loaded; path does not fire |
| heat_min / heat_max | 40 / 0 | Contradicts the cool buckets; leave until a real test |
| 1R (`synth_stop_pct`) | 5% | ~20× median scalp MFE |
| trail | on, min_give $0.06 | What actually exits (~86s) |
| feed | iex | Delayed SIP only — do not switch live feed |
| slots / EOD | 2 / 15:50 | |
| book / shelf tick | 1.0s / 0.25s | |

Operational traps: tunables live in `bot_config.json` (`load_config()` never
reads `signal_engine.env`); trader reloads config each poll; mini cannot
`git push` (commit there, fetch from the MacBook); prefer `./trading restart`
on the mini Terminal (Keychain). Deploy from MacBook: commit →
`./scripts/deploy_mini.sh`.

---

## 2. Evidence (Claude, 2026-08-20) — still the baseline

### 2a. That day's book (paper)

59 closed trades, 28.8% win rate, sum −0.33R. 55/59 `local_trail`, median
hold 86s, median MFE +0.04R / MAE −0.02R. Trail gives back the typical
+0.23% run and pays the spread. Post-exit drift +0.13% @5m, −0.23% @30m:
the trail is not cutting winners.

### 2b. Admission vs controls (`tools/admission_null.py`)

593 RTH admissions, 2026-08-14..20, SIP 1m. **Legacy WITHIN** (any RTH
instant) made timing look −4.2σ @30m — **hindsight-loaded**: the dart can
buy the morning run-up that put the name on the list. Same-instant
controls (ACROSS, price-matched OUTSIDE) were ~flat. Defensible claim:
**no positive timing edge against any same-instant alternative.**

### 2c. Watched-name drift (legacy WITHIN)

60m mean +0.681% vs median +0.149% — fat right tail. Desk harvests at 86s
where the median is zero. Grok later showed IWM over the same windows is
~0, so this is not "the tape was up."

### 2d. Exhaustion buckets point backwards

Cool 0–50% is the only consistently positive 30m range. Live `heat_min=40`
excludes it. 75–90% is +0.511% between two deep-red neighbors — treat as
noise until replicated. Heat sweep produced no candidate; every cell kept
86s exits.

### 2e. Simulator had never traded

`no_rsi_data` / `rsi_not_rising` on every bar. Fixed in `sim_rstop_path`.
Empty runs now refuse a verdict. Historical "no overlay beat live" was
empty. Do not promote from a sweep with n=0.

H1 (cool pullback, 30–60m) as screened in `entry_rule_screen` is **dead**.
Cool and hot both lost to WITHIN (that is what exposed hindsight). vs
OUTSIDE, fourteen cells 0.0–1.4σ — **flat**. RSI / %R / RVOL / zone / live
gates carry no 30–60m information on this tape. Do not permute them again.

H2 (short the admission) is screen-only: squeeze names, unpriced borrow.
H3 overnight / H4 slower different universe remain the honest forks if
late-hold dies out of sample.

---

## 3. What Grok built (2026-08-20 night → 21)

### Kernel — `tools/desk_null.py`

Shared by `admission_null`, `entry_rule_screen`, `thesis_screen`.

- **ELIGIBLE-WITHIN** — dart only after first watch that day (kills
  pre-list run-up). Legacy WITHIN kept so the 4σ inflation is visible.
- **IWM residual** — name minus small-cap beta over the same horizon.
- **OUTSIDE vol+price** — skip rather than silently fall back to price-only.
- **20 bps vs cash** — spread cancels in a paired same-cost row; this is
  the bar versus sitting out.
- `--flatten-et 15:50` so a 60m window from 15:10 is not scored past EOD.

Screens run **on the mini**. MacBook `secrets.json` has no Alpaca keys.

### Gate 1 — `tools/thesis_screen.py` (627 RTH admissions, 8/14..20)

Every **30m** slice FAIL. Eligible-within milder than legacy (all: 3.5σ vs
5.2σ). Vol-matched outside flat (0.5σ). Chase (already +2% from the open)
worst common slice. IEX-blind names poison (−0.78% median). Research less
bad than scanner, still FAIL.

At **60m**, one slice PASS, and it survived flatten 15:50:

| slice | 30m | 60m to 15:50 |
|---|---|---|
| all / open / chase / research | FAIL | FAIL |
| **late 14:00–15:30** | FAIL | **PASS** n=136, net +0.35%, vs eligible +0.26% 2.3σ |

Caveats: five sessions; IWM bid into the close (84% up) though residual
still +0.38%; mean negative, median green (left tail); 2.3σ just over the
bar. This is permission to sweep *that slice's exits*, not to retune the
scalp.

### Gate 2 — `optimize_rstop` late hold

```bash
python3 tools/optimize_rstop.py --admitted --admit-tod 14:00-15:30 \
  --arm-at-admit --no-book --from 2026-08-14 --to 2026-08-20 --feed sip \
  --search tools/rstop_search_late_hold.json --tag late_hold
```

`--arm-at-admit` buys the first in-window bar (matches the screen, skips
heat/RSI). `--admit-tod` is admit clock, not "still on the list at 14:00."
Trail off uses the **hard synth stop**, not the 0.10R shelf (that shelf
*is* the 86s scalp).

| cell | n | win | held $ | mean $ | folds |
|---|---|---|---|---|---|
| baseline (live trail) | 35 | 46% | +$9 | $0.26 | — |
| **trail off, 2% stop, dead 30** | 34 | 53% | **+$80** | **$2.35** | **3/5** |
| any trail still on | 35 | 46% | +$9 | $0.26 | 0/5 |

$2.35 on $1k = **0.24%/trade**. After 20 bps ≈ **0.04%**. Live shelf is
**negative** after the same haircut. Max DD $70 on $80 gross. n=34 just
clears 30. Give knobs inert when trail is off. Artifact gitignored:
`benchmarks/optimize_rstop_2026-08-20_late_hold.json`.

### Gate 3 — wired, then turned off

`desk_late_hold.py` + hooks in `should_arm_buy`, `_decision_for_place`,
`place_scaled_entry`, `apply_local_trail`. When `ai_late_hold_paper` is
true: no arms outside 14:00–15:30; only names whose **admit_ts** is in that
window; 2% hard stop; no shelf; no T1 scale-out; flatten through 2%; 30m
dead; 15:50 EOD. Morning names keep morning `admit_ts` (`late_hold_not_late_admit`).

The operator rejected sitting out 9:30–14:00 to collect fills. Flag is
**false**. Daytime scalp runs. Late-hold evidence continues via
`thesis_screen` on shadow+bars after the close. 9:30–14:00 is a **log**
(shadow, research, tape), not a second entry thesis and not a reason to
re-stamp `admit_ts` at 14:00.

---

## 4. Pipeline (unchanged)

Cheapest gate first:

1. **Screen** — `thesis_screen` / `entry_rule_screen` vs desk_null. Bar is
   `rule − eligible-within` with sigma, net of 20 bps vs cash. Green `fwd`
   is not enough.
2. **Sweep** — `optimize_rstop` with exits sized to **that** horizon. Candidate
   = beats baseline held-out $ AND majority of folds AND n≥30. EMPTY RUN is
   not a verdict.
3. **Forward paper** — only if the operator wants a live book change, one
   experiment at a time, graded by the null, not a green day.

Do not add RSI/%R/RVOL/zone permutations. That family is dead.

---

## 5. Next steps, in order

Superseded 2026-08-21 evening — late 60m **died** on the session test.
Follow [docs/PROFIT_REDESIGN.md](docs/PROFIT_REDESIGN.md) §10:

1. Deploy/restart so `desk_product=observe` is live (no new scalp fills).
2. After each close on the mini, venv: `eod.py`, `h4_screen.py`, late
   `thesis_screen` (autopsy only). Watchdog should do this.
3. Do not enable `ai_h4_paper` / `ai_late_hold_paper` / spread-k.
4. H4 FAIL with ≥5 sessions → do not loosen to squeeze names; fork H3 or
   a longer H4 with the same null.
5. H4 UNDERPOWERED → collect. H4 PASS → gate 2 sweep, then operator OK
   for paper.

---

## 6. How to run the lab (mini)

```bash
python3 tools/admission_null.py --days 5 --horizon-min 30
python3 tools/thesis_screen.py --days 5 --horizon-min 30
python3 tools/thesis_screen.py --days 5 --horizon-min 60 --flatten-et 15:50
python3 tools/entry_rule_screen.py --days 5 --horizon-min 30   # H1 family; expect FAIL
```

Tests (no Alpaca): `tests/test_desk_null.py`, `test_thesis_screen.py`,
`test_desk_late_hold.py`, `test_optimize_rstop.py`.

---

## 7. Weaknesses still open

- Five sessions, one regime, paper fills, no spread in the sim.
- Late PASS just over 2σ; IWM last-hour bid; fat left tail.
- OUTSIDE is still not news-matched. Eligible-WITHIN is the timing null.
- Watchlist churns daily. Late admits must *join* 14:00–15:30; a dead
  afternoon is n=0, which is a result.
- Fills of the 86s scalp still do not measure research-idea quality.

— Claude (Opus) 2026-08-20 day: measurement and H1 screen.
— Grok 2026-08-20 night–21: kernel, late-hold gates, deploy, operator
  rejected the 14:00 lock; this file rewritten for Claude pickup.
