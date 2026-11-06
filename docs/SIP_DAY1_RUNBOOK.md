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

## Buy-to-Monday sequence (operator 11/05: "set up a Sunday-night checklist")
Run on the MINI (the Alpaca keys live there). The tool: `.venv/bin/python tools/sip_sunday_check.py` (tests/test_sip_sunday_check.py).
1. **Friday (or any weekday), after buying, market OPEN:** `tools/sip_sunday_check.py --entitlement`. PASS = the newest SIP SPY trade is <= 30 s old.
   It writes ai_reports/sip_entitlement.json. FAIL = stop; the plan is not live SIP (do not flip anything).
   Why not on the weekend: a delayed-only plan also passes the latest-trade call on days-old trades, so a weekend probe proves nothing.
2. **Friday after the close:** set `alpaca_bar_feed` "sip" and `ai_sip_realtime` true in config/bot_config.json, `ALPACA_BAR_FEED=sip` in
   signal_engine.env, rebuild cache/avg_session_volume.json on SIP daily bars, keep `ai_phase_b_dry_run` true, restart through the LaunchAgent.
3. **Sunday night:** `tools/sip_sunday_check.py` (no flag). GO needs all nine lines OK: stamp passed within 5 days, both knobs and the env var read sip,
   live_data_feed()/delay terms read sip/0, volume cache fresh, Phase B dry, no open positions, no traceback. Any [NO] = the desk stays on IEX.
4. **Monday:** first hour watch for 429s, then re-read stale-tape ceilings, spread cap and RVOL floors (tools/sip_readiness.py lists them).

## IEX vs SIP entry cost (operator 11/06: "stop adding filters, do something new"; registered before the flip)
Hypothesis: the never-green share is partly entry cost. On SIP the desk sees real prices (IEX spreads read 1.5x-200x wide),
so fills should cost less against the SIP midpoint and fewer entries should end never-green.
- Tool: tools/entry_cost_daily.py (nightly) - every fill vs the SIP NBBO midpoint at the fill second, plus the day's never-green share.
- Baseline: the last 10 IEX sessions before the flip. Test: the first 10 SIP sessions (no config change other than the two knobs and
  the RTH re-reads the runbook lists; any other change restarts the count).
- One read at 10 SIP sessions, day-clustered: (1) median buy cost vs mid lower on SIP; (2) never-green share lower on SIP;
  (3) net bp/trade higher on SIP. Supported = all three in the predicted direction, (2) by >= 5 points.
- Confound, stated now: different days and markets. The read reports SPY open->close per session for both windows; a large
  regime gap between the windows makes the read information only.


## If SIP is bought on a Saturday (operator 11/06: "I plan buying SIP access tomorrow ... just for the month for testing")
The entitlement test cannot pass on a weekend, so the timeline is:
1. **Saturday:** buy. Do NOT flip anything. Sunday night `tools/sip_sunday_check.py` will read NO-GO on line 1 (no stamp): that is correct, not a fault.
2. **Monday from 07:00 ET (before the open):** `tools/sip_sunday_check.py --entitlement`. PASS = newest SIP SPY trade <= 30 s old. FAIL = the plan is not live SIP; stop and tell Claude.
3. **Monday after the 16:00 close:** flip the two knobs and the env var, AND set `sip_from_et` in docs/studies/feed_regime.json to the first SIP regular-session open (e.g. `2026-11-10T09:30:00`; the forward tests read IEX and SIP entries as separate samples, and the Sunday/Monday check refuses GO without it), rebuild cache/avg_session_volume.json on SIP daily bars, keep `ai_phase_b_dry_run` true, restart through the LaunchAgent.
4. **Monday night:** `tools/sip_sunday_check.py` (no flag) must read GO.
5. **Tuesday 11/10:** first SIP session. Premarket (04:00-09:25) runs DRY on live SIP prints; the day desk trades on SIP from 09:45.
6. One-month test: cancel at the end unless the plan's keep-bar is met (SIP_MONTH_PLAN section 6). The momentum-names premarket test (docs/studies/momentum_premarket_prereg.json) and the IEX-vs-SIP entry-cost read (section above) are the two registered reads.
