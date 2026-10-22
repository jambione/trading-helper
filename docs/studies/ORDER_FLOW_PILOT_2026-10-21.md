# Order-flow pilot: FAIL on profit, but trade flow is a real, small signal (2026-10-21)

Prereg: [order_flow_pilot_prereg.json](order_flow_pilot_prereg.json) (amended .. amended_5, budget_restated), all before any
outcome was read. Script: `tools/studies/order_flow_pilot.py` at ce750081 (skeptic READY on beb24dc0; amended_5 narrowed
one gate after a timestamp-only check). Raw: [ORDER_FLOW_PILOT_2026-10-21_raw.md](ORDER_FLOW_PILOT_2026-10-21_raw.md).

**The question.** Before a desk-style pre-square entry, do buyers lifting the ask (G1, Lee-Ready-signed SIP trade flow in
the last 60 s), a bid-heavy book (G2, NBBO size imbalance) or both (G3) predict the next 3 minutes? Data: 15,563 entries,
110 sessions, free SIP ticks. Each group (top third) is compared with the rest of the pre-square entries, mid to mid, and
it must also pay after crossing the spread.

**Verdict: all three FAIL.** The 97.5% upper bound is below +5 bp in both halves, so each is ruled out as a +5 bp edge.
But G1 is not zero.

| Group | Half A: gated - rest | Half B | Gated net after the spread |
|---|---|---|---|
| G1 trade flow | **+1.9 bp (t 3.54)** | **+1.5 bp (t 3.41)** | -16.0 / -12.5 bp |
| G2 book imbalance | +0.5 bp (t 0.88) | +0.9 bp (t 1.86) | -14.9 / -11.7 bp |
| G3 both | +1.9 bp (t 2.27) | +1.8 bp (t 3.15) | -15.2 / -10.9 bp |

**Trade flow is a genuine signal.** It is the first predictor in this repo's entry work to beat its control above the
Bonferroni bar in both halves (t_crit 2.47).

- It survives every robustness check: name-day equal (+2.0 / +1.3), dropping the top 3 names or days (+1.1 to +1.7), and
  terciles cut within each hour (+2.0 t 3.45 / +1.5 t 3.06), so it is not time of day.
- It is positive within every tercile of the onset bar's own return (+1.6 to +3.3 bp, except one mid-tercile half at
  +0.2), so it is order flow, not a big onset bar.
- It shows up at every horizon: +1.2 / +0.9 bp at 60 s and +1.9 / +1.3 at 300 s.

**It is far too small to trade.** Order flow adds about +1.5-2 bp. The exit study says this desk needs about +5 to +11 bp
of 3-minute entry edge. The gated trades lose 11-16 bp after crossing the spread on this unscreened sample (the desk's
spread-capped names cost less, about 3 bp). Fewer gated entries reach 2x the spread at their 3-minute best (6.5% vs 10.3%).
The signal is a slightly better average, not bigger runs.

**Not tested:** the live-feed version (IEX trades against IEX quotes). That matters only if the effect were tradable;
at 1.5-2 bp it is not, so the if_pass IEX step and the held-out confirmation are not triggered. The held-out sample stays
unread.

**What it closes.** The free public feed's two classic short-term sources are now both measured. Bar indicators give
about 0. Trade-side order flow gives about +1.5-2 bp, real but a fifth of the need. No free-feed entry signal tested
here reaches the desk's break-even, and the entry search on this data is done. Order flow could still be a small
ingredient: an ADD-ON to a stronger entry, or a tie-breaker between candidates. It cannot be the edge on its own.
