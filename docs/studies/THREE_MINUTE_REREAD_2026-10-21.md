# Three-minute re-read: FAIL (2026-10-21)

Prereg: [three_minute_reread_prereg.json](three_minute_reread_prereg.json) (amended, amended_2, both before the score).
Script: `tools/studies/three_minute_reread.py` at aaf4d17b, scored by `three_minute_autoscore.py` (b6f87441) after it
checked every one of the result-skeptic's round-2 READY conditions. Raw tables:
[THREE_MINUTE_REREAD_2026-10-21_raw.md](THREE_MINUTE_REREAD_2026-10-21_raw.md).

**The question (operator 10/21).** What gives us a profitable 3-minute run that our exit can capture? We tested every
entry rule and filter built here at a 3-bar hold. Entry is at the open of the minute after the signal bar, and exit is at
the close two minutes later. The data is 110 warehouse sessions (2026-05-11 .. 10-15). Each entry is compared with the same
name's later minute (d) and with a random other name at the same minute (x), net of a 5 bp round trip.

**Answer: nothing.** 17 of 18 cells FAIL, so each is ruled out at +5 bp or more. S4 is UNDERPOWERED. No cell reached
confirmation, so the 60 held-out sessions (fetched, complete) stay unread.

| Group | Cells | Lift vs controls (both halves) | Verdict |
|---|---|---|---|
| %R / RSI rules | PRE, SQ, MID, BOTHUP, OSL, IGN, RSI2T, CON | -0.3 .. +0.3 bp on 47k-195k entries per half | FAIL (upper bounds <= 0.5 bp) |
| Video setups | S3, S5, S6, S7 | -1.2 .. +0.3 bp | FAIL. S5 and S7 lose (t -2.0 / -2.9) |
| Video setup | S4 (first new high) | +1.8 / +5.4 bp d, -1.2 / +6.2 bp x, on 744 / 432 entries | UNDERPOWERED. The upper bound is 12 bp |
| Filters on PRE | H1-H5 | -1.1 .. +1.8 bp DiD | FAIL (upper bounds <= 3.9 bp) |

**What an exit could have captured.** The mean maximum favourable excursion inside the 3 minutes is about +10-12 bp. The
mean maximum adverse excursion is about -10-12 bp. That holds for every rule (IGN +15-19 / -15-19, S4 about +/-25). The
runs exist, but they are symmetric: no rule tilts the window up. Raw net at the 5 bp cost sits at about -5 bp, which is the
cost itself. At the desk's 3 bp it is about -3 bp.

**The desk's own fills** (1,183 fills over 46 days from outcomes.jsonl, names in the warehouse): +1.2 / +0.1 / +0.7 /
-0.7 bp gross at 1 / 2 / 3 / 5 minutes. Every t is under 0.8, so this is flat.

**Information cells that are not evidence.** S4 half B is +5-6 bp with a +16.8 bp before-11:25 cell (t 2.45) on 432
entries. That is one half, underpowered, and among many printed cells, so it is not a lead. If it is ever revisited, it
needs its own prereg on new data. S6's lag-1 / 5-minute lifts of +0.6-1.0 bp (t 2-3) are far below the 5 bp bar.

**Calibration.** Binding null OK (2.4M synthetic entries, max |t| 2.37 vs 3.58). Placebo OK (max |t| 1.20). Every
condition the skeptic set held: fetch 2026-01-14 .. 04-24 complete, count unchanged, null max |t| 2.37.

**What it closes.** Together with the 10-minute studies, this settles the operator's question on the free feed. No entry
rule, setup or confirmation filter here predicts the direction of the next 3 minutes. The move is about +/-10 bp both
ways, so exits can only reshape the win rate. Order flow (order_flow_pilot_prereg.json, trade-side signing on SIP ticks) is
the one registered route left; it has not been run.
