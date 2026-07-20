# Webull → Alpaca Migration Roadmap

**Audience:** this doc is written for an AI coding agent picking up the work cold. It assumes no memory of prior sessions — every claim below was verified against the repo as it exists on `master-mac` at the time of writing (2026-07-20). Re-verify anything that looks stale before acting on it.

**Scope decision (already made by the user, do not re-litigate):** full retirement of Webull. This means killing the screen-scraped L2/tape/watchlist monitors and the Webull Desktop GUI automation, not just swapping the broker API underneath them. That trades away true multi-level order-book depth — Alpaca's standard market-data plans expose top-of-book (NBBO) quotes and trade prints, not a multi-level book the way Webull's Advanced Quotes subscription (or the OCR scrape of the Desktop app) did. If that tradeoff turns out to be a problem in practice, the fallback is to stop after Phase 1 (provider swap only, keep the OCR monitors alive) — **check with the user before reversing course**, don't silently keep the OCR pipeline running "just in case."

---

## 1. Current state — how Webull touches this repo

Four independent surfaces, not one:

| Surface | Files | What it does |
|---|---|---|
| **Bridge provider** | `webull_bridge/providers/webull.py`, `webull_bridge/config.py` | Real Webull OpenAPI SDK (`webull-openapi-python-sdk`) for orders/positions/account/depth, behind a clean `MarketDataProvider`/`BrokerProvider` ABC. Powers the Mobile Trader iPhone app via `webull_bridge/routes.py` (mounted in `dashboard.py`). |
| **OCR desktop monitors** | `webull-l2/` (`l2_signal.py`, `l2_core.py`, `tape_core.py`, `calibrate.py`, `score_confidence.py`), `momentum-monitor/watchlist_ocr.py` | ~4,000+ lines of `mss` + `cv2` + `pytesseract` screen-scraping of the Webull Desktop app: L2 order book, Time & Sales tape, watchlist movers sidebar. |
| **GUI automation** | `windows_agent.py`, `mac_agent.py`, `transcription/workflows.py` (`workflow_add_wb`) | Drives pyautogui to type tickers into the Webull Desktop app window. Triggered by momentum-monitor's 1-9 hotkeys (`LoadHotkey` in `momentum_signal.py`), which load a symbol into **both** Webull Desktop and TradingView. |
| **Window position bookkeeping** | `position_windows.py` | Saves/restores the "Webull L2 Monitor" console window's screen position. Cosmetic only. |

**Alpaca is already integrated** — this is not a greenfield add:
- `alpaca_trader.py` — order execution for `signal_engine.py`, gated by `TRADER_MODE` (`off`/`paper`/`live`) env var in `signal_engine.env`. Has working `init()`, `buy()`, `sell()`, `get_order()`, `get_open_positions()` against `alpaca.trading.client.TradingClient`. Already does notional (fractional-share) orders and extended-hours limit-order handling.
- `alpaca_api.py` — historical bars + latest-trade price via `alpaca.data.historical.StockHistoricalDataClient`, with IEX/SIP feed selection and retry-with-backoff.
- Both already read `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from `signal_engine.env`.

So Phase 1 below is mostly **wiring existing, proven logic into a new interface**, not writing an Alpaca integration from scratch.

---

## 2. The landmine — read this before touching `webull-l2/`

`webull_bridge/l2.py` does this:

```python
_L2_DIR = str(Path(__file__).resolve().parent.parent / "webull-l2")
sys.path.insert(0, _L2_DIR)
from l2_core import (L2Book, LongView, Signal, SignalEngine, WallTracker,
                      market_bias, playbook, project_price)
```

`webull-l2/l2_core.py` is pure-stdlib signal logic (book state, wall detection, trend/bias scoring) — **it has no OCR dependency of its own**. It's the shared brain that `webull_bridge/engine.py`'s `SymbolEngine` runs on for the Mobile Trader app. The only reason it lives inside `webull-l2/` is that the desktop monitor (`l2_signal.py`) also imports it.

**Deleting the whole `webull-l2/` folder in one pass will silently break the Mobile Trader app.** The correct order is:

1. Move `l2_core.py` out of `webull-l2/` to a shared location (e.g. a new top-level `signal_core.py`, or `webull_bridge/l2_core.py` if you'd rather keep it bridge-local — either works, just pick one and update the one import site).
2. Update `webull_bridge/l2.py`'s import path (and drop the `sys.path` hack).
3. *Then* delete the OCR-only files: `l2_signal.py`, `tape_core.py`, `calibrate.py`, `score_confidence.py`, `region_cache.json`, `webull-l2/config.json`.
4. Also relocate/keep `momentum-monitor/watchlist_ocr.py`'s `top_movers(rows, n, rank)` function if you want to keep it — it's a generic sort-by-percent helper with no OCR dependency, only `WatchlistReader` (the actual scraper) is OCR-bound.

---

## 3. Phase 1 — Broker/data provider parity in `webull_bridge/`

Goal: the Mobile Trader app works against Alpaca instead of Webull, with zero changes to `webull_bridge/engine.py` or `routes.py` (that's the point of the existing abstraction).

1. Read `webull_bridge/providers/base.py` (the ABCs: `MarketDataProvider.subscribe_depth`/`snapshot`, `BrokerProvider.place_order`/`cancel_order`/`orders`/`positions`/`account`) and `webull_bridge/providers/webull.py` (the reference implementation) before writing anything — mirror its structure exactly.
2. Create `webull_bridge/providers/alpaca.py`:
   - `AlpacaBroker(BrokerProvider)` — wraps `alpaca.trading.client.TradingClient` (synchronous SDK, same pattern as `WebullBroker`: every call goes through `loop.run_in_executor`). Reuse the order-lifecycle logic already proven in `alpaca_trader.py` (`submit_order` with `MarketOrderRequest`/`LimitOrderRequest`, `close_position`, `get_all_positions`, `get_account`, `get_order_by_id`) rather than re-deriving it — that file has already handled the extended-hours limit-order edge case and fractional/notional sizing.
   - `AlpacaMarketData(MarketDataProvider)` — **do not try to build multi-level depth.** Synthesize a depth=1 `L2Book` (single bid level, single ask level) from the latest quote (`StockLatestQuoteRequest` — verify exact class name against the installed `alpaca-py` version, don't assume). This mirrors the fallback path `WebullMarketData` already takes when the account lacks the Advanced Quotes subscription (see `webull.py`'s `_fetch()` — it catches "depth not more than 1" and drops to `self.depth = 1`), so `L2Book` consumers downstream don't need to change shape.
3. Add `"alpaca"` as a third branch in `webull_bridge/providers/__init__.py`'s `_make_market_data()` / `_make_broker()` (currently a two-way `if name == "webull" ... else: mock` branch).
4. Decide credential source: reuse the existing `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` env vars (recommended — `alpaca_trader.py` already established that convention, no reason to introduce a second naming scheme) rather than adding `alpaca_app_key`-style fields to `config/webull_bridge.json`.
5. Add `scripts/alpaca_smoke.py` mirroring `scripts/webull_smoke.py` — a read-only credential/connectivity check to run before flipping `config/webull_bridge.json`'s `"provider"` to `"alpaca"` in anything real.
6. Add `tests/test_alpaca_bridge.py` mirroring `tests/test_webull_bridge.py`.
7. `scripts/position_feed.py` and `webull_bridge/engine.py` are already provider-agnostic — confirm they work unmodified once the provider flips.

**Verification for Phase 1:** run `scripts/alpaca_smoke.py` against paper credentials, then set `config/webull_bridge.json`'s `provider` (or just `broker_provider`/`market_data_provider`) to `"alpaca"` and exercise the Mobile Trader app's order/position/account views end to end against a paper account before touching live.

---

## 4. Phase 2 — Retire the OCR pipeline and GUI automation

After the landmine relocation in §2:

1. Delete `webull-l2/l2_signal.py`, `tape_core.py`, `calibrate.py`, `score_confidence.py`, `region_cache.json`, `webull-l2/config.json`, `webull-l2/README.md` (or fold anything still-relevant from it into this doc).
2. Delete `momentum-monitor/watchlist_ocr.py`'s `WatchlistReader` class and the OCR capture/locate plumbing (`locate_watchlist_region`, etc.) — keep `top_movers()` only if you relocate it per §2.4. This removes the "movers strip" feature from momentum-monitor with no direct replacement (see Phase 4 for why that's an acceptable trade, or a place to invest instead).
3. Remove `workflow_add_wb` from `transcription/workflows.py`, and its call sites in `windows_agent.py` / `mac_agent.py`. Confirm `workflow_add_tv` (TradingView automation) is untouched — that one has nothing to do with Webull.
4. Remove momentum-monitor's `LoadHotkey` Webull leg — decide whether hotkeys should still auto-load TradingView only, or whether the whole load-on-hotkey feature gets simplified/dropped now that there's no Webull Desktop window to sync.
5. Remove the "Webull L2 Monitor" entry from `position_windows.py`.
6. Update `launch_monitors.bat` — drop the `l2_signal.py` pane. Decide whether `momentum_signal.py` now launches solo (single window, no split-pane) or the launcher script gets simplified further.
7. Confirm `momentum-monitor/momentum_signal.py`'s core loop (`Feed`, `Alerter`, polling `/api/state`) is untouched by all of the above — it's dashboard-driven, not Webull-driven, and per the code (`fetch_state()` → `Feed.ingest()`) has no dependency on the OCR modules except for the now-removed movers strip.

**Verification for Phase 2:** `momentum_signal.py` should run standalone with no `webull-l2` imports left anywhere except the relocated `l2_core.py`. Grep the repo for `webull-l2` and `watchlist_ocr` after this phase — anything left pointing at deleted files is a bug.

---

## 5. Phase 2b — Redesign signal pillars that assumed multi-level depth

`WallTracker` and the imbalance pillar in the relocated `l2_core.py` compare relative sizes across multiple book levels. At depth=1 (all Alpaca gives you), "wall detection" against a single price level is either meaningless or needs a different definition (e.g. NBBO size skew instead of a multi-level wall). This is a real product decision, not a mechanical port:

- Option A: drop wall detection from the Mobile Trader stance entirely, keep only trend/bias/VWAP-style pillars that work fine on a stream of top-of-book quotes and trades.
- Option B: redesign "wall" around bid/ask size imbalance at the top of book (cruder signal, but not fabricated from data that doesn't exist).

Either way, flag this to the user before shipping — don't let the UI keep showing a "wall" number that's now computed from data that can't support the original claim.

---

## 6. Phase 3 — Config/credential cleanup

Only after Phase 1 has run in production long enough to trust it:

- Remove `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` / `WEBULL_ACCOUNT_ID` / `WEBULL_REGION` defaults from `webull_bridge/config.py`'s `DEFAULTS`.
- Remove the Webull-only fields from `config/webull_bridge.json` (keep the file — it still holds thresholds, `max_engines`, etc.).
- Remove `webull-openapi-python-sdk` from any requirements file, and `cv2`/`mss`/`pytesseract`/`plyer` from `webull-l2/requirements.txt` (or delete that file if nothing in the repo needs OCR anymore — double check `tv-monitor/` doesn't independently depend on the same packages before assuming it's safe to drop them repo-wide).
- Clean up the now-unused `.gitignore` entries for `webull_trade_sdk.log*` / `webull_sdk.log*` if those logs stop being produced.
- Delete `webull_bridge/providers/webull.py` itself only once you're confident you won't want it as a reference — consider leaving it in place, just unreachable, for one release cycle.

---

## 7. Phase 4 — Alpaca-only features worth adding

This is the "what does Alpaca give us that Webull didn't" ask. Some of these are already realized, some need new work. **Every alpaca-py class/endpoint name below is unverified against current docs — check `alpaca-py`'s actual API before implementing, don't assume the names are current.**

- **Already realized, just point it out:** notional (fractional-share) order sizing is already live in `alpaca_trader.py`'s `buy()` — Webull's retail API doesn't do this as cleanly.
- **Reliability win (not a "feature," but real value):** dropping the OCR pipeline eliminates a whole bug class the recent git history was actively fighting — DPI-awareness crashes, `mss.MSS()` vs `mss.mss()` breakage, "tick-flash" misreads, the 139→6 / 6.81→23.74 OCR misread problem `RefFeed`'s docstring cites as ~30% of the historical L2 log. It also removes the "keep the L2 panel visible and unobstructed" operating constraint entirely — no more babysitting a screen region.
- **Crypto trading** — Alpaca supports commission-free crypto with the same account/API surface, 24/7 (no market-hours gate). Could justify an always-on momentum strategy variant that doesn't sit idle overnight. Verify current supported pairs/fees before committing to this.
- **Real-time news stream** — Alpaca offers a news API/websocket. Could feed a new "news" pillar into the signal engine or the Mobile Trader stance. Verify the exact client/endpoint name in the current SDK.
- **Options trading API** — a new strategy surface if the account is approved for it. Verify current options-trading support/approval requirements before scoping work here.
- **Consolidate onto Alpaca's own data stream, drop Finnhub** (optional, not required by this migration): `finnhub_stream.py` currently supplies real-time trade prints to `realtime_bars.py` and (formerly) to the OCR monitor's `RefFeed`. Since Alpaca is now the sole broker and already has a websocket trade/quote stream, you could retire the Finnhub dependency and its free-tier 50-symbol cap. This is independent of the Webull migration (Finnhub was never a Webull thing) — treat it as a separate cost/complexity reduction, not a requirement.
- **SIP feed option** — `alpaca_api.py` already has a `data_feed` config knob (`IEX` vs `SIP`) via `_get_feed_arg()`. If the account has a SIP subscription, flipping this gets full-tape data instead of IEX-only, improving backtest/signal accuracy repo-wide, not just for this migration.

---

## 8. Suggested execution order for Grok

1. Phase 1 (provider parity) — lowest risk, reuses proven code, immediately testable against paper.
2. Phase 2 landmine relocation (§2) — mechanical, do it before any deletion.
3. Phase 2 deletions — after confirming Phase 1 works and nothing else references the doomed files.
4. Phase 2b (pillar redesign) — needs a product decision; surface it to the user rather than guessing.
5. Phase 3 (cleanup) — only after Phase 1 has proven itself in production.
6. Phase 4 (new features) — opportunistic, pick off whichever items are actually wanted; verify SDK specifics before implementing any of them.
