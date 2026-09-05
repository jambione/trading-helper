# Decision ledger (Package 1 — observe-only)

**Behavior-neutral.** Same arms, same refusals, same trail. This package only
adds a single append-only ledger so “why didn’t X arm?” is answerable without
grepping `shadow.jsonl` + `events.jsonl` + the watch file.

Live product unchanged: stream-fresh tape → EXH rising + RSI cool (≤60) +
mistimed/soft_ob → marketable local-stop → ratchet. Strength/burst stay OFF.

## Where it writes

Day-split JSONL (ET calendar date):

```text
ai_reports/decision_ledger/YYYY-MM-DD.jsonl
```

**Primary hook:** `ai_positions.log_shadow_sample` — one row per watched
symbol per `poll_once` (densest existing path). Also mirrors selected
`log_event` kinds: `entry_ok`, `entry_fail`, `local_trail`,
`local_trail_working`, `desk_flatten`, `unprotected_flatten`, `arm_recheck`.

Write errors are swallowed (fail-open). Trading never depends on the ledger.

## Row fields

| Field | Meaning |
|-------|---------|
| `ts` | Unix time |
| `symbol` | Uppercase ticker |
| `stage` | `watch` \| `arm` \| `entry` \| `trail` \| `exit` |
| `tape_src` | `stream` / `stale_tape` / `rest` / … |
| `tape_age_sec` | Print age at sample |
| `exh_state` | Exhaustion state string (when known) |
| `pctr_rising` | Bool or null |
| `cm_rsi` | CM RSI-2 level |
| `cm_rsi_peak` | Confirm-window peak when present |
| `macd_narrowing` | Bool or null (gap falling) |
| `macd_bearish` | Bool or null |
| `arm_ok` | Bool or null |
| `arm_why` | Raw gate string (keep this — bucket is a view) |
| `arm_bucket` | One of eight veto buckets (null when `arm_ok` true) |
| `git_version` / `config_fp` | Same regime stamps as shadow/events |

## Veto buckets (`desk_arm_buckets.arm_bucket`)

| Bucket | Examples |
|--------|----------|
| `readiness` | `tape_only`, `stale_quote`, `need_stream`, `no_macd_data`, `macd_not_realtime*` |
| `exh` | `exh_falling`, `no_exhaustion*`, `exh_rising_required` |
| `rsi` | `rsi_extended`, `rsi_not_rising`, `cm_rsi*` band fails |
| `macd_dir` | `macd_bearish`, `macd_gap_narrowing`, `macd_gap_*` |
| `heat` | `mistimed_heat`, `soft_ob`, `late_heat`, `cheap_ob*` |
| `spread` | `spread`, `wide_spread` |
| `zone` | `above_zone`, `below_zone`, `no_structure`, `wait_setup` |
| `other` | anything else (raw `arm_why` still on the row) |

## How to read a morning

```bash
.venv/bin/python tools/decision_ledger_day.py
.venv/bin/python tools/decision_ledger_day.py --day 2026-09-05
```

Prints a bucket histogram for 09:30–11:00 ET and top symbols stuck in
`readiness` / `heat` / `exh` / `macd_dir` / `rsi`.

One-liner for a symbol:

```bash
rg '"symbol": "NIKI"' ai_reports/decision_ledger/2026-09-05.jsonl | tail
```

Look at `arm_why` + `arm_bucket` on the last `stage=arm` rows before 11:00.

## What this is not

- Not a new entry engine.
- Does not change `should_arm_buy`, knobs, seed admission, or trail math.
- Does not replace `shadow.jsonl` (ledger is additive).
