# Go-Live Plan — Paper → Real Money

Drafted 2026-09-18. Every number below was measured off the mini
(`ssh mac-mini-away`) or read out of this repo on that date.

## Scope

This plan covers **machinery readiness**: what has to be true before the desk
can route real orders without losing money to a bug, an outage, or an open
port. It is an engineering checklist.

**P&L is a separate track and is out of scope here.** The desk's current
expectancy (−0.0421R over 790 closes, 7 green sessions of 31) is a known input,
not a gate in this document. §7 records what has already been ruled out on that
track so the knowledge is not lost, and §6 notes the one place where it has a
direct engineering consequence — how much money Stage 1 needs, which is "enough
to produce real fills" rather than "enough to matter."

The organizing idea: **the first live dollars are a test instrument.** Their job
is to surface the differences between the paper broker and the real one. Size
and stage criteria follow from that.

---

## 1. Current state (measured 2026-09-18)

Things that are already right, and should not be disturbed:

- `config/bot_config.json` on the mini is **byte-identical** to the repo copy
  and tracked in git. No config drift between dev and live.
- `require_auth: true` in the mini's `config/secrets.json`.
- `ai_trading_host = Jonathans-Mac-mini.local` — the host lock is set, so the
  MacBook cannot mutate the broker account.
- `engine_env._v_trader_mode` refuses `TRADER_MODE=live` from the dashboard.
- `secrets.json`, `signal_engine.env`, `users.json` are all untracked; the
  secret-scan workflow and pre-commit hooks are in place.
- Test suite: **3,247 passed, 3 failed** in 175s.

Operational facts that shape everything below:

- Desk runs on one Mac mini, reached over Tailscale. 790 graded closes across
  31 sessions, median 13 fills/session — but 40 and 67 on the last two
  sessions, since `ai_phase_b_dry_run` went `false`.
- `local_trail` is 578 of 790 exits (73%), and it is a **software** exit.
- Paper slippage: `entry_slippage_r` mean −0.0996R, `exit_slippage_r` mean
  −0.0426R.

---

## 2. Blockers — must be closed before any live dollar

These are ordered by what goes wrong if you skip them. None of them depend on
the P&L track.

### 2.1 Unauthenticated remote write to credentials and risk limits

**This one is live right now, with real broker credentials behind it.** Worth
fixing this week whether or not go-live happens this quarter.

The dashboard is exposed at `trading.jbrasfield.com` through a plain Cloudflare
tunnel straight to `localhost:8888` (`config/cloudflared-config.yml`), with no
Cloudflare Access policy in front.

`dashboard.py:3342-3356`:

```python
def _request_identity(request: Request) -> tuple[str, str]:
    token = first_valid_token(...)
    username = get_token_username(token) if token else ""
    if not username:
        query_user = request.query_params.get("user", "").strip().lower()
        if query_user == "jmb":
            username = "jmb"
    return token, username
```

`_AuthMiddleware.dispatch` (`dashboard.py:3468-3470`):

```python
token, username = _request_identity(request)
if not (verify_token(token) or username == "jmb"):
    return 401
```

Appending **`?user=jmb`** to any request yields `username == "jmb"` with no
token, and the middleware passes it. Same pattern gates `dashboard.py:3469`
and `3652`.

`POST /api/config` (`dashboard.py:4701`) has **no route-level check at all**
beyond that middleware, and writes any of the 490 `SAFE_CONFIG_KEYS`:

- `api_key`, `secret_key` — the broker credentials themselves
- `ai_trading_enabled`, `ai_trader_enabled`, `ai_trading_host`
- `ai_risk_pct`, `ai_trade_amount`, `ai_max_positions`,
  `ai_max_open_risk_pct`, `ai_max_position_pct`
- `ai_daily_loss_limit_r` — the kill switch itself

The handler reconnects the broker data client when `api_key`/`secret_key`
change.

Verified by reading code, not by probing the live host. Say the word and I'll
confirm against the running dashboard.

**Fix**

1. Delete the `?user=jmb` fallback in `_request_identity`.
2. Add explicit authorization to `POST /api/config` and every other mutating
   endpoint — don't rely on middleware alone.
3. Split `SAFE_CONFIG_KEYS`. Credentials and risk limits should not be
   web-writable at any privilege level; move them behind a file edit plus
   restart, the way `TRADER_MODE=live` already is.
4. Cloudflare Access in front of the tunnel.
5. Audit the `/api/agent/` public prefix in `_PUBLIC_PREFIX` — it bypasses auth
   entirely.
6. Set `desk_secret` (currently unset, so `_desk_secret_ok` falls back to
   `engine_control_secret`).
7. Rotate credentials after the fixes land.

**Done when:** an unauthenticated request can neither read state nor write
config, and credentials/risk limits are not reachable over HTTP at all.

---

### 2.2 No broker-side stops — positions go naked if the process dies

`ai_broker_stop_enabled = False`. Two consequences:

1. Stops live **only in the desk process**. With `local_trail` at 73% of exits,
   three quarters of the book's protection depends on `ai_trader.py` running,
   the Finnhub stream delivering, and the mini having power and network.
2. It **disables the unprotected-entry rail** as a side effect
   (`alpaca_trader.py:186`: `if cfg.get("ai_broker_stop_enabled") is False:
   return False`), so buys may open with no protective exit at all.

The comment documenting why that rail exists:

> On 2026-08-06 one of them opened 353 shares of CELH — 83% of account
> equity — with no stop, after the bracket was rejected for extended hours.
> It sat naked for 44 minutes. ALOY and XNDU took the same path on 08-04.

In paper that was a log line. With real money it is a microcap that can gap 30%
while nobody is managing it.

**Fix**

- Every live position carries a **broker-side** stop from the moment it fills.
- The software trail may ratchet *inside* that stop; it must never be the only
  line of defense.
- Handle the extended-hours bracket rejection explicitly — that is the specific
  path that produced CELH. If the protective order cannot be placed, the entry
  does not happen.

**Done when:** you open a paper position, `kill -9` the desk, and confirm the
broker still holds a protective order. This is a test to write, not a thing to
reason about.

---

### 2.3 No fill ledger — the trade log records submissions ✅ BUILT 2026-09-18

> **Status:** `fill_ledger.py` + `tools/fill_reconcile.py` landed, 21 tests.
> Not yet deployed to the mini, and the daily reconcile is not yet on a cron.
> See "What shipped" at the end of this section.


`alpaca_trade_log.json` carries 1,000 rolling rows, every one with
`order_status: OrderStatus.PENDING_NEW`. No fill confirmation, no fill price,
no fill quantity. A BUY row means an order was *sent*.

It is also polluted: 18 SELL rows carry
`<MagicMock name='mock.close_position().status'>` — unit test runs have written
into the production log.

On 2026-09-16 a replay of the log showed 4 names still open while
`claude_positions_state.json` reported `positions: {}` and `reconcile.n_live: 0`.
The log and the broker disagreed, and the log was wrong.

With real money you need an auditable record of what was sent, what filled, at
what price, and what it cost — for reconciliation, for disputes, and for taxes.
This is also the substrate every later measurement sits on, including the whole
P&L track.

**Fix**

- Append-only fill ledger written from order-status callbacks / `get_orders`
  polling, not from submission.
- Make the test suite physically unable to write to production log paths —
  inject the path, and fail the test if it resolves under the repo root.
- Daily reconciliation against Alpaca's activities endpoint.

**Done when:** the ledger and the broker agree on every fill for a full session,
checked automatically.

**What shipped (2026-09-18)**

`fill_ledger.py` — append-only, day-split on the ET day of the **fill**, at
`ai_reports/fills/YYYY-MM-DD.jsonl`. Two event kinds, neither ever mutated:

- `submit` — mirrored out of `alpaca_trader._log_action`, the chokepoint all 21
  `submit_order` paths already funnel through, so a new order path is covered
  the day it is written
- `fill` — broker truth from `get_filled_orders`, polled on
  `ai_fill_ledger_poll_sec` (60s), deduped on
  `(order_id, status, filled_qty, filled_avg_price)`

A partial fill that later completes appends a second row rather than editing the
first; `fold_orders()` collapses the stream to current truth. The dedupe set
rebuilds from disk, so a mid-session restart does not double-write.

`_guard_path()` raises under `PYTEST_CURRENT_TEST` if a write would land inside
the repo — the MagicMock rows in `alpaca_trade_log.json` are exactly the bug it
refuses to inherit. Tests get a real path via `set_ledger_path_for_tests()` or
the existing `AI_REPORT_DIR` override.

`tools/fill_reconcile.py` — exits 0 clean / 1 on any finding, so it can be a
cron guard. `BROKER_ONLY` (broker filled it, ledger never saw it) is the finding
that matters; a day that could not be checked counts as a finding, not a pass.

Verified against the paper account: `get_filled_orders` returns 19 filled orders
over 3 days with real `filled_qty` / `filled_avg_price` / `filled_at`. Note the
live timestamp format is `2026-09-17 19:50:14.391999+00:00` — a space, not `T` —
now pinned by a test, since a parse failure would file every fill under the poll
day instead of the fill day.

**Still open:** deploy to the mini (commit → push → pull → bounce); put
`fill_reconcile.py` on a post-close cron; and decide whether
`ai_fill_ledger_enabled` should stay in `SAFE_CONFIG_KEYS` — it is web-writable
today for consistency with the other ledger flags, which §2.1's split should
revisit, since turning off the audit trail is not an ordinary config change.

---

### 2.4 There is no live code path

Flipping to real money is **not a config change**. The AI desk has no live path,
by deliberate design.

`ai_trading.py:99-114`:

```python
import alpaca_trader

# HARD RULE: AI desk path never places live orders through this module.
alpaca_trader.init(
    mode="paper",
    ...
)
_ready = alpaca_trader.is_active()
_mode  = "paper" if _ready else "off"
```

```python
def is_ready() -> bool:
    return _ready and _mode == "paper"
```

`is_ready()` gates three call sites: `ai_trader.py:424`,
`ai_positions.py:1199`, `ai_entry_watch.py:13498`.

**Replace the rail rather than deleting it:**

- Live mode requires **two independent enables** (e.g. an env var *and* a file
  on disk that is not in git), so no single edit or config push can arm real
  money.
- Keep `engine_env._v_trader_mode`'s refusal of `TRADER_MODE=live`, and extend
  the same refusal to whatever new switch you add.
- **Assert the account matches the intended mode at startup.** Today
  `alpaca_trader.init` does `paper = (_mode == "paper")` and never verifies what
  came back. Log the account number and refuse to start if it isn't the expected
  one.

**Key separation.** Live and paper Alpaca keys are different values from
different pages, but the code reads a single pair — `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` — in `ai_trading.py`, `config.py`, `movers_screener.py`,
`rs_screener.py`, `backtest_v2.py`, `desk_core.py` and elsewhere. One env file
that can address either account is how you trade the wrong one by accident. Add
distinct `ALPACA_LIVE_*` vars resolved only on the live path. Note the existing
constraint: Alpaca creds live only in `signal_engine.env` and
`config.load_config()` cannot see them, so root-level scripts must resolve keys
explicitly — new plumbing has to respect that split.

**Host lock.** `ai_trading_host` is set on the mini. Keep it. The reason it
exists: both boxes ran against the same paper key, risk caps are enforced *per
instance* (so real exposure could be double what either believed), and
`liquidate_all` closes **every** position in the account — whichever box reached
15:50 first flattened the other's book. With real money, consider making the
live path refuse to *start* on the wrong host, not just refuse to mutate.

**Done when:** a reviewed diff adds the path, a dry run proves the mode
assertion fires against a deliberately wrong account, and `TRADER_MODE=live`
remains unreachable from any network-facing surface.

---

### 2.5 Staleness guards fail open

`price_age_sec` is `None`, and that single dead field disables the ratchet, the
arm check, and the blind-book staleness check — they fail **open**. Twenty-six
closes are already tagged `stale_data`. Relatedly, the desk `price` is fed back
from the dashboard, so a static value with a moving age is the tell.

Separately, logged `spread_r` overstates the real book by 1.5x–200x because IEX
quotes are not the consolidated book, so spread-based gates read a number that
is wrong by a varying amount.

**Fix:** repair the field, then make its absence a refusal. Every guard that
consumes a freshness value must fail **closed** — missing age means stale means
no trade.

**Done when:** each staleness guard refuses under a missing field, with a test.

---

## 3. Operational resilience

With paper money an outage is a gap in the data. With real money an outage is an
unmanaged position. All of these have been observed before:

- **Single point of failure.** One Mac mini over Tailscale (`ssh mac-mini-away`;
  the `.local` alias is dead). No failover. Dead mini + software-only stops = an
  unmanaged position.
- **SSH orphans block the LaunchAgent.** A process started over ssh holds the
  port, so the agent dies at exit 3. `ps eww` `SSH_` vars are the proof; killing
  the orphan restores Keychain access.
- **Restarting over SSH kills Claude auth** (`deploy_mini.sh` does it too). Fix
  is a Terminal restart on the mini — never `claude /login`, never an API key.
  The runbook has to account for this, because the standard remote-restart path
  degrades the desk.
- **The mini cannot `git push`.** Commit on the mini, fetch its branch from the
  MacBook, push from there. Awkward under time pressure.
- **"Ship it" is four steps**: commit, push, pull on the mini, *and* bounce the
  stack. A pushed commit is not deployed until processes restart, and several
  knobs bind only at startup — the "config-only, no restart" note in
  `deploy_mini.sh` is wrong for those.

**Actions**

1. **A flatten-everything switch that does not depend on the desk process.**
   Standalone script, runnable from a phone, cancels all open orders and
   liquidates all positions using the live keys directly. Test it monthly.
2. **Heartbeat alerting.** If `ai_trader.py` stops writing state for N seconds
   during RTH with a position open, page yourself. Right now silence looks the
   same as a quiet market.
3. **UPS on the mini**; disable automatic OS updates during market hours.
4. **Written runbook** for: desk crashed with positions open; broker API down;
   Finnhub stream dead; mini unreachable; tunnel down. Each with the exact
   command.
5. **Fix the deploy story** so a config change cannot be half-applied — mark
   startup-binding knobs explicitly and have the deploy script read that marker.

**Done when:** you can flatten the book from your phone in under 60 seconds
without the desk process cooperating, and you get paged when it dies.

---

## 4. Money, tax, and broker mechanics

Paper trading hides all of this, and none of it depends on the P&L track.

- **PDT.** `TRADING_ENGINE.md` states FINRA's pattern-day-trader rule and the
  $25k minimum were eliminated 2026-06-04 (Reg Notice 26-10), that Alpaca
  implemented it that day, and that `ai_pdt_protect=off` is therefore fine under
  $25k. **Verify directly with Alpaca against your actual live account before
  relying on it** — the software gate was turned off on this reading, and a
  sub-$25k account at 13–67 day trades per session is exactly the configuration
  that rule governed. Get it in writing.
- **Cash vs margin.** Margin brings day-trading buying power rules and
  maintenance requirements; cash brings T+1 settlement and good-faith violations
  at this frequency. Decide deliberately and make sure the sizing code knows
  which.
- **Wash sales.** At 13–67 trades/session in a small repeating universe,
  adjustments will be extensive, and `ai_watch_max_entries_per_symbol_day = 0`
  (unlimited) makes it worse. You need per-lot records — which §2.3's ledger
  provides and the submission log does not.
- **Taxes.** Short-term gains are ordinary income; losses cap at $3,000/year
  against ordinary income with the rest carried forward. Talk to an accountant
  before the first live trade.
- **Account separation.** Keep the live desk in its own account, funded
  separately from anything else.

---

## 5. Measurement plumbing

Not a go-live blocker, but it is the prerequisite for the P&L track, and the
work is cheapest to do while nothing is at stake.

**The book cannot see its refusals.** `log_shadow_sample` fires once per
*watched* symbol per poll, so a name refused at seed or inclusion writes no
shadow row, no ledger row, no forward return. `_SEED_DROP_SAMPLE_CAP = 8` in
`ai_entry_watch.py` keeps 8 sample symbols per source per snapshot; everything
else is a counter. On 2026-09-15 RTH that was **300,361 seed drops** and
**56,449 inclusion refuses** going unscored. Every admission gate is
unfalsifiable by construction — you cannot tell whether a filter helps without
the population it removed.

*Fix:* log the refused population — symbol, reason, features, timestamp —
uncapped and append-only. Then grade kept-vs-dropped forward MFE/MAE with the
`tools/gate_screen.py` + `tools/desk_null.py` controls that already exist.

**Source attribution is merge-contaminated.** `position_shadow.source` is the
output of `_merge_source` (`ai_entry_watch.py:4855`), which resolves
**ownership**, not **origin**. So per-source numbers describe rows a source
ended up owning, not rows it found.

*Fix:* log `proposer` pre-merge, separately from `owner`, and score
**proposals** rather than fills — hundreds of observations per day instead of
~12. Design exists at `scratchpad/SOURCE_SCORECARD_DESIGN.md`.

**Config churn outruns evidence.** ~3.5 live-config changes per session against
~12 fills per session, with per-trade sd ≈ 0.21R. Any A/B needs ~138 fills per
arm to see a 0.05R effect, and every `config_fp` stamp resets the pool. Worth
knowing now because it applies to the *machinery* stages below too: a config
change mid-stage invalidates the stage.

---

## 6. The ramp

Each stage proves something about the plumbing. Criteria are checked
**automatically**, not by judgment in the moment. Config is frozen within a
stage — a change resets it.

Because P&L is not the objective yet, **fund each stage at the minimum that
produces real fills.** The money is there to surface paper-vs-live differences,
so more of it buys no more information.

**Stage 0 — Shadow live (2 weeks).** Live keys, live account, `TRADER_MODE`
still paper. Prove the live path initializes against the right account, the mode
assertion fires, broker stops attach, and nothing routes an order. Zero dollars
at risk.
*Pass:* mode assertion demonstrated firing on a deliberately wrong account; zero
orders routed.

**Stage 1 — Minimum size, one position (4 weeks).** `ai_max_positions = 1`.
Purpose is to measure the paper→live slippage delta. Paper baseline is
−0.0996R entry / −0.0426R exit; live on $1–20 microcaps will be worse, and you
need the real number.
*Pass:* every fill reconciles against the broker; every position carried a
broker-side stop; measured slippage recorded.
*Abort:* any position ever found without a broker-side stop, or any
ledger/broker disagreement.

**Stage 2 — Quarter size (4 weeks).** Concurrency comes back on. This is where
per-instance risk caps, `liquidate_all`, and the daily brake get exercised
against real fills.
*Pass:* `ai_daily_loss_limit_r` observed halting entries correctly; EOD
liquidate clean; open risk never exceeded `ai_max_open_risk_pct`.
*Abort:* any risk cap breached, or the flatten switch fails its monthly test.

**Stage 3 — Half size (4 weeks).** Resilience under load.
*Pass:* at least one unplanned interruption handled per the runbook with no
unmanaged position; heartbeat alerting fired and was actionable.

**Stage 4 — Target size.** Only after three consecutive stages pass.

**Standing rules**

- **The daily brake stays on.** `ai_daily_loss_limit_r = 3.0` is currently
  tripping on ordinary sessions — that is information about the strategy, not a
  limit to raise.
- **Pre-commit the drawdown that ends the experiment** — a dollar figure written
  down before Stage 1 that flattens the book and returns to paper. Do not
  renegotiate it in the moment.
- **Never scale size into a stage that failed its criteria.**
- Note that `ai_phase_b_dry_run` is currently `false`, which took fills from ~9
  to 40–67 per session. Decide deliberately whether Phase B is inside or outside
  the ramp before Stage 1, and don't change it mid-stage.

---

## 7. The P&L track — what is already ruled out

Recorded so the other track doesn't re-run these. Not gates in this document.

| Search | Result |
|---|---|
| **Exits** — 42 stop/target/trail/ladder shapes, 2,486 de-correlated moments | All land between −0.35% and +0.02% against a 0.20% round trip. Exits control win rate (29%→55%) and median completely, and the mean not at all. |
| **Entries** — EXH cross, MACD cross, MACD rising, 21-bar breakout, 370 symbol-days | 0.09-point spread across every setup, all negative, with the do-nothing control mid-pack and beating EXH — the shipped config's trigger. |
| **Horizon** — unconditioned holds on the momentum list | Negative at 30m (−0.32%) through 5 days (−5.21%), monotonically worse. Megacaps off the screen print exactly the charged cost, which validates the harness. |
| **Liquidity** — same harness split by $/minute | Thinnest quartile −0.29% @30m, most liquid −0.32%. The fade is a property of the **selection**, not the names. |

The one thing that has graded positive against realized outcome is
`admit_range_pos` — where a name sits in its own day range at admission —
monotone across five buckets, moving the book from −1.81% to −1.15% at a cap of
90. It **cuts losses rather than creating profit**: median barely moves and win
rate stays ~40%. It is in the config today at 90.

Mechanically: exits cannot help because mean = drift − costs, and no stopping
rule creates drift in a driftless path. Entries have not helped because median
admission is **+8.2%** on the day — the move is over — and the desk then ranks
by `abs(pct)` and takes the top 12, sorting *toward* the worst bucket. The
remaining levers are a different universe/selection method, earlier information
(blocked on SIP entitlement — a subscription decision, not an engineering one),
or lower execution cost.

---

## 8. Repo hygiene

Test state: **3,247 passed, 3 failed** (`.venv/bin/python -m pytest -q`, 175s).

| Failing test | Why it matters |
|---|---|
| `test_entry_path_is_stamped_by_every_entry_path` | The assertion `'"entry_path": decision.get("entry_path") or "unknown"'` is gone from `ai_positions.py`. It guards the stamp saying which path opened a trade — provenance, directly relevant to §5. |
| `test_every_js_module_pin_is_current` | `static/js/feeds.js` changed after its `?v=176` pin in `app.js`. Browsers serve stale JS; a dashboard showing old state during a live session is a hazard. |
| `test_old_stream_age_never_paints_ready` | Fails in the full run, **passes in isolation** — shared state between tests. It guards the §2.5 staleness rail, so the flakiness isn't cosmetic. |

Also:

- `rs_cache.sqlite` is **191 MB** in the working tree. Confirm it is ignored and
  absent from history.
- `claude_positions_state.json` and `active_symbol.json` are stale on the
  MacBook and live on the mini. The mini is the source of truth; anything
  reading local copies is reading fiction.

---

## 9. Summary

**Must close before a live dollar:**

1. Unauthenticated remote write to credentials and risk limits (§2.1) — live
   today regardless of go-live timing
2. No broker-side stops; 73% of exits depend on one process staying alive (§2.2)
3. No fill ledger — submission log disagrees with the broker (§2.3)
4. No live code path; replacing the rail is a design task (§2.4)
5. Staleness guards fail open (§2.5)

**Then:** ops resilience (§3), broker/tax mechanics (§4), and the ramp (§6).

**Rough sequencing.** §2.1 and §2.3 are worth starting now — the security hole
is open with real credentials behind it, and the ledger is what every later
measurement sits on. §2.2, §2.4 and §2.5 are perhaps 1–2 weeks together. §3 and
§4 run in parallel and are partly non-engineering (UPS, accountant, broker
confirmation on PDT). Stage 0 can begin once §2 is closed.

§5 is off the critical path for go-live but on the critical path for the P&L
track, and it is cheapest to build while nothing is at stake.
