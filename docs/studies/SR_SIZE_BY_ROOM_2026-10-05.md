# Size by room to resistance — result (2026-10-05)

Pre-registration: [`sr_size_by_room_prereg.json`](sr_size_by_room_prereg.json) /
[`SR_SIZE_BY_ROOM_PREREG.md`](SR_SIZE_BY_ROOM_PREREG.md), committed at
**`363728bbefc86b4bea1ed5301df7b5f51918cb1a`** before any outcome was computed.
Queue: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) item #2.
Script: `tools/studies/sr_size_by_room.py`. Raw: `ai_reports/sr_size_by_room/`.

## Verdict

**PRIMARY: FAIL**

| | Half A (2026-09-04..09-18) | Half B (2026-09-21..10-02) |
|---|---|---|
| arms | **410** (6 days) | **1399** (10 days) |
| mean w / under-floor share | 0.583 / 83% | 0.612 / 78% |
| sized gross / **net** | +14.3 / **+2.7 bp** | +2.8 / **−9.5 bp** |
| equal-size gross / net | +26.3 / +6.3 bp | +4.5 / −15.5 bp |
| measured RT (mean(w)×20) | 11.7 bp | 12.2 bp |
| **sized − equal gross** | **−12.0 bp** (t −5.59) | **−1.7 bp** (t −0.60) |
| lift > RT? | **no** | **no** |

Printed: **"PRIMARY: FAIL** (bar, both chronological halves: sized−equal gross > mean(w)×20 bp, n >= 50; also report net)".

## Plain words

Half-sizing opens when room to the next charted resistance is under 0.40% does **not** beat
equal full size. About 80% of arms sit under the floor (many with no resistance above → room 0),
so the rule mostly just scales the book down. In half A equal-size was already gross-positive;
cutting size threw away +12 bp of gross contribution. In half B the gross lift is only −1.7 bp
(sized net is less bad than equal, −9.5 vs −15.5, but that is not the pass bar).

## Information (not the decision)

| cell | Half A lift / sized net | Half B lift / sized net | lift>RT both? |
|---|---|---|---|
| skip vs full @0.40% | −24.0 / −1.0 | −3.4 / −3.4 | no |
| half-size @0.60% | −12.2 / +2.8 | −2.1 / −9.4 | no |
| F fills half-size @0.40% | — (n 0) | +6.3 / −26.5 (RT 13.4; no) | no |

## Caveats
SIP ≠ IEX; 15-min hold; flat cost×w; same mined sessions; median room 0.00% (none-above → 0).
