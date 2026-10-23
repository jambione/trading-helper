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
| **Premarket-level power bar:** reversal off the premarket high/low, target the other level ("8 of 9") | 100 / 100 | not read | not read | — | **UNDERPOWERED**: 94 days per half under the 150-day floor; the setup is rare under a mechanical rule |

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
