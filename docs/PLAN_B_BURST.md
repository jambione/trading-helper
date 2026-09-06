# Plan B — Burst + RSI-2 (pre-registered trial)

**Status:** code-ready, honesty-gated, **LIVE OFF**.  
**Desk for Tuesday:** stays on **Plan A** (stream-fresh + EXH↑ + RSI≤60 cool + soft_ob/mistimed + local trail). Do **not** enable Plan B for the open.

This document freezes the rule, fill model, and pass/fail bars **before** scoring holdout days. Do not retune thresholds on holdout.

---

## Rule (frozen)

| Piece | Value |
|-------|--------|
| Universe | Premarket mention burst (04:00–09:30 ET) |
| Trigger | First **closed** 1m bar at/after 09:30 with **CM RSI-2 ≥ 70** |
| RSI polarity | **HIGH** (low-RSI arm failed sessions; do not flip) |
| Exit | Desk local trail / ratchet; hard stop **−5%**; no ladder |
| Scale-out | **0** (ladder measured worse at every tier) |

### Known prior (discovery — not holdout)

- Window: **2026-08-24 .. 2026-09-04** (in-sample; commit `b4a4a58`).
- In-sample looked strong (high P(+3 before −3), positive mean/median on ~10 sessions).
- **Why off:** in-sample on the discovery window; fill one bar later collapsed results; pre-registered bar missed; enabling one gate must not place orders.
- This is **not** a new edge to re-discover — it is a candidate waiting on honest latency + holdout.

---

## Dual gates (both must flip for live)

| Knob | Safe default | Meaning |
|------|--------------|---------|
| `ai_strength_trade_enabled` | **False** | Off → module returns without deciding |
| `ai_strength_trade_dry_run` | **True** | On → same row shape as live, **never** calls order APIs |

Live orders require **both**:

```text
ai_strength_trade_enabled = true
ai_strength_trade_dry_run = false
```

Enabling only one is intentionally insufficient (typo / unintended loop protection).

Signal logging (`ai_strength_signal_enabled`, default **True**) writes latency rows only and cannot place.

### Operator switch

**Dry-run only** (log would-place rows to `ai_reports/plan_b_burst.jsonl`, no orders):

```json
"ai_strength_trade_enabled": true,
"ai_strength_trade_dry_run": true
```

**Live** (only after holdout PASS — not for Tuesday):

```json
"ai_strength_trade_enabled": true,
"ai_strength_trade_dry_run": false
```

**Safe after this PR / default:**

```json
"ai_strength_trade_enabled": false,
"ai_strength_trade_dry_run": true
```

Canonical decision log: `ai_reports/plan_b_burst.jsonl`  
Signal latency log: `ai_reports/strength_signals.jsonl`

---

## Latency honesty (non-negotiable)

Every signal / decision row stamps:

| Field | Meaning |
|-------|---------|
| `signal_bar_ts` | Close time of the triggering closed 1m bar |
| `decision_ts` | When the desk decided (poll clock) |
| `latency_sec` | `decision_ts - signal_bar_ts` |
| `fill_model` | Declared model (`signal_close` \| `next_open` \| `next_bar_close`) |

### Fill models

| Model | Definition | Role |
|-------|------------|------|
| `signal_close` | Fill at signal bar close | Optimistic counterfactual only |
| `next_open` | Fill at **next** bar open | **REALISTIC / pass-fail default** |
| `next_bar_close` / `plus_2_open` | +1 close / +2 open | Counterfactual stress |

**Why `next_open` is the pass model:** the rule fires on a *closed* bar. The earliest executable print after that decision is the next bar’s open. Scoring pass/fail on `signal_close` re-creates the optimistic discovery number that already failed when delayed one bar.

Counterfactual scoreboard (same signals, all models):

```bash
python3 tools/plan_b_burst_scoreboard.py --fixture path.json
python3 tools/plan_b_burst_scoreboard.py --signals ai_reports/strength_signals.jsonl --bars-json bars.json
```

---

## Pre-registered pass / fail (HOLD BEFORE NEW DAYS)

### Holdout

- Sessions **after** the discovery window `2026-08-24 .. 2026-09-04`.
- Primary holdout start: **2026-09-08 (Tue) forward**.
- Also include any post-discovery days already on disk that were **not** used to pick the rule.
- **Do not retune** RSI min, stop, trail, or burst window on holdout.

### PASS — all of the following under **`next_open`**

1. **med realized R > 0** on holdout  
2. **sum PL% > 0** *or* per-trade expectancy **> 0.40%** round-trip friction (stated friction)  
3. **n ≥ 30** scored trades (frozen minimum; alternative session floor was considered and rejected for this freeze — use signal count)  
4. **Latency:** median `latency_sec` must be achievable on the live desk poll — **FAIL if median latency > 60s (one bar)**

### FAIL if

- Only **win%** looks good while **med R** or **$ / expectancy ≤ 0**
- Only **`signal_close`** works (next_open / +1–2 bar collapse)
- Dual gates would allow a live place with enable false or dry_run true

Win% is informational. It is **not** the pass metric.

---

## Isolation from Plan A

- Burst evaluate/consider runs in its own block after the Plan A watch loop.
- It does **not** OR into cool / EXH↑ / soft_ob / mistimed / RSI≤60 arm gates.
- Late OB cannot enter Plan A via burst.
- Book/slot cap: `min(ai_strength_max_open, desk effective_max_positions)` — burst cannot be the path that fills the book beyond the tighter cap.

Plan A Tuesday knobs stay untouched by this pack.

---

## Out of scope (this pack)

- Enabling live burst  
- Changing Plan A Tuesday knobs  
- SIP purchase  
- Optimizing RSI threshold on holdout  

---

## After Tuesday

1. Keep Plan A through the session.  
2. Run holdout scoreboard when enough post-discovery signals exist.  
3. Kill / continue / consider enable **only** if PASS under `next_open` with latency honest.  
```
