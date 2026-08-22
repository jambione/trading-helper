# Handoff for Claude — 2026-08-21 night (into the weekend)

You have no access to the Grok/operator sessions that produced this.
It is self-contained. **Challenge anything.** Claims carry evidence.
Do not restore auto-buy because a dashboard looks quiet.

**Git (mini = origin = MacBook):** `master-mac` @ `d31a373`
*Split ratchet stomps from harvest holds; screen daily, do not arm.*

**Live book:** `desk_product=observe` — watch, shadow, ghost chips, **no
auto-arms**. `ai_h4_paper=false`. `ai_late_hold_paper=false`. Paper Alpaca.
Host: Mac mini (`trading.jbrasfield.com`). MacBook is the git/deploy box
(no Alpaca keys).

**Read next:** [docs/PROFIT_REDESIGN.md](docs/PROFIT_REDESIGN.md) (H4
architecture, written before the operator pulled the thesis back to the
ratchet). This file is **more recent** on what the operator actually
wants tested.

---

## 0. One page

The operator's **intended product** is not H4 and not “wait two weeks
then buy.” It is:

> **%R / RSI only need to be directionally right. The 0.10R working
> shelf (ratchet) is the profit capture.**

Grok treated the shelf as the thing that *killed* the hold (86s exits,
MFE &lt; spread). The operator says that *is* the system, and Friday
was a **frozen broken shelf** (stomp on the print) they were told not
to fix so we could “collect data.”

**Both can be true at once:**

- Instant kills (hold &lt; 10s) are **not** a test of direction. They
  are ~**2% wins** over 48 trades. Geometry bug / freeze.
- Thursday 8/20 **harvest** (hold ≥ 30s) was **+0.65 R** — the day they
  remember.
- **Eight sessions** of that harvest slice: **2/8 green, p=0.97, FAIL.**
  Dropping stomps does not flip the week.

**Live config stays observe.** Do not arm Thursday settings, spread-k,
late-hold, or H4 paper. Next lab is `tools/harvest_screen.py` after
each close (watchdog 16:05). Kill bar unchanged: session majority,
MFE − spread &gt; 0, not one green Thursday.

Grok's odds on **H4-as-coded** (liquid open, 2-day, 2% stop) producing
profit in a couple of weeks: low. Said that out loud. Operator has
more faith in the **ratchet + indicators** than in H4. Respect that;
do not cheerlead a PASS.

---

## 1. Who believes what

| Party | Thesis | Status |
|---|---|---|
| Operator | Directional RSI/%R (need not be perfect realtime) + ratchet harvests the run | **Primary.** Not armed. Screen: harvest_screen. |
| Grok 8/21 evening | Scalp family dead; H4 liquid multi-day | Architecture shipped; **side log**, not the operator's plan |
| Claude 8/20 | Honest measurement; H1 dead; late 60m then a candidate | Late **FAIL** once 8/21 + session test |

Do **not** permute RSI/%R/RVOL/zone on the squeeze list again as a
*timing* overlay (H1 FAIL vs eligible-within). The operator is **not**
asking for another heat grid. They are asking: **if the shelf is not
stomping the print, does the harvest slice pay?**

---

## 2. Live config (mini `config/bot_config.json`)

| knob | value | meaning |
|---|---|---|
| `desk_product` | **observe** | `should_arm_buy` / `place_scaled_entry` → `desk_observe` |
| `ai_h4_paper` / `ai_h3_paper` | false | |
| `ai_late_hold_paper` | false | Operator refused sitting out 9:30–14:00 |
| `ai_watch_start_time` | **04:00** ET | Shadow/seed from premarket; **buys still RTH** and still observe |
| `ai_trade_style` | Observe | Cosmetic |
| heat 40 / 5% 1R / give 0.10R / min_give $0.06 / IEX / 2 slots / 15:50 | frozen | Leftover scalp only; do not retune live |
| `ai_local_trail_give_spread_k` | **0** (absent → default 0) | Mini had k=1 **unsaved from dashboard** 8/21; deploy **overwrote** it. Do not turn on without a PASS |

Tunables live in `bot_config.json`. `load_config()` does **not** read
`signal_engine.env`. Trader reloads config each poll.

**Deploy:** MacBook commit → `./scripts/deploy_mini.sh` (stack restart,
not full Desktop). Mini cannot `git push`. SSH restart from the laptop
**logs Claude CLI out of Keychain**; Grok research is fine. Restore
Anthropic from a **Terminal on the mini** (`scripts/session.command`).

---

## 3. What is on the dashboard

Same URL: `https://trading.jbrasfield.com` / mini `:8888`.

- Momentum / trending / research / AI Watch: **unchanged as panels**.
- Header badge should read **observe · Trader Bro**.
- **Ghost H4 strip** on the Watch panel (`desk_lab.py`): $10+ names,
  % from **today's open**, 2% stop mark, cheap vs wide spread, vs SPY.
  **Sim, not a fill.** Click chip → select ticker.
- Auto-buy off. Ghost chips are sport while we gather tape from 04:00.

Hard-refresh after deploy (Cmd+Shift+R).

---

## 4. Evidence you must not mix

### 4a. Stomp vs harvest (`tools/harvest_screen.py`, mini, 8/12–8/21)

| bucket | n | win | sum R | median hold |
|---|---:|---:|---:|---:|
| ALL | 325 | 27% | −14.1 | 103s |
| **STOMP &lt;10s** | **48** | **2%** | **−4.16** | **8s** |
| FAST 10–30s | 51 | 16% | −4.64 | 18s |
| HARVEST ≥30s | 226 | 35% | −5.30 | 217s |

Thursday 8/20 harvest: **+0.65 R**, n=39, 38% win. Friday 8/21 harvest:
**−0.73 R**. Session sign harvest-only: **2/8 green** (8/12, 8/20). FAIL.

Friday rows with `spread_r`: **inside book** (spread_r &gt; 0.10) median
hold **22s**, 3 stomps. **Outside:** median hold **140s, 0 stomps**.

Friday MFE − spread (n=27): median **−0.032 R**, 30% of rows &gt; 0.

**8/20 vs 8/21 all-trades** (do not call Friday uniquely “3s deaths”):

| | Thu 8/20 | Fri 8/21 |
|---|---:|---:|
| n | 59 | 49 |
| median hold | 85s | 86s |
| &lt;10s | **11** | 5 |
| median R | −0.021 | −0.021 |
| median MFE | +0.039 | +0.038 |
| sum R | −0.33 | −1.56 |

Friday was **frozen** (`git_version` all `226eca8`). Operator identified
shelf on the print **in the morning** and was told not to change it.
Thursday already had stomps; they are **not** “only a Friday bug.” They
**are** a separate bucket from harvest.

### 4b. Go-live / paper lie (8/21 `tools/eod.py`)

49 trades, paper **−1.56 R / −$3.49**, live-equivalent **−3.98 R / −$8.77**.
Median MFE − spread **−0.032 R**. Go-live 2/10 sessions. **Not met.**
Paper under-charges the exit (~0.018 R/trade measured).

### 4c. Late 60m (was the only PASS; died)

8/14–20 late 14:00–15:30 hold to 15:50 looked PASS (n=136, 2.3σ). With
8/21 and **session** independence: **FAIL** (n=143, 4/6, one day 71% of
n). Chase **0/6**. Research list 60m FAIL after 20 bps. Do not enable
`ai_late_hold_paper`.

### 4d. H4 liquid fallback (after hours 8/21)

No `rs_ratings.json` on the mini. `h4_screen` used 20 megacaps, 2d / 2%
stop: **FAIL** (net −0.56%, vs SPY −0.28% 0.8σ, 10/19 sessions). Blind
open-and-hold is not an edge. RS screener **off**. Do not turn
`ai_h4_paper` on.

### 4e. Claude 8/20 baseline (still true as measurement)

- Simulator had **never traded** (`no_rsi_data` every bar). Fixed.
  Empty sweep ≠ “no overlay beat live.”
- H1 cool pullback vs eligible-within: **FAIL**. Do not permute that
  family as a 30–60m *timing* screen.
- Legacy WITHIN was hindsight-loaded (pre-list run-up). Use
  **eligible-within** + **session** sign + max-day-share 50%.
- Heat ≥ 40 excludes the only cool 30m bucket that looked green.

### 4f. Research never got a fair trial

Fills are `entry_path=watch`. Afternoon notes (IOVA/TEM) are not the
86s book. Do not use scalp outcomes to score idea quality.

---

## 5. Code map (what shipped after 4d71ef6)

| Commit | What |
|---|---|
| `b281ae8` | `desk_product=observe`, `desk_h4.py`, `h4_screen.py`, fill-truth `sell_time`, instrumentation required-fields, EOD watchdog |
| `4b57af7` | Watch/shadow **04:00** ET |
| `51319a6` | Ghost H4 chips on the dashboard (`desk_lab.py`) |
| `d31a373` | `harvest_screen.py` + EOD job |

Arm veto: `desk_product.py` → `ai_entry_watch.should_arm_buy` and
`ai_positions.place_scaled_entry`. Partial test cfg **without**
`desk_product` still behaves as `scalp_legacy` (see `tests/conftest.py`).

H4 positions skip the 0.10R shelf and survive EOD/SOD
(`liquidate_all(except_symbols=…)`). Unused while paper is off.

---

## 6. Lab on the mini (venv — system python3 has no Alpaca)

Watchdog ~16:05 ET appends stdout to `logs/learn.log`:

```bash
.venv/bin/python tools/eod.py --days 10
.venv/bin/python tools/thesis_screen.py --days 1 --horizon-min 60 --slices late --flatten-et 15:50
.venv/bin/python tools/h4_screen.py --days 20
.venv/bin/python tools/harvest_screen.py --days 10
.venv/bin/python tools/after_hours_smoke.py          # no keys needed
.venv/bin/python tools/after_hours_smoke.py --lab    # mini, no orders
```

**PASS rules (do not weaken):** n≥30, ≥5 sessions, net after 20 bps &gt; 0,
paired dart ≥2σ, session sign p≤0.05, no session &gt;50% of sample.
Harvest also needs **median MFE − spread &gt; 0**. A green Thursday is
not a pass. EMPTY RUN is not a verdict.

MacBook cannot fetch bars. Screens that need Alpaca: mini only.

---

## 7. Next steps, in order (for you)

1. **Do not arm.** Observe through Monday at least. Success = **0 new
   watch BUYs**, `arm_why=desk_observe` once the watch is polling,
   04:00 shadow, ghost chips on $10+ names.
2. After close, read `logs/learn.log`: harvest stomp vs 30s+, eod
   live-eq, h4_screen (fallback until RS exists).
3. **Operator thesis next, not H4 paper.** If harvest + outside-book
   (spread_r ≤ 0.10) ever PASSes session-level **and** MFE − spread &gt; 0,
   *then* discuss one paper flag: shelf **outside the spread** (give ≥
   k×spread, seed under the bid — not on last). Operator confirms
   before it fires. Grok overwrote mini's unsaved `give_spread_k=1`.
4. **Do not** enable `ai_late_hold_paper`, `ai_h4_paper`, heat/RSI
   grids, or `desk_product=scalp_legacy` because “Thursday felt better.”
5. RS file is **missing** on the mini. Optional later: one ratings
   build so H4 lab is not megacap fallback. Not a reason to trade.
6. If a BUY prints Monday: **bug**, stop, do not “tune heat.”

---

## 8. Glossary

- **Ratchet / working shelf:** software stop ~0.10R under the print
  (`ai_local_trail_*`). Raise-only. Print through → `local_trail`.
  Broker stops are off; this *is* protection. Operator: this is how
  profit is captured. Grok: this is also how 86s deaths happen when
  give &lt; spread.
- **Stomp:** hold &lt; 10s, ~2% win. Shelf on/through the tape.
- **Harvest:** hold ≥ 30s — the slice that was green Thursday.
- **H3:** overnight/gap. Not built. Flag off.
- **H4:** 2-day hold, 2% hard stop, liquid/RS universe, no 86s shelf.
  Screen only. Paper off.
- **RS:** IBD-style rank vs SPY (~12m). File `rs_ratings.json` — **not
  on the mini.**
- **Eligible-within:** dart on the same name only *after* first watch
  that day. Not the old WITHIN (hindsight).
- **Session:** unit of independence. Admissions in one afternoon are
  one observation.

---

## 9. Weaknesses (say these if you promote anything)

- Spread on **outcomes** only from 8/21 (n=28 of 49). Thursday harvest
  has **no** MFE − spread.
- Sim (`optimize_rstop`) **does not charge spread**; Friday spread-k
  grid was **identical to baseline** — not a disproof of k×spread.
- Paper cheaper than live ~0.02 R/exit.
- IEX thin names, ask-as-%R (EXH 100 off REST ask). Operator accepts
  directional (not exact) indicators; **prints** still cannot be fake.
- Ghost H4 is mark-to-open on the **momentum list**, not the RS book.
- Observe + 04:00 watch has **not** had a Monday yet (written Friday
  night / weekend).

---

## 10. Operator constraints (do not violate)

- Do not change live knobs during a session “to collect cleaner data”
  without them. They already sat on a broken Friday because they were
  asked to freeze. If the shelf is stomping, **that day’s harvest
  numbers are mixed with a bug** — say so, as this file does.
- They find a dead watchlist boring; ghost chips exist so observe is
  not a blank board. Do not turn chips into orders.
- They have **more faith in the old parts** than in H4. If you only
  push H4, you are arguing with the person who owns the account.

```
Mini:  .venv/bin/python tools/harvest_screen.py --days 10
       .venv/bin/python tools/eod.py --days 10
Dashboard: observe · ghost H4 strip · no auto-buy
```

— Claude 8/20: measurement, H1, late candidate.
— Grok 8/20–21: desk_null, late FAIL on session test, observe, H4 lab,
  ghost book, harvest split.
— Operator 8/21 night: ratchet is the product; Friday freeze ≠ fair
  test; Thursday harvest is the memory to respect, not to promote.
