# Historical simulator plan (operator 10/10: "time is of the essence")

Goal: answer entry/exit questions on 60-120 HELD-OUT past sessions this week instead of waiting 6-9 weeks for fresh ones.

Approach: run the LIVE code (tools/replay_session.py) on days that were never recorded, by synthesizing the recording:
the watchlist comes from a rebuilt historical universe, engine rows from the replay's existing SynthEngine path (engine
evaluate_state on IEX premarket + SIP session 1-minute bars), prices/costs from archived SIP data. No new entry/exit port.

## Workstreams (parallel)

| WS | Owner | Deliverable | Must NOT |
|---|---|---|---|
| U universe | agent | `tools/studies/hist_universe.py`: per day, the names live would have admitted and when (movers / tight / momentum rules from the live admission code, RAW prices for the $ floor), JSON per day; recall/precision vs `ai_reports/admit_range.jsonl` on 9/24-10/9 per source | compute any forward return / P&L on any day |
| R synthetic replay | agent | `replay_session.py --synthetic-universe FILE` for a day without a recording; fidelity vs the normal replay on 9/24-10/9 (same recorded watchlist fed through the synthetic path): trade overlap, trades/day, net bp/trade | look at outcomes on days before 2026-09-24 |
| P prereg | main session | `docs/studies/hist_sim_prereg.json`: held-out window, variants (live presquare entry x %R / %R+RSI entry x np_lob / SuperTrend / leash-off exits), statistic, pass bar, fidelity gate; skeptic before any held-out run | — |

## Gates, in order
1. U recall on recorded days stated (sources not reconstructable: trending/agy/xai/bb_live are named, not guessed).
2. R fidelity on recorded days: synthetic vs recorded replay, stated before the held-out run; a fidelity floor is in the prereg.
3. Prereg committed + skeptic, THEN the held-out batch (mini, nice'd, lanes), ONE read.
4. A pass goes to a live paper A/B; the fresh-session tests become confirmation.

Held-out window: sessions before 2026-09-24 (no recordings, never run with this strategy). Exact window in the prereg.
