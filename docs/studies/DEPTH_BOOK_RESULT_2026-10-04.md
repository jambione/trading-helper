# Stage 2 depth test (Nasdaq top-5 book imbalance as an entry gate): UNDERPOWERED, no verdict

Run exactly as pre-registered in [`depth_book_prereg.json`](depth_book_prereg.json) (62fcbbc). Script:
`tools/studies/databento_markout_probe.py`. Data and how to reuse it: [`DEPTH_BOOK_DATA.md`](DEPTH_BOOK_DATA.md).
Data: Databento XNAS.ITCH mbp-10, `[signal-35 s, signal]`, 7,353 random entries (3 per name-day, the day's
40 largest $10+ gappers, 63 sessions 2026-06-01..08-28, seed 17). Outcome: buy at the SIP ask at +0.5 s, sell at the
SIP bid at +0.5 s + 120 s (`exec_report.nbbo_at`). Spend: $6.27 quoted, from the free credit.

## Pre-registered decision

| | H1 first half (6/1-7/15, 31 sessions) | H2 second half (7/16-8/28, 32 sessions) |
|---|---|---|
| gated (OBI5_dm top tercile, cut 0.1021 fixed on H1) minus rest, 120 s, raw | **-3.31 bp**, day-clustered SE 2.45, t -1.35 | **-0.58 bp**, SE 1.70, t -0.34 |
| gated n / mean net (after the spread) | 1,192 / -29.25 bp (median -19.8, hit 18.6%) | 1,181 / -24.83 bp (median -16.7, hit 18.4%) |
| rest n / mean net | 2,384 / -25.94 bp | 2,504 / -24.26 bp |
| all entries mean net | -27.04 bp (n 3,576) | -24.44 bp (n 3,685) |
| 1 s-guard gated minus rest | | -1.37 bp, t -0.84 |

- Quote failures: 0 of 7,261 survivors (abort bar 2%). Book drops: no_book 54, stale 28, halt 10.
- **Power check (H1): 2.84 × 2.45 = 6.96 bp > 5 bp → FAILS.** Per the pre-registration the run is
  underpowered and **no verdict is drawn** (neither PASS nor the "not worth buying" FAIL conclusion).
- For information only, the second half would have failed (a) +5 bp/t≥2 (it is -0.58 bp), (b) gated net > 0
  (-24.8 bp), and (d) guard t≥1.5; only (c) n≥600 holds.
- Split note: the prereg text says "sessions 1-30 / 31-60" but also "63 sessions, split at the middle session";
  63 sessions were used, split 31/32.

## Information (not deciding)

- 3-sd winsorized gated-minus-rest: H1 -2.60 (t -1.56), H2 -0.50 (t -0.38).
- OBI1 top tercile: H1 -3.42 (t -1.55), H2 -1.71 (t -1.02).
- ASK_THIN30 bottom tercile (asks draining): H1 +1.79 (t 0.70), H2 -0.11 (t -0.04).
- Gross mid-to-mid 120 s, gated minus rest: H1 +0.18 (t 0.08), H2 +2.08 (t 1.20); all entries drift ≈ 0
  (H1 +1.7 bp, H2 -0.2 bp). Median quoted SIP spread at entry 15.2 bp; that spread is the whole loss.
- Clock-hour gated-minus-rest: 09:xx is the only hour positive in both halves (H1 +24.4 t 1.64; H2 +10.0 t 0.90);
  not significant, a stratum picked after looking, so **unconfirmed, needs a new held-out period**.
- 60 s and 300 s horizons (filled in by a re-score 2026-10-04 14:40 ET, 0 lookup failures; the 120 s numbers are unchanged),
  gated minus rest:

  | hold | net H1 | net H2 | gross mid-to-mid H1 | gross mid-to-mid H2 |
  |---|---|---|---|---|
  | 60 s | -2.18 (t -1.07) | -0.04 (t -0.04) | +0.91 (t 0.47) | **+3.22 (t 3.45)** |
  | 300 s | -0.85 (t -0.20) | -1.77 (t -0.77) | +2.16 (t 0.52) | +1.32 (t 0.60) |

  Every horizon loses about the spread net (all-entry net -25..-27 bp). The one significant cell, H2 60 s
  gross +3.2 bp, is the textbook short-horizon imbalance effect: the mid leans about a fifth of the spread toward
  the heavy side, then fades by 300 s. It does not replicate in H1 (+0.9), and it is about 1/5 of the 15 bp
  spread it would have to beat. It is information only and does not change the decision.

## Plain words

Across 7,261 random entries in desk-like gappers, buying when Nasdaq's top-5 book leans to the bid did no better
than buying at other times. It was slightly worse in both halves, and every entry lost about the 15 bp spread at
a 2-minute hold. The pre-registered power bar was missed (SE 2.45 bp vs the needed ≤ 1.76), so formally
there is no verdict. But nothing here points toward an edge large enough to pay a 15 bp spread. There's no case
for buying live Nasdaq depth as an entry gate on this evidence. Caveats: Nasdaq-only book, a June–August
regime, random entries standing in for desk entries; the desk-fill secondary sample was not run.
