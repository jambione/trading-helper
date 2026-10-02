# Overnight book: one-parameter variants on the 10-year panel (2026-10-02)

Pre-registered: `overnight_variants_prereg.json` (dcb9856, before any result). Script: `tools/studies/overnight_variants.py`.
Base = the live config (top-20 12-1 momentum, ≥ $5, ADV ≥ $50M, equal weight, drop picks down > 1% on the day).
IS 2017-01-13..2021-12-31 (1,251 nights), OOS 2022-01-03..2026-09-24 (1,186 nights). Gross bp/night; net = gross − 4 bp
on nights with a book. Diff = variant − base, paired by night.

## Bottom line
- **Nothing passes. The live configuration is already the best, or tied with the best, of the 18.** No variant beats it out of sample
  (the best OOS diff is −0.0 bp). The pass bar was +2 bp with t ≥ 3.
- **Shorter momentum is worse out of sample:** 3-1 −12.7 bp/night (t −3.2) and 6-1 −6.6 (t −2.0). Both looked better in sample
  (+6, t ≥ 2), the classic overfit trap. Keep 12-1.
- **Book size:** 5 or 10 names are noisier and no better; 30–40 are no better. Keep 20.
- **Liquidity floor, $10 price floor, inverse-vol weights, SPY down-day skip:** all within ±1 bp OOS. No reason to change.
- **The −1% intraday filter:** +4.1 bp IS (t 1.7) but +0.9 bp OOS (t 0.4) over no filter. Weaker than the 10/1 estimate, still not
  harmful. It holds 12.2 names on average vs 20 (it uses the full-day close; live uses 15:40, so live drops fewer, e.g. 15/20 on 10/2).
- **Tail risk:** the base book's worst OOS night was **−12.5%**, and N5's was −30%. This is the cost of an unhedged overnight book;
  more names (N30/N40) cut the OOS worst night to −10.5%/−9.7%, but not in 2017–21 and at −1.5/−2.2 bp/night; see OVERNIGHT_BOOK_SIZE_2026-10-02 (stay at 20).

## Table
```
nights IS 1251 (2017-01-13..2021-12-31), OOS 1186 (2022-01-03..2026-09-24)

variant   names  IS gross  OOS gross  OOS net  green   worst    IS diff (t)    OOS diff (t)  verdict
BASE       12.2     +19.6      +17.0    +13.0    56%   -1252              —               —
N5          2.9     +27.2      +11.2     +7.5    51%   -2992    +7.6 (+1.3)     -5.8 (-1.3)  no
N10         6.0     +22.4      +16.0    +12.1    55%   -1911    +2.8 (+1.0)     -1.0 (-0.4)  no
N30        18.7     +17.3      +16.3    +12.3    56%   -1047    -2.2 (-1.5)     -0.7 (-0.4)  no
N40        25.3     +18.4      +13.7     +9.7    57%    -968    -1.2 (-0.6)     -3.3 (-1.8)  no
L6         12.2     +26.3      +10.4     +6.4    54%   -1375    +6.7 (+2.3)     -6.6 (-2.0)  worse
L9         12.1     +21.9      +13.6     +9.7    54%   -1263    +2.3 (+1.1)     -3.4 (-1.4)  no
L3         12.4     +25.7       +4.3     +0.3    52%   -1224    +6.1 (+2.0)    -12.7 (-3.2)  worse
L12_0      12.2     +24.1      +12.9     +8.9    54%   -1911    +4.5 (+1.9)     -4.1 (-1.6)  no
ADV20M     12.0     +17.6      +16.9    +12.9    55%   -1129    -1.9 (-1.0)     -0.1 (-0.1)  no
ADV100M    12.5     +20.6      +17.0    +13.0    55%   -1117    +1.0 (+0.6)     -0.0 (-0.0)  no
ADV250M    12.9     +17.9      +16.4    +12.4    55%   -1547    -1.6 (-0.6)     -0.6 (-0.2)  no
PX10       12.3     +17.9      +16.5    +12.5    56%   -1070    -1.6 (-1.5)     -0.5 (-0.5)  no
IVOL       12.2     +17.4      +16.2    +12.2    56%   -1214    -2.1 (-1.6)     -0.9 (-0.6)  no
F_OFF      20.0     +15.4      +16.1    +12.1    57%   -1141    -4.1 (-1.7)     -0.9 (-0.4)  no
F_M2       14.3     +18.9      +16.5    +12.5    57%   -1252    -0.6 (-0.5)     -0.5 (-0.4)  no
F_0         9.8     +20.1      +17.0    +13.1    55%   -1157    +0.6 (+0.3)     -0.0 (-0.0)  no
SPY_SKIP   11.6     +17.9      +16.8    +13.2    51%   -1252    -1.6 (-0.9)     -0.2 (-0.1)  no
```
