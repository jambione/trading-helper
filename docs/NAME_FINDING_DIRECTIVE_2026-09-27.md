# Name-finding prime directive and 9/27 findings — 2026-09-27

Written Sunday 2026-09-27 so that Claude or AGY can pick up today's work cold.
Branch `master-mac`. Everything in the findings section was read-only: no code,
config, or restart changes were made today.

---

## 1. PRIME DIRECTIVE (Jonathan, 2026-09-27)

Profit comes from serving the best possible names to the book, early and at
the right time.

- Entries (the EXH fast %R −50 cross) and exits (the 0.35% peak ratchet, the
  chase, and the rest) stay as they are for now.
- All effort goes into name finding and early seeding.
- Jonathan's manual profits came from momentum names traded premarket through
  about 10:30–11:00 ET, and some came from 15:00–16:00 ET.
- He wants the desk ready for the 9:30–11:00 ET window.

## 2. Target criteria (Ross Cameron / Warrior Trading "five pillars")

These are the starting criteria. Each threshold is to be tuned by study, not
adopted as-is.

- **Price:** $2–20. The sweet spot is $2–10 or $3–8. Avoid names under $1.
- **Gain:** up at least 10% from the prior close.
- **Relative volume:** at least 5× (2× is the looser setting).
- **Catalyst:** a news catalyst is preferred (FDA, earnings, M&A, filings).
- **Float:** under 10M shares (20M or 50M are the looser settings).
- **Timing:** the name is found premarket and in the first 1–2 hours of the
  session.

## 3. Current live movers criteria

Sources: mini `config/bot_config.json`, `config.py`, and `movers_screener.py`.

- **Feeds:** the Alpaca top-50 gainers, the Alpaca most-actives top 100, and a
  universe scan every 120 s that keeps the top 25.
- **Filters:** price $20–100; change at least 1.5%; RVOL at least 1; today's
  dollar volume at least $1M ($500K on IEX in the scan); common stock only (no
  warrants, rights, units, or levered ETPs, and no OTC); tape continuity is
  off (`ai_movers_min_live_pct` is 0); session carryover keeps up to 40 names.
- **Premarket scan (on):** the same price band, a gap of at least 1.5%, a last
  trade no more than 5 minutes old, and previous-day dollar volume of at least
  $20M.
- **Book seating:** at least $2M of dollar volume today, RVOL at least 1,
  price at least $20, and tape no more than 60 s old. Up to 25 movers are
  seeded.
- **Momentum:** `ai_watch_momentum_min_price` is also 20.

The original movers design was $2–20, at least 10% up, at least $1M of dollar
volume, and 80% tape continuity. Moving to the $20–100 band turned the movers
list into "liquid names that are up a little", which is not what the prime
directive asks for.

## 4. Findings today (all read-only)

### (a) Round-4 S1 spread / quote-imbalance: null

Gross edge was about 0 to +1.5 bp, net was about minus the spread, and the
result equals random entry. It ran on the 30 most liquid names, which are
mostly ETFs and mega caps.
Commit `0bc5482`; doc `docs/studies/STRATEGY_EDGE_ROUND4_VOLUME_SPREAD_2026-09-26.md`.

### (b) Momentum runway study

Commit `97cddec`; doc `docs/studies/MOMENTUM_RUNWAY_2026-09-27.md`; script
`tools/studies/momentum_runway_study.py`.

- There is no usable runway after seeding. Good windows (+50 bp before −35 bp)
  exist about 8 times per name-day, but their median length is 2 minutes, and
  the reverse move is more frequent (42% versus 23.5%). The windows reflect
  volatility, not direction.
- The −50 crosses are random-timed. 228 timing rules were tested out of
  sample, and none helped except avoiding wide spreads.
- The minutes before seeding averaged +35 bp. This carries a hindsight caveat.

### (c) Why momentum seeds are labelled around 10:50

- It is mostly a label artifact. The book echoes names back onto the
  dashboard (93% of the dashboard ticker rows on 9/25 were `src=book`), and
  the momentum seeder re-claims them. 74% of those names were first seeded by
  another source. The real first seed was about 9:56 ET, and the name was
  seated about 10:17 ET.
- There are also real code gates in `ai_entry_watch.py`:
  - The `mom_open` soft seed silently skips when the premarket price is stale
    (pct is None, it needs at least 8%, and no log row is written).
  - The premarket time-adjusted RVOL reads about 0, so the name is refused as
    `thin_rvol`. This happened about 4,500 times from 9/22 to 9/25.
  - `cfg.get("ai_watch_min_pct_change", 50.0) or 50.0` makes a config value of
    0 act as +50%.
  - Only about 21% of premarket momentum names are in the $20–100 band at the
    open.
- **Proposed fixes (NOT built):**
  - Ignore `src=="book"` rows in the momentum seeders, or keep the first
    proposer's label.
  - Use a live SIP/IEX price before 9:30 and treat premarket RVOL as unknown.
  - Log the silent pct skip.
  - Fix the `or 50.0` fallback.
- **Recommended timing:** after the Monday 9/28 barebones verdict, so that
  Monday stays a clean comparison with Friday. Jonathan has not decided.

### (d) Sub-$20 premarket panel names, 9:30–11:00 ET, with real SIP spreads

The sample covered 18 days and 941 name-days. Nothing is capturable after the
spread.

- Under $5: the spread is about 87 bp and net is about −130 bp.
- $5–10: the names fade after the open (gross −72 bp).
- $10–20: gross is about flat and net is negative.
- Top gappers bought at 9:35 or 9:45 ET had an MFE of 300–600 bp but were at
  −150 to −300 bp after 30 minutes.
- Only the $20–100 −50 crosses from 9:30 to 11:00 ET and late in the day were
  about breakeven.
- **Caveat:** this used panel-mention names, not the five-pillar filter.
  Manual execution (limit orders, discretion, longer holds, premarket trading)
  is not modelled.

### (e) General

Weeks of tests found no intraday edge on free data (Claude's 9/26 findings
plus rounds 1–5). The only weak positive is 12-1 month momentum on the top 5
of 108 names, held about 20 days.

Documented slower edges worth testing on years of free daily data are
multi-month momentum, post-earnings drift, overnight versus intraday returns,
and catalysts. The AI catalyst shadow logger starts Monday 9/28 at 08:45 and
12:15 ET and will run for 6–8 weeks.

## 5. DONE (9/27): five-pillar study and Discord squeeze-levels study

### (a) Five pillars: no capturable runway in any cell

Commit `9f046aa`; doc [`docs/studies/FIVE_PILLARS_2026-09-27.md`](studies/FIVE_PILLARS_2026-09-27.md);
script `tools/studies/five_pillars_study.py`.

- 540 threshold cells were tested (price x gain x RVOL x float x catalyst) on 12 months
  of SIP data. None is positive out of sample.
- The strict five pillars ($2-20, >=10%, >=5x RVOL, float <10M, news) give about
  6.9 names a day. 82% of them qualify premarket, with a median qualify time of 07:41 ET.
  So the names can be found early.
- But net is about minus the spread: roughly zero drift is left after they qualify.
- Low float adds no drift and doubles the spread (median 158 bp for <10M vs
  about 90 bp overall). It is the most damaging pillar for net.

### (b) Discord "Squeeze Potential Alert" levels: buying the break loses

Doc [`docs/studies/DISCORD_SQUEEZE_LEVELS_2026-09-27.md`](studies/DISCORD_SQUEEZE_LEVELS_2026-09-27.md);
script `tools/studies/discord_squeeze_levels_study.py`. It used 191 alerts from Bullish
Bob's room, 9/1-9/25 (18 sessions), with SIP bars and quotes.

- Buying breaks of the posted levels loses even before costs (at best flat: zero-cost
  bracket E1 -1.3%, E3 +0.1%; zero-cost 30-minute hold negative for every entry). After the real SIP
  spread plus 1 cent it loses about -2.3% to -3% a trade with the room's own bracket, and
  more with a 30-minute hold. Every level entry did worse than a random minute on the same name.
  Both alternate-day halves are negative, although 18 days is a weak check.
- The levels sit far above the price. Price was below L1 at 187 of 188 alerts
  (median -32%), and L1 printed before 10:30 only 36 times.
- The free IEX feed cannot see these names premarket. There were zero IEX bars before
  08:00 ET on all 190 name-days, and IEX volume is 0.6% of SIP from 06:30 to 09:30.
- Nightly watchlists find volatility, not direction. The next-day median close vs open is
  -1.9%. The only faint lead is the VOLUME category at a +1.9% median close vs open
  (54% green, n=80). This is not significant on its own.

## 6. NEXT STEPS and open decisions for Jonathan

- When to ship the seeding fixes in §4(c).
- Whether to widen the band for five-pillar names, depending on the study.
- Whether to run the longer-hold daily-data study.
- The book server stays in shadow.
- Whether to delete the 8 merged remote branches.

## 7. Monday 9/28 checks

- **12:27 ET:** check that the catalyst logger wrote rows to
  `ai_reports/ai_catalyst/2026-09-28.jsonl`.
- **18:31 ET:** read the barebones verdict (the nightly PASS/DRIFT/FAIL, and
  Monday versus Friday on tape_only share, opens, and concurrency). The
  freshness rollback is `ai_watch_quote_freshness=false` plus a kickstart,
  and it happens only with Jonathan's OK.

## 8. Operating rules

- **Ship:** commit and push on the MacBook, then `git pull --ff-only` on the
  mini, then if needed `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk`.
  Never run `./trading restart` over ssh. After a restart, check
  `logs/ai_trader.log` for desk_io recording and `agy_auth=ok`.
- The mini is reached with `ssh mac-mini-away '…' < /dev/null`. Its repo is
  `~/repo/trading-helper`; it cannot push, so deploy with `git pull --ff-only`.
- Never run pytest on the mini.
- Free data only (no paid live SIP).
- No code, restart, or trading changes during 09:30–16:00 ET without
  Jonathan's OK.
