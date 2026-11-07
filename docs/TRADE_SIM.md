# S3/S4 trade simulation (tools/trade_sim.py), 2026-11-07 10:44 ET

776 desk entries since 2026-10-08 with cached post-fill SIP prints (max 1500 s). Price-only: trend hold OFF in every arm. Sim baseline mean net -11.85 bp vs logged desk net -7.84 bp (the gap is the trend hold, fills and the window). Bad-tick filter dropped 1070 of 7005628 prints (0.015%).

| variant | verdict | n | changed | base -> var net bp | mean day delta | t | halves | drop5 | boot p5 | 1 s delay delta | failed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| lock50 | **PASS** | 776 | 105 | -11.9 -> -11.3 | +0.91 | 2.03 | +1.04/+0.78 | +0.09 | +0.29 | +0.01 |  |
| step01 | **FAIL** | 776 | 389 | -11.9 -> -13.4 | -1.62 | -3.89 | -1.36/-1.88 | -2.36 | -2.30 | +0.24 | both halves > 0; day-t >= 2.0; drop best5 days+top5 names > 0; bootstrap p5 > 0 |
| overtake_off | **FAIL** | 776 | 214 | -11.9 -> -13.1 | -1.33 | -6.46 | -1.22/-1.43 | -1.59 | -1.67 | +0.08 | both halves > 0; day-t >= 2.0; drop best5 days+top5 names > 0; bootstrap p5 > 0 |
| argon_exits | **FAIL** | 776 | 442 | -11.9 -> -12.1 | +0.38 | 0.49 | +0.46/+0.29 | -1.19 | -0.76 | +0.08 | day-t >= 2.0; drop best5 days+top5 names > 0; bootstrap p5 > 0 |
| t15_60 | **FAIL** | 776 | 257 | -11.9 -> -10.0 | +2.31 | 6.05 | +1.98/+2.63 | +1.23 | +1.75 | -0.59 | 1 s delay still > 0 |
| t20_60 | **FAIL** | 776 | 122 | -11.9 -> -10.7 | +1.38 | 4.95 | +1.26/+1.51 | +0.69 | +0.97 | -0.31 | 1 s delay still > 0 |
| t25_60 | **FAIL** | 776 | 71 | -11.9 -> -11.2 | +1.01 | 3.57 | +1.04/+0.98 | +0.32 | +0.60 | -0.09 | 1 s delay still > 0 |
| t20_40 | **FAIL** | 776 | 122 | -11.9 -> -11.1 | +0.92 | 4.95 | +0.84/+1.00 | +0.46 | +0.64 | -0.20 | 1 s delay still > 0 |
