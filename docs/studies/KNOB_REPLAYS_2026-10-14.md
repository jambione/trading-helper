# Per-knob replays (2026-10-14)

Replays of the live code (`tools/replay_session.py`, sha fad7998) on 2026-10-07/08/09/12/13 with the 10/13 "new" config pinned (10:30 start, np180, pre-square + square arms, arm RVOL 1.25, tight 1.5% / RVOL 1.25, RSI max 65). Each arm changes one admission/arm knob; `base` is `/tmp/simnew/DAY_new.json` (same config, no change). Costed with the full SIP quoted spread at entry (`tools/studies/spread_rvol_score.py --arms ... --refs base`). RVOL values are on the inflated (pre-option-A) scale the desk recorded.

| arm | change | opens/day | opens/10m | concurrency | win % | median hold | net bp/trade (t) | value bp/day (t) | vs base, bp/day (t) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base | — | 22.6 | 0.62 | 0.12 | 31% | 94 s | −2.6 (−2.7) | −53.7 | — |
| rvol | arm RVOL floor 1.25 → 0 | 42.0 | 1.15 | 0.21 | 32% | 64 s | −3.1 (−3.5) | −119.5 | **−65.8 (−3.3)** |
| tightpct | tight min move 1.5% → 0.5% | 21.4 | 0.59 | 0.10 | 31% | 78 s | −2.7 (−3.0) | −47.9 | +5.7 (+0.8) |
| tightrvol | tight RVOL floor 1.25 → 0 | 23.6 | 0.65 | 0.12 | 31% | 80 s | −3.4 (−2.6) | −72.2 | −18.5 (−1.9) |
| rsi | arm RSI max 65 → 100 | 23.4 | 0.64 | 0.11 | 39% | 84 s | −2.5 (−2.2) | −59.1 | −5.4 (−1.1) |
| loose | all four | 74.4 | 2.04 | 0.41 | 34% | 72 s | −3.9 (−5.3) | −275.9 | **−222.2 (−7.5)** |

**Reading.** Removing the arm RVOL floor nearly doubles opens and the loose set triples them, but every trade still nets negative (−2.5 to −3.9 bp after spread), so each extra open adds loss: rvol costs 66 bp/day (t −3.3), loose 222 bp/day (t −7.5). Tight-screen knobs and the RSI cap change little. The opens problem and the per-trade problem are separate: loosening gates buys opens at a slightly worse per-trade mean; it cannot make the book profitable while the average trade loses ~3 bp.

**Live link.** The live arm floor is 0.45 on the honest scale since 10/14 evening (≈ 0.6 inflated), between these arms. The spread × RVOL sweep (queued) measures 0.6 / 0.95 / 1.25 with today's config.
