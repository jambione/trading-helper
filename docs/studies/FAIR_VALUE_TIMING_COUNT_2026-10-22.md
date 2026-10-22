# Fair-value timing layer: stopped UNDERPOWERED at the count step (2026-10-22)

Prereg: [fair_value_timing_prereg.json](fair_value_timing_prereg.json) (amended). Script: `tools/studies/fair_value_timing.py`
(87fb8a93). Outcome-blind: no outcome was read.

**What happened.**
- After the 0.05% spread cap, 3,626 pre-square entries remain; 3,203 have a fair-value Z (290 lack peer coverage, 133
  lack a full 30-minute window).
- Only about 3% of them are CHEAP (Z <= -1): 52 in half A and 45 in half B, on 3 days with enough entries each side.
- F2 (cheap AND strong buy flow) has 5 and 14.
- The registered floors (F1 1,000 IN, F2 300 IN, 20 days per half) are far out of reach. By the prereg (amended (1)) the
  test stops as **UNDERPOWERED**, with no fallback.

**Why: the desk's entries are almost never cheap.** At a pre-square entry the median Z is +0.25 (p10 -0.49, p90 +1.14).
13% of entries are RICH (Z >= +1) and 3% are cheap. The pre-square trigger buys a name that has just outrun its market and
peers, so the fair-value layer rarely has anything to say about these entries. That is a finding in its own right: the
desk's timing and fair-value cheapness point in opposite directions.

**What it leaves open (needs its own prereg).** The operator's original framing was a timing level that does not
depend on the indicators. The honest test of that idea scores every admitted-name minute (not only pre-square onsets)
and buys when the name is cheap versus its fair value. Cheapness is then the entry trigger, not a filter on an
already-rich entry. The same model, controls and bars would apply. The RICH share (13%) also suggests a cheap avoid-filter
read ("do not buy when Z >= +1"), which is information only here.
