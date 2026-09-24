# Holistic book review — 2026-09-24

Read-only review of the paper desk (`jambione/trading-helper`), written ~12:50 ET
while the market was open. No code or config was changed on the mini. Numbers
come from `rehearse_open.py`, today's ledgers on the mini, live `signal_state.json`
/ `entry_watch_state.json` / source JSON files, and greps of the `replay-tool`
tree on the MacBook.

**Jonathan's yardstick (only):**
1. A full, fresh book of quality names ($20–$100, real momentum) ready to arm.
2. At least one open about every 10 minutes all session.
3. Keep the system simple.

**Design direction he locked at 12:43 ET:**
- Keep the arm: fast %R (EXH) crossing −50.
- Quality is already defined by **Movers + Trending + Research (Grok/AGY)**. Do
  not re-tune quality downstream.
- Build a **book server** that seats those names at the right time (below −50,
  approaching/rising toward the cross) with fresh data so the arm can fire.

---

## Executive answer

**Biggest leak (measured):** names from the three quality sources are available
early, but the desk does not keep them seated with one honest price clock
through the −50 approach. Seats die as `tape_only` / `dead_unknown` /
`mid_rise_stale` while the engine often still has a live print. Result today:
first open at **11:05** (95 minutes after the bell), **7 fills by ~12:44**, and
long stretches with **zero opens per 10-minute slot**.

Supporting numbers (09:30–12:40, `rehearse_open`, measured):

| Pass criterion | Result |
|---|---|
| Book ≥ 10 by 09:40 | **FAIL** — 4 |
| ≥ 6 armable at 09:40 | **FAIL** — 1 |
| Median armable after 09:40 ≥ 6 | **FAIL** — median **1** |
| Data-blocked share of seats < 10% | **FAIL** — **40%** |
| Every arm reaches an order | **FAIL** — 8 arms, 6 entries (confirm_slip, buy_cap) |
| First buy by 09:45 | **FAIL** — 11:05:05 |
| ≥1 open / 10 min (Jonathan) | **FAIL** — see funnel |

The volume-clock + armable-only projection on `replay-tool` still leaves
data-blocked ~37% and book@09:40 at 3 — so **Claude's supply fixes alone do not
hit the goals**. Freshness (one price, one clock) is the missing piece.

---

## A) End-to-end path of a name

```
Movers / Trending / Research (Grok, AGY, Claude)
        │  files: movers_stocks.json, trending_stocks.json,
        │         claude_suggestions.json, grok_suggestions.json
        ▼
dashboard watchlist (transcription/wb_watchlist.json)
        │  also: Discord OCR, momentum mentions, engine push src=book
        ▼
ai_entry_watch.sync_watch_from_source_panels
        │  desk_candidate_rows → soft_seed → push_candidates_to_engine
        │  → ensure_watch_stream → passes_inclusion / admit_arm_gates
        ▼
entry_watch_state.json  (the book / seats)
        ▼
poll_once → should_arm_buy (−50 mid_rise) → confirm → ai_trader / ai_positions
```

### Hop details

| Hop | Owner process | Price it reads | Timestamp / freshness | Refusal labels (examples) |
|---|---|---|---|---|
| Movers seed | `movers_screener.py` | SIP daily / scan IEX | Live clock vs **delayed SIP volume** (bug fixed on branch, not live) | thin_rvol, price band, warrant/ETP shape |
| Trending seed | `trending_screener.py` | Stocktwits + IEX rvol | Panel refresh | red, thin_rvol, score |
| Research seed | `ai_trader.py` / suggest publishers | Often **no desk quote** until seated | Board publish time | thesis list; live `$20` floor **skipped** today (INFQ $14, CLF path) |
| Momentum / Discord | `dashboard.py` + OCR | Mention heat | mention window | Not a Jonathan quality source |
| Watchlist | `dashboard.py` | Finnhub WS, Finnhub REST, Alpaca trades/quotes | `price_age_sec`; board stale 120s; **src=book exempt from 15‑min purge** | list grows 50+ |
| Admit | `ai_entry_watch` (in `ai_trader` poll) | `live_print` = max(engine rt_*, dash) | `ai_watch_admit_max_tape_age_sec` 120; movers 60; heating 300 | no_tape, stale_tape_admit, below_min_price, not_uptrend, thin_rvol, admit_range_pos, … |
| Seat / book | `ai_entry_watch` | Row `last_ask` + src | dead_seat 90s (temp), unarmable 30s, stale_timeout 180s, … | dead_unknown, never_armable, stale_tape_cap |
| Arm | `ai_entry_watch.should_arm_buy` | `decision_price` / stream | `ai_watch_decision_max_age_sec` 60 (temp) | wait_mid_rise, tape_only, spread_wide, gapped_down, mid_rise_stale, engine_stale |
| Confirm → order | same → `ai_trading` / positions | Re-pull ask | confirm slip (px lim 0 today), buy_cap per poll | confirm_slip, buy_cap, confirm_stale |
| Flatten | `ai_positions` | `live_print` | `ai_stale_data_max_age_sec` 60 (temp) | stale_data (RKLB 11:12) |

### Counts (config on mini + code)

| Kind | Count | Notes |
|---|---|---|
| Distinct price sources / clocks | **~9** | Finnhub WS, Finnhub REST, Alpaca batch trades, Alpaca per-symbol quotes, Alpaca bars→engine, engine `rt_*`, book row stamp, SIP delayed (spread/rvol), scanner snapshot |
| Freshness knobs in `bot_config.json` | **33** | ages, TTLs, graces, evicts, stale timeouts (listed in appendix) |
| Admission / arm / eviction gate labels | **50+** | conjunctive admit + mid_rise family + spread/gap + many MACD/RSI leftovers |

That many clocks and knobs is the opposite of "simple," and it is how a seat
can be `tape_only` next to a young engine print (CDNA).

---

## B) Today's funnel (measured)

### Per 10-minute slot (09:30–12:35) — from `rehearse_open`

Columns: **book** seats, **arm** armable, **gate** intended-gate blocked,
**data** data-blocked, **cand** candidates seen, top seat blocks / admit refusals.

| time | book | arm | gate | data | cand | opens | note |
|---|---:|---:|---:|---:|---:|---:|---|
| 09:30 | 3 | 0 | 2 | 1 | 11 | 0 | thin book |
| 09:40 | 4 | 1 | 2 | 1 | 12 | 0 | fail criteria |
| 09:50 | 2 | 0 | 1 | 1 | 10 | 0 | |
| 10:00–10:10 | 3–5 | 0 | 1–3 | 1–2 | 10–12 | 0 | |
| 10:15 | 8 | 1 | 2 | 5 | 16 | 0 | CRWV arm→confirm_slip 10:17 |
| 10:20–10:45 | 6–14 | 0–1 | 0–7 | 4–11 | 18–24 | 0 | Finnhub deadlock window |
| 11:00 | 5 | 1 | 2 | 2 | 22 | **1** | RKLB 11:05 |
| 11:10 | 7 | 3 | 2 | 2 | 18 | 0 | RKLB stale_data flatten |
| 11:50 | 9 | 3 | 5 | 1 | 23 | **1** | VNOM |
| 12:10 | 8 | 5 | 1 | 2 | 18 | **3** | AR, INFQ, RKLB |
| 12:20 | 8 | 3 | 2 | 3 | 22 | **1** | CLF (under $20 research hole) |
| 12:35 | 9 | 4 | 4 | 1 | 20 | 0 | |
| 12:40 | — | — | — | — | — | **1** | DHT (after rehearse end) |

**Opens by 10‑min slot (entry_ok events):** 11:00×1, 11:50×1, 12:10×3, 12:20×1, 12:40×1.
Morning 09:30–11:00: **eighteen** empty slots. Gaps between fills after first:
53m, 13m, then a burst, then 21m.

### Top refusal / eviction reasons → intended vs plumbing

| Reason | Where | Approx count (RTH) | Class |
|---|---|---|---|
| thin_rvol | admit seed | 84k ledger lines | **Plumbing** this morning (SIP volume vs live clock); **intended** after volume-clock fix + real thin names |
| tape_only / stale_tape | arm decisions | 1703 | **Plumbing** (dual clock / watchlist quote starvation) |
| spread_wide | arm | 1209 | **Intended** (real wide spread). Premarket SIP read before ~09:46 was **plumbing**; branch waits until 09:30+delay |
| gapped_down | arm | 1143 | **Intended** |
| wait_mid_rise | arm | 1096 | **Intended** (not at −50 cross yet) — *good* if seated |
| below_min_price | admit | ~9.9k | **Intended** when price real & <$20 (samples were $1–$14, not null) |
| stale_tape_admit | admit | ~9.1k | **Plumbing** |
| mid_rise_stale | arm | 344 | **Plumbing** (crossed, then data age blew the 60s window) |
| dead_unknown drops | watch_drop | 419 | **Plumbing** (CDNA× many) |
| no_rsi_data | arm | 170 | **Plumbing** (bars/engine), not an RSI-*direction* opinion |
| confirm_slip | watch_skip | 1 (CRWV) | **Plumbing** relative to goal (cancelled *favorable* move; px lim now 0) |
| buy_cap | watch_skip | 1 (RKLB) | **Plumbing** (poll counter; fixed in code path with reset) |
| Research <$20 fill | INFQ | 1 fill | **Plumbing** (research skipped band; `admit_arm_gates` on branch closes) |

### Single biggest leak (quality exists → open)

**Not** "we lack quality names." Movers alone had **27** in-band names near
midday; **19** of them were **off** the book. Trending had **18**, only RKLB
seated.

**Leak:** the admit/seat layer does not act as a server that (1) takes the
three sources, (2) holds them with one fresh price through the below−50
approach, (3) ranks by distance-to-cross. Instead it juggles four+ intake
paths, 33 freshness knobs, and a watchlist that leaks `src=book` rows until
Alpaca 429s starve the same tape the seats need.

Evidence the supply was early enough when a cross *did* fire on-book:

| Symbol | First in quality source | Cross (`last_mid_rise`) | Lead |
|---|---|---|---|
| VNOM | movers 09:56 | 11:57 | ~121 min |
| DHT | movers 11:02 | 12:43 | ~101 min |
| CRWV | trending 06:13 | 10:17 | hours |
| RKLB | trending 04:00 | 11:04 | hours |

So the miss is **timing of seating + freshness**, not "source too late."

**Missed at the right time (partial measure — honest about limits):**

- Decision ledger only sees symbols **while seated**. On-book −50 crosses
  observed today: **7**. Off-book historical crosses need bar replay (not run
  mid-session against the shared Alpaca budget).
- **Snapshot now (~12:45):** engine has names with fast %R ≤ −50 that are
  **not** on today's book evaluation — e.g. BMNR (−50.7, movers what-if name),
  FIGR (−64, in band). Several others ≤ −50 are outside the $20–$100 band
  (META/ORCL/FSLR) or under $20 (VG) — correctly non-targets.
- **CDNA (clearest story):** movers name; seated and **evicted repeatedly**
  (`dead_unknown` / steal) while engine had live prints; later spent long
  intervals on book in `wait_mid_rise` without converting; by 12:45 fast %R
  was already −10 (cross window gone). That is the failure mode in one ticker.

---

## C) Alpaca request budget (measured + inferred)

Shared paper account; config `alpaca_max_rps=0.6` (~36 paced req/min **per
process** if obeyed — processes do not share one pacers).

| Process | Evidence | Estimate |
|---|---|---|
| `dashboard.py` | **140** real 429 lines after 11:44 restart; **avg ~2.7/min**, peaks **10/min** of *failures alone*; logs show **per-symbol** `latest quote X failed` plus batch `latest trades failed for 15 symbol(s)` | Dominant consumer. Watchlist **51–52** all `src=book` → quote fanout |
| `signal_engine.py` | **53** `[ALPACA] 429 fetch_bars(...)` | Bars refresh per subscribed name |
| `ai_trader.py` | **4** 429-ish | Arm/confirm quotes |
| Movers / trending | Not broken out; movers uses screener + SIP bars | Bursty on scan |

**Cause:** `src=book` rows are exempt from age purge **and** treated as held
for the cap (`dashboard.load_tickers`), so the list grows to the Finnhub ~50
ceiling and still hammers Alpaca for SIP/IEX quotes. Engine push max 44 feeds
that loop.

---

## RSI-direction check — does it earn its keep?

Jonathan keeps the **−50 cross**. Question: extra RSI-*direction* filters.

| Check | Live config | Today's decision-ledger | Verdict |
|---|---|---|---|
| Mid-rise requires **slow %R rising** at the cross (`pctr_slow_rising`) | Built into `_mid_rise_allows_buy` | Crosses that armed all had the mid_rise path; cannot see "fast crossed / slow flat" in this ledger (those never set the cross latch → stay `wait_mid_rise`) | **Keep for now** as part of the one arm definition; measure properly with bar replay (what-if: latch on fast cross alone) |
| `ai_watch_arm_cm_rsi_require_rising` | **False** (already off) | `rsi_not_rising` **0** | Already not earning cost — leave off |
| `ai_watch_require_exh_rising` / `exh_falling` | True | `exh_falling` **9** vetoes | Tiny; optional |
| `no_rsi_data` / `rsi_not_realtime_alpaca` | — | **170 + 4** | **Data plumbing**, not direction — fix with seating/bars, don't treat as alpha |

**Honest gap:** ledgers do not record "fast crossed while slow not rising."
Recommend a one-day `rehearse_whatif` flag: arm on fast −50 cross **with and
without** `pctr_slow_rising`, score +1%−before−1%. Until that, do **not** pile
on more RSI direction gates.

---

## rvol_pace ≥ 1.64 vs Movers filters

Movers already define quality with:

- price band (live config $20–$100),
- min % change, common-stock / non-ETP shape,
- SIP (or scan) **rvol** floor,
- dollar-volume / per-minute continuity floors,
- optional most-actives + session retention.

`rvol_pace_sip` is a **second** volume statistic (minute SIP pace vs 20-day,
delayed ~16m). Live `ai_watch_min_rvol_pace=0` (observe only).

**Recommendation:** **drop as an arm/admit gate.** At most use as a **ranking
tiebreak** on the ready-queue. It is redundant with Movers' own volume filters
for supply decisions and would shrink the book Jonathan wants full.

---

## D) Simplest architecture that meets the goals

### Target shape (~8 bullets)

1. **Three sources only** for supply: Movers, Trending, Research (Grok/AGY).
2. **One book server** owns seats (replace today's sync + soft_seed + momentum
   + Discord + engine-push-as-candidate tangle).
3. **Admit = hard intended gates only:** $20–$100, gap-down, real SIP spread
   (after delay), cooldowns, loss brake — plus "we have a dated print."
4. **One price + one timestamp per name** = fresher of engine `live_print` and
   dashboard trade; that age is the **only** freshness input to admit / arm /
   evict / flatten.
5. **Rank seats by distance-to-cross:** fast %R below −50 and rising / near
   level first; refill from a short backup queue of source names.
6. **Warm early:** subscribe Finnhub + load bars as soon as a source names the
   symbol (before it is "armable"), so %R exists when the cross happens.
7. **Arm unchanged:** fast %R cross up through −50 (keep slow-rising until
   measured); then confirm→order with no cancel on favorable slip.
8. **Batched quotes; watchlist = subscription set capped ~40**, book rows
   expire when the server drops the seat (no immortal `src=book` leak).

### How early to seat / how many seats (inferred from today + mechanics)

- Finnhub needs membership to print; engine needs bars for %R — practical
  warm-up **1–3 minutes** after subscribe on liquid names, longer on thin.
- Cross window for mid_rise is short (`mid_rise_max_age` ~60s). Seats must
  already be warm **before** the cross, not at it.
- Goal ≥1 open / 10 min with sparse cross rate ⇒ need a **pipeline** of
  approaching names, not 1–2 armable. Pass bar **≥6 armable** / **book ≥10 by
  09:40** is the right proxy; with 40% data-blocked today you need ~10–14 live
  seats to net ~6 armable. After one-clock fix (target data-blocked <10%),
  **~10–12 quality seats** plus a **~10-name warm queue** should be enough.
- Today's 7 on-book crosses with huge source lead times say: **seat when the
  source first prints the name in-band and %R is still below −50 (or unknown
  but warming)** — not after it is already "interesting" on the dashboard.

### DELETE or collapse under that design

| Delete / collapse | Why |
|---|---|
| Momentum / Discord / bb_live as **admit supply** | Not in Jonathan's three sources; noise vs Movers/Trending/Research |
| Soft-seed + morning-flood parallel path | Same job as the book server refill |
| Engine push **as candidate intake** | Keep push only as **subscription** for names the server already chose |
| Immortal `src=book` watchlist exemption | Cap/TTL tied to seat life; stops 429 spiral |
| Duplicate freshness knobs (collapse to **one** age ceiling used everywhere) | 33 → ~3 (admit warm, arm/decision, flatten) |
| `admit_range_pos`, heating special tape ages, macd arm lanes, square/triangle arms while mid_rise is the one arm | Extra gates fighting the −50 server |
| `rvol_pace` as hard gate | Redundant with Movers; observe/rank only |
| Research price-band skip | One band for all sources (`admit_arm_gates`) |
| Per-symbol Alpaca quote storms | Batch only; cache SIP spread longer |

Keep intended: −50 wait, gap-down, real wide spread, $20/$100, cooldowns, loss
brake. Do **not** raise max positions.

---

## Ordered plan for tonight (vs Claude's handoff)

Claude's order was: reverts → watchlist leak → one price/clock → merge
replay-tool → admit_arm_gates + rvol 1.0 → deploy → multi-day replay.

| Step | Action | vs Claude | Serves goal | Verify | Risk |
|---|---|---|---|---|---|
| 0 | Revert afternoon temp knobs after 16:00 | **Keep** | Honesty for next session | config diff | Low |
| 1 | Watchlist: expire/cap `src=book` with seats; batch quotes | **Keep Tier 1a/b** | Fresh tape, fewer 429s → goal 1–2 | 429/min ↓; list ≤40 | Medium (don't drop open positions) |
| 2 | **One price, one clock** for admit/arm/evict/flatten | **Keep Tier 2.1 — raise priority above supply tweaks** | Cuts data-blocked; CDNA-class | rehearse data-blocked <10%; CDNA/RKLB cases | Medium |
| 3 | Merge `replay-tool`: volume-clock + `admit_arm_gates` + rehearse tools | **Keep** | Morning movers supply; research band hole | whatif + rehearse 09-21..24 | Low–med |
| 4 | Book server slim: **3 sources only**; rank by distance-to-cross; refill | **Add / reorder ahead of Claude Tier 2.4–5 research piles** | Goals 1–2 + simplicity | book≥10@09:40, ≥6 armable, ≥1 open/10m | Med (intake rewrite) |
| 5 | Drop rvol_pace as gate; leave observe or rank tiebreak | **Drop** Claude arm-comparison emphasis on rvol_pace≥1.64 as filter | Simplicity | whatif unchanged supply | Low |
| 6 | Confirm: no favorable-slip cancel; keep short confirm path | **Keep Tier 2.6** | Every arm→order | arms==entries on rehearse | Low |
| 7 | Measure slow-%R requirement with whatif (optional same night) | **Add** | Answer RSI-direction | +1%−before−1% with/without | Low |
| — | Scanner warm-up / more Finnhub tricks / Grok prompt piles | **Defer** | — | — | — |
| — | Premarket scanner rebuild | **Defer** | — | — | — |

**Pass criteria (ship gate):** book ≥10 by 09:40; ≥6 armable; data-blocked <10%;
every arm reaches an order; **and** ≥1 open per 10 minutes across the session
(or a documented structural exception only at the open warm-up minute).

---

## Appendix — freshness knobs counted (33)

`ai_entry_limit_ttl_sec`, `ai_entry_unconfirmed_ttl_sec`,
`ai_phase_b_entry_limit_ttl_sec`, `ai_phase_b_print_max_age_sec`,
`ai_phase_b_seat_stale_sec`, `ai_stale_data_flatten`, `ai_stale_data_max_age_sec`,
`ai_watch_admit_grace_sec`, `ai_watch_admit_max_tape_age_sec`,
`ai_watch_db_bar_refresh_sec`, `ai_watch_dead_seat_evict_sec`,
`ai_watch_decision_max_age_sec`, `ai_watch_engine_stale_max_sec`,
`ai_watch_far_exh_evict_sec`, `ai_watch_heating_admit_max_tape_age_sec`,
`ai_watch_macd_max_age_sec`, `ai_watch_max_stale_tape_seats`,
`ai_watch_movers_admit_max_tape_age_sec`, `ai_watch_no_stream_strike_grace_sec`,
`ai_watch_os_triangle_max_age_sec`, `ai_watch_scout_ttl_sec`,
`ai_watch_square_max_age_sec`, `ai_watch_stale_restream_grace_sec`,
`ai_watch_stale_restream_pins_only`, `ai_watch_stale_timeout_grace_sec`,
`ai_watch_stale_timeout_include_need_stream`, `ai_watch_stale_timeout_quiet_max_sec`,
`ai_watch_stale_timeout_reseed_sec`, `ai_watch_stale_timeout_sec`,
`ai_watch_stream_subscribe_grace_sec`, `ai_watch_unarmable_evict_sec`,
`rs_max_p0_staleness_sessions`, `rs_max_stale_frac`.

---

## Measured vs inferred

| Claim | Status |
|---|---|
| Funnel table, pass criteria, arm_why counts, entry times, 429 counts, watchlist src=book size, CDNA drops | **Measured** |
| Dual-clock as cause of tape_only beside live engine | **Measured** pattern (CDNA/handoff + live_print code path); not every tape_only row re-proven |
| Off-book −50 crosses for all quality names today | **Not fully measured** (needs bar replay); snapshot + CDNA + BMNR used as partial |
| ~10–12 seats enough after freshness fix | **Inferred** from data-blocked math + pass bars |
| rvol_pace redundant with movers as gate | **Inferred** from overlapping intent; not A/B traded |
| Slow %R rising worth keeping | **Unproven** in today's ledger; recommend whatif |

*Branch: `holistic-review` off `origin/replay-tool`. Not pushed. Mini tree untouched.*
