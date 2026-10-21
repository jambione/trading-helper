# Confirmation indicators on desk arms: H2 FAIL, rest UNDERPOWERED (2026-10-21)

Prereg: [indicator_confirm_prereg.json](indicator_confirm_prereg.json) (amended, amended_2, amended_3). Script
`tools/studies/indicator_confirm_study.py` at 67067f8f, scored 2026-10-21 16:15 ET after a result-skeptic READY. Raw:
[INDICATOR_CONFIRM_DESK_2026-10-21_raw.md](INDICATOR_CONFIRM_DESK_2026-10-21_raw.md).

Population: the desk's real opens (decision-ledger arms with arm_ok True, $10+, 10:00-15:30, 31 sessions 9/08-10/20; 911
kept). Each arm's 10-minute net (10 bp) is compared with the same name's later minute; a filter is graded on the
difference between the arms it keeps and the arms it drops.

| Cell | Verdict | Read |
|---|---|---|
| H2 ADX >= 20 (skip chop) | **FAIL** | Half A DiD -40.6 bp (t -2.18). Half B +8.3 bp (t 2.47), under its t_crit of 2.90. The halves disagree in sign |
| H1 ADX trend | UNDERPOWERED | Half A has 40 IN arms (MDE 90 bp). Half B +1.9 bp (t 0.61) |
| H3 OBV, H4 A/D, H5 confluence | UNDERPOWERED | 12-89 IN arms per half; not scored |

This agrees with the powered history study the same day
([INDICATOR_CONFIRM_HIST_2026-10-21.md](INDICATOR_CONFIRM_HIST_2026-10-21.md): H2-H5 FAIL, H1 underpowered). The
information tables show H2 half B positive across variants (+7 to +10 bp, t 2.6-3.0, e.g. on SIP features). Half A is
strongly negative in the same variants (paper-fills DiD -54 bp, t -2.5), so the cell is noise around zero, not a hidden
edge. Video setups: 392 arm name-days counted, no fetch, not scored (the history study covered S3-S7).
