# Wider-stop lead, held out (2026-10-03)

Pre-registered: `wider_stop_prereg.json` (5eb0044). The lead (0.25% initial stop + 0.35%-from-peak trail) was the best
of 24 exit variants on 9/29-10/2 (-2.5 vs live -8.8 bp). Tested here only on held-out days 9/16-9/25: 189 desk trades
>= $10 and 918 same-name random windows, SIP ticks (the 1-cent test's simulator), paired against each trade's live exit.
Evaluator: `tools/studies/wider_stop_eval.py`.

| exit | net bp (t) | vs live, paired (t) | win | median hold | random entries |
|---|---|---|---|---|---|
| live exits | -20.0 (-0.67) | — | — | — | — |
| **0.25% + 0.35% trail (PRIMARY)** | -20.0 (-5.11) | **+0.0 (-0.38)** | 21% | 53 s | -23.2 |
| 0.50% + 0.35% trail | -20.0 (-3.44) | -0.0 (-0.34) | 25% | 77 s | -22.6 |
| 1c + last-1c (user's idea) | -23.2 (-6.86) | -3.2 (-0.55) | 1% | 1 s | -23.9 |

**Verdict: FAIL.** On held-out days the wider stop exactly ties the live exits (means -19.97 vs -19.97 bp; per-trade
outcomes differ widely, e.g. GRML 9/22 +970 live vs -56 simulated). The 9/29-10/2 advantage was selection. It confirms
"exits cannot move the mean": every exit shape lands on the same average; only the entry decides it.
