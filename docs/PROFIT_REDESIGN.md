# Profit redesign — contract

**Written 2026-08-21, after the close.** Evidence is on the mini
(`ai_reports/eod_2026-08-21.log`, `ai_reports/daily/2026-08-21.*`,
`ai_reports/screens/thesis_2026-08-21.log`,
`benchmarks/optimize_rstop_2026-08-21_spread_k.*`). Git at writing:
`4d71ef6`. This file is the design we implement against. Do not retune
the old scalp instead of following it.

If you need to roll the product back, this is the document that says
*why* the default book stopped buying.

---

## 1. What the data already decided

The live product on 2026-08-14..21 was an IEX **86-second continuation
scalp**: heat ≥ 40, 5% 1R, 0.10R working shelf, 2 slots, flatten 15:50,
paper. That family is **falsified**.

| Claim | Result | Anchor |
|---|---|---|
| Typical scalp pays for the round trip | **No.** Median MFE − spread **−0.032 R** on 2026-08-21 | `tools/eod.py` |
| Paper P&L is the number | **No.** 49 trades paper **−1.56 R / −$3.49**, live-equivalent **−3.98 R / −$8.77** | same |
| Ten-session go-live | **Fail.** 2/10 sessions live-positive (need 7/10); MFE−spread not > 0 | `eod.py --days 10` |
| RSI / heat / zone / RVOL as entry | **Fail** vs eligible-within (2026-08-20 `entry_rule_screen`) | `GROK_HANDOFF.md` §2 |
| Trail / spread-k overlays | **Null.** 15 cells on 8/17–21 identical to baseline, 0/5 folds | `optimize_rstop_2026-08-21_spread_k.csv` |
| Late 14:00–15:30 hold to 15:50 | **Fail once 8/21 is in and the unit is the session.** n=143 net +0.315%, vs eligible +0.298% 3.2σ, **4/6 sessions p=0.344**, one day = 71% of sample | `thesis_screen` 16:50 ET 8/21 |
| Chase (already +2% at admit) | **Fail 0/6 sessions**, net −0.564% | same |
| Research list at 60m | **Fail**, net −0.200% after 20 bps | same |
| 8/21 late slice alone | **UNDERPOWERED** n=7 | same |

Go-live bar (session-level, live-equivalent, unchanged):

- 7 of 10 sessions live-positive
- ≥ 30 trades
- median MFE − spread > 0

A green paper day is not a pass. A pooled σ on admissions is not a
pass. **The session is the unit of independence.**

---

## 2. Goal

Protect capital first. Then find **one** trade that clears the lab
gates, and only then allow paper arms.

- **Protect:** no new entries from a falsified product.
- **Profit:** a different universe and a hold long enough that typical
  travel is several times the spread, graded the same way we killed the
  scalp.
- **Honesty:** keep the measurement stack that made today’s FAIL
  visible.

This is not “make the 86s scalp work.” That path is closed.

---

## 3. Keep / kill / build

### Keep (the machine)

- Mini stack: dashboard, engine, `ai_trader`, Discord, tunnel, watchdog.
- Paper Alpaca, host guard, protective-exit requirement, one book owner.
- Shadow / rejects / events / outcomes / position_shadow.
- `tools/desk_null.py` (eligible-within, IWM/SPY residual, 20 bps
  haircut, **session** sign test, max-day-share).
- `tools/thesis_screen.py`, `tools/eod.py` (live-equivalent + MFE−spread).
- Replay tuner that **refuses** to write `bot_config.json`.
- Crossing-cost on the arm row (RTH coverage ~90% on 8/21).
- Research write-ups as **research**, not as an entry trigger.
- RS screener + swing screener **code** (both were off; they become the
  H4 universe, not a second scalp).

### Kill as the money path (leave the code, do not permute)

- Auto-arm of the 86s continuation scalp (`desk_product=scalp_legacy`
  only if an operator explicitly restores it).
- New RSI / %R / RVOL / zone / heat / trail sweeps on the squeeze
  watchlist.
- `ai_late_hold_paper` (session test failed).
- Spread-k / BE-after-round-trip / trail-from-book flags (grid was
  inert; stay **false**).
- Promoting hybrid-exit from one tape.
- Going live. Paper stays paper until go-live clears on the **new**
  product.
- Using research notes as proof the scalp was right.

### Build (this redesign)

1. **Product switch** — one knob, `desk_product`, that says what may
   *buy*. Default **`observe`**: stack up, no new auto-arms.
2. **H4 lab** — liquid universe, daily (or multi-day) hold, hard stop,
   **no** 0.10R shelf, vs SPY + 20 bps, session null. Paper flag off
   until PASS.
3. **H3 stub** — overnight/gap, flag off, not screened yet. Do not
   implement a live overnight book in this pass.
4. **EOD pipeline** that actually runs the screens (`eod.py`, late
   `thesis_screen`, `h4_screen`) instead of only replay overlays.
5. **fill_truth** day filter (it was scoring `exit_time`/`ts` that the
   FIFO pair never writes — hence 0 trades every ledger line).
6. **instrumentation_check** that does not cry wolf when optional
   fields are sparse (`look_reason` 22%, `pctr_slow` often absent).

---

## 4. Product model

```
desk_product:
  observe        default. Watch, shadow, research, flatten leftovers.
                 should_arm_buy → False, "desk_observe".
  scalp_legacy   the falsified 86s path. Restored only by explicit config.
  h4_swing       liquid multi-day. Arms only if ai_h4_paper is true.
  h3_overnight   reserved. ai_h3_paper stays false.
```

Resolution when `desk_product` is **omitted** from a partial cfg dict
(unit tests): behave as `scalp_legacy` so existing arm tests keep their
geometry. `DEFAULT_CONFIG` and live `bot_config.json` **set**
`observe`, so `load_config()` is capital-safe.

Paper flags (all default false):

| flag | meaning |
|---|---|
| `ai_h4_paper` | H4 may place paper orders. Requires a documented gate-1 PASS. |
| `ai_h3_paper` | H3 may place paper orders. Not granted in this pass. |
| `ai_late_hold_paper` | stays false |

Unknown `desk_product` → `observe` (fail closed).

### What still runs in `observe`

- Watch admission, quotes, exhaustion telemetry, shadow rows.
- Research slots (ideas only).
- EOD/SOD flatten of **non-H4** leftovers so a restart cannot leave a
  naked scalp overnight.
- Watchdog, daily_learn, eod, screens.

### What does not run in `observe`

- New `place_scaled_entry` from watch / open-bell / research paper
  JSON (unless `desk_force`, which is tests / explicit operator).
- Enabling late-hold or spread-k as a workaround.

---

## 5. H4 — the profit candidate

**Different information** than the scalp: not RSI-on-squeeze, not
14:00–15:30 on the same names.

### Universe (conjunctive)

From `rs_ratings.json` when present, else skip RS and still require
price + dollar volume (a name we cannot rank is not automatically
qualified):

- last price ≥ `h4_min_price` (10)
- 50-day avg dollar volume ≥ `h4_min_dollar_vol` ($5M)
- RS rating ≥ `h4_min_rs` (80) when a rating exists
- quote spread ≤ `h4_max_spread_pct` of mid (0.10% — round trip in the
  same units as the 20 bps haircut) when a live quote exists
- IEX/SIP must actually print the name (no all-day-blind names)

This is the opposite of `ai_watch_min_pct_change=50` + Stocktwits heat.

### Trade (paper, after PASS only)

- Enter at the session open (or first reliable print) of a name that
  is in universe that morning.
- Hard stop `h4_stop_pct` (2%) from entry. **No** 0.10R working shelf.
- Hold `h4_hold_days` (2) completed sessions, or stop, whichever first.
- No dual T1 scale-out. No 15:50 flatten of H4 names. No SOD wipe of
  H4 names. Overnight is the point.
- Size from existing `ai_risk_pct` / notional caps. Slots stay small
  (`ai_max_positions`).

### Gate 1 — `tools/h4_screen.py`

Daily bars (not 1m squeeze admissions). For each eligible name-day,
hold `h4_hold_days` or stop. Score:

- `fwd` = name return
- `net` = fwd − 20 bps haircut (vs cash)
- `eligible` = SPY over the same window (the dart is “own the market”,
  not “random time on this squeeze name”)
- session = entry date
- `desk_null.verdict`: n≥30, net median > 0, paired vs SPY > 0 at ≥2σ,
  ≥5 sessions, sign-test p≤0.05, no session > 50% of sample

PASS is permission to **sweep exits** (gate 2) for *this* horizon.
It is **not** `ai_h4_paper=true`.

### Gate 2

Only after gate 1 PASS: `optimize_rstop`-class sweep on **daily**
holds (stop / hold days), majority of folds, n≥30, beats baseline
held-out $. EMPTY RUN is not a verdict.

### Gate 3 — forward paper

Only if the operator sets `desk_product=h4_swing` **and**
`ai_h4_paper=true` after reading a PASS. One experiment. Grade with
`eod.py` live-equivalent and MFE−spread, session-level.

### What H4 is not

- Re-stamping morning squeeze admits at 14:00.
- Holding the 86s names longer with the same shelf.
- Overnight on IEX-blind sub-$5 names (that is a different, worse
  risk class — closer to a broken H3).

---

## 6. H3 (named, not built)

Hold across the close / into the open on a **liquid** name. Different
risk (gap). Screen only after H4 has a verdict. `ai_h3_paper` stays
false. Do not mix H3 fills into the H4 book.

---

## 7. Lab pipeline (mini, after each close)

Watchdog 16:05 ET, venv Python (system `python3` has no Alpaca client):

1. `tools/daily_learn.py --day TODAY` (existing)
2. `tools/eod.py --days 1` (and keep the 10-day go-live in the log)
3. `tools/thesis_screen.py --days 1 --horizon-min 60 --slices late --flatten-et 15:50`
   — autopsy of the old slice, not a promotion path
4. `tools/h4_screen.py` — the actual candidate
5. `desk_tape pack` / `replay_ab` stay, still must not write config

Stdout of these jobs appends to `logs/learn.log` so a nonzero rc is
not a mystery.

Do **not** add RSI permutation screens to this list.

---

## 8. Capital and flatten rules

| Event | Scalp leftover | H4 position |
|---|---|---|
| 15:50 EOD liquidate | flatten | **keep** (and keep its broker stop) |
| SOD liquidate | flatten + drop local state | **keep** local state + broker position |
| local 0.10R trail | on | **off** (hard stop only) |
| sell_signal → breakeven | on | **off** |

`alpaca_trader.liquidate_all(except_symbols=…)` must not cancel H4
stops. A flatten that cancels all orders then “excepts” the shares
leaves a naked overnight — the 2026-08-07 USAR failure mode.

---

## 9. Honesty fixes in this pass

**fill_truth n=0.** `_pair_round_trips` writes `sell_time`.
`daily_learn._fill_truth` filtered on `exit_time` / `ts`, so every
completed FIFO trip was dropped. Filter on `sell_time`.

**instrumentation_check rc=1 all day 8/21** while shadow had 13k
rows. Failure was “a decision field is never present.” `look_reason`
and `pctr_slow` are often sparse by design. Split **required**
(score, arm_why, reject reason; plus log freshness) vs **optional**
(coverage only). SILENT/STALE on shadow still fails.

---

## 10. Directional goals (how we will know it worked)

**Horizon A — this week (observe + lab)**

- Zero new scalp fills after deploy/restart.
- EOD log contains live-equivalent, late thesis (even if UNDERPOWERED),
  and an H4 screen line.
- fill_truth trades > 0 on a session that actually filled (legacy
  leftovers) or honest 0 when the book did not trade.
- Watchdog instrumentation rc=0 while shadow is writing.

**Horizon B — H4 gate 1**

- First PASS or a clean FAIL/UNDERPOWERED with n sessions ≥ 5.
- If FAIL: stop. Do not loosen universe to squeeze names. Fork H3 or
  a still-slower H4 (5–10d) with the same null.
- If UNDERPOWERED: collect. Do not arm.

**Horizon C — paper H4**

- Operator-explicit flags. Go-live bar on **H4 sessions**, not a
  pooled 8/14–20 scalp memory.
- Live account is not in scope until that bar is green.

**Non-goals**

- Dashboard cosmetics, toast/sound, more trail knobs.
- Dual Grok/Claude books.
- SIP upgrade as a substitute for a universe change (feed still
  matters for H4 quotes; it does not revive the scalp).

---

## 11. Architectural map (after this change)

```
Discord / trending / research ──► watch (telemetry only in observe)
RS ratings / dollar volume   ──► H4 universe ──► h4_screen (lab)
                                      └──► ai_h4_paper (off) ──► trader

trader: observe  → no new buys, flatten leftover scalps
        h4_swing → hard 2% stop, no shelf, survive EOD/SOD
        scalp_legacy → frozen path, not default

lab (mini, 16:05): eod.py · thesis_screen late · h4_screen · daily_learn
decision metric:   live-equivalent R  and  MFE − spread
unit:              session, not admission
```

Config that is **not** a product (do not “fix P&L” by turning these):
heat_min 40, synth_stop 5%, local_trail give 0.10R, IEX, 2 slots.
They remain on disk for `scalp_legacy` and for shadow counterfactuals.
They do not fire while `desk_product=observe`.

---

## 12. Operator commands

After hours (brackets will not accept; do not place paper orders):

```bash
# laptop — no Alpaca keys needed
.venv/bin/python tools/after_hours_smoke.py

# mini — daily bars + H4 screen + EOD, still no orders
.venv/bin/python tools/after_hours_smoke.py --lab
```

```bash
# after pull on the mini — stack reload, not full Desktop session
./trading restart

# confirm product
python3 -c "import json; print(json.load(open('config/bot_config.json')).get('desk_product'))"

# lab (venv — required)
.venv/bin/python tools/eod.py --days 10
.venv/bin/python tools/h4_screen.py --days 20
.venv/bin/python tools/thesis_screen.py --days 1 --horizon-min 60 --slices late --flatten-et 15:50
```

Do not set `ai_h4_paper=true` or `desk_product=h4_swing` without a
gate-1 PASS in `ai_reports/screens/` and an explicit operator OK.

---

## 13. After-hours lab (2026-08-21, 17:31 ET)

Market closed. No orders. Mini `--lab` on 20 liquid names (RS file
absent on the mini — fallback, **not** an RS PASS):

| | |
|---|---|
| hold / stop | 2d / 2% |
| n | 232 swings, 20/20 names had bars |
| net median vs cash | **−0.56%** |
| vs SPY | −0.28% 0.8σ |
| sessions | 10/19 positive, p=0.500 |
| verdict | **FAIL** |

Buying megacap opens and holding two days with a 2% stop is SPY minus
stop/haircut drag. H4 still needs a **setup** on the liquid universe,
not a blind open. The pipe works after hours; this slice is not a trade.

## 14. Evidence this file is allowed to claim

- 2026-08-21 session: 49 closed, 47 local_trail, win 34.7%, paper −1.56 R.
- Live-eq −3.98 R; median MFE−spread −0.032 R; go-live 2/10.
- Spread-k grid: every overlay = baseline −$13.36, do_not_promote.
- Late 60m 8/14–21: FAIL on session test.
- Watchdog used system jobs that discarded stdout; thesis was not in
  the EOD path until this redesign.

Anything not in that list is a hypothesis for the lab, not a finding.
