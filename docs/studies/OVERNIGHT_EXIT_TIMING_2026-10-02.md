# Overnight book exit timing (2026-10-02)

> **Skeptic review 2026-10-03** ([SKEPTIC_REVIEW_2026-10-02.md](SKEPTIC_REVIEW_2026-10-02.md) §3): the verdict stands, but
> read it as **no later exit is better gross (t −0.6 to −1.4, not significant); market sells after the open add spread on
> top. Keep the auction.** The net t −2.9..−3.7 comes from charging the 09:35 half-spread (~14.7 bp) at every later time,
> and "monotone giveback" is one cumulative path. The gross column was not produced by the committed script. Limit/hybrid
> exits were not tested.

Pre-registration: `docs/studies/overnight_exit_timing_prereg.json` (98bdd00). Script: `tools/studies/overnight_exit_timing.py` (mini). 500 nights 2024-09-27..2026-09-25, top-20 liquid 12-1 momentum picks bought at the close, SIP 1m bars 09:30–10:30 on the next morning.

**Verdict: keep selling at the opening auction.** No later exit beats it, before or after costs.

| Exit | Gross/night | Gross vs open (t) | Net/night | Net vs open (t), older / newer half |
|---|---|---|---|---|
| Opening auction (live) | +20.5 bp | — | **+17.3 bp** | — |
| 09:35 | +17.7 | −2.8 (−0.6) | −0.2 | −17.5 (−3.7): −19.6 / −15.3 |
| 09:45 | +13.6 | −6.9 (−1.1) | −4.2 | −21.4 (−3.3): −24.0 / −18.9 |
| 10:00 | +10.0 | −10.5 (−1.3) | −8.1 | −25.4 (−3.1): −27.0 / −23.8 |
| 10:30 | +6.6 | −13.9 (−1.4) | −11.5 | −28.8 (−2.9): −25.2 / −32.4 |

- **Gross:** the names give back steadily after the open (−3 bp by 09:35 → −14 bp by 10:30). The gap isn't significant at any one time, but it's monotone.
- **Net:** a post-open market sale pays half the early-morning spread (wide right after the open), so every later exit loses 17–29 bp against the auction (t −2.9 to −3.7, both halves).
- **Costs:** open = 2 bp per auction leg; later = 2 bp + half the per-name modeled 09:35 spread (conservative for 10:00+).
