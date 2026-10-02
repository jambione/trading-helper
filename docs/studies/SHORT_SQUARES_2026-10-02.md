# Short the desk's own entry signals (2026-10-02)

Pre-registration: `docs/studies/short_squares_prereg.json` (213b0e2, before results). Script: `tools/studies/short_squares_study.py` (70d3c42, run on the mini).
Data: 21 days (09-04..10-02), 873 admitted name-days, SIP 1m bars with extended hours; 298 desk buy fills since 09-23. Costs by price tier ($10+ 0.20%, $5–10 0.40%, <$5 1.00% round trip). Primary set: shortable and easy-to-borrow today, not in short-sale restriction.

## Bottom line

**FAILS, decisively.** Shorting at the square loses money in every period, at every hold and with every stop.

| A: short at the square, primary set (1,604 trades) | tune 09-04..17 | validate 09-18..25 | holdout 09-28..10-02 |
|---|---|---|---|
| **30 m, +3% stop (pre-chosen cell)** | −0.47% (t −6.8) | −0.35% (t −7.9) | −0.28% (t −5.2) |
| 5 m, no stop | −0.30% | −0.24% | −0.35% |
| 15 m, +1% stop | −0.33% | −0.27% | −0.30% |
| win rate (range across cells) | 22–35% | 25–35% | 22–37% |

- Pooled tune+validate at the pre-chosen cell: −0.38% per trade, t −10.4. Every one of the 60 cells (3 horizons × 5 stop settings × 4 sets) is negative on all three periods.
- **Before costs it's about zero.** The primary set mostly costs 0.20–0.40% a round trip, about the size of the net loss. **Price neither falls nor rises after a square on average; the short just pays the spread.** That matches the long side's finding that setups are noise-equivalent (same mean as a random minute).
- **The squeeze tail is real but secondary.** All names: 30 m squeeze p99 +18.9%, max +224%, 12% of entries go +3% against the short. The primary set is milder (p99 +5.3%, max +37%).

| B: desk's actual fills flipped to shorts, primary (234) | 09-23..28 | 09-29..10-02 |
|---|---|---|
| 30 m, +3% stop | +0.07% (t 0.7), median −0.08% | +0.01% (t 0.2), median −0.10% |
| 5 m | −0.09% | −0.17% |

- The post-fill drift down (−43 bp at 30 m, 2026-09-29 study) shows up as roughly **+0.2–0.3% gross at 30 m**. At tier costs it nets to about zero; with the desk's measured ~11 bp spread it would be roughly +0.1%, at t < 1, with a **negative median** in both halves. It's the long trade's mirror image: the fill moment loses to the spread on both sides.
- The desk's gates (spread, staleness, price) select tamer names: squeeze p99 only +2.8%. But there's nothing left to harvest after costs.

## Why "we can always find a downturn" misleads

Every entry is followed by *some* dip (median 30-minute squeeze +0.5%; prices wiggle both ways). A rule that shorts at the signal and covers on a schedule or stop captures the average move, and the average move after a square is about zero. The spread then decides the sign.

## Verdict

Don't build a short side on the square signal. Combined with `short_fade_study` (admitted names, 2026-09-25), both directions on this universe are cost-dominated coin flips on free data. The remaining levers are the ones already in play: cost (limit entries, the three-arm test), avoiding the worst cells (the open, sub-$10 names), and the overnight book.
