# NR7 + inside day breakout: stopped UNDERPOWERED at the count step, not scored (2026-10-23)

- **Prereg:** `docs/studies/nr7_inside_prereg.json` (as amended).
- **Code:** `tools/studies/nr7_inside.py`, sha e11e0620.

**Count step** (outcome-blind):
- **Trades:** 1,428 in half A and 1,333 in half B, over 55 days each. Each trade is a long break of an NR7 inside day.
- **Smallest detectable effect:** 7.6 bp (A) and 5.6 bp (B), measured from control minus a second control.
- **Skipped, counted:**
  - 1,700 trades opened above the level (gap-through);
  - 4,506 never triggered.

**Reading.** Both halves are above the 5 bp limit, so the study is UNDERPOWERED as registered. No outcome was read, so this is **untested, not failed**.

**Review status.** The code was not code-reviewed. Stopping at the count step reads no outcome, so a review could not change a verdict. It would be needed before any longer-sample rerun.

**Prior.** Low: practitioner lore with no published test on US stocks. A longer sample would need its own pre-registration.
