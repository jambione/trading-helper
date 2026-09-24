# Grok CLI brief — night of Thu 2026-09-24 (start after 16:05 ET)

You are implementing tonight's plan for the trading-helper paper desk. Work on the MacBook repo `~/repo/trading-helper`. The live desk is the Mac mini (`ssh mac-mini-away`, same repo path; always pipe ssh with `< /dev/null`).

## Read first (in this order)
1. `docs/PLAN_2026-09-25.md` — the plan and pass bar (branch `runway-study`, local on this MacBook).
2. `git show origin/replay-tool:docs/HANDOFF_2026-09-24.md` — live state, rules, the three replay-tool commits.
3. `docs/HOLISTIC_BOOK_REVIEW_2026-09-24.md` — where names die, what to collapse (branch `runway-study`).
4. `docs/RUNWAY_STUDY_2026-09-24.md` + `tools/studies/mid_rise_runway_study.py` — the ranking.
5. `docs/RATCHET_STOP_STUDY_2026-09-24.md` and `docs/SESSION_RECORDER_DESIGN.md` if present.

## Hard rules
- No paid data or services. Free feeds only (IEX, Finnhub 50 slots, SIP 15+ min delayed, one shared Alpaca budget ~200 req/min).
- Do NOT restart or deploy before 16:05 ET. Never start desk processes or run logins over ssh. Restart only via `ssh mac-mini-away 'launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk' < /dev/null`.
- Repo is public: never commit secrets.
- Never loosen an intended gate: −50 cross wait, gap-down, real wide spread, $20/$100 band, cooldowns, loss brake. Do not change the arm (fast %R −50 cross, mid_rise). Do not add new hard gates. Do not raise max positions.
- Exits: no no_progress flatten; left_overbought is the thesis exit; trail is a backup leash; EOD flatten ~15:50. Change exits only if the ratchet study is clear on both train and test halves.
- Evidence before changes: every step verified on the replay, not one day's P/L.
- "Ship" = commit, push, `git pull --ff-only` on the mini, AND kickstart. The mini's tree may show `valid_tickers.txt` modified (the desk rewrites it); don't use deploy_mini.sh's dirty guard, pull + kickstart instead.
- The 16:02–16:05 config reverts are handled separately. Before you start, confirm on `origin/master-mac` that `ai_watch_max_sip_spread_pct=0.2`, `ai_watch_decision_max_age_sec=15`, `ai_stale_data_max_age_sec=15`, `ai_watch_dead_seat_evict_sec=30`. If not, do them first.

## Goals (the yardstick)
A full, fresh book of quality names from Movers, Trending and Research, seated before the −50 cross; at least one open per ~10 minutes; exits that capture profit; a simpler system than today.

## Build, in order (one commit or small PR per step, each replay-verified)
1. **Replay harness** on the snapshot archive `~/session_snapshots/2026-09-24/` on the mini (`state_snapshots.jsonl.gz`: records `{ts, file, mtime, sha, data}` for movers/trending/signal_state/wb_watchlist/entry_watch_state/admit_funnel/positions/bot_config, plus copied ledgers). Drive the book/admit/arm logic on a simulated clock with a fake broker filling from 1-min bars. Extend `tools/rehearse_open.py` / `rehearse_whatif.py` rather than starting over. Output the pass-bar metrics per 10-min slot.
2. **Session recorder** (per `docs/SESSION_RECORDER_DESIGN.md` if present): append-only JSONL.gz per day of every price update (source, symbol, price, ts), quote failures/429s, source nominations with ts, config fingerprint. Low overhead, no extra API calls. Files outside the git tree.
3. **Ticker-list leak + batching** (handoff Tier 1a/1b): `transcription/wb_watchlist.json` `src=book` rows must expire when they leave the book (TTL tied to seats); batch the dashboard's per-symbol latest-quote calls; log requests per process.
4. **One price, one clock** (handoff Tier 2.1): every book record uses the fresher of engine `live_print` and dashboard quote as `price`/`price_ts`; arm (`ai_watch_decision_max_age_sec`), flatten (`ai_stale_data_max_age_sec`, ai_positions.py ~6210) and evict (`ai_watch_dead_seat_evict_sec`) read that one age. Fix `promote_stream_src_if_print_fresh` (only consults live_print when src != stream). A 429 is not "no data". Keep −50 cross state across evictions. Tests: CDNA, RKLB, ECO cases. Target data-blocked < 10% on replay.
5. **Merge `replay-tool` into `master-mac`** (5cc38ff, 22845f0, 765ca8a) with `ai_watch_admit_arm_gates: true` and `ai_watch_movers_min_rvol: 1.0`. This applies the $20–$100 band to every source, including research (INFQ/CLF hole).
6. **Book server with runway ranking**: supply = Movers + Trending + Research only. One ranked queue: `seat_priority = runway_score + closeness_to_−50`, where runway_score uses day change %, used daily range, EMA9 slope, room below HOD / 30-min swing high (near-HOD scores lower), a morning bump (first 90 min), a small movers bump. Refill seats from the top; short backup queue; start Finnhub/bar warm-up as soon as a source names a ticker. Put it behind a knob with a **shadow mode** (builds its own book, logs would-have-done, no orders) and default it to shadow unless it clearly beats the current book on the replay.
7. **Exits**: apply the ratchet study recommendation only if clear on both halves; otherwise leave exits unchanged and note why.
8. **Deploy**: full test suite green; replay 2026-09-21..24 (ledgers) and the 09-24 snapshot archive against the pass bar, old vs new; commit, push, pull on mini, kickstart. Verify `agy_auth=ok` in `logs/ai_trader.log`, `[SNAP] rebuilt` lines in `logs/dashboard.log`, fresh `signal_state.json`, recorder writing, shadow logging, and run `tools/live_check.py`.

## Pass bar
Book >= 10 by 09:40 · >= 6 armable · data-blocked < 10% · every arm reaches an order · >= 1 open per 10 min.

## Out of scope tonight
Arm changes, new hard gates, premarket scanner rebuild, deleting dead square/MACD code, paid data.

## Report back
Per step: commit SHA, what changed, replay numbers vs baseline (pass bar per slot), tests. Final: mini HEAD, live config diff, what's live vs shadow for Friday, and anything you skipped and why.
