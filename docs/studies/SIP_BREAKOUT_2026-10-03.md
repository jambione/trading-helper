# Stage 1 of the paid-data decision: SIP volume breakout, held out (2026-10-03)

Pre-registered: `sip_breakout_prereg.json` (175bc9b, amended before data c0a8538). Script:
`tools/studies/sip_breakout_study.py`. Universe: the day's 40 largest gaps (>= +2%, open >= $10, prior-day $vol >= $20M),
2026-05-01..2026-09-11 (92 days, 3,581 name-days), SIP 1-minute bars. Signal: volume > 3x 20-min mean and close > 15-min
high; entry next bar open; cost = full SIP spread at entry.

| arm | half | n | median spread | gross15 | net15 (t) | net30 |
|---|---|---|---|---|---|---|
| all signals | first | 3,593 | 20.1 | +5.0 | -27.9 (-5.3) | -22.8 |
| all signals | second | 3,538 | 13.9 | -5.1 | -29.3 (-10.1) | -30.8 |
| **capped (spread <= 5 bp)** | first | 323 | 3.8 | **+0.9** | **-2.8 (-0.9)** | -2.7 |
| **capped (spread <= 5 bp)** | second | 417 | 3.4 | **+0.2** | **-3.2 (-1.5)** | -3.5 |
| capped + quiet-tape filter | both | 322 / 417 | | | -2.7 / -3.2 | |
| capped random control | first / second | 287 / 419 | | +9.1 / +2.2 | +5.5 / -1.3 | |

**Verdict: FAIL.** On months it never saw, the breakout has no edge on tight-spread names (gross +0.2..+0.9 bp, below
random minutes in the same names), and on all names its bursts carry 14-20 bp spreads (net -28 bp). The +9.4 bp of
9/16-9/25 did not replicate. The quiet-tape filter removes almost nothing here (1 of 323 signals).

**Decision implication:** real-time SIP is not worth buying for this signal family. Combined with the other SIP-history
studies (entries, runway, exits, halts, temperature: all on SIP data, all no directional edge), paying for the real-time
feed would not supply an edge we have failed to find in its history. The remaining paid-data question is stage 2:
order-book depth/imbalance, which SIP does not contain — test a small historical sample before any subscription.
