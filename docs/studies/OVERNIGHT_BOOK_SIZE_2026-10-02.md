# Overnight book: 20 vs 30 vs 40 names, risk and whole shares (2026-10-02, for the 10/9 review)

> **Skeptic review 2026-10-03** ([SKEPTIC_REVIEW_2026-10-02.md](SKEPTIC_REVIEW_2026-10-02.md) §4): **"stay at 20" is a default,
> not a finding** — no decision rule was fixed in advance, and 30/40 names do not lose in both halves (N30 −2.2/−0.7, N40
> −1.2/−3.3 bp). Tail metrics are single observations and reverse between halves. Holds: the tail is market beta (~1.4);
> the lever for it is a SPY overnight hedge (untested). Beta 1.44 here vs 0.6 in LONGER_HOLDS: different benchmarks (SPY
> overnight vs close→close); alpha after beta and cost ≈ +8-9 bp/night.

Script: `tools/studies/overnight_book_size.py` (measures committed before the run, d7f2584). Live rules otherwise:
12-1, ≥ $5, ADV ≥ $50M, equal weight, −1% intraday filter (so N20/N30/N40 hold 12.2/18.7/25.3 names on average).

## Bottom line: stay at 20
- **Return:** 30 and 40 names give up 1.5 and 2.2 bp/night over all 10 years (t −1.3 / −1.7). Net Sharpe is the same or lower
  (OOS 1.14 / 1.14 / 0.94).
- **Tail protection is small and inconsistent.** OOS the worst night improves (−12.5% → −10.5% → −9.7%) and so does the worst month
  (−16.7% → −14.5% → −12.8%). But in 2017–21, 30 names had a *worse* worst night (−15.3% vs −14.5%), a worse drawdown (−37% vs −33%) and a worse
  month (−27% vs −24%). The worst 1% of nights (CVaR) barely moves: −7.1% / −6.9% / −6.5% over all years.
- **Why:** the risk is the market, not the names. The book's beta to SPY's overnight move is about **1.4 at every size**
  (1.44 / 1.43 / 1.39). More momentum names add more of the same market exposure, so the bad nights stay bad.
- **Whole shares:** at $2,500 every size tracks its own equal-weight book (0.99 / 0.98 / 0.97). At $1,000, 40 names drops to 0.92.
- **Correction to OVERNIGHT_VARIANTS:** "N30/N40 cut the tail at no OOS cost" was too strong. The OOS worst night improves,
  but not consistently across periods, and it costs 1.5–2 bp/night.

If the tail matters, the lever is the market exposure (beta ~1.4), e.g. a SPY overnight hedge. That is untested and would be its own study.

## Raw
```

nights IS 1251, OOS 1186; names held (OOS mean): N20 12.2, N30 18.7, N40 25.3

OOS 2022-26
book   gross    net    sd  Sharpe  green  worst%    p1%  CVaR1%  maxDD%  longest  worst mo%
N20    +17.0  +13.0   182    1.14    56%   -12.5   -5.2    -6.9   -26.1      249      -16.7
N30    +16.3  +12.3   172    1.14    56%   -10.5   -4.9    -6.5   -23.6      411      -14.5
N40    +13.7   +9.7   163    0.94    57%    -9.7   -4.8    -6.4   -25.9      391      -12.8

IS 2017-21
book   gross    net    sd  Sharpe  green  worst%    p1%  CVaR1%  maxDD%  longest  worst mo%
N20    +19.6  +15.6   161    1.53    61%   -14.5   -5.1    -7.4   -33.4      207      -24.4
N30    +17.3  +13.3   151    1.40    61%   -15.3   -4.6    -7.5   -37.1      222      -27.4
N40    +18.4  +14.4   140    1.63    63%   -14.1   -4.1    -6.7   -35.6      222      -24.8

ALL
book   gross    net    sd  Sharpe  green  worst%    p1%  CVaR1%  maxDD%  longest  worst mo%
N20    +18.3  +14.4   172    1.33    59%   -14.5   -5.3    -7.1   -33.4      599      -24.4
N30    +16.8  +12.9   161    1.26    59%   -15.3   -4.8    -6.9   -37.1      766      -27.4
N40    +16.1  +12.1   152    1.27    60%   -14.1   -4.6    -6.5   -35.6      763      -24.8

paired diff vs N20, all nights:
  N30: -1.5 bp/night (t -1.3)
        beta to SPY overnight N30 1.43  N40: -2.2 bp/night (t -1.7)
        beta to SPY overnight N40 1.39; N20 1.44

whole shares (all nights; corr = whole-share book vs its own equal-weight book):
  account     N20 names/dep/corr     N30 names/dep/corr     N40 names/dep/corr
   $1,000     10.1 /  99% / 0.97     12.6 /  99% / 0.95     14.0 /  99% / 0.92
   $2,500     12.1 / 100% / 0.99     17.4 / 100% / 0.98     21.4 / 100% / 0.97
   $5,000     12.5 / 100% / 1.00     18.8 / 100% / 0.99     24.9 / 100% / 0.99
  $10,000     12.6 / 100% / 1.00     19.1 / 100% / 1.00     25.7 / 100% / 1.00
  $25,000     12.7 / 100% / 1.00     19.2 / 100% / 1.00     26.0 / 100% / 1.00
```
