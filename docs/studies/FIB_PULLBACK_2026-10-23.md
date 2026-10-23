# Fibonacci golden-pocket pullback: scored 2026-10-23

**Verdict: UNDERPOWERED by the registered rule. In practice, NOT PASSED: after the 5 bp cost the trade loses money in both halves.**

- Prereg: `docs/studies/fib_pullback_prereg.json`, with `amended` (skeptic FIX FIRST, 13 items) and `amended_2` (code-review FIX FIRST, 8 items). Both amendments were registered before any GP outcome was read.
- Code: `tools/studies/fib_pullback.py`, module sha b7414588…, prereg commit ac43666f.
- Skeptic: READY on round 2 (a7e8ca2).
- Output on the mini: `ai_reports/fib_pullback/result.json`. The earlier count, from code before the fixes, is kept in `superseded_v1/`.

## Rule (practitioner method)
- **Swing:** from the day's low L, a move of at least 2% to a high H made at or after 09:45.
- **Zone touch:** a bar reaches the 50% retracement without closing below the 78.6% line.
- **Confirmation:** the first bullish bar within 5 bars, closing above VWAP.
- **Entry:** the next bar's open.
- **Bracket:**
  - stop: a close below the 78.6% line;
  - target: H;
  - time stop: 30 minutes;
  - cost: 5 bp.
- **Sample:** 400 sampled names (the study's usual book-like sample) × 110 SIP sessions.
- **Control:** a random bullish bar above VWAP in the same name and the same clock hour, with the same % stop, % target and time stop.

## Primary (day-equal, day-clustered)

| | Half A | Half B |
|---|---|---|
| GP trades (with a control) | 1,464 (1,255) | 1,026 (910) |
| GP minus control | **+31.2 bp, t 7.8** | **+36.0 bp, t 9.1** |
| GP net after 5 bp (all GP trades) | **-7.6 bp** | **-3.7 bp** |
| Win rate | 50.3% | 50.1% |
| Exits: time / target / stop / gap | 818 / 324 / 223 / 99 | 674 / 169 / 113 / 70 |

**Why it did not pass.** The pass rule needs both a lift of at least +5 bp with t ≥ 2 *and* GP net > 0. The lift is there; the net is not. It is not a FAIL because the lift's t is not ≤ -2 and its upper bound is not below +5 bp.

**Fibonacci specificity: no.**
- GP beats SHALLOW (the 23.6-38.2% depth) by +23 / +30 bp (t 5.5 / 8.0).
- GP ties DEEP (the 70-78.6% depth, stop at L): -0.0 / +0.6 bp (t 0.0 / 0.5).
- So deeper pullbacks are less bad, but nothing about 50-61.8% in particular.

## Reading the large lift
The lift does not mean the Fibonacci trade earns money. It mostly measures how badly the control loses.

- **Control trades lose 22-26 bp gross.** A random bullish-bar, above-VWAP entry in a name that just ran 2% loses about that much over these 30-minute brackets. This is the universe fade the desk lives in (fills drift -28 to -43 bp at 15-30 min).
- **GP trades gross about 0 overall:**

  | | Half A | Half B |
  |---|---|---|
  | All GP trades, gross (trade-weighted) | +0.2 bp | +0.8 bp |
  | All GP trades, gross (day-weighted) | -2.6 bp | +1.3 bp |
  | GP trades with a control, gross | +8.6 bp | +9.7 bp |
  | GP trades with no control (209 / 116) | **-70 bp** | **-61 bp** |

- **The control is missing in falling name-hours.** No bullish bar above VWAP exists in the same hour there. Those GP trades lose heavily, so the paired statistic also leans in GP's favour by selection.
- **Every pullback variant beats its own control:**

  | Variant | Lift over its control |
  |---|---|
  | NO_VWAP | +50 bp |
  | DEEP | +32 bp |
  | NO_CONFIRM | +26 / +34 bp |
  | RANDOM_DEPTH | +14 / +16 bp |
  | SHALLOW | +8 / +6 bp |

  That pattern says "waiting for a pullback avoids part of the fade," not "Fibonacci levels hold."

## What it means for the desk
- **No new entry rule.** A deep-pullback entry, Fibonacci or not, roughly breaks even before costs on SIP bars, which is still below the ~+11 bp entry edge the exits need.
- **The one lead is relative:** buying after a pullback loses ~25 bp less than buying a random bullish bar in the same names. This matches the earlier "near support least bad" result.
  - Turning that into a test would need its own prereg against the desk's own entries.
  - On 10/14 and 10/16, waiting for a dip after a signal cost 14-36 bp. That points the other way, so the prior is low.
- **Desk config unchanged** (the floor20 freeze).

## Known issue (cosmetic)
The `net_by_cost` label "2bp" is really 3 bp (`int(0.0003*1e4)` = 2). The other labels are correct.
