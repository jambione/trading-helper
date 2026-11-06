# Weekend agenda 11/07-11/08 (operator 11/06: test against the simulation, look for strategies that capture more and lose less)

Rules: every item is pre-registered BEFORE its run (bar fixed, one read, no retuning), runs after hours / on the weekend (no desk API budget limit),
uses free historical SIP or the recorded sessions, and ships NOTHING by itself. Confidence = my chance it produces a result that survives its own bar.
Tonight (11/06): 16:03 SIP flip + restart, 16:37 early-path run (price path + trade flow after the fill; strong-signal bar and pays-on-paper list).

| # | Item | Why it was blocked before | Data / tool | Bar (summary) | Effort | Confidence |
|---|---|---|---|---|---|---|
| 1 | Real-time quiet-tape gate, HELD-OUT re-test | worked only on real-time SIP (t +2.3 on 8/4-9/30); delayed SIP flat; never bought | SIP 1m bars on desk fills 10/1-11/6 (a true out-of-sample window); the 9/30 study's rule fixed as written (last 5 min vol < 0.25x session pace) | skipped fills worse than kept, t >= 1.65 and net/trade better, both halves | 2-3 h build, 1 h run | 20% |
| 2 | Do IEX bars hide the signal? Recompute the entry measures on SIP bars | rs_er15, rs_pos30, rs_rise15, wr slopes were built from sparse IEX minutes | SIP 1m bars for the 483 desk entries since 10/15; compare AUC and tercile table with the IEX-logged values | EXPLORATORY: says whether the forward tests would read better on SIP; no gate | 2 h | 50% that SIP sharpens at least one measure; 10% that it becomes a strong signal |
| 3 | Early-cut rule on the real exit code (only if tonight's run finds a strong or pays-on-paper rule) | needs a new default-off exit knob + replay | replay_session / sweep_replays on 25 recorded days, rule frozen from TRAIN | mean day delta > 0, t >= 1.5, both halves, 15 unseen days | 4-6 h build, 4-5 h replay | 15-20% (decided by tonight) |
| 4 | Desk signals on SIP engine bars vs IEX engine bars, paired days (T2) | the desk's %R/arm read IEX; never compared | replay_session --engine-bars sip vs iex, 10 recent days | paired day delta in net bp and never-green share; no ship | build 1 h, compute 4-5 h | 30% that SIP is clearly better; informational either way |
| 5 | Opening 15 minutes 09:30-09:45 (T6) | free SIP pacing blind until 09:46 | replay with entries from 09:30 on SIP engine bars | net/trade of 09:30-09:45 entries > 0 and > the rest, both halves | 2 h | LOW 8%: lifetime 09:30 entries were the worst bucket (n 145, -40 bp) |
| 6 | Stale-tape refusals: how much did IEX gaps cost? (T3) | 19% of arm refusals were IEX gaps | recorded days: refused names that printed on SIP; count and outcome | information only | 2 h | n/a |
| 8 | Spread compression at the arm (start-of-run tell) | needs SIP quotes | SIP quotes 5 min before each desk entry 9/16-11/05 (docs/studies/spread_compression_prereg.json) | compressed entries better, t >= 2, both halves, and make money | 3 h build, overnight fetch | 10-15% |
| 9 | The operator's 1-2-3 entry (point 3 = the real open) | never tested as written | SIP 1m bars around each desk arm 9/16-11/05, fresh simulated exits for both arms (docs/studies/one_two_three_prereg.json) | more money per day than buying at the arm, t >= 2, both halves, and positive per trade | 3-4 h build, 1-2 h run | ~10% |
| 7 | Premarket forward scorer (for the SIP month) | needs the lane's dry-run arms (from Monday) | premarket ledger + SIP ticks | the momentum prereg's own bars | build only | n/a |

Order: 2 -> 1 -> 4 -> 9 -> 8 -> 6 -> 5. Item 3 dropped: the 11/06 early-path run found no strong and no pays-on-paper rule (running list row 102). Item 7 is Monday-week work.
Not on the list (already failed on SIP-quality data, do not re-run): momentum under ideal data (-15 bp), volume-burst breakout held-out (-2.8 bp),
the entry/exit shape families, never-green cut at minute 1-5, calendar flows.
