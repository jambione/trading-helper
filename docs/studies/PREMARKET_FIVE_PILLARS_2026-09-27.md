# Premarket five-pillars study (2026-09-27)

**Question.** Can buying five-pillar momentum names *premarket* (entries 07:00–09:25 ET) capture a move after the real SIP spread? The answer decides whether Alpaca's ~$99/mo paid live SIP feed is worth it for that purpose.

**Answer: no.** Across 288 threshold cells × 5 entries × 7 exits (10,080 tests) on 248 sessions, **no P1 cell is net-positive in both halves**. Gross 15-minute drift is already negative for most cells (median about −105 bp). Net lands at about minus the quoted spread, typically −150 to −450 bp. The few breakout-entry (P2) cells that print net>0 in both halves have test t ≈ 0.1 and are noise.

**SIP recommendation:** do **not** buy paid live SIP in order to trade these premarket five-pillar names. Historical SIP already shows there is no capturable edge after costs. Live SIP would only matter for a strategy that first has edge.

Script: `tools/studies/premarket_five_pillars_study.py`. Caches and raw report live on the mini under `/tmp/pm` (`report.txt`, `grid.pkl`, `spreads.json` + `spreads2.json`).

## Method
- **Candidates come from premarket bars, not daily bars.**
  - Alpaca daily bars are RTH-only: on 2026-03-10, 20 of 133 names had a premarket high above the daily high.
  - `scan`: 30-minute SIP bars from 04:00 to 09:30, every session from 2025-09-02 to 2026-09-25, for every active common stock with a prior close of $1–25 (about 4,000–4,800 names a day).
  - `cands`: premarket high ≥ +10% and premarket volume ≥ 20k gives **11,232 name-days over 248 sessions**. **4,372 of these (39%) are premarket-only movers** that the daily-bar candidate set of the earlier study missed.
- **Premarket RVOL** = cumulative volume since 04:00 ÷ the name's own mean cumulative premarket volume at the same point over the prior 20 sessions.
  - The baseline is interpolated between half-hour boundaries and floored at 5k shares.
  - 302 name-days have no baseline (fewer than 5 prior sessions); they fail the RVOL cells.
- **Minute bars.** 1-minute SIP bars from 04:00 to 10:31.
- **Loose set.** $2–20, ≥ +10%, cumulative premarket volume ≥ 50k by the 09:25 close: **5,397 name-days (~21.8 a day)**. Superset of every grid cell.
- **News and float.** Alpaca/benzinga from 16:00 the prior day to the decision minute; current float only (known for 1,570 of 1,612 symbols).
- **Costs.** SIP NBBO median spread in a 10-second window at marks every 10 minutes from 07:00 to 09:20, plus 09:25, 09:35 and 10:00.
  - Entry spread: nearest mark within 10 minutes. Exit spread: nearest within 20 minutes, else entry spread (18.2% of event rows used that fallback).
  - Entry-mark distance: 0 min 22%, ≤2 min 43%, ≤5 min 92%.
  - Net = gross − full quoted spread (half in, half out) − 1¢ round trip when price < $5.
- **Grid.** Price {$2–20, $2–5, $5–10, $10–20} × gain {10, 20, 30%} × premarket volume {50k, 250k} × premarket RVOL {none, 5, 10} × float {any, <10M} × news {any, yes} = **288 cells**.
- **Entries.** Qualify = first real minute ≤ 09:24 where all cell conditions hold.
  - P1: next bar's open.
  - P2: 1¢ over the premarket high so far.
  - P3: 1¢ over the prior completed 5-minute high.
  - P4: first close back above VWAP after a close below it.
  - RND: all minutes after qualifying (random baseline).
- **Exits.** Brackets BRpct (+2%/+3%, 2% trail, −5% stop) and BRc (+$0.05/+$0.08, $0.10 trail, $0.15 stop); holds H5/H15/H30; hold to 09:35 and 10:00; plus MFE/MAE over 30 minutes.
- **Validation.** Train = even-index sessions (A), test = odd (B). t-stats use per-day means.
- **Events analysed.** 47,973 event rows; 1,440 cell-entry pairs with events.

## Compact table: test half (B), selected heads (bp)

| Cell | names/day | P1 H15 gross / net (t) | P1 T0935 net | P1 BRpct net | RND H15 net | med spread |
|---|---|---|---|---|---|---|
| Loosest: $2–20, ≥10%, PMvol≥50k | 19.1 | −104 / **−276 (t −10.8)** | −447 | −259 | −193 | ~92 |
| Simple: ≥250k & PM-RVOL≥5 | 11.3 | −91 / **−219 (t −6.2)** | −419 | −203 | (similar) | ~72 |
| **Strict five pillars** (≥50k, RVOL≥5, float<10M, news) | **4.75** | −168 / **−324 (t −4.9)** | −453 | −246 | −203 | ~80 |

Train half (A) tells the same story: loosest P1 H15 net −237 (t −7.5); strict −128 (t −1.3). Strict is the only head with a slightly positive *gross* on A (+21 bp), and even that flips hard on B.

**Search summary (288 cells, n≥30 both halves):**
- P1: net>0 in both halves for **0** cells on every exit. Max test t for H15 is **−1.04**.
- P2 H30: 3 cells net>0 both halves; max test t **0.12** (noise).
- P2 T0935: 1 cell; max test t **−0.06**.
- All other entry×exit pairs: 0 cells net>0 in both halves, or max test t well below 1.
- Best-A cells routinely flip sign on B (example: P1 H15 best-A net +291 → test −232).

Pooled gross>0 on H15 with n≥60: P1 13/288 cells (median gross −105, max +117); P3 101/288 (median −21); P4 **0/288**. Positive gross is rare and does not survive the spread.

## Tradeable subset still loses
Restricting to spread ≤ 100 bp and 5-minute dollar volume ≥ $50k (so a $5k order is ≤10% of that window):

| Head | tradeable names/day | days with ≥3 | P1 H15 net A / B |
|---|---|---|---|
| Loosest | 8.9 | 243/248 | −110 / −153 |
| Simple | 7.0 | 240/248 | −147 / −130 |
| Strict | 2.7 | 122/248 | −118 / −224 |

Tighter still (spread ≤ 50 bp & dv5 ≥ $100k) cuts supply to ~1.3–4.3 names/day and does not flip the sign.

## What the tape looks like
- Median MFE30 on loosest P1 is about +340 bp against MAE30 about −460 bp: there are spikes, but the adverse move is larger and the spread (~90 bp) sits in the middle.
- Holding into the open (T0935 / T1000) is *worse* than a 15-minute premarket hold: loosest P1 T0935 net about −400 to −450 bp on both halves.
- Earlier qualifiers (<07:00) are less bad on H15 net (−190 pooled) than 08:00–09:25 qualifiers (−410 to −430), mainly because their spreads are tighter (~73 bp vs ~130 bp), not because drift improves.
- Breakout entry (P2) and VWAP reclaim (P4) do not help. P2 pays a higher dollar-volume tape and still loses after costs.

## Caveats
- Float is current, not historical.
- Universe is currently active symbols only (survivorship).
- Spreads are 10-minute marks, not exact entry-minute quotes; 18% of exits fell back to the entry spread.
- Free IEX cannot see these names premarket (established in the Discord squeeze study); this study used historical SIP, which is the right feed for the question.
- Manual discretion, limit-order queueing, and sizing into the bid are not modelled. The study asks whether a mechanical premarket buy clears the quoted spread. It does not.

## Bottom line for the desk
Premarket five-pillar name-finding can fill a book early (strict ~4.8 names/day; loosest ~19), and 39% of the premarket ≥10% movers never appear in the daily-bar candidate set. That is useful for *seeding*. It is not a reason to buy paid live SIP or to auto-enter those names before 09:30. Edge after costs is absent; paid SIP does not create edge that historical SIP already ruled out.
