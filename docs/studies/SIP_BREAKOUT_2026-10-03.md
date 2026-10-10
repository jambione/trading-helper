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

## Addendum 2026-10-10: raw-price floor re-score (verdict unchanged: FAIL)

The universe applied the $10 open floor to the SPLIT-ADJUSTED daily panel (found in
[ROOM_HOD_OOS_2026-10-09.md](ROOM_HOD_OOS_2026-10-09.md)). Checked against RAW SIP 1-minute bars (the
`ai_reports/sr_breakout_hist` cache for 5/1-9/10, a one-off raw fetch for 9/11): **66 of 3,581 name-days opened below
$10 raw** (65 reverse splits with adjusted/raw factors 5-625, e.g. HUBC $0.15, INLF $0.07, CPOP $0.51; plus NOK 9/4 at
$9.99 with no split). They supplied 206 of 7,131 signals (7 capped) and 208 controls (10 capped). Re-scored from
`ai_reports/allsym/sipbrk_events.pkl` with those name-days dropped (`tools/studies/rawfloor_rescore.py`, raw output
[RAWFLOOR_RESCORE_2026-10-10_raw.md](RAWFLOOR_RESCORE_2026-10-10_raw.md)):

| arm | half | n | gross15 | net15 (t) | as published |
|---|---|---|---|---|---|
| all signals | first / second | 3,407 / 3,518 | -0.8 / -4.7 | -31.7 (-9.5) / -28.8 (-10.2) | -27.9 / -29.3 |
| **capped (<= 5 bp)** | first | 317 | -3.0 | **-6.7 (-1.7)** | -2.8 |
| **capped (<= 5 bp)** | second | 416 | -2.5 | **-5.9 (-2.2)** | -3.2 |
| capped random control | first / second | 278 / 418 | +3.2 / +2.9 | -0.5 / -0.6 | +5.5 / -1.3 |

- **Verdict unchanged: FAIL.** The capped breakout is now clearly negative and still below its random control in both halves.
- The 17 capped penny events show quoted spreads of 1-5 bp on $0.1-$3 stocks, which a 1-cent tick cannot produce,
  and 15-minute moves from -441 to +1,123 bp. Their quotes are suspect. A handful of them had moved the published
  capped means by about 4 bp and the first-half control by about 6 bp.
- Not repaired: the 66 dropped slots were not refilled from the 41st-largest gap onward. Names whose raw open was
  >= $10 but whose adjusted open was < $10 (a later forward split) were never admitted. The factor check found no
  forward split among the admitted names, but it cannot see names that were never admitted.
