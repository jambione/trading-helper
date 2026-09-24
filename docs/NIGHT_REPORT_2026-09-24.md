# Night report — 2026-09-24 / deploy for 2026-09-25

## Done (in order)

| Step | Result |
|---|---|
| 0a Config reverts | spread 0.2, decision 15s, stale 15s, dead-seat 30s — pulled on mini at 16:05, no restart |
| 0 Merge `runway-study` | Diff beyond docs/studies = replay-tool only. Full suite: **3608 passed, 1 xfailed** |
| 1 Replay harness | `tools/rehearse_snap.py` + concurrency / opens-per-10m in `rehearse_open.criteria` |
| 2 Session recorder | `session_recorder.py` → `ai_reports/sessions/YYYY-MM-DD/`; hooks on dashboard prints + alpaca 429/batches |
| 3 Ticker leak + batching | Orphan `src=book` expires when not in `_committed_symbols`; L2 subscribed path batch-only |
| 4 One price, one clock | `promote_stream_src_if_print_fresh` always consults `live_print`; dead-seat re-stamps; 429 hold in `quote_is_live`. Tests: CDNA, ECO |
| 5 `valid_tickers` deploy | Untracked + gitignored; seed `valid_tickers.default.txt`; `deploy_mini.sh` no longer trips on desk rewrite |
| 6 admit_arm_gates | `ai_watch_admit_arm_gates=true`, `ai_watch_movers_min_rvol=1.0` |
| 7 Book server | `book_server.py`, mode **shadow** (default live config). Rank = runway + closeness to −50; pace soft-cap ~4×; no day-change kink (name-quality correction) |
| 8 Legacy arms | `legacy_arms.py` registry of switched-off arm settings; mid_rise early-return already isolates live. Unread keys removed from bot_config. Full body extraction deferred to keep replay identical |
| 9 Exits | **No changes.** Ratchet study summary below |
| 10 Deploy | See end of report |

## Ranking note (brief vs correction)

The pasted night brief still said day-change cap ~8% and pace cap ~2.5×. Commit `c6a816e` (Jonathan, 15:59) updated the brief from the symbol-day review: **no day-change kink**, pace **soft-cap ~4×**. Implementation follows that correction.

## Ratchet / local_trail study (for a later exit session)

**Verdict:** the live-ish trail (**arm_r 0.06 + peak_give 0.35%**) is the **most aggressive cutter** — ~150 cut-short vs ~35 saved on 414 trades. Prefer trail as backup leash: **raise arm_r toward 0.25**, keep left_overbought as thesis, EOD 15:50, no_progress off. Do not change exits until entry/book changes are proven.

## Retired / removed settings

**Removed from live `bot_config` (unread):**
- `ai_watch_momentum_require_flag`
- `swing_min_eps_growth`, `swing_max_eps_growth`
- `tv_chart_url`

**Owned by switched-off arms (still in defaults; live mid_rise does not use for buy):** see `legacy_arms.LEGACY_ARM_SETTINGS`.

**Book server retires when promoted to live:** soft-seed momentum/Discord intake, heating RVOL supply lanes as intake (pace becomes rank-only). Listed in `book_server.RETIRED_WHEN_LIVE`.

## Pass bar (how to score)

```bash
# Observed ledger (full day)
.venv/bin/python tools/rehearse_open.py --day 2026-09-24 --start 09:40 --end 15:50 --step 10

# Snapshot archive (today starts ~13:07)
.venv/bin/python tools/rehearse_snap.py \
  --snapshots ~/session_snapshots/2026-09-24 --start 13:10 --end 15:50 --step 10
.venv/bin/python tools/rehearse_snap.py \
  --snapshots ~/session_snapshots/2026-09-24 --start 13:10 --end 15:50 --step 10 \
  --simulate-one-clock
```

Headline targets: ≥1 open ≥80% of session, ≥2 open ≥50%, book≥10 by 09:40, ≥6 armable, data-blocked <10%, ≥1 open / 10 min, net P/L ≥0 per day without worse R/trade than baseline.

## Verify after kickstart

- `agy_auth=ok` in `logs/ai_trader.log`
- `[SNAP] rebuilt` in `logs/dashboard.log`
- Fresh `signal_state.json`
- Recorder writing under `ai_reports/sessions/YYYY-MM-DD/`
- Shadow log `ai_reports/book_server_shadow/YYYY-MM-DD.jsonl`
- `tools/live_check.py`
