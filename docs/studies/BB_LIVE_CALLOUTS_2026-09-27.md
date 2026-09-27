# Bullish Bob LIVE (Trader Bro) call-out study (2026-09-27)

**Question.** The squeeze-level study showed that buying published L1/L2 breaks loses. The desk already archives the caller's **LIVE** mic callouts (`ai_reports/bb_live.jsonl`) as Trader Bro suggestions. When he names a symbol, does that timestamp have capturable edge after the real SIP spread?

**Answer: no.** Buying the next SIP minute's open at the call time loses after costs on every primary bucket. Gross 15-minute drift is about flat to slightly negative. The call does **not** beat a random later minute on the same name. The tape is wild (median MFE30 ~+800 bp, MAE30 ~−850 bp), which is why the room *feels* profitable, but a mechanical buy-at-call does not capture it.

Script: `tools/studies/bb_live_callouts_study.py`. Caches on the mini under `/tmp/bb` (`calls.json`, `sip/`, `spreads.json`, `grid.pkl`, `report.txt`).

## Method
- **Input.** `ai_reports/bb_live.jsonl` on the mini (dashboard archive). Deduped on `(ticker, text, second)`. **1,730** calls, **34** sessions (**2026-08-10 → 2026-09-25**), **563** name-days.
- **Stance filters** (same patterns as `ai_entry_watch._bb_call_is_actionable`):
  - **all** — every deduped call (1,722 scored).
  - **actionable** — drop exit/avoid language (`sold`, `out`, `trimmed`, `not for me`, …): **1,542**.
  - **lean** — actionable and text matches lean-in words (`test`, `hod`, `break`, `on watch`, …): **591**.
  - **first_act** — first actionable call per symbol-day: **516**.
- **Entry.** Next 1-minute SIP open at or after the call timestamp.
- **Costs.** Full quoted SIP NBBO spread at the call minute (median over 60 s; 5-min lookback if empty) + 1¢ round trip when entry < $5. Missing quote on 40 rows (spread treated as 0 and flagged).
- **Exits.** Hold 5 / 15 / 30 minutes; hold to 11:00; hold to 15:50; MFE/MAE over 30 minutes.
- **Baseline.** Mean of 5 random later opens on the same name/day, same 15-minute hold and cost.
- **Validation.** Alternate sessions: half A = even index, half B = odd. t-stats on per-day means.

## Compact table (bp; net = after full quoted spread)

| Bucket | half | n | net 15m mean / med (t) | gross 15m | net→11:00 | MFE30 / MAE30 med | call−random net15 |
|---|---|---|---|---|---|---|---|
| **actionable** | A | 725 | −106 / −332 (−0.3) | +63 | −597 | +826 / −873 | |
| **actionable** | B | 817 | −246 / −319 (−1.6) | −113 | −611 | +804 / −826 | pooled −28 bp (t 0.3) |
| lean | A | 268 | −80 / −194 (−0.9) | +43 | −702 | +938 / −863 | |
| lean | B | 323 | −230 / −271 (−2.2) | −125 | −508 | +804 / −793 | pooled −42 bp (t −0.6) |
| first_act | A | 264 | −156 / −465 (0.1) | +101 | −487 | +714 / −810 | |
| first_act | B | 252 | −330 / −349 (−2.9) | −132 | −340 | +773 / −725 | pooled **+4 bp (t 0.3)** |
| all | A/B | 811/911 | −115 / −253 | +51 / −118 | −624 / −574 | ~+790 / ~−850 | −9 bp (t −0.3) |

**Tradeable subset** (actionable, spread ≤ 100 bp, price ≥ $2, has quote): net 15m **−71 / −338** (A/B); gross already **−10 / −280**. Holding to 11:00 is worse (~−750 to −900 bp).

## Splits (actionable, pooled)
- **Clock:** early premarket is worst (04–07 net 15m −572 bp). 09:00–09:30 mean +160 bp but median −223 and t 0.4 (noise). RTH 09:30–11: −442 bp.
- **Price:** <$2 mean looks least bad (−49) with median −442 (skew). $2–5 / $5–10 / $10–20 all clearly negative. ≥$20 is about flat (+15 mean, +2 med, t −1.0, n=86) — not a pass.

## How this sits next to the squeeze-level study
| Signal | Mechanical buy result |
|---|---|
| Posted L1/L2 squeeze breaks | Loses (~−2% to −3%/trade after costs) |
| **LIVE callout timestamp** | Loses (~−1% to −2.5% / 15m after costs in bp terms above) |
| Nightly watchlists | High volatility, weak/negative direction |

Same shape in both LIVE and level studies: **large favorable spikes exist, larger adverse moves eat them, and the published/spoken moment is not a selective entry.** The room can still look skilled because MFE is huge (~+8% median in 30 minutes) and humans remember those prints.

## Caveats
- When Discord's `said` stamp is empty, `at` is often the OCR capture time. That can lag the true mic moment by seconds to a bit longer; it does not reverse a multi-hundred-bp net hole.
- Call text is free-form. "Actionable" only removes clear exits/passes; many lines are narration (`vol`, `halt`, `no news`) rather than entries.
- 34 sessions. Halves are 17/17. Good enough to reject a large edge; a tiny edge could hide.
- No modeling of limit-order queueing, partial size, or skipping. The study asks whether buying the call minute clears the spread.

## Desk takeaway
Keep using LIVE callouts as a **name-finding / attention seed** (already `ai_watch_seed_bb_live`). Do **not** treat the call timestamp as an arm or as proof of edge. Entries stay on the desk's own gates. Paid SIP is still required to *see* these names premarket; it does not create edge at the call minute.
