# AI Desk — where to pick up on the server

**Written 2026-08-05, end of session.** Branch `master-mac` @ `e59991d`.

Every claim in this file was verified against source or live state on 2026-08-05, with the
anchor given. Where something is unverified it says so explicitly. Do not trust a line here
that lacks an anchor — check it before building on it.

---

## 1. Deploy to the Mac mini

The desk runs on the Mac mini (`trading.jbrasfield.com`). The MacBook is a dev box.

```bash
cd <repo>
git fetch origin
git rev-parse --abbrev-ref HEAD        # <- CHECK THIS FIRST
git checkout master-mac
git pull
# restart: dashboard.py, signal_engine.py, ai_trader.py
```

**Check the branch before anything else.** `master-mac` is **359 commits ahead of `main`**, and
`main` contains nothing `master-mac` lacks. If the Mac mini tracks `main`, none of this exists
there — not the zone geometry, the 5% stop, RSI-2/%R gating, the limit entries, or the wiring fix.

Canonical launcher is [start_all.py](../start_all.py); it reads `config/bot_config.json` and starts
dashboard → engine → ai_trader → discord/trending as enabled. Note it looks for `venv/bin/python`
(no dot) and falls back to system `python3`; this repo has `.venv`, so the fallback is what runs.

### Day-1 verification, in order

1. `curl -s localhost:8888/api/meta` returns 200 — dashboard is up.
2. Indicator map is non-empty:
   ```python
   import ai_entry_watch as ew; print(ew.DASHBOARD_URL, len(ew._engine_indicator_map()))
   ```
   Expect the remote URL and a non-zero count. Zero means the wiring regressed.
3. **Watch `no_indicators` disappear from the blocker codes.** Before `e59991d` it was returned for
   every symbol on every poll ([ai_entry_watch.py:2171-2174](../ai_entry_watch.py#L2171-L2174)).
   Real rejections (`indicators_faded`, `above_zone`, `sell_signal`) are the healthy state.
4. **Confirm the push actually lands** — see P0-2 below. This is the one part of the fix that is
   still unverified.
5. On the first fill, confirm `claude_reports/outcomes.jsonl` gains a record whose `entry_price`
   equals the broker's `filled_avg_price` and whose `close_reason` matches where it actually exited.

---

## 2. State of the world

### Running where

| process | box | places orders? |
|---|---|---|
| `dashboard.py` | Mac mini (+ MacBook, dev) | no |
| `signal_engine.py` | Mac mini (+ MacBook, dev) | no — order path is dead code |
| `ai_trader.py` | Mac mini | **yes** |

`ai_trader.py` was **stopped on the MacBook on 2026-08-05** — see P0-1.

### What shipped this session

| commit | what |
|---|---|
| `e59991d` | one dashboard; the indicator gate finally has data |
| `4650c46` | marketable limit capped at the zone top, not a market order |
| `731242b` | entry band widened to 4% |
| `1638abe` | duel mode off by default |
| `8b0e856` | reachable zone, binding gates, honest READY |
| `a871a42` | outcomes priced off real fills; notional cap; buying-power check |
| `6df6589` | stop-market exits; bracket child leg ids |

### ⚠ Security — do this regardless of the roadmap

`engine_control_secret` is present with a real value in `config/bot_config.json`, which is
**tracked in git** and has been since `33a4aee`. It predates this work. Rotate it and move it to the
gitignored `config/secrets.json`, which already follows that pattern. Rotating alone is not enough —
the old value stays in history.

---

## 3. The evidence base

Replay of the desk's exact entry/exit rules over **1,220 symbol-days** — 90 days of real IEX minute
bars, on the 23 names the watchlist has actually held. 10bps round-trip cost assumed.

**What the desk does today (5% stop, 1.5R target):**

| | |
|---|---|
| exits at the 15:50 clock | **77.2%** |
| exits at the stop | 15.1% |
| exits at the target | **7.7%** |
| expectancy | **−0.0027 R** (t = −0.13) |
| median hold | 341 min |

**Why the target can't be reached:**

| | |
|---|---|
| median intraday range (high−low)/open | **5.44%** |
| p75 range | 7.77% |
| days offering a ≥7.5% up-move from the open | **9.0%** |

The stop (5%) is nearly a full day's median range; the target (+7.5%) is above the p75 of the
entire day's travel, and must be captured from a pullback entry in one session.

**The exits subtract value:**

| | expectancy | t |
|---|---|---|
| full rule set (5% / 1.5R) | −0.0027 R | −0.13 |
| same entries, no stop, no target | **+0.0155 R** | +0.61 |

Not market drift: mean open→close across all 1,220 symbol-days was **−0.006%** (t = −0.05).

**Parameter grid** — expectancy improves monotonically as the stop widens and converges to zero at
8%. That is the signature of a zero-edge entry plus cost drag (cost in R = `cost / stop_width`), not
of an edge with an optimum. The statistically *strong* results are the negative ones — 2%/0.5R at
t = −4.56. **Do not "improve" this by tightening the stop.**

**The indicator gate looked real and then died.** Timing entries on `cm_ok AND pctr_ok` (via the real
`strategy_three_indicator` code, not a reimplementation) gave +0.0418 R, t = +2.16, with the logical
complement (`neither`) oppositely signed at −0.0345 R. Cluster-robust by symbol t = +2.66. Then:

```
first half    n=503   exp=+0.0977 R   t=+3.32
second half   n=515   exp=-0.0129 R   t=-0.52
```

The entire effect is in the first 45 days and absent in the second 45. Dropping one symbol (SEDG)
takes t from 2.16 to 1.51. **Treat as unproven.** ~40 configurations were tried; a best t of 2.16
is what chance produces at that count.

**Caveats on all of the above:** IEX bars are a slice of the tape; fills are modeled optimistically
at the zone top; the limit order is assumed to fill rather than rest; the universe is today's
watchlist replayed backwards, so it is mildly survivorship-biased toward names popular *now*.

---

## 4. Roadmap

### P0 — the desk cannot be honestly evaluated until these are true

**P0-1. One trader per account.** Both boxes used the same Alpaca paper key (`...UNNR`). This
MacBook's log shows `reconcile_unmanaged` for **CMG** (622 events, 08-04), **UPST** (253, 08-05),
**AGEN** (32, 08-04) — none of which this box opened (it opened GLXY, NVDA, SMCI). Consequences:
`ai_max_positions=5` and `ai_max_open_risk_pct=5.0` are enforced per-instance on a shared account,
so real exposure can be double what either box believes; and `liquidate_all` at EOD closes *every*
position in the account, so whichever box reaches 15:50 first flattens the other's book.

*Plausible but unproven:* the single GLXY outcome record — entered 19.44, exited 19.35 six minutes
later, tagged `stopped_out` with the stop at 18.412 — looks like an external close rather than a
stop. Confirming needs the Mac mini's log.

**Decide: one box owns the account, or give each its own paper key.** Until then no risk aggregate
and no outcome record is trustworthy.

**P0-2. Verify `push_candidates_to_engine` lands.** `e59991d` fixed and verified the *read* path
(indicator map 0 → 8 symbols). The *write* path was deliberately not tested — POSTing to
`/api/tickers/add-bulk` mutates a production watchlist. Confirm on the first session that pushed
candidates appear in the remote's ticker list within one engine scan (`scan_interval_sec`, 60s).
See [ai_entry_watch.py:1398](../ai_entry_watch.py#L1398).

**P0-3. Outcome capture.** `claude_reports/outcomes.jsonl` holds **1 record against 37 `entry_ok`
events**, and that record uses the old `exit_price_approx` key (pre-`a871a42` code). The plumbing was
fixed — [ai_positions.py:1097](../ai_positions.py#L1097) now writes `exit_price` — but it has never
been observed producing a correct record end-to-end. Until it does, `realized_r_today` is 0.0, the
daily-loss gate has no data, and **no change below can be scored.** This gates everything.

### P1 — what the backtest says is actually wrong

**P1-1. Selection.** Admission is Stocktwits watcher count. That picks DKNG, UBER, INTC, SNAP —
names widely-held enough to be efficiently priced, median day range 5.44%. Day-trading edge comes
from dislocation: a catalyst, real relative volume, and a float small enough to move. DKNG alone
accounts for 910 watch events on 120k watchers. Replace popularity with real RVOL + gap + catalyst.
`ai_min_dollar_volume` is checked in code ([ai_entry_watch.py:1532](../ai_entry_watch.py#L1532))
but its configured value is `0.0`, so the liquidity floor is off.

**P1-2. Target and horizon contradict each other.** A +7.5% target on a 15:50 flatten is unreachable
91% of days. Either bring the target inside the day's realistic range, or stop flattening daily.
Pick one — do not tune around it.

**P1-3. Mean-reversion entry on a momentum universe.** RSI-2 oversold is Connors mean reversion,
designed for a 1–3 day hold, currently bolted to a trend-selected universe with a day-only exit.
Either buy continuation (break of the pullback high, stop under the pullback low), or hold the
mean-reversion trade overnight.

**P1-4. Stops have no relationship to the chart.** A fixed 5% on a name whose median day travels
5.4% is a volatility bet, not a risk decision. Stops belong under structure — the pullback low, or
VWAP.

**P1-5. Concentration.** 27 of 37 entries were SMCI; 8 NVDA; 2 GLXY. Three names in three days. Not
a portfolio.

### P2 — dead knobs and silent failures (verified 2026-08-05)

| # | item | status |
|---|---|---|
| P2-1 | `ai_watch_min_adx` gate | **cannot fire** — `signals.adx` is implemented but nothing publishes `adx`; the engine never emits it |
| P2-2 | `time_stop_days` | **still live** at [ai_positions.py:142](../ai_positions.py#L142), [:867](../ai_positions.py#L867), [:1295](../ai_positions.py#L1295). The day-only horizon was confirmed intentional, so this is dead weight — delete it or drop the day-only rule |
| P2-3 | `_pct_change_value` | the fraction-vs-percent heuristic is still a literal `pass` returning the raw float, under a docstring claiming it converts. Implement it or delete the claim |
| P2-4 | `config_effective` echo | **not built.** No way to confirm the running process holds the config on disk — which is how "the running process is stale code" went unnoticed for a day |
| P2-5 | `claude_*` aliases | **16 duplicated pairs** in `bot_config.json`. Editing the wrong copy silently does nothing. Fold legacy → `ai_*` on load, warn once, never write back |
| P2-6 | local Discord OCR | `transcription/wb_watchlist.json` is empty, unwritten since Aug 2. Momentum now comes from the remote's feed; the local path is dead |

Already done, do not redo: `validate_ai_config` exists ([config.py:370](../config.py#L370)) **and is
wired** ([ai_trader.py:417](../ai_trader.py#L417)); `exit_price` renamed;
`ai_min_dollar_volume` is checked on the watch path.

### P3 — before spending real money

- **Size down until 100+ scored trades exist.** At 1% risk / 20% notional the sample costs real
  money to collect.
- **Do not act on the parameter sweep.** Nothing in it survived out-of-sample. Treat that grid as a
  list of ways to lose, not a menu of settings.
- Getting the desk *working* and getting it *profitable* are separate problems. As of 2026-08-05
  only the first has moved.
