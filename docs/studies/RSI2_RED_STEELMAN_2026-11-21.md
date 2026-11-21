# RSI-2 red: the operator's strategy, at its strongest (for Neon's second opinion)

Written 2026-11-21 by Claude at the operator's request ("Steelman my strategy ... pass that to Neon and get its opinion"). Neon: please judge both the strategy and the testing, and say what you would run next. Repo files are cited so every number can be checked.

## 1. The strategy, as the operator trades it

- **Indicator:** ChrisMoody `_CM_RSI_2_Strategy_Lower` on 1-minute bars with extended hours. A bar is **red** when RSI(2) > 90, close > SMA(5) and close < SMA(200).
- **Claim:** red marks the start of a short profit run. Price rises while the bars are red and stalls after. The runs the desk captures last about **3 minutes**, so the window that matters is **3-5 minutes**, not 15-30.
- **Entry:** get in as red appears, on the first bar. The exit is the desk's existing ratchet stop, with no colour-based exit.
- **Discretion (the part that matters):** not every red is taken.
  - **Take** reds where RSI-2 **climbs and rounds / plateaus** into red, and reds that come early off a washed-out low with %R oversold and then the oversold-exit triangle.
  - **Skip** reds where RSI-2 **spikes steeply** into red ("steep indicators signal a stall coming").
  - Longer reds go with longer runs.

## 2. The case for it (strongest form)

1. **The runs are real, and red sits on them.** Operator charts with SIP confirmation:
   - BE 11/17: a 10:55-11:01 red, 163.5 -> 169.2, about +3.5% in 6 minutes.
   - BE 11/16: a 10:41 red, about +2% to 10:57.
   - WDC 11/18: an 11:58-12:01 red, 282 -> 293.
   - ON 11/17: a 10:56-11:02 red, +2%.
   - CBRS 11/18: 11:58 and 12:16 reds into a run to 98.5.
2. **The skip rule does real work on the days he studies.** On 11/16-18, $50-$400 names, closed reds bought at the next open ([tools/studies/rsi2_red_approach.py](../../tools/studies/rsi2_red_approach.py), days excluded from the scored test):
   - **gradual** approach (RSI-2 >= 80 the bar before): 3,168 reds, **+0.8 bp** at 5 minutes, **+0.3 bp** vs same-minute names;
   - **steep** approach (RSI-2 < 60 the bar before): 1,154 reds, **-2.5 bp**, **-2.7 bp** vs same-minute names.

   That is a **~3 bp spread in the operator's favour**. Steep reds fade, as he says.
3. **Red length tracks the run, in hindsight.** Across 95k "after 2 red bars" entries: reds that ended at entry -5.4 bp; went 1-2 more bars +2.2; longer +17.9 bp ([docs/studies/rsi2_red_persistence_prereg.json](rsi2_red_persistence_prereg.json) result). If any live cue predicts length (rounding, plateau, approach), the payoff exists.
4. **The tests may have measured something he doesn't do.** Several early test designs mis-specified the strategy, and the operator caught each one:
   - a "sell when red ends" exit;
   - buying one bar late;
   - counting mid-bar flickers as "red" (AXON 11/17 14:54 and CVX 11/18 11:33 never closed red; IBKR 11/16 12:42 is a 1-bar SIP red absent on his feed);
   - a +1.5% day filter that excluded every example he gave.

   His chart feed ("indicative data") matched SIP colours on only 57% of his marked bars (75% allowing a 1-minute shift), so the tests' red and his red differ on some bars.
5. **He trades a judgment, not a rule.** No single mechanical rule may capture which reds he takes. A null on mechanical variants does not refute discretionary skill.

## 3. The case against (what the tests found)

All tests are pre-registered, skeptic-reviewed, run on SIP 1-minute data for 103 sessions (2026-04-28..09-23), and compared with other names entered at the same minute with the same entry type and exits. 2026-09-24..11-18 is sealed and unused.

| Variant | Signals | Lift vs same-minute names (1st / 2nd half) | Smallest detectable |
|---|---|---|---|
| red start, mid-bar (buy as it turns red) | 163,626 | ratchet -1.1 / -0.4 bp; 1-5 min -0.8..-0.05 | 0.3-2 bp |
| red start, bar close, next open | 146,571 | about 0 at 1-5 min and ratchet | 0.2-1 bp |
| after 2 / 3 red bars | 95k / 55k | -0.7..+0.8 bp; 2-bar reds no better than 1-bar reds | 1-2 bp |
| red on 5- / 10-minute bars | 65k / 12k | about 0; 10-min mid-bar +2.4 / +1.0 (t 2.5) = ~1.7 bp, below the pre-set +3 bp bar | 1-3 bp |
| red within 2 bars of a 15-min low | 35,087 | slightly worse: -2.0..-0.3 bp (t -1.8..-3.5) | 1-2 bp |
| red + %R oversold-exit triangle | 35,815 | about 0 (-0.6..+0.2) | 0.4-1.7 bp |
| gradual vs steep approach | 102,951 / 37,427 | both about 0; gradual - steep = -0.2 bp at 5 min, -0.5 ratchet (t -1.95) | 0.3-1.2 bp |

- **Base rates:** only about half of reds reach a 2nd bar, and about half of those reach a 3rd. Length is close to a coin flip at each bar, so it is not knowable when you would buy.
- **Days matter:** the gradual-vs-steep spread was +3 bp on 11/16-18 and -0.2 bp over 103 sessions.
- **Costs:** gross moves at 1-5 minutes were 0 to +1.2 bp for every variant, against roughly a 5-10 bp round-trip spread.

Prereg files: [rsi2_colour_timing_prereg.json](rsi2_colour_timing_prereg.json), [rsi2_red_persistence_prereg.json](rsi2_red_persistence_prereg.json), [rsi2_red_htf_prereg.json](rsi2_red_htf_prereg.json), [rsi2_red_washout_prereg.json](rsi2_red_washout_prereg.json), [rsi2_red_approach_prereg.json](rsi2_red_approach_prereg.json).

## 4. The proposed next test

Run **gradual vs steep** (the scored contrast in rsi2_red_approach) on the **sealed CONFIRM sessions** (2026-09-24..11-18, minus 11/16-18, about 37 days). It overrides the step-1 gate, which failed, and would be labelled that way. The result stands either way:
- **claim supported:** gradual beats steep and beats same-minute names, in the 3-5 minute window and on the ratchet, at day-clustered t >= 2;
- **claim not supported:** anything short of that.

## 5. Questions for Neon

1. Is the testing fair to the strategy as described? Is anything still mis-specified (entry timing, what counts as red, feed, exit)?
2. Is there a live-knowable version of the operator's discretion that has **not** been tested and that you would test first?
3. Is the CONFIRM run of gradual vs steep the right next step, or a waste of the holdout?
4. Given costs (~5-10 bp round trip) and the measured gross (0-1.2 bp), is there any version of RSI-2 red worth more compute?
