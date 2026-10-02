# Overnight book: account size vs whole-share auction orders (2026-10-02, for the 10/9 review)

Script: `tools/studies/overnight_account_size.py` (read-only). The live picker (overnight_book.rank) was replayed on the last
120 buy days (to 10/1), and each night's 20 picks were sized into whole shares (auction orders cannot be fractional; Alpaca fractional
orders are time_in_force=day only). Returns are close→next open from adjusted SIP daily bars; share prices are the raw close.
The live 15:40 −1% intraday filter is not applied.

Pick prices (raw close) p10/25/50/75/90: **$13 / $28 / $95 / $299 / $886**. Full equal-weight 20: +23.9 bp/night, sd 253 bp.

| account | names held | deployed | mean bp | sd bp | corr w/ full | tracking sd | nights for t=2 at 16 bp | $/night at 16 bp |
|---|---|---|---|---|---|---|---|---|
| $100 | 3.2 | 96% | +46.3 | 412 | 0.82 | 253 | 2,882 | 0.15 |
| $250 | 6.6 | 98% | +35.2 | 321 | 0.88 | 159 | 1,665 | 0.39 |
| $500 | 9.0 | 99% | +32.0 | 279 | 0.88 | 132 | 1,234 | 0.79 |
| $1,000 | 11.4 | 100% | +23.1 | 253 | 0.95 | 82 | 1,005 | 1.59 |
| $2,500 | 14.7 | 100% | +23.1 | 263 | 0.98 | 46 | 1,082 | 3.99 |
| $5,000 | 16.1 | 100% | +32.6 | 280 | 0.98 | 57 | 1,225 | 7.99 |
| $10,000 | 18.4 | 100% | +24.8 | 263 | 1.00 | 27 | 1,078 | 15.99 |
| $25,000 | 20.0 | 100% | +24.3 | 256 | 1.00 | 7 | 1,024 | 39.99 |
| live rule ($100, $25/order, 1 share) | 4.5 | 65% | −8.0 | 164 | 0.76 | 113 | 988 | 0.10 |

## Read
1. **Fidelity, not proof, scales with size.** Correlation with the full book goes 0.82 at $100 → 0.95 at $1k → 0.98 at $2.5k → 1.00 at $10k.
   Below ~$1k the account is a different (cheap-name, high-noise) book.
2. **No size proves the edge quickly.** At ~250 bp nightly sd, a 16 bp edge needs ~1,000 nights (about 4 years) for t = 2 at any size.
   Live money confirms *execution* (auction fills, fees, partial fills), not the return. The return evidence is the 10-year backtest.
3. The 120-night means (+23 to +46 bp) differ by noise; don't read them as a size effect.
4. The live rule holds 4.5 names and leaves 35% of the cash idle; it is a fill test, which is all it was meant to be.
