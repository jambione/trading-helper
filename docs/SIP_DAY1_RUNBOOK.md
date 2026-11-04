# SIP day 1 runbook (operator 2026-11-04: "preparation for when I buy SIP on a live account")

Plan of record: docs/SIP_MONTH_PLAN_2026-10-15.md (one month, keep only if it pays; reversible by design).

## State on master-mac (2026-11-04)
- S1 feed switch: DONE, defaults unchanged. Two knobs: `alpaca_bar_feed` ("iex" -> "sip") and `ai_sip_realtime` (false -> true).
  `alpaca_api.live_data_feed()` is the one feed every live request uses; `alpaca_api.sip_delay_min()` zeroes every SIP delay term
  (ai_movers_sip_delay_min pacing in tight_screener and the admission spread gate; the 16-minute spread and gap reads).
- Guard: tests/test_sip_readiness.py fails if any live module hard-codes IEX again (allow-list with reasons inside).
- Check tool: `.venv/bin/python tools/sip_readiness.py` prints today's feed and delays and the flipped ones.
- Premarket lane: Phase B runs in dry run and is blind on IEX (11/04 diagnostic: 0 of 24 admitted names had 100+ IEX minute bars, median
  0 vs 126 SIP; arm refusals 99.5-100% phase_b_stale_print / phase_b_missing_print; 0 arms 11/03 and 11/04).
  The premarket code lives in tag `archive/premarket-2026-11` (9 commits, code only; README inside lists its 6 extra test failures).

## Before the first SIP morning (all free, none needs SIP)
1. Premarket branch: DONE 11/04 (63dd1050). The nine premarket commits are on master-mac, Phase B still dry. The six extra failures are
   fixed (the Phase B rail settings are fingerprinted; the leftover IEX copy is gone; test stubs take the position) and
   tests/test_premarket_rth_untouched.py proves the working-sell helper is False in the regular session. Full suite: the same 7 known
   failures. Live only at the next planned restart; no restart was done for it. The 7fed2972 'RTH parity' package stays in the
   archive tag archive/desk-2026-11-03 (it re-adds day-desk exit changes), take pieces only after a replay.
2. T2-T6 simulations from the month plan; T1 (premarket economics, premarket_t1_sim_prereg.json READY 10/29) is a large build with a
   LOW prior (expected lift 0 +- 10 bp); run it only if the premarket decision depends on it.
3. Rebuild cache/avg_session_volume.json on SIP daily bars at the flip (RVOL needs one feed on both sides).

## Day 1
1. config/bot_config.json: `alpaca_bar_feed` "sip", `ai_sip_realtime` true. signal_engine.env: `ALPACA_BAR_FEED=sip` (the env var wins).
2. After the close, restart through the LaunchAgent (launchctl kickstart gui/$(id -u)/com.jambi.trading-desk), never over ssh.
3. `tools/sip_readiness.py`: every line reads sip and 0 min. Check the dashboard log for 429s (SIP plan allows ~10,000 calls/min).
4. Re-read the numbers the tool lists (stale-tape ceilings, spread cap, RVOL floors) against a day of SIP data before trusting entries.
5. Phase B: only after step 1 above. Premarket orders must be limit orders with extended_hours=True (Alpaca rule); paper fills are
   documented in docs/PHASE_B_ALPACA_PAPER_EXTENDED_FILLS.md. Stay dry-run the first morning; read the ledger; then go live on paper.

## Rollback
The same two knobs back to iex / false, plus ALPACA_BAR_FEED=iex, and a restart. No SIP-only code path may become required.

## Keep or cancel
One number: measured improvement in net $/day on paper versus the plan's cost, per SIP_MONTH_PLAN section 6.
