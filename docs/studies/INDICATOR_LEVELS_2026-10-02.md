# Support/resistance and indicator levels at the desk's arms: results (2026-10-02)

Pre-registration: `INDICATOR_TEST_PLAN_2026-10-02.md` (3ccedd5, d63f7e1, be9fa20, all before the run).
Script: `tools/studies/indicator_levels_study.py`. Raw tables: `INDICATOR_LEVELS_2026-10-02_raw.md`.

Data: 20 sessions (9/04–10/02), 901 admitted name-days, **1,807 square arms**, **348 desk fills** at $10+, and
40,975 random control minutes in the same names. SIP 1m bars with extended hours. **0 fetch failures.**
Outcome: 15 minutes after entry, minus 0.20% round-trip cost. Lift = event minus random minutes in the same name and clock hour.

## Bottom line
1. **Nothing passed.** No cell of the 10 pre-registered tests and none of the 12 H1/H2 cells beat random minutes by +5 bp in both halves.
   Almost every cell is *below* random minutes.
2. **The baseline problem is the arm itself:** square arms are **−21 / −24 bp** worse than random minutes in the same name and hour
   (halves A/B), and desk fills **−31 / −28 bp**. Realized round trip on the 348 fills: **−13.9 bp**.
   No level filter turns that positive. It only makes it less negative.
3. **H1 "breaking resistance is bullish" is the worst hypothesis.** On every indicator the break cell is at or below the average arm:
   ChartPrime break_res −22 / −23 bp, LuxAlgo above_res −25 / −27, PDH/PMH broke and held −24 / −25, VWAP reclaim ≤ 15 min −24 / −45.
   For fills: −28 to −55 bp. On these names a break of resistance is the top of the move, the same finding as "buying the top" and the squares.
4. **H2 "right on support" is the least bad, but still negative.** Near-support cells run about +5 to +15 bp better than the
   average arm (SR channels −10 / −14, Bjorgum −9 / −20, nearest support < 0.62% −7 / −17, VWAP < 0.8% above −9 / −13), but still
   below random minutes in both halves. **It is the same direction as the user's manual rule, and it is not big enough to make a trade positive.**
5. **"Best one":** by the pre-registered rule there is no best indicator, because none passes. The least-bad H2 cells were SR channels and
   Bjorgum on the arms, and SR channels on the fills.
6. Market tide, relative strength vs SPY, the squeeze release, and high-volume nodes: no passing cell either.

## Correction the same evening: the "least bad" ranking depends on the yardstick
The lift above compares each arm with random minutes in the **same name and hour**. The question a skip rule faces is different:
is this arm worse than the **other arms**? Re-cut by half, with day-paired differences against the rest of the arms:

| cell (square arms) | half A in / vs rest (t) | half B in / vs rest (t) |
|---|---|---|
| near support (< 0.6%) | −9.2 / −7.2 (−0.5) | −21.2 / −18.6 (−1.4) |
| near VWAP (< 0.8%) | −14.9 / −15.5 (−1.5) | −15.7 / −13.1 (−0.8) |
| stretched above VWAP (≥ 1.75%) | +2.7 / +27.5 (+1.6) | −10.2 / +17.5 (+0.7) |
| far from support (≥ 1.5%) | −2.3 / +24.4 (+1.1) | −11.8 / +9.6 (+0.5) |
| any resistance break (H1) | −8.0 / +3.1 (+0.7) | −9.9 / +9.9 (+0.8) |
| just under the prior-day high (< 1%) | −25.2 / −10.9 (−1.7) | −19.7 / +6.6 (+0.8) |

Against the other arms, near-support/near-VWAP arms are no better, and stretched or breakout arms are no worse. The H1/H2 ordering
in the bottom line holds only against same-hour random minutes, because a running name's random minutes are also high.
**No cell is a reliable skip rule:** each flips sign or has t < 2 in a half. On fills, the only same-sign cell is
"just under the prior-day high" (−14 / −74 bp vs the rest, t −2.8 / −2.2), but on n = 22 / 15 over 4 days a half it is a watch item, not a gate.

## Port check (RKLB 10/2 vs the user's screenshots)
- ChartPrime HV boxes: the resistance box matches to the cent (74.06, depth to 74.22). The Break Res and hold events land
  2–4 minutes later than on TradingView, probably from ATR(200) seeding: our history starts at the prior day's 04:00, while
  TradingView's starts much earlier. Levels are right; event timing is approximate.
- Bjorgum zones at 14:10: 74.01–74.08 and 74.35–74.47 overhead, 73.60–73.79 below. These look consistent with the chart, but
  that screenshot has no price axis, so it is not a strict check.

## What this means
The levels a manual trader reads don't rescue the square arm. The arm is the problem: it buys 21–24 bp worse than a random minute
in the same name. "Near support" leans the right way, consistent with the earlier "room below the high of day" finding, but nothing here
is a gate. Saturday's 60-day re-check is only for cells that passed, so there is nothing to carry forward.
