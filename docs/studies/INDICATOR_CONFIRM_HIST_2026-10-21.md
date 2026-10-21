# Confirmation indicators on history: FAIL (2026-10-21)

Prereg: [indicator_confirm_hist_prereg.json](indicator_confirm_hist_prereg.json) (amended, amended_2, amended_3 — all before
the score). Script: `tools/studies/indicator_confirm_hist.py` at c865df99. Raw tables:
[INDICATOR_CONFIRM_HIST_2026-10-21_raw.md](INDICATOR_CONFIRM_HIST_2026-10-21_raw.md).

**The question.** Do ADX, OBV / A/D confirmation, the article's four-way confluence, or the 10/8 video setups add
anything to an entry? Tested on 110 warehouse sessions (2026-05-11 .. 10-15). H1-H5 filter desk-style pre-square entries on
a 400-name liquid sample. S3-S7 are standalone 10-minute entries on every name (~1,850 a day). Each entry is compared with
the same name's later minute (d) and with a random other name at the same minute (x), net of a 10 bp round trip.

**Answer: no.** Seven of ten cells FAIL: H2-H5 as filters, and S3, S5, S6 and S7 as entries. A FAIL means the effect is
ruled out at +5 bp or more. The other two, H1 and S4, are UNDERPOWERED. None passed.

| Cell | What it is | Verdict | Read |
|---|---|---|---|
| H1 | ADX >= 25 and +DI > -DI | UNDERPOWERED | DiD d +0.9 / -0.2 bp. The cross-control upper bound is 5.5 bp in half A, just above the bar, so this is not a lead |
| H2 | ADX >= 20 (skip chop) | FAIL | DiD -0.5 / -0.3 bp. Refused chop entries do no worse than kept ones (OUT lift +0.3 / +0.7) |
| H3 | OBV confirms the 30-min high | FAIL | DiD -0.1 / -1.8 bp |
| H4 | A/D confirms the 30-min high | FAIL | DiD -1.6 / -1.3 bp |
| H5 | Article confluence (trend + support + RSI turn + volume) | FAIL | DiD +0.4 / -0.6 bp |
| S3 | Break and retest | FAIL | lift -1.0 / -0.7 bp |
| S4 | First candle to a new high | UNDERPOWERED | -2.0 / +6.5 bp on 737 / 435 entries. MDE 13 / 19 bp; it can only fail on t <= -2 |
| S5 | Double bottom / pin at a level | FAIL | t -2.84 on x (half A) and -2.11 on d (half B). It loses money |
| S6 | Sweep, displacement, FVG retrace | FAIL | +1.6 bp (t 2.72) on d in half A only. x is +0.7, and half B is 0.0 |
| S7 | ICC second break | FAIL | +1.3 / -1.2 bp |

**The pre-square entry itself loses.** Every H cell's IN group nets -8 to -10 bp at a 10 bp cost and -1 to -3 bp at the
measured 3 bp. Raw lift vs a random minute is about 0. No filter changes that.

**Information cells (cannot pass) show nothing.** Pairwise ANDs, ADX terciles, C1 legs and SIP features are flat. So are
the every-5th-minute base population, setups as filters on PRE, and the time split. Two cells sit at |t| ~2.6 out of
roughly 150 printed, which is the noise you expect. Two information cells are not evidence:

- **"Drop the top 1% of name-days" is negative everywhere (t -2 to -4).** That trim removes only the name-days that helped
  IN most, so it pushes DiD down by construction. It is a one-sided robustness check, not a finding.
- **"Board-like name-days" is empty: a script bug.** The up-2% flag is a numpy bool and the filter tests `is True`. It does
  not touch any verdict. A fix means rerunning the count, null and placebo steps (script sha gate).

**Caveats.**
- 75% of PRE onsets (69k of 92k) fail the 80% IEX coverage rule. H covers names with dense IEX prints only.
- These are unscreened liquid names, not the movers board.
- The warehouse has been mined about 10 times; held-out data is only forward.
- S2 is not here (operator 10/21). It stays with structure_pullback, whose extension is still pending.
- The video's own 3R / 80-minute geometry is untested here. S3-S7 are closed only as 10-minute entries.

**Calibration.** Placebo (coin-flip IN/OUT on real data) OK: max |t| 1.76. A binding fresh-seed null (120 sessions x 2,500
names) OK: max |t| 2.62 vs a 3.39 bound, with no investigate flags. An earlier 40-session null failed on a normal-quantile
bound. amended_3 replaced it with this one before it ran, with the operator's approval. The skeptic re-implemented S3-S7
from the prereg text: 0 disagreements on 7,499 name-days.

**What it closes.** Together with the 10/2 levels study and the RSI/MACD work, the generic "confirmation indicator" list is
now tested on desk-style entries. Trend strength, volume confirmation and multi-indicator confluence add nothing at 5 bp or
more on the free feed. The desk-arm version (indicator_confirm_study) scores tonight; only H2 can reach a verdict there.
