# S3/S4 trade simulation (tools/trade_sim.py), 2026-11-07 11:39 ET

Trades: 776 matched since 2026-10-08 (1 unmatched outcomes), 776 simulated, 0 skipped for missing prints. 14 variants (cells) on one sample: about one t~2 is expected by chance; the lim grid is correlated with itself.
Sim baseline mean net at exit latency 0 / 0.5 / 1.0 s: -5.37 / -5.27 / -5.23 bp vs logged desk -7.84 bp. Known baseline gaps: no trend hold, no dead_trade / no_progress / double_check / left_overbought / EOD exits, no broker-mark low.

Verdict at 0.5 s exit latency (both arms). IMPROVES = beats the sim baseline on every condition; it does NOT mean profitable (last column).

| variant | verdict | changed | base -> var net (0.5 s) | delta @0 / @0.5 / @1.0 s | t | halves | winsorized halves | drop5 (reported) | boot p5 | failed | profitable @3 / @10 bp |
|---|---|---|---|---|---|---|---|---|---|---|---|
| lock50 | **NO** | 56 | -5.3 -> -5.2 | +0.08 / +0.11 / +0.16 | 1.13 | +0.05/+0.16 | +0.05/+0.03 | -0.12 | -0.04 | day-t >= 2.0; bootstrap p5 > 0 | no / no |
| step01 | **NO** | 441 | -5.3 -> -5.2 | -0.20 / -0.21 / -0.20 | -0.39 | -0.00/-0.42 | -0.16/-0.72 | -1.40 | -1.08 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| overtake_off | **NO** | 207 | -5.3 -> -5.1 | +0.23 / +0.10 / +0.10 | 0.53 | +0.32/-0.12 | +0.20/-0.22 | -0.30 | -0.21 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0 | no / no |
| argon_exits | **NO** | 449 | -5.3 -> -5.4 | -0.52 / -0.50 / -0.36 | -1.01 | -0.29/-0.72 | -0.40/-0.85 | -1.63 | -1.31 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| t15_60 | **NO** | 225 | -5.3 -> -5.7 | -0.50 / -0.50 / -0.44 | -4.00 | -0.27/-0.72 | -0.28/-0.76 | -0.80 | -0.71 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| t20_60 | **NO** | 113 | -5.3 -> -5.4 | -0.18 / -0.16 / -0.10 | -1.70 | -0.22/-0.10 | -0.25/-0.15 | -0.36 | -0.33 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| t25_60 | **NO** | 66 | -5.3 -> -5.3 | +0.01 / -0.02 / -0.05 | -0.29 | -0.17/+0.12 | -0.19/+0.03 | -0.18 | -0.15 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| t20_40 | **NO** | 113 | -5.3 -> -5.3 | -0.12 / -0.11 / -0.07 | -1.70 | -0.15/-0.07 | -0.16/-0.10 | -0.24 | -0.22 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| lim15_60_th2 | **NO** | 185 | -5.3 -> -5.3 | -0.04 / -0.16 / -0.11 | -0.96 | -0.33/+0.01 | -0.34/-0.20 | -0.50 | -0.43 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| lim15_60_th4 | **NO** | 143 | -5.3 -> -5.5 | -0.14 / -0.27 / -0.26 | -1.74 | -0.45/-0.09 | -0.45/-0.31 | -0.57 | -0.51 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| lim20_60_th2 | **NO** | 97 | -5.3 -> -5.1 | +0.18 / +0.11 / +0.11 | 0.74 | -0.22/+0.43 | -0.23/+0.19 | -0.24 | -0.12 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0 | no / no |
| lim20_60_th4 | **NO** | 79 | -5.3 -> -5.3 | +0.03 / -0.03 / -0.01 | -0.25 | -0.25/+0.18 | -0.26/-0.04 | -0.33 | -0.25 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0; S4 all exits 1 s late > 0 | no / no |
| lim25_60_th2 | **NO** | 55 | -5.3 -> -5.1 | +0.20 / +0.19 / +0.18 | 1.38 | -0.12/+0.50 | -0.13/+0.21 | -0.13 | -0.01 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0 | no / no |
| lim25_60_th4 | **NO** | 42 | -5.3 -> -5.2 | +0.13 / +0.13 / +0.13 | 0.92 | -0.14/+0.39 | -0.16/+0.09 | -0.18 | -0.07 | both halves > 0; day-t >= 2.0; winsorized both halves > 0; bootstrap p5 > 0 | no / no |
