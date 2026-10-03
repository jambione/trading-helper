# Ideas worth adding (2026-10-03)

From the skeptic reviews and the spread work on 2026-10-02/03. Evidence is in
`docs/studies/SKEPTIC_REVIEW_2026-10-02.md` and the studies named below. Numbers are bp per round trip.

**Where the day desk stands:** since the 9/29 gates, entries are random or slightly better (+3.2 bp vs same-name
random minutes), the post-entry move is about zero (−2.6 ± 4.0), and the spread costs about 7.7 bp, 76% of the loss.
Desk P&L is about −7 to −9 bp per trade. **The lever is cost, not entries or exits.**

## A. Build as live A/B tests (after a close)

| # | Idea | Evidence | Expected | Test |
|---|---|---|---|---|
| 1 | **Passive exits:** trail, target and time exits rest a sell limit at the mid for 10 s, then cross. Stops and the 15:50 flatten stay market orders. | `passive_exit_sim.py`, 250 sells 9/29-10/2: +3.7 bp at 10 s (t 4.9), +4.2 at 30 s (t 7.9); fills 70-86% | +3 to 4 bp | rotate market vs limit-at-mid, like the entry arms |
| 2 | **Wait for a tighter spread on wide names:** when the spread at entry is > 5 bp, wait up to 10 s for it to tighten to 0.75x, then cross; tight names cross at once. | `spread_wait_sim.py`, 509 buys 9/16-10/2: +4.2 bp on wide-spread buys (t 3.4), +2.6 overall | +2 to 3 bp | rotate wait vs no-wait on wide-spread entries |
| 3 | **Keep the three-arm passive entry running** about 10 sessions, then grade it. | Live since 10/1; day 1 inconclusive (n ≈ 23 per arm) | up to ~+4 bp | `tools/entry_arm_score.py` nightly (SIP mid, end-to-end) |

Items 1 and 2 together target about 5-7 bp of the ~8 bp cost: the first plausible path to break-even.

## B. Config decisions (yours)

| # | Idea | Evidence | Trade-off |
|---|---|---|---|
| 4 | **Tight SIP spread cap**, 0.03-0.05% (live is 0.2%) | `cost_gates_study.py`: ≤3 bp names −3.4 vs −8.8 (after), −6.9 vs −19.1 (before); halves the loss in both windows | about half the opens; costed replay at 0.10%/0.05% pending |
| 5 | Momentum spread exemption → false | committed ff153e4 (10/1 decision); replay shows no difference on 10/1-10/2 | none; **needs the mini pull** |

## C. Studies to run next

| # | Idea | Why |
|---|---|---|
| 6 | **Real-spread gate:** judge names by their SIP spread history at that time of day (free, 15-min delayed), not the live IEX quote | IEX overstates spreads 1.5-3.3x; would sharpen #4 and lose fewer good names |
| 7 | **Halt-reopening auctions:** enter or exit momentum names at the LULD reopening cross (no spread) | Free SIP trade conditions flag halts and reopening prints; new and untested |
| 8 | **More auction-based books** (horizon reframe) | The spread is a third to half of a 100-second move but about 2 bp of a night; the only edge found is auction to auction |
| 9 | **SPY overnight hedge at ~1.4x** for the overnight book's tail | Tail risk is market beta (book size review); untested |
| 10 | Overnight edge on data before 2016, plus alpha after beta | Stalled when the mini's ssh dropped; the edge's one open check |
| 11 | Grade the gap-down and range-position gates on logged refusals, clustered by name-day | Both live gates are in-sample; desk review step 4 |
| 12 | Intraday (open→close) leg of the overnight top-20 names | Are they names the day desk should avoid? Desk review step 5 |
| 13 | Recheck the 10:00-10:30 slot after ~10 more sessions | Worst half hour in both windows (−19.9, t −6.2; −52.4, t −3.3), but chosen after the fact |

## D. Operations

- **Pull on the mini and restart the overnight agents before Monday's 15:40 buy** (filter off, exemption off). Run it from the mini's Terminal, not ssh:
  `cd ~/repo/trading-helper && git pull --ff-only && for l in com.jambi.overnight-book com.jambi.overnight-live com.jambi.month-end-book; do launchctl kickstart -k gui/$(id -u)/$l; done`
- Nightly after the close: `tools/exec_report.py` and `tools/entry_arm_score.py`.

## Closed: tested, not worth adding

| Idea | Result |
|---|---|
| Price floor above $10 | Cost falls but the move worsens; −8.8 → −9.4 at $50 |
| Spread ÷ movement gate | No better than a plain spread cap; one lucky quintile |
| Later start (10:00 or 10:30) | −8.2 vs −8.8; spreads already normal by 09:45 |
| Re-entry cap or loss cooldown | Flips between windows; repeat names have tighter spreads |
| Holding longer | Every wider trail loses more (the fade) |
| Shorting the fade | Coin flip |
| Steady-runway or vol-pace entry leans | Movement, not direction; −2.1 bp gross |
| 1¢ initial stop + ratchet | Skeptic tick-level test pending |
