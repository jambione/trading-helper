# Three YouTube opening setups on 10 years of SPY + QQQ: two FAIL, one UNDERPOWERED (2026-10-23)

The operator pasted three videos from one presenter. Each rule was:
- pre-registered with mechanical thresholds;
- reviewed by the skeptic twice, at prereg and at code;
- run on SIP 1-minute bars for SPY and QQQ, 2016-01 .. 2026-10-15 (data-only account).

Data rules:
- the held-out window 2026-01-14 .. 04-24 was never fetched;
- the 21 NYSE half days were dropped;
- cost is 2 bp.

The primary test is **long** trades, since the desk is long-only. **Lift** = the trade minus a random entry in the same symbol on the same day, placed after the signal, with the same percent stop and target.

| Setup (video claim) | Long trades A / B | Lift vs random entry | Net after 2 bp | Win rate | Verdict |
|---|---|---|---|---|---|
| **Fence-bar retest:** break of the first 5-min range, retest, SMA20 with you, 2R ("4 of 5 days won") | 589 / 665 | −0.2 / +0.3 bp (t −0.2 / 0.2) | −2.2 / −2.0 bp | 36% | **FAIL** (lift upper bound 2.5 / 3.3 < 5 bp) |
| **Power bar off the 20-SMA:** large 2-min bar from the anchor line, clears the left, 2R | 1,748 / 1,359 | +1.3 / +1.1 bp (t 1.5 / 1.4) | −1.4 / **−2.5 bp (t −2.8)** | 35% | **FAIL** (half B net t ≤ −2) |
| **Premarket-level power bar:** reversal off the premarket low, target the premarket high ("8 of 9"); final rule = amended_4 + amended_5 (see below) | 384 / 425 | −0.9 / +2.4 bp (t −0.5 / 1.2) | **−2.5 (t −2.1)** / −0.6 bp | 52% / 45% hit the target first | **FAIL** (half A net t <= −2) |

## What the numbers say
- **"4 of 5" and "8 of 9" were small samples.** Over 10 years:
  - the fence bar hit its 2R target 189 times out of 589 in half A (32%), and its stop 370 times;
  - the anchor power bar won 35%.
- **A 2R bracket breaks even only above a 33% win rate.** Both setups sit right at that line, which is what a coin-flip market produces. It matches our earlier finding that exits can't create an edge.
- **Information cells (cannot change the verdicts):**
  - the plain 5-minute ORB without the retest was worse (lift −2.3 / −1.9 bp);
  - dropping the SMA filter was worse;
  - the short side was ~0 lift and net negative;
  - 1.5R targets and a stop at the retest bar were worse.
- **The anchor power bar has a small positive lift** (+1.1 to +1.3 bp, t ~1.5 both halves): the real-but-tiny pattern again. It loses money after 2 bp.

## Files
- **Preregs:** `fence_bar_prereg.json`, `pm_power_bar_prereg.json`, `anchor_power_bar_prereg.json` (with amendments).
- **Code:** `tools/studies/{fence_bar,pm_power_bar,anchor_power_bar,etf_bars,etf_minute_fetch}.py`.
- **Results:** `ai_reports/{fence_bar,anchor_power_bar}/result.json` and `pm_power_bar/count.json`.

**No desk change.**


## Premarket power bar: how it got to a fair test (added 2026-10-23 evening)
1. **First count: UNDERPOWERED, no outcome read.** About 100 long trades per half.
2. **Data fix.** The operator's chart (QQQ 2025-07-15) showed that a single 08:05 SIP wick had set the premarket low about 1% below the level a chart reader would mark. Over 10 years, a wick like that sets the premarket low on 21% (SPY) and 27% (QQQ) of days.
   - amended_4 took the levels from candle bodies instead.
   - The count was still underpowered: 89 and 86 trades per half.
3. **Funnel (signal conditions only, no outcomes).** Price came within 0.15% of the premarket low before 10:30 on **77%** of symbol-days, so "the magnet" is real. The strict power bar passed only **4%**, against the video's claim of setups on about 75% of days.
4. **Looser definition (amended_5).** The operator approved one looser definition, reviewed twice: 1-3 combined 2-minute bars, at least 1.5x the usual size for that time, at least 50% body, green. The count was then powered (320 and 361 days per half), and the study was scored.

**Result.**
- **The level-and-bounce idea does not pay.** It hits the target before the stop about half the time, as a coin flip would.
- **Every variant nets negative or about zero:**
  - the strict single bar, wick levels, and levels from 04:00;
  - the 10:30 exit;
  - the short side: −1.2 / +0.4 bp.
