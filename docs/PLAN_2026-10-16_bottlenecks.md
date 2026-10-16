# Bottleneck plan (operator 10/16)

Goal: a strategy with promise to work with. Success is DIRECTIONAL progress: ideas that point the right way
and get stronger with more data, not only full passes.

## New scoreboard tier: PROMISING

Declared 10/16, before any of today's results. It applies to every test from now on.

- **PASS** (unchanged): pre-registered bar met in both halves. Money only ever follows a PASS.
- **PROMISING**: the right sign in both halves, combined t >= 1.5, and still positive at the MEASURED cost
  (about 3 bp round trip on $10+ names with the 0.05% spread cap). It earns a forward or paper slot and a
  bigger-sample re-test. It is never money.
- **FAIL**: anything else.

## The bottlenecks, ranked by how much they block profit

1. **Evidence throughput.** The desk makes about 12 fills a session, and config changes ship faster than
   evidence arrives (live-config-churns-faster-than-evidence). Studies queued behind the desk's 200/min Alpaca
   budget, so most ran only at night.
   - *Attack:* the data-only account (live 10/16) removes the budget wait.
   - Next: build a 10-year minute-bar warehouse once on that account. Cover the top ~100 movers a day plus
     random controls (~1-3 GB, with disk checked first), so each new idea scores in minutes, not overnight.
2. **The cost assumption changed and old verdicts were never re-graded.** Many FAILs were judged at 10-25 bp
   round trip. The measured spread is about 3 bp now (focus-is-day-trading, 10/9).
   - *Attack:* one pre-registered re-grade of every past result that was gross-positive, at the measured cost,
     with a multiplicity bar. Expected source of the first PROMISING list.
   - Candidates:
     - SIP volume breakout (+9 bp gross);
     - day-hold pilot (+17 bp/day vs random, t 1.5);
     - re-entry after a win (t 2.9);
     - needle C2;
     - %R+RSI on SIP (+4).
3. **Defence works; offence has not.** Every filter that AVOIDS losers held up:
   - the $10 floor (cheap names were 81% of the loss);
   - the spread gate;
   - the gap-down block;
   - ratchet patience (+58 bp/day, t 2.1).

   Every filter that tries to FIND winners failed. The desk was last measured at about -3.5 bp/trade.
   - *Attack:* treat "per-trade mean toward 0" as the directional metric for the live desk. Track it per
     session on the running list.
   - At about 0 net, any small selection lift is profit, so the bar for offence drops from "beat 10-20 bp" to
     "beat 0".
4. **Hypothesis source.** Most tests were indicator timing on 1-minute bars, the most crowded edge in the
   market. The structural flows that showed anything:
   - month-end (t 3.7, forward pending);
   - overnight ranking (t 9.7 relative, absolute is beta).
   - *Attack (one prereg each, on the warehouse):*
     - post-earnings drift (multi-day, mid caps);
     - a market-neutral version of the overnight ranking (long top / short bottom, so beta cancels);
     - retail-attention reversal (Stocktwits trending, multi-day).
5. **Live data feed.** Several leads show only on real-time SIP and vanish on IEX (volume breakout, %R+RSI,
   quiet tape).
   - *Attack:* after item 2, if two or more SIP-only leads come out PROMISING, that is the evidence for a
     one-month real-time SIP trial. That is a spend decision for the operator, not made here.
6. **The operator's eye is untested.** TGL was found by eye while the desk missed it. The Call logger scores
   every call (15-min paper hold, measured spread).
   - *Attack:* 30+ calls, compared with the desk's fills over the same minutes. If PROMISING, build a desk mode
     around the operator's setups.

Plumbing (arm-to-fill delay, slow poll, seat thrash) buys opens, not profit (momentum-optimal-conditions). It
stays on the list for the busy-book goal, but below items 1-6 for profit.

## Order

| When | Item |
|---|---|
| Today | gap-down score (running); exit-sweep score; RTH poll profile; operator calls all session |
| Tonight | prereg + run the cost re-grade (item 2); warehouse fetch starts on the data account (item 1) |
| Next 2 sessions | earnings drift and market-neutral overnight preregs (item 4) on the warehouse; desk per-trade mean tracked daily (item 3) |
| ~30 calls | operator-eye read (item 6) |
