# Strategy decision memo — 2026-10-22

**For:** the operator. **Decision needed:** where the desk's effort and money go next, now that the exit side is
measured and the free-feed entry search is closed.

## Bottom line

- **The exits are not the problem.** On 851 real fill paths, the live exit stack keeps 71–78% of any entry edge that
  arrives within 3 minutes ([EXIT_CAPTURE](studies/EXIT_CAPTURE_2026-10-21.md)).
- **The entries are the problem, and the bar is now a number.** The desk breaks even when an entry carries roughly
  **+5 to +11 bp of 3-minute edge**: +11 bp on replayed fills (95% CI 7–15), about +5 bp after shifting to live's slightly
  better booked results. Today's entries carry about 0.
- **No free-feed entry signal tested reaches that bar.** Bar indicators give about 0. Trade-side order flow is real but
  gives +1.5–2 bp, about a fifth of the need.
- **Recommendation:** stop building intraday entry rules on the free feed. Put the next month into the one cheap,
  credible structural lead (month-end), and spend on real-time SIP only for questions free data cannot answer. Keep the
  intraday desk on paper as a data recorder, with no config churn.

## What is closed (and why it stays closed)

| Family | Result | Where |
|---|---|---|
| Indicator triggers / filters: %R, RSI, MACD, ADX, OBV/A-D, confluence, S/R, VWAP, Bollinger | about 0 vs a same-name random minute at 3 and 10 minutes; 17 of 18 cells FAIL at 3 min | [3-min re-read](studies/THREE_MINUTE_REREAD_2026-10-21.md), [history](studies/INDICATOR_CONFIRM_HIST_2026-10-21.md), [desk arms](studies/INDICATOR_CONFIRM_DESK_2026-10-21.md) |
| Chart setups (10/8 videos S3–S7) | FAIL as 10-min and 3-min entries; S5 loses money | same |
| Entering earlier or later | lag-0 vs lag-1 both about 0; the "earlier" gains were look-ahead | 3-min re-read; early-entry tests 10/13 |
| Name filters | raise the win rate, not the mean | needle / board-name studies |
| Exits as a profit source | 42 shapes ~0 gross on zero-edge entries; exits keep an edge, they cannot create one | exit sweep, exit capture |
| Order flow (SIP trade flow, NBBO imbalance) | trade flow +1.5–2 bp, t ~3.5 in both halves, robust, but FAILS the 5 bp bar; book imbalance about 0 | [order-flow pilot](studies/ORDER_FLOW_PILOT_2026-10-21.md) |
| Shorting the same signals | symmetric moves; earlier short tests were coin flips with a squeeze tail | 10/2, 10/16 studies |

What makes this conclusive is the window shape: after any entry, price reaches about **+10–12 bp and −10–12 bp** within 3
minutes, equally. The runs exist; nothing on the free feed tilts them.

## The options

### A. Paid real-time SIP month (~$99, approved 10/15 in principle)

- **What it can answer that free data cannot:** premarket (IEX is nearly blind there), sub-minute execution, the
  momentum-alert scalp at real speed, and order flow as an *add-on* to a faster signal.
- **What we already know:** historical SIP simulations put the known leads at +0–9 bp **gross**: volume breakout +9, 1 s
  news +4, %R+RSI +4, order flow +1.5–2. That is below or at the edge of the +5–11 bp bar, before the subscription
  cost.
- **GO only if, before paying:** a pre-registered simulation on free *historical* SIP shows one candidate entry beating
  the +5 bp bar with net > 0 after the spread, in both halves. Then the paid month is a live confirmation, not a search.
- **During the month, KEEP paying only if:** that candidate holds at ≥ +5 bp net per trade on live paper over ≥ 15
  sessions, and the book clears the keep-bar of about $4.70/session (about 21 bp-trades a day at $2,250 sizing).
- **NO-GO:** no candidate passes the historical-SIP simulation. Then a paid month only re-measures what we already know.

### B. Day-hold (hold 2–3 morning picks all day)

- **Pilot (9/8–10/8, 14 days):** picks +17 bp/day vs random same-list names −30, difference +48 (t 1.5); the 10-year
  skeleton FAILED.
- **Forward run (registered, 9 scored sessions so far):** picks **−32 bp/day** (t −2.2), worse than random names
  (−23 bp, t −1.0). The pilot's direction has not held.
- **Go/no-go (already registered):** read at 40 scored sessions (about December); anything not PASS is FAIL. It runs
  nightly at no cost.
- **Recommendation:** let it finish as registered, but build nothing on it. On current direction it fails.

### C. Month-end rebalancing flow (SPY vs TLT on the last session of the month)

- **Evidence:** +20.6 bp/trade (t 3.7) over 289 months, positive in 17 of 25 years, month-end-specific (placebo about
  0), next-month reversal as theory predicts. **Caveat:** k = 1 was a secondary cell (the registered k = 3 failed
  validation), so it is a post-hoc pick, and it is small money: ~12 trades a year, about 2.5%/yr.
- **Status:** built and running **score-only** on the mini (`com.jambi.month-end-book`). First event is the **10/29 close
  → 10/30 close**. Real paper orders need a separate paper account (`config/secrets.json.monthend`), which only the operator
  can create.
- **GO to paper orders now:** the operator creates the paper account before 10/29. The risk is one SPY or TLT position
  for one session a month.
- **GO to real money (later):** ≥ 6 paper events whose mean net is > 0 and whose fills match the score-only
  prices within 2 bp. Treat the 25-year history as the evidence and the forward events as an execution check, since 6
  events cannot prove an edge on their own.
- **NO-GO:** fills slip more than ~5 bp from the auction prices, or the forward mean is negative after 12 events.

### D. The intraday desk itself

- Keep it running **on paper** as the data recorder (fills, ledger, replay inputs) at the new **$20 floor**.
- **Freeze entry-config changes.** "Live config churns faster than evidence" (~3.5 ships per session against ~12 fills)
  is the failure mode to avoid.
- No real money on the intraday desk until an entry clears the +5–11 bp bar on held-out data.

## Recommended sequence

1. **Now:** freeze intraday entry research on the free feed, and record the bar (+5–11 bp of 3-minute edge) as the gate
   for any future entry idea.
2. **Before 10/29:** operator creates the month-end paper account; the agent moves from score-only to paper orders.
3. **Next 2–3 weeks:** if SIP is still wanted, pre-register *one* historical-SIP simulation (premarket momentum alerts,
   or volume breakout + order flow as an add-on) against the +5 bp net bar. Buy the month only if it passes.
4. **Day-hold:** read at 40 sessions as registered.
5. **The held-out sample** (2026-01-14..04-24, unread) stays reserved for whichever candidate passes first.

## What I would not do

- Retune exits to fix losses: they already keep a fast edge.
- Add more bar indicators, or stack them: the information is not in the bars.
- Buy SIP to "see what happens": the historical simulations already tell us most of the answer.
- Read short forward samples (day-hold, month-end) as proof either way.
