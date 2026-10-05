# Exit-only at resistance — result (2026-10-05)

Pre-registration: [`sr_exit_resist_prereg.json`](sr_exit_resist_prereg.json) /
[`SR_EXIT_RESIST_PREREG.md`](SR_EXIT_RESIST_PREREG.md), committed at
**`4aea5ed97ad2357f386f1bd00cbb2975843e787b`** before any outcome was computed.
Queue: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) item #1.
Script: `tools/studies/sr_exit_resist.py`. Raw: `ai_reports/sr_exit_resist/`.

## Verdict

**PRIMARY: FAIL**

Pass bar (locked): in both chronological halves, signal mean gross − control mean
gross must exceed that half's measured round-trip spread, with ≥ 50 paired trades.
Also report net after costs. Neither half clears the bar; lift is **negative** in both.

| | Half A (2026-09-04..09-18) | Half B (2026-09-21..10-02) |
|---|---|---|
| paired trades (days) | **118** (5) | **587** (10) |
| signal gross / **net** | −1.3 / **−21.3 bp** (t −1.88) | +0.1 / **−19.9 bp** (t −3.57) |
| control (no-S/R time stop) gross / net | +15.8 / −4.2 bp | +9.3 / −10.7 bp |
| measured RT spread | 20.0 bp | 20.0 bp |
| **signal − control gross** | **−17.1 bp** (t −1.42) | **−9.2 bp** (t −1.45) |
| lift > RT? | **no** | **no** |
| hit target / win (net>0) | 49% / 26% | 48% / 33% |
| median room at entry | 0.56% | 0.45% |

All: n 705, lift −10.5 bp vs RT 20.0 bp, net −20.1 bp. Drops: no_resistance_above 1104.

Printed verdict, exactly: **"PRIMARY: FAIL** (bar, both chronological halves:
signal−control gross > measured RT spread, n >= 50; also report net after costs)".

## Plain words

Keeping the desk's square opens and taking profit at the bottom of the next charted
resistance block **loses to** simply holding to the same 30-min / 15:55 time stop
without any S/R rule. The resistance exit is about −17 / −9 bp worse before costs in
the two chronological halves — the opposite of clearing a 20 bp round-trip hurdle —
and nets about −20 bp a trade after the 0.20% cost. About half the trades hit the
target; that early exit appears to cut winners short relative to the time stop.

## Information cells (not the decision)

| cell | Half A lift / net | Half B lift / net | lift>RT both? |
|---|---|---|---|
| E + support stop (n 117 / 579) | −17.5 / −21.4 | −8.8 / −19.5 | no |
| F fills, no stop (n 0 / 207) | — | +2.4 / −42.6 | no (A empty; B short of RT) |
| F + support stop (n 0 / 201) | — | +2.9 / −32.7 | no |

Adding the optional stop under support does not change the picture. Fills (F) only
exist in half B on this sample and stay deeply net-negative.

## Caveats (from the prereg)

- Control is the no-S/R time stop, not a full live ratchet replay.
- Flat 0.20% cost (= measured RT 20 bp); limit fill at target when high trades through.
- Target fixed at entry; SIP bars (live desk uses IEX).
- Same ~20 sessions already mined for levels and three prior order-block studies.
