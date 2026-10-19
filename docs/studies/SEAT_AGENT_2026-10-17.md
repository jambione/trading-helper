# 08:30 list as a seat: FAIL (2026-10-17)

Pre-registration: `docs/studies/seat_agent_prereg.json`, including `amended_1`, written before any round-trip return. Result-skeptic Mode 1 returned READY after that amendment. Script: `tools/studies/seat_agent.py --score`. One run, on the mini's `~/session_snapshots`. No cutoff was moved after the number existed.

The question was the seat, not a new entry. A round trip is a paper buy and the sell the desk actually made. The buy had to match an `entry_ok` within 120 seconds, inside 09:30–15:50 ET. Agent: the symbol was on that morning's Grok or Gemini list (`last_ok` in [08:30, 09:30)). Movers: it was not on that list, and the movers file had already named it. The spread is the one in the broker prices. No second 3 bp was charged.

Half A is 2026-09-25 through 2026-10-06 (8 archived days, the median included). Half B is 2026-10-07 through 2026-10-16, with 2026-10-08 left out because that folder has no morning research clock. 2026-09-24 has no suggestion snapshots and was not scored.

## Result

Day-equal means, on days that had at least one trip in each arm. `diff` is the morning-list day-mean minus the movers day-mean. Trip counts are those paired days only.

| half | paired days | morning trips | movers trips | morning mean | diff | t |
|---|---:|---:|---:|---:|---:|---:|
| A | 7 | 23 | 290 | −3.9 bp | +8.5 bp | 1.82 |
| B | 2 | 4 | 40 | −9.3 bp | −4.3 bp | −0.69 |
| both | 9 | 27 | 330 | −5.1 bp | +5.6 bp | 1.36 |

**FAIL.** Half A had enough days and enough trips. The morning-list trades on those days averaged about −3.9 bp. The bar required that average to be above zero. It was not. They lost less than the movers trades on the same days. Losing less was not the bar.

Half B has 2 days and 4 morning-list trades, under the floor of 6 days and 20 trips. The written rule says a powered half that is already negative closes the question.

Across all 27 morning-list trips: 7 were also on the movers list before the buy, and 4 had a list `pct_change` already at or above 10. Every one of the 27 had a `pct_change` on the row. 71 in-window round trips had no `entry_ok` inside 120 seconds and were left out. 124 others were neither seat. Those counts were not used to change the rule.

The live book was not changed.
