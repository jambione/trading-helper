# Running list

Kept through the session: every concern, request and open item, with status. Newest day first.
Status: **open**, **running** (queued or in progress), **done**, **dropped** (with why).

## 2026-10-21 (Wed)

### Operator concerns
| # | Item | Status | Where |
|---|---|---|---|
| 1 | Pasted 'top indicators for entry' list: was all of it tested on our opens? | done (audit) | Tested and failed: MAs/EMA pullback, HH/HL, RSI, MACD, RS vs SPY, RVOL, VWAP, Bollinger, ATR stops, S/R, regime/tide, news, confluence scores. Untested: ADX (engine never publishes it), OBV/AD, the article's confluence example, video setups on desk arms |
| 2 | Pre-register ADX, OBV/AD, video setups on our opens (same-name random-minute control) | registered | docs/studies/indicator_confirm_prereg.json, skeptic FIX FIRST applied 10/21: arm_ok True arms only (~354/651 per half), DiD vs harness control, IEX features; H5 = article confluence; video setups count-only. Script tools/studies/indicator_confirm_study.py written 10/21; null OK (max |t| 1.45, bar amended to Bonferroni ~3.0); count step on mini: only H2 scored both halves (MDE 45 / 13 bp), H1 B only, H3-H5 UNDERPOWERED; video 392 arm name-days, no fetch. skeptic READY 67067f8f; --score queued on the mini 16:15 ET 10/21 (H2 is the only cell that can be graded) |
| 3 | S2 video-setup history study stalled | open | count step 10/9: MDE 22 bp > 15, pre-committed extension to 2025-11-03 never run (ai_reports/structure_pullback/count.json on the mini) |
| 4 | Power the untested indicators (operator 10/21: 'how can they be powered') | open - needs skeptic | history route: docs/studies/indicator_confirm_hist_prereg.json - PRE entries on 400 warehouse names x ~110 sessions; H1-H5 as filters (same-name AND cross DiD), video S3-S7 standalone on all names (S2 dropped, operator 10/21); skeptic FIX FIRST applied. Count step 10/21 (tools/studies/indicator_confirm_hist.py, outcome-blind): every H and V cell powered both halves (H MDE 3-7 bp, S3/S5/S6/S7 1.4-2.0, S4 13-19); 75% of PRE onsets fail the 80% IEX coverage rule (69k of 92k) and leave every H cell; same-name control gap IN-OUT t ~1.8-2.3 for H3-H5 in half B (why the cross DiD is required). Null/placebo/handcheck built; skeptic r2 BLOCKED on a normal-quantile null bound, operator approved amended_3; binding fresh-seed null (120 sessions, 'ich-null-confirm') OK: max |t| 2.62 vs 3.39 at df 59, no investigate flags; placebo OK 1.76 vs 3.17; S3-S7 verified by the skeptic's independent re-implementation (0 disagreements on 7,499 name-days). SCORED 10/21 06:49: H2-H5 and S3/S5/S6/S7 FAIL, H1 and S4 UNDERPOWERED, none pass (docs/studies/INDICATOR_CONFIRM_HIST_2026-10-21.md). Known info-cell bug: board-like table empty (numpy bool 'is True'), verdicts unaffected |
| 5 | What gives a 3-minute profit run our exit can capture? (operator 10/21) | DONE - FAIL | nothing tested predicts 3-min direction (runs are volatility, not direction; exits own the win rate, not the mean). Two preregs: docs/studies/three_minute_reread_prereg.json (13 rules + 5 filters at a 3-bar hold on the warehouse, confirmation on 60 earlier sessions) and docs/studies/order_flow_pilot_prereg.json (Lee-Ready trade flow + NBBO size imbalance 60 s before PRE entries, 180 s, free Alpaca SIP ticks). Book imbalance was tested 10/4 (depth_book, underpowered, leaned negative) 10/21: three_minute_reread.py built (aaf4d17b); first null OK (max |t| 2.37), count: all 18 cells powered (rules MDE 0.3-0.8 bp); skeptic r1 FIX FIRST applied incl. operator-approved BOUNCE rule (amended_2); count/null rerun + held-out fetch (70 sessions before 4/27 -> ai_reports/warehouse_confirm) queued 16:45 ET; then skeptic r2 SCORED 11:32 (autoscore, all READY conditions met): 17 of 18 FAIL, S4 UNDERPOWERED, nothing to confirm; MFE/MAE ~+/-10-12 bp symmetric; desk fills +0.7 bp at 3 min (docs/studies/THREE_MINUTE_REREAD_2026-10-21.md). Held-out 60 sessions fetched, unread. Order-flow pilot not run |

## 2026-10-15 (Thu)

### Operator concerns (end of day)
| # | Item | Status | Where |
|---|---|---|---|
| 1 | Few opens today | done (review) | opens/10m 1.59 / 1.10 / 0.58 (10/13/14/15; 10/15 = outages). Refusals: setup 63-68% (exh_falling 32-37%, exh_not_tight 20-24%), data 26-31% (tape_only 17-24%). Entries at 88-93% of the prior 30-min range since pre-square-only (~70% of the run done). NEXT: replay tight gap 15→20 and after-square wait 10→5 for opens and net/trade |
| 17 | Never-green trades with a big stop (operator 10/16: UPS, CRH) | PARKED | tested 3 ways, none helped: 0.3% initial stop +0.9 bp/day (t 0.03), no-progress cut (live -13 bp/exit, replay -36.6 bp/day), np gated by the double check -22.3. Untested blend: tighten to -0.3% if not green after ~2 min, not worth it now (two nulls; exits ~0 in 42 shapes). Revisit stops only after the entry changes (#15/#16) |
| 16 | Seat names BEFORE their turn | SKIPPED | gate #15 failed: seating earlier would buy coin flips sooner |
| 15 | Desk watches names only after their turn (RPRX) | DONE - FAIL | pre-screen turn test 10/17: BOTHUP -0.2 bp (t -1.2, n 283k), MID -0.0 (n 228k) vs random later minutes on 400 unscreened names x 119 sessions; both fail the cross control. %R turns carry no information, screened or not. docs/studies/PRESCREEN_TURN_2026-10-17.md |
| 14 | Entries after the move (operator 10/16 VRNS 10:18, CRH 10:28 'move is over'; LHX 10:53 'sideways') | tested | confirm-then-buy (wait 1-2 min, buy only if held) FAIL / UNDERPOWERED, worse than buying at once (CONF1 - PRE -35.6 bp); docs/studies/CONFIRM_ENTRY_2026-10-16.md. 11th entry-timing rule to fail; PROMISING re-grade of mid-rise tonight is the remaining timing lead. 11:06-11:07 UPS 99.48 / CRH 84.27 re-entry: 'sideways opens' at the range top under the box (OB skip rule already FAILED 10/6). Untested: intraday price efficiency ratio (choppy vs trending last 20-30 min) - offered to operator. CRH re-entry exited after 78 s on 'stale quote' = IEX gap, not real staleness (iex-trade-gaps-are-not-staleness). SIDEWAYS FILTER 10/16: SLOWUP (operator's %R read) FAIL - right direction pooled (kept +11.7 vs skipped flat/falling -18.4 bp) but halves reverse, t 1.0, kept still nets -6.8 bp; TRER UNDERPOWERED. docs/studies/SIDEWAYS_FILTER_2026-10-16.md |
| 13 | Few opens 10/16 (4 in 09:30-10:53, 0.5/10m) | tonight | seats 4 at 09:30-10:00 (restart warm-up + few candidates), 199 evictions mostly ~30 s after admission, QQQ -0.9%. Arm trail_dc_seat150 (150 s grace) queued in tonight's sweep (/tmp/ecs_20261016n, sha c2c1e3c, starts 16:15). RULE: no desk restarts 09:25-10:00 (the 09:32 restart cost the open's warm-up) |
| 2 | Delays when we should have opened (AAL ~70 s; CRH 10/16 filled 84.52 vs 84.43 ask at arm) | running | RTH live profile read 10:50 10/16: book loop heavy pass p50 3.7 s vs 3 s tick (max ~7 s, shelf gap up to 6.2 s). Costs: uncached get_clock per tick (1483 calls, 48 s), load_config deep copy (402k calls, 60 s), json file re-parsing (39.7M loads, 113 s), repeated open-orders/positions/snapshot fetches (~175 s), _held_qty ~5.9 s/call. Fix handed to a separate session (caching; ship after close). ai_watch_profile_path still on - remove after the fix lands |
| 3 | Exit sweep, amendment 3 | running | Live exit is the hold (trail_dc since 09:32 10/16). The no-progress cut stays off. Daytime replay is already running on the data account: `/tmp/ecs_20261016n`, sha 8bbede5, nice 19. Arms: off, trail_dc, trail_dc_seat150 (seat grace 150 s), trail_dc_cool30 (re-entry cooldown 30 s), trail_dc_lob (engine-only triangle), trail_dc_init06 (tighter initial stop, 0.06R ~0.3% instead of 0.12R ~0.6%; the UPS 10/16 question), trail_dc_np_dc. Keep trail_dc only if it beats off in BOTH halves AND the paired t is >= 1.5 over the 8 days. Both halves with t below 1.5 still sets `ai_trail_exit_trend_hold` false. A new arm ships only if it beats trail_dc the same way. seat150 and cool30 must also not lower opens/10m. |
| 4 | Double check holds a sell at or below entry | DONE - KEPT live | 8-day rerun 10/16 (sha 8bbede5): vs off +40.8 bp/day t 2.29, halves +56.9 / +24.8. Same sweep: cool30 +7.2 (halves disagree), engine-only triangle +2.7, 0.3% initial stop +0.9, seat150 -17.3, np_dc -22.3 vs trail_dc - none ships. docs/studies/EXIT_SWEEP_2026-10-16.md. sqwait0/5 still to run (Grok) |
| 5 | Test momentum names when they appear on the data seed | running | momentum_alert_markouts, momentum_alert_ticks (30-60 s, premarket split), clock_windows (from 16:15) |
| 8 | Earlier entry to capture more of the ~70% run (pre-square forms near the top) | done - FAIL | warm re-score (morning visible): PRE +15.2/+0.1, IGN -8.2/+27.1, MID +6.4/+2.1 (closest; net -1.4/-12.9), RSI2T +1.6/+0.6 bp vs random later minute; none clears +5 bp t>=2 both halves |
| 9 | Operator's TGL read (05:06 10/16): uptrend (trending momentum) + dip, buy when squares resume after the ▼ pullback | open | operator: 'overall trending momentum and dip with an up trend'. Encode: established uptrend (higher highs, price above short EMA), pullback with %R staying high, entry when squares resume, price above the pullback low; pre-register; score honestly (spread, later-only control) on cached sessions + premarket SIP. Desk blocks this today (post-square wait 10 min, presquare_only) |
| 12 | New options (operator 10/16): gap-down short + calendar family | DONE - both FAIL | calendar: 0 of 9 cells (CALENDAR_FLOWS_2026-10-16.md). Gap-down short: FAIL both halves, net -29.6 / -15.5 bp/day, vs flat-gap control -10.8 (t -3.0) / +3.7 (t 0.8); the look-ahead official-open version would have shown +47.6 bp/trade (GAPDOWN_SHORT_2026-10-16.md). Gap-down stays an avoid-filter only |
| 11 | Operator 'sharp trending angles vs smooth sideways' (TGL 10/16) | done - FAIL as a rule | steep turn: A lift +7.0 (t 0.42) / B -0.4; net -15/-13 bp; fires ~5 min before 42% of desk fills. Discretionary read measured by the Call bar instead |
| 10 | Human-in-the-loop calls | live 10/16 05:18 | desktop Call bar (any ticker; any clicked name fills it; setup tags; desk snapshot; today's calls with move since). Scorer: premarket included, SIP spread, later-only control; scored nightly, read at 50-100 calls |
| 7 | Limit orders 1-2 cents under the bid to save spread | open | score with the G2 read (passive arms filled ~25%, adverse); limit-below FAILED 10/14 (dipped fills -65.6 vs unfilled +57.7 bp). Test bid-$0.01 / bid-$0.02 fill rate + adverse selection on recorded ticks |
| 6 | Review today's exits: trail updates and no-progress | done (first pass) | 21 trades: np on (before 11:25) -3.5 bp / 36% win; np off -10.1 bp / 20% (3 dead_trade exits at 22 min: -15/-21/-32 vs np cuts -15.5 avg). Trail gives back small peaks (+5-8 → ~0). After-exit move mixed (FIG +139 vs WMT -128). TODO: stop-path per trade (event field names) |

### Shipped today
- Weekly book live (paper); AGY on gemini-3.8-flash-high; first order Fri 10/16 15:58
- Discord OCR on ScreenCaptureKit (Screen Recording re-granted 09:56)
- Desk AGY model fixed (all AGY calls had failed since 10/14 11:00)
- ETFs/ETNs/funds never take a book seat
- Book: Gates column (replaces SR), Status uses the arm's tape ceiling, near-arm highlight (needs tight lines), one-line mobile book, mobile positions history
- No-progress cut OFF (11:25)
- Delay cuts (12:45): pre-square confirm 1 poll, recent-volume gate off, G2 entry/exit tests off
- Movers admission print-age 60 → 180 s (13:26)
- Finnhub stream reconnects when its write side dies (fixed twice; live 13:31)

### Incidents
- 05:52-10:22 and 12:58-13:31: our Finnhub socket stopped sending (new names never subscribed, book emptied) - fixed
- 12:55-14:16: Alpaca data vendor outage (IEX and SIP) - external; 10/15 is a degraded day in every study

### Tonight (mini, after 16:15)
- Exit sweep, 10 arms × 8 days → tomorrow's exit config
- Oversold ▲ entry test → **FAIL** both halves (net -28 / -10 bp, lift -4.2 / +0.6 vs random later minute); ▲ arm stays off
- Momentum-alert studies → SIP-month keep-bar ($4.70/day)
- Simulator fidelity gates (hist_sim_fidelity.sh) - PAUSED 16:55 (kill -STOP 48464 22892 23630) so the exit sweep and alert studies get the Alpaca budget; resume with kill -CONT after the sweep

### Build tonight (before the open)
- Arm decision on the 3 s book tick; profile and cut the watch poll
- Pre-subscribe candidates before admission; verify every subscribed name prints, re-subscribe if not
- Opens review + exit review (items 1 and 6)
- Arm-proximity scan design

### Incidents tonight
- 17:40: the scheduled nightly --exact replay of 10/15 (restarts + Alpaca outage) grew to a 17 GB footprint and stalled every replay (swap); killed (10/15 is degraded anyway). TODO: memory cap / guard on nightly replays

### Incidents 10/16 (overnight)
- 03:30-04:28: the mini's disk filled (187 replay work dirs, 66 GB); trader + engine died. Operator removed them 04:4x (68 GB free). Replay fixed (fe2df1b). Desk restart pending
- Exit sweep stopped at 03:30 (7 days): np180 vs off -36.6 bp/day (t -0.87) - no change, cut stays off. Gated arms were BLIND: the replay never fed the bar store (ob_feeds 0), so np_dc == np180 and trail_dc == off. Fixed (ob_feed_on + DC_STATS counters); rerun all deciding arms at the new sha tonight

### Proposed (waiting on the operator)
- Longer-horizon exits/sizing for the weekly book and day hold on 10 yrs of daily bars
- Strategy map: https://claude.ai/artifact/HiTvNTRPc6ZGat9zs4Q2J9

### Parked / for later
- Volume profile (operator: never mind)
- JEV model (park until a timing edge exists)
- Why the Finnhub write side keeps dying (auto-reconnect makes it harmless for now)
- Re-score PRE/SQ with warmed %R lines (same cold start the ▲ test had)
