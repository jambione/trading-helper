# Short squeeze (forced buyers): stopped UNDERPOWERED at the count step, not scored (2026-10-23)

Prereg: [short_squeeze_prereg.json](short_squeeze_prereg.json). Amendments amended .. amended_4 cover the skeptic's prereg
review and two code-review rounds. Script: `tools/studies/short_squeeze.py` (1a64e065).
- **Data:** 162 FINRA short-interest files (2020-01-15 .. 2026-09-30). Alpaca daily bars were fetched per symbol RUN
  with asof mapping (19,372 runs over 16,766 symbols; FB maps to Meta's history; BRKB and BFB map correctly).
- **No event return was read.** The count step prints standard errors and counts only.

**The question (operator, 'sell me this pen').** When a heavily shorted $20+ stock breaks out, do the shorts' forced
buy-backs make it beat comparable breakouts in normally shorted stocks over the next 5 days, by at least +50 bp?
- **Breakout:** +3%, twice normal volume, a 20-day closing high.
- **Groups:** top-decile days-to-cover vs normal-DTC breakouts in the same week, volatility tercile and dollar-volume
  tercile.
- **Trade:** buy at the next close, beta-adjusted 5-day hold.

**Why it stopped.**

| | 2020-22 | 2023-26 |
|---|---|---|
| Matched high-DTC breakouts | 797 | 1,161 |
| Normal-DTC breakouts | 3,812 | 6,238 |
| Standard error (largest of week / name / calendar-time) | 42 bp | 36 bp |
| **Smallest detectable effect** (bar: 50 bp) | **119 bp** | **102 bp** |

- **Data quality was fine:** missing names 0.27% (HIGH) vs 1.18% (NORMAL), inside the abort rule; event drops about 0%.
  FINRA's days-to-cover agrees with our own (Spearman 0.95).
- **The problem is noise:** five-day returns of breakout stocks swing too much. With about 1,000 matched events per half,
  only an effect above about 1% per week could be told apart from noise. By the registered rule the study stops
  UNDERPOWERED, so the outcome was never computed.

**What it means.** The free FINRA data cannot test the idea at a useful size. Detecting 50 bp would need about 5-6 times
as many events, which the 2017-2019 files cannot supply. A short-squeeze effect larger than about 1% per week is not
ruled in or out, because no returns were read. The idea stays untested, not failed.
