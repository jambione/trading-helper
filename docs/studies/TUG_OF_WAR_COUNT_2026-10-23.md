# Overnight vs intraday (tug of war): stopped UNDERPOWERED at the count step, not scored (2026-10-23)

- **Prereg:** `docs/studies/tug_of_war_prereg.json`. Amended_2 starts scoring on 2026-05-26 so that no look-back touches the held-out window.
- **Code:** `tools/studies/tug_of_war.py`, sha at 231ef8f4.

**Pre-check (outcome-free).** Alpaca daily open/close was compared with the warehouse 09:30 open and 15:59 close on 20 seeded name-days. Median gap: 0.4 bp. The two sources agree.

**Count step:**

| | Half A (05-26 .. 07-29) | Half B (07-30 .. 10-15) |
|---|---|---|
| Days | 45 | 55 |
| Long trades | 7,303 | 8,839 |
| Smallest detectable effect (placebo MDE) | 7.7 bp | 5.04 bp |

**Result:** both halves are above the 5 bp limit, so the study stops UNDERPOWERED as registered. No outcome was read. The idea is **untested, not failed**.

**Why the power is short:** a full-day hold (09:45 to the close) is noisy. Half A also lost about 10 sessions to the strict held-out separation.

**What would test it:** more sessions. A longer warehouse, or a daily-bar-only version on years of data, would need its own pre-registration.
