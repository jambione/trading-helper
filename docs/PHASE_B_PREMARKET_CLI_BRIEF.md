# PASTE-READY — Grok CLI: Phase B premarket session (Hybrid C)

**Status (2026-09-17):** Plumbing shipped / dry default in `docs/PHASE_B_PREMARKET.md`. Paper/dry only — **OFF by default**. Do **not** enable live Phase B buys. Do **not** change RTH Plan A arms, seats, or EOD/SOD behavior except where an explicit `phase_b` flag scopes the path.

Repo: `jambione/trading-helper` · branch: `master-mac`  
Live mini: `/Users/jambimac/repo/trading-helper`  
Design lock: `docs/PHASE_B_PREMARKET.md` (strategy Hybrid C + session/orders + arm strip + universe + score/pass gates)

**Ship order:** session flag + clock → Finnhub-last price path → limit entry + working-sell exit → momentum-first universe/book → ledger/scoreboard.  
**Restart:** after close / off-hours only. Never mid-RTH for this pack.

---

## Why

RTH is a fixed ~6.5h ceiling. Phase B adds ~04:00–09:30 ET of *tradable* time for profit-margin via extra armable minutes — **built around** Alpaca extended-hours **limit-only** orders and free Finnhub last (no SIP/NBBO assumption).

**Not Plan B burst.** Plan B = premarket universe → RTH entry at RSI≥70 (stays gated). Phase B = actually trade premarket.

---

## Goals

1. Explicit `phase_b` session that can arm/enter **only** 04:00–09:20 ET, flatten 09:25→flat by 09:28, **no RTH handoff v1**.
2. Price/arm/stop/limit anchored on **Finnhub stream last** (refuse stale prints); IEX ask not used for Phase B arm.
3. Entry = DAY limit + `extended_hours=True`; TTL cancel → `phase_b_unfilled`.
4. Exit = working limit sell (Phase B only) + hard −5% stop on last; chase knobs reused.
5. Universe: **momentum seed primary**, then movers, mention burst, trending; **keep sub-$5** (desk `ai_watch_min_price` ~$2); 4–6 seats; max 1–2 concurrent opens; isolated from RTH elite pins.
6. Stripped arm: EXH 40–70 rising; RSI block only falling when RSI>10; no cm_rsi_max / mistimed / soft_ob / MACD.
7. Observe scoreboard + pass/fail bars from the design doc (separate from RTH Phase 1/2).
8. Fail-open / default-off — RTH Plan A unchanged when Phase B knobs are false.

## Non-goals

- Not enabling Plan B burst live (`ai_strength_trade_enabled` stays false)
- Not SIP / not flipping `_feed_kw` to SIP
- Not green catch-up / overtake work
- Not raising RTH position ceiling or sharing elite pin seats
- Not mid-session deploy
- Not auto-PASS → live; live bar is paper PASS + confirm week (human)

---

## Config knobs (add to `config.py` + `SAFE_CONFIG_KEYS`; defaults OFF / safe)

```text
ai_phase_b_enabled = false                 # master — no Phase B buys when false
ai_phase_b_dry_run = true                  # when enabled: log/score path, place nothing (or shadow-only — pick one and document)
ai_phase_b_start_time = "04:00"
ai_phase_b_entry_cutoff = "09:20"
ai_phase_b_flatten_start = "09:25"
ai_phase_b_flat_deadline = "09:28"
ai_phase_b_max_seats = 6
ai_phase_b_max_open = 2
ai_phase_b_min_price = null                # null → use ai_watch_min_price (~2.0); do NOT hardcode 5.0
ai_phase_b_entry_limit_ttl_sec = 45.0
ai_phase_b_entry_limit_pad_pct = 0.15
ai_phase_b_entry_limit_pad_max_px = 0.05
ai_phase_b_hard_stop_pct = 5.0
ai_phase_b_print_max_age_sec = 15.0
ai_phase_b_exh_min = 40.0
ai_phase_b_exh_max = 70.0
ai_phase_b_require_exh_rising = true
ai_phase_b_rsi_block_falling_above = 10.0  # direction-only; no cm_rsi_max
ai_phase_b_arm_confirm_ticks = 1
ai_phase_b_working_sell = true             # scoped to phase_b positions only; do not flip global ai_premarket_working_sell for RTH
ai_phase_b_chase_step_sec = 2.5            # mirror ai_premarket_chase_step_sec
ai_phase_b_max_exit_slip_r = 0.25
ai_phase_b_no_rth_handoff = true
ai_phase_b_ledger_enabled = true
```

Wire into `bot_config.json` only as **explicit false/dry** defaults if you write the file — never leave enabled true.

Reuse existing helpers where possible: `ext_hours_now()`, `buy_limit_at_price`, `working_sell_replace` / `_rest_working_sell`, `ai_premarket_*` tests as patterns — but **scope by position tag `phase_b`**, do not turn on global premarket working sell for RTH flats.

---

## Part 1 — Session flag + clock gates

**Files (likely):** `ai_entry_watch.py` (`watch_session_active` / `trading_hours_active` / buy skip), `ai_positions.place_scaled_entry` (today refuses when market closed), `ai_trader.py` (SOD/EOD — do not break).

### Behavior

| Clock (ET) | Phase B |
|------------|---------|
| before 04:00 | off |
| 04:00–09:20 | arms + new entry limits if `ai_phase_b_enabled` |
| 09:20 | cancel resting Phase B entry limits; no new entries |
| 09:25 | flatten starts (working sell / chase) |
| 09:28 | must be flat; log fail if not |
| 09:30+ | SOD wipe of any leftover = **score failure** for that lot; still must not hand off into Plan A when `ai_phase_b_no_rth_handoff` |

### Implementation notes

- Add `phase_b_session_active(now)` and `phase_b_allow_entries(now)` helpers (testable).
- Bypass the closed-market refuse in `place_scaled_entry` **only** when: Phase B enabled, dry_run rules satisfied, `phase_b_allow_entries`, and order will be limit+extended_hours.
- Tag every Phase B position / order note with `phase_b=true` (or source/session field) so exits/flatten/ledger can filter.
- **Do not** call `handoff_working_sell_to_rth` for Phase B lots when `ai_phase_b_no_rth_handoff`.

### Tests

- Clock boundaries 03:59 / 04:00 / 09:19 / 09:20 / 09:25 / 09:28 / 09:30
- `ai_phase_b_enabled=false` → zero Phase B entry attempts
- RTH `trading_hours_active` unchanged when Phase B off

---

## Part 2 — Finnhub-last price path

**Files (likely):** stream latest price helper (HANDOFF noted `finnhub_stream.get_latest_price`), arm price in `ai_entry_watch`, limit anchor in `ai_positions._entry_limit_price` / `_marketable_local_limit`.

### Behavior

- Phase B arm + limit anchor + hard stop mark = **stream last** only.
- Refuse arm if last missing or age > `ai_phase_b_print_max_age_sec` (15s).
- Do **not** use IEX ask as Phase B arm/limit anchor (ask often stale/0 overnight).

### Tests

- Stale last → refuse with stable reason token (`phase_b_stale_print` or similar)
- Fresh last → limit px = last * (1+pad) capped by pad_max_px

---

## Part 3 — Limit entry + working-sell exit

### Entry

- Always `LimitOrderRequest`, `time_in_force=DAY`, `extended_hours=True` for Phase B.
- TTL = `ai_phase_b_entry_limit_ttl_sec` (45); on expiry cancel → ledger `phase_b_unfilled`.
- If `ai_phase_b_dry_run`: do not submit broker orders; still log would-be limit + score hooks.

### Exit

- On fill: place/maintain working limit sell via existing replace/chase pattern; chase every `ai_phase_b_chase_step_sec`; respect `ai_phase_b_max_exit_slip_r` until flatten window, then allow one-step wider to meet flat deadline.
- Hard stop: last ≤ entry * (1 - hard_stop_pct/100) → aggressive working sell / flatten.
- Flatten 09:25–09:28: cancel entries, chase exits, count `flat_on_time`.

### Tests

- Extend tests in `tests/test_premarket_knobs.py` style for Phase B–tagged positions
- Dry run places nothing on broker mock
- TTL cancel emits unfilled reason

---

## Part 4 — Universe + book (momentum first)

**Files (likely):** soft-seed / momentum seed paths in `ai_entry_watch.py`, movers/trending seeds, seating caps.

### Behavior

- Phase B book separate from RTH elite pins / scout seats (own list or tagged seats).
- Admit priority: **momentum → movers → mention burst → trending**.
- **Off v1:** research / seed_rank into Phase B book.
- Min price: `ai_phase_b_min_price` or `ai_watch_min_price` — **do not skip sub-$5**.
- Max seats 4–6; max concurrent opens 1–2 (`ai_phase_b_max_open`).
- Demote if no fresh last for ~60–90s after seat (shorter patience than RTH stale-stay).
- After 09:25: drain-only (no new admits).

### Tests

- Momentum candidate admitted before trending when seats tight (priority)
- Sub-$3 name not rejected solely for price if ≥ min_price
- Open #3 refused when max_open=2

---

## Part 5 — Arm strip (Phase B only)

When deciding Phase B arm (not Plan A):

- EXH in `[ai_phase_b_exh_min, ai_phase_b_exh_max]` and rising if required
- RSI: block only if falling AND rsi > `ai_phase_b_rsi_block_falling_above`
- Confirm ticks = `ai_phase_b_arm_confirm_ticks` (1)
- Explicitly ignore Plan A mistimed / soft_ob / cm_rsi_max / MACD for this path

### Tests

- RSI 65 falling → block; RSI 65 rising → allow (strip)
- EXH 35 → block; EXH 55 rising → allow
- MACD off does not refuse Phase B arm

---

## Part 6 — Ledger + scoreboard

Mirror `admit_ledger` / `decision_ledger` style:

- `ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl` — arm, limit, fill, unfilled, exit, flatten, slip, mfe hooks (fail-open writes)
- `tools/phase_b_scoreboard.py` — reads ledger (+ outcomes if needed); prints pass/fail vs **pre-registered** bars in `docs/PHASE_B_PREMARKET.md`:

Pass (all): `n_fills≥25` (or ≥5 days×≥3 fills); `fill_rate≥40%`; expectancy after **0.50%** RT friction > 0 (or sum PL%>0); med MFE≥0.10R; `flat_on_time_pct≥95%`; med entry slip vs last ≤ +0.40%.

Fail-fast: win%-only; any SOD wipe of phase_b lot; peak opens >2; retune on scoring window.

### Tests

- Synthetic ledger → PASS and FAIL fixtures
- Friction constant documented in script / doc; changing it is a deliberate commit

---

## Ship checklist

1. Unit tests green for Parts 1–6
2. Defaults: `ai_phase_b_enabled=false`, `ai_phase_b_dry_run=true`
3. Manual: with enabled+dry on mini off-hours, confirm ledger lines and **zero** broker orders
4. Update `docs/PHASE_B_PREMARKET.md` status line to “plumbing shipped / dry default”
5. Commit message: `phase_b: Hybrid C paper plumbing (session, last-price limits, momentum book, scoreboard) — default off`
6. Do **not** push-enable live; do **not** flip RTH knobs

## Out of scope follow-ups (do not do in this CLI run)

- Live enable / size-up
- SIP trial
- Plan B burst enable
- Green catch-up on Phase B
- Research/seed_rank into Phase B universe

---

## Done means

- Design behaviors above are implemented behind OFF/dry defaults
- RTH Plan A path unchanged when Phase B disabled
- Scoreboard can run on dry ledger and emit PASS/FAIL/NO DECISION
- Short note in PR/commit body pointing at `docs/PHASE_B_PREMARKET.md` freezes
