# Premarket depth test: pre-registration (2026-10-04, before any data)

Machine-readable spec: [`premarket_depth_prereg.json`](premarket_depth_prereg.json). Written and committed before any
premarket data (Alpaca or Databento) was fetched for this study.

- **Universe:** 63 sessions (2026-06-01..08-28). Momentum names qualify point-in-time between 07:00 and 09:24 ET:
  price $2–20, at least +10% vs the prior close, at least 50k premarket shares, and premarket volume at least 10%
  of the 20-day average daily volume. Built from free Alpaca SIP bars. No float filter.
- **Signal (primary):** the first 1-minute close above the premarket high after qualifying, one per name-day.
  **Control:** 3 random minutes per name-day after qualifying (seed 23).
- **Pricing:** Databento XNAS.ITCH book. Buy at the ask and sell at the bid 0.5 s after the decision for net;
  mid-to-mid for gross. Primary hold 5 min; information holds 2 min, 15 min and to 09:35.
- **Bar (Jonathan's):** signal minus control, *before costs*, must beat the measured round-trip spread (gross − net
  of signal entries) **in both chronological halves** with day-clustered t ≥ 2. The power check comes first:
  2.84 × SE ≤ the first-half round-trip spread and signal n ≥ 100 per half, else no verdict.
- **Budget:** $10 hard cap on the quoted total, from the free credit. The design is the first affordable rung of
  mbp-10 → mbp-1 → mbp-1 for $2–10 names → drop sessions from the start.
