# Honest early entry, round 2 (mid-rise, RSI-2 turn): FAIL (2026-10-13)

Prereg: `docs/studies/early_entry_mid_rsi2_prereg.json` (result-skeptic READY after 2 rounds, before any score).
Same harness, cached bars, control, cost and split as `EARLY_ENTRY_HONEST_2026-10-13.md`.
Null calibration: `tools/studies/early_entry_honest_null.py --study mid_rsi2`.

## Verdict
**MID (live mid-rise, bar close): FAIL. RSI2T (CM RSI-2 oversold turn): FAIL.** That closes all four of AGY's early-entry arms on this harness (PRE, IGN, MID, RSI2T).

| rule | half | n | gross | net (10 bp) | lift | t |
|---|---|---:|---:|---:|---:|---:|
| MID | A 9/08–9/24 | 423 | +10.5 | +0.5 | +11.4 | +0.36 |
| MID | B 9/25–10/13 | 736 | −2.1 | −12.1 | +4.2 | +2.24 |
| RSI2T | A | 1230 | −1.3 | −11.3 | +2.2 | +0.31 |
| RSI2T | B | 2101 | −2.2 | −12.2 | −0.6 | −0.33 |

## Notes
- **Mid-rise is the closest miss.** It beats its later-minute control in both halves (+11 and +4 bp) and the same-minute cross control (+15 bp, t 1.3), and it fires a median 4 minutes before desk fills (68% by ≥ 3 min). It still fails: half A is noise (t 0.36), half B's lift is under the 5 bp bar, and net after a 10 bp round trip is +0.5 and −12 bp. What it shows is a timing tilt smaller than the spread, not a way to make money.
- **RSI-2 turns** lead fills by 7 minutes but are coin flips; at 5 minutes they are −7.6 bp (t −2.1). Net at 20 bp: −22 bp (t −6.8).
- **MIDCAP** (with the admission RSI-2 ≤ 65 cap) is information only: 95 fires, underpowered, −62 / +11 bp.
- The board base rate is the same −8.3 bp per 10 minutes as in round 1.

## Raw output
# Honest early entry — 2026-09-08 .. 2026-10-13

days 26, half A ends 2026-09-24. Cost 10 bp RT, hold 10 min.

Scored signals per half: MID A 423 / B 736, RSI2T A 1230 / B 2101, MIDCAP A 39 / B 56, SQ A 276 / B 362

| rule | half | n | days | gross | net | lift | t | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| MID | A | 423 | 13 | +10.5 bp | +0.5 bp | +11.4 bp | +0.36 | FAIL |
| MID | B | 736 | 13 | -2.1 bp | -12.1 bp | +4.2 bp | +2.24 | FAIL |
| RSI2T | A | 1230 | 13 | -1.3 bp | -11.3 bp | +2.2 bp | +0.31 | FAIL |
| RSI2T | B | 2101 | 13 | -2.2 bp | -12.2 bp | -0.6 bp | -0.33 | FAIL |
| MIDCAP | A | 39 | 13 | -15.3 bp | -25.3 bp | -62.0 bp | -1.39 | UNDERPOWERED |
| MIDCAP | B | 56 | 13 | +3.3 bp | -6.7 bp | +11.4 bp | +1.07 | UNDERPOWERED |
| SQ | A | 276 | 13 | -11.8 bp | -21.8 bp | -13.3 bp | -0.48 | FAIL |
| SQ | B | 362 | 13 | -11.3 bp | -21.3 bp | -5.9 bp | -1.11 | FAIL |

**Verdicts:** MID FAIL, RSI2T FAIL, MIDCAP INFO, SQ INFO

### Information only

Lift at other holds (day-equal, t):
- MID: 5m +2.1 bp (t +0.25, n 1149); 15m -4.6 bp (t -0.38, n 1150); 30m +4.6 bp (t +0.26, n 1141)
- RSI2T: 5m -7.6 bp (t -2.06, n 3309); 15m -3.3 bp (t -0.57, n 3305); 30m -4.9 bp (t -0.89, n 3287)
- MIDCAP: 5m -10.3 bp (t -1.28, n 94); 15m -26.2 bp (t -0.92, n 95); 30m -43.5 bp (t -1.17, n 95)
- SQ: 5m -12.1 bp (t -1.09, n 632); 15m -23.1 bp (t -1.45, n 633); 30m -32.0 bp (t -1.09, n 627)

Lead before desk fills (latest fire within 15 min):
- MID: 297/1057 fills had a prior fire; median lead 4 min; lead >= 3 min 68%; 1185 fires, 18% followed by a desk fill within 10 min
- RSI2T: 537/1057 fills had a prior fire; median lead 7 min; lead >= 3 min 79%; 3390 fires, 10% followed by a desk fill within 10 min
- MIDCAP: 20/1057 fills had a prior fire; median lead 5 min; lead >= 3 min 70%; 97 fires, 14% followed by a desk fill within 10 min
- SQ: 197/1057 fills had a prior fire; median lead 2 min; lead >= 3 min 47%; 654 fires, 23% followed by a desk fill within 10 min

Other controls and costs (lift unless noted; day-equal, t, n):
- MID: cross +15.4 bp (t +1.28, n 1156); close_entry +9.9 bp (t +0.64, n 1159); net20 -15.8 bp (t -1.65, n 1159); later-control overlaps the hold 17%
- RSI2T: cross +9.5 bp (t +1.54, n 3321); close_entry +0.3 bp (t +0.07, n 3331); net20 -21.7 bp (t -6.81, n 3331); later-control overlaps the hold 16%
- MIDCAP: cross +16.9 bp (t +0.79, n 94); close_entry -26.8 bp (t -1.00, n 95); net20 -26.0 bp (t -1.46, n 95); later-control overlaps the hold 16%
- SQ: cross -5.6 bp (t -0.27, n 635); close_entry -4.9 bp (t -0.35, n 638); net20 -31.5 bp (t -2.95, n 638); later-control overlaps the hold 15%
- every board minute, gross 10m: -8.3 bp (t -2.77, n 73031)

Artifact demo (shift-5 lift on rule-free rising board minutes): +95.5 bp (t +8.93, n 1335)

Drops: {'MID_off_board': 5371, 'RSI2T_off_board': 22100, 'MIDCAP_off_board': 342, 'SQ_off_board': 5539, 'no_bars': 107, 'RSI2T_no_bar': 59, 'SQ_no_bar': 16, 'MID_no_bar': 26, 'MIDCAP_no_bar': 2}
