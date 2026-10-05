# Breakout confirmation (not the first poke) — result (2026-10-05)

Pre-registration: [`sr_breakout_confirm_prereg.json`](sr_breakout_confirm_prereg.json) /
[`SR_BREAKOUT_CONFIRM_PREREG.md`](SR_BREAKOUT_CONFIRM_PREREG.md), first committed at
**`6b0047ee924e0f24b54e0084d5cda990fac0fe2d`** before any outcome was computed.
Queue: [`SR_TEST_QUEUE_2026-10-05.md`](SR_TEST_QUEUE_2026-10-05.md) item #2b.
Script: `tools/studies/sr_breakout_confirm.py`. Raw: `ai_reports/sr_breakout_confirm/`.

## Verdict

**PRIMARY: FAIL**

| | Half A | Half B |
|---|---|---|
| paired events | **240** (5 days) | **1062** (10 days) |
| confirmed gross / **net** | +24.4 / **+4.4 bp** | +16.3 / **−3.7 bp** |
| first-poke gross / net | +43.8 / +23.8 bp | +32.9 / +12.9 bp |
| measured RT | 20.0 bp | 20.0 bp |
| **confirmed − poke gross** | **−19.4 bp** (t −2.27) | **−16.6 bp** (t −2.12) |
| lift > RT? | **no** | **no** |
| mean confirm delay | 1.7 bars | 2.0 bars |

Printed: **"PRIMARY: FAIL** (bar, both chronological halves: confirmed−poke gross > measured RT, n >= 50; also report net)".

## Plain words

Waiting for a close above the broken top **plus a higher low** before entering loses about
17–19 bp of gross to buying the first poke on the same break events. The delay (about 2
bars) gives back the edge; confirmed net is near zero / slightly negative while first-poke
net is still positive before a live trail is modelled. Volume confirmation and “either”
rules are the same story (info lifts about −16 to −21 bp).

Poke-only events that **never** confirm within 15 min are awful (half A n 18 gross −55.8 bp;
half B n 169 gross −49.5 bp) — those look like failed breakouts — but requiring confirmation
on the events that *do* confirm still underperforms just taking the poke on that same set.

## Information (not the decision)

| cell | Half A lift | Half B lift | lift>RT both? |
|---|---|---|---|
| volume confirm paired | −21.1 | −21.4 | no |
| higher-low OR volume | −18.6 | −16.0 | no |

## Caveats
SIP ≠ IEX; 15-min hold ≠ live trail; flat 0.20%; same mined sessions; confirmation delay is intentional.
