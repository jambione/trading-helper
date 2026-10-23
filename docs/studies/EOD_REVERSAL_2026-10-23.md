# End-of-day reversal: scored 2026-10-23, not passed (UNDERPOWERED by the rule, zero in half B)

- **Prereg:** `docs/studies/eod_reversal_prereg.json`, with amendments C1-C4 and amended (1)-(5), registered at c3264bb5.
- **Code:** `tools/studies/eod_reversal.py`, sha 9f454b47. The skeptic's code review was READY; its info-field fixes were applied before scoring.
- **Output:** `ai_reports/eod_reversal/result.json` on the mini.

## Design
- **Paper:** Baltussen, Da & Soebhag (2025).
- **Signal:** rank every liquid warehouse name (~1,640 a day) by its return from the prior close to 14:59.
- **Trade:** buy the bottom decile (~165 a day) at the 15:30 open; sell at the 15:59 close.
- **LIFT:** the losers' return minus the average name's return over the same window.

## Result

| | Half A (5/11-7/29) | Half B (7/30-10/15) |
|---|---|---|
| Trades | 9,076 | 9,006 |
| LIFT (losers minus the average name) | +5.4 bp, t 1.18 | **+0.0 bp, t 0.00** |
| Loser trades, net of 5 bp | +2.9 bp (t 0.5) | **−7.8 bp (t −2.3)** |
| Drop the 3 best days | +0.2 bp | −2.1 bp |
| Detectable effect (MDE) | 1.7 bp | 2.0 bp |

**Verdict.** UNDERPOWERED under the registered rule: half A's upper bound is above the +5 bp bar, so it cannot be called a FAIL. In practice it is no edge:
- the effect is zero in half B, a well-powered half (it could detect 2 bp);
- loser trades lose money after costs there.

## Information (cannot change the verdict)
- **Half A's lift tracks the market.** The day LIFT moves with SPY's last half hour (slope 1.13): the losers are high-volatility names that rise when SPY rises. In half B the slope is 0.13 and the intercept is 0.
- **It looks like it only worked on down days in half A.** Days with SPY down to 15:00 show +16.7 bp (t 2.0), but half B's down days show −1.8 bp. That reads as noise.
- **Other cuts:**
  - quintile instead of decile: +3.7 / −0.0 bp;
  - the winner decile: −0.7 / −1.8 bp;
  - ranking on the intraday part alone: +7.4 / −0.5 bp;
  - $20+ names: +6.3 / −0.7 bp.

## Why the paper's effect does not show here
- **Our names are the liquid ones.** The warehouse is $20M+/day names, where the paper's effect is weakest: its value-weighted quintile spread is 3.8 bp, while its small-stock spread is 14.7 bp.
- **The thinnest names were removed.** Names with no exact 14:59 bar (~13% of name-days, the thinnest) were skipped before ranking, as registered. That removes where the effect is strongest.
- **The exit is not the auction.** It uses the 15:59 bar close, not the closing auction the paper's last trade reflects.
- **The periods differ.** Our sample is 110 sessions in 2026; the paper's runs 1993-2019.

**No desk change.**
