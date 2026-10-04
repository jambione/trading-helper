# Resting entries: a buy limit that cancels instead of crossing (2026-10-03)

Pre-registered: `resting_entry_prereg.json` (f25755c). Script: `tools/studies/resting_entry_study.py`. 413 desk entries
>= $10 (9/16-10/2) and 824 same-name random entries. Market baseline = buy the SIP ask, sell the SIP bid after the trade's
own hold. Resting = buy limit at the SIP bid or mid (rounded down), FILLED only when round-lot SIP trades below it total
our size within W s, else skipped (0); filled trades sell at the SIP bid after the same hold. All prices from the tape.

Desk entries (market baseline -16.0 bp/signal):

| arm | fill % | net/signal | vs market (t) | net/fill | missed trades' baseline | halves net/signal |
|---|---|---|---|---|---|---|
| bid 10 s | 30% | -3.1 | +12.9 (2.16) | -10.2 | -14.9 | -4.8 / -1.2 |
| **bid 30 s (PRIMARY)** | 51% | **-8.3** | **+7.7 (1.20)** | -16.3 | -10.0 | -13.4 / -2.7 |
| bid 120 s | 73% | -10.7 | +5.3 (0.99) | -14.7 | +2.2 | -17.8 / -3.0 |
| mid 10 s | 48% | -6.7 | +9.3 (2.69) | -14.0 | -9.3 | -11.0 / -2.0 |
| mid 120 s | 82% | -10.4 | +5.6 (1.35) | -12.7 | +3.9 | -16.4 / -3.8 |

Random entries (baseline -12.9): every arm beats market (+6.4 to +12.2 bp, t 2.1-3.9); bid 10 s -0.7, bid 30 s -1.3
per signal.

**Verdict: FAIL** (PRIMARY not >= +3 bp at t >= 2, and negative in both halves).

What it does show:
- Resting beats the market entry in all 16 cells, desk and random, by +5 to +13 bp per signal; shorter waits beat longer.
  Most of the gain is trading less: the missed signals were losers too (baseline -9 to -15 bp at 10-30 s), so skipping
  them saves their loss plus the spread.
- Adverse selection is real: per FILLED trade the desk loses -10 to -16 bp (fills come on dips); only at 120 s do the
  missed trades turn into winners (+2 to +4).
- It never turns positive on desk entries: a loss reducer, not an edge. Random entries resting at the bid get to about
  break-even (-0.7 / -1.3), which says again that the entry choice adds nothing.
- Cost of using it: 30-50% fill rate, i.e. half to a third of the opens.
