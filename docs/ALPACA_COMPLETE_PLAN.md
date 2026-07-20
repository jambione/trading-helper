# Webull → Alpaca: Complete Migration Plan

**Status:** planning complete (2026-07-20). Ready for implementation when the user says go.  
**Audience:** implementer (human or agent) with no prior session memory.  
**Related:** `docs/ALPACA_MIGRATION_ROADMAP.md` (Claude’s initial map). This document **supersedes** that roadmap where they differ; keep the older file as historical context only.

**Scope decision (locked):** full retirement of Webull — OpenAPI bridge provider, Desktop OCR L2/tape/watchlist, and GUI automation that types into Webull Desktop. Do not keep the OCR pipeline “just in case” without an explicit user reverse.

---

## 0. Executive summary

| Today | Target |
|---|---|
| Orders via Alpaca (`alpaca_trader.py`) already work | Same, plus Mobile Trader broker via Alpaca |
| Market depth / L2 monitor via Webull Desktop OCR (+ optional Webull SDK) | **Alpaca Flow Monitor**: quotes + trades websocket, depth-1 NBBO only |
| Confidence banner: trend + tape + VWAP | **Keep** — fed by real Alpaca prints/quotes (better tape, no OCR) |
| Multi-level walls / BookFlow | **Retire** — cannot be honest without L2 |
| Movers via Webull watchlist OCR | Alpaca screener API (`movers` / `most-actives`) |
| Finnhub for live trades (engine + old L2 ref) | Optional later; Flow Monitor does not use Finnhub |

**Biggest reliability wins:** kill OCR failure domain; exchange-held brackets/trailing stops later; one developer API for paper/live.

---

## 1. Locked product decisions

These were open during planning; they are now **defaults**. Change only with an explicit decision, not mid-implementation drift.

| # | Decision | Locked choice |
|---|---|---|
| D1 | Multi-level L2 | **Gone.** Depth-1 NBBO only. UI never claims walls from multi-level book. |
| D2 | Confidence pillars | **Trend + tape + session VWAP** only. Imbalance/touch skew is detail, not a pillar. BookFlow retired. |
| D3 | Monitor alerts (v1) | **Banner-transition only** (GO LONG / BEAR / STAND ASIDE). No BUY/SELL from multi-level imbalance. |
| D4 | Size-up rules | **2/2** (when a pillar abstains) = normal confidence headline. **3/3** = size-up. Grade **C** never size-up. Early session (VWAP immature) may show 2/2 only. |
| D5 | Feed | **IEX only** (free Alpaca tier). SIP requires a paid data plan — not used. |
| D6 | Active symbol | Dashboard momentum list + manual focus in the monitor. **No** OCR symbol detect from Webull. Hotkeys load TradingView only (Webull leg removed). |
| D7 | Stream architecture (v1) | **Separate processes**: Flow Monitor has its own `StockDataStream`; bridge provider uses REST/poll or its own stream. Shared stream service is a later optimization. |
| D8 | Credentials | **Only** `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (env / `signal_engine.env`). No second naming scheme. |
| D9 | Package names | Shared math → `flow_core/`. Terminal process → `flow_monitor/`. Do not put new code under `webull-l2/`. |
| D10 | Discord OCR | **Out of scope** — stays as ticker source. Not Webull. |
| D11 | Phase 4 priority | (1) brackets/OCO, (2) screener movers, (3) trailing stops, (4) Alpaca stream for engine / drop Finnhub, (5) news, (6) crypto/options later. |
| D12 | Webull OpenAPI provider | Keep `trade_bridge/providers/webull.py` unreachable for one release after flip, then delete in Phase 3. |

---

## 2. Current state (verified against repo)

### 2.1 Four Webull surfaces (all retire)

| Surface | Location | Role |
|---|---|---|
| Bridge provider | `trade_bridge/providers/webull.py`, config | Orders/positions/account + depth for Mobile Trader |
| OCR desktop monitors | `webull-l2/*`, `momentum-monitor/watchlist_ocr.py` | L2, tape, movers scrape |
| GUI automation | `windows_agent.py`, `mac_agent.py`, `transcription/workflows.py` (`workflow_add_wb`) | Type tickers into Webull Desktop |
| Window bookkeeping | `position_windows.py` | “Webull L2 Monitor” console position |

### 2.2 Already Alpaca

| Module | Role |
|---|---|
| `alpaca_trader.py` | `TRADER_MODE` off/paper/live; notional buy; ext-hours limits; sell/close |
| `alpaca_api.py` | Historical bars, latest trade, IEX/SIP knob, retries |
| `signal_engine.py` | Strategy + client-side exits; uses trader + bars |

### 2.3 Landmine (do not delete blindly)

`trade_bridge/l2.py` imports pure signal logic from `webull-l2/l2_core.py` via `sys.path` hack.  
`l2_core.py` has **no OCR dependency**. Mobile Trader (`SymbolEngine` in `trade_bridge/engine.py`) depends on it.

**Order:** relocate math → update import → then delete OCR files.

### 2.4 Provider factory today

`trade_bridge/providers/__init__.py`: only `"webull"` vs mock. No `"alpaca"` branch yet.

---

## 3. Target architecture

```
                    ┌──────────────────────────────────┐
                    │  Alpaca (paper or live)          │
                    │  TradingClient + StockDataStream │
                    │  (+ screener REST, later news)   │
                    └────────────┬─────────────────────┘
           ┌─────────────────────┼─────────────────────┐
           ▼                     ▼                     ▼
  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────┐
  │ alpaca_trader  │  │ flow_monitor     │  │ trade_bridge      │
  │ signal_engine   │  │ (terminal UI)    │  │ providers/alpaca   │
  │ (strategy)      │  │ quotes+trades    │  │ depth-1 books      │
  └─────────────────┘  └────────┬─────────┘  │ + broker orders    │
                                │            └─────────┬──────────┘
                                ▼                      ▼
                         flow_core/              Mobile Trader
                         (shared pure math)      (stance / book UI)
```

**Not in v1:** crypto engine, options, shared single stream microservice, Finnhub removal from `signal_engine`.

---

## 4. Alpaca Flow Monitor (replaces Webull L2)

### 4.1 What it is

A headless Rich terminal that:

1. Streams Alpaca **quotes** + **trades** for watched symbols.
2. Builds **depth-1** books from NBBO.
3. Runs **trend / tape / session VWAP** confidence with hysteresis.
4. Shows touch skew (bid_size/ask_size), spread, last prints, optional movers.
5. Logs CSV for offline scoring.
6. Alerts on **banner stance changes** only (v1).

### 4.2 What it is not

- Not multi-level L2.
- Not wall PULLED/CONSUMED.
- Not Webull-window-dependent.
- Not a replacement for `signal_engine` strategy entries.

### 4.3 Pillar specs (implementation contract)

**Trend**

- Series `(ts, mid)` from quote mids; seed on hop from buffered trades/bars for that symbol.
- Window 300s; coverage ≥ 0.6; robust median-band endpoints (port from `SignalEngine.trend_pct`).
- Drop OCR glitch/ref-price gates.

**Tape**

- Alpaca trade prints; side: quote rule then tick rule (no color).
- 60s window; `tape_gate_ok` (min_sided=4, sided_share=0.5); `tape_dom_min=0.25`.
- Dominance = (buy_vol − sell_vol) / (buy_vol + sell_vol) when gate passes.

**Session VWAP**

- Port `SessionVWAP`; maturity `vwap_min_age=900`; age = coverage from first print seen, not session anchor slept through.
- Pre-subscribe watchlist so VWAP warms before focus hop.

**Banner**

- Majority vote; hysteresis `long_confirm_secs=20`.
- Size-up only on 3/3 and grade A/B (not C).
- Hop: if trend mostly seeded, require tape lead before size-up presentation (`tape_confirms`).

**Quality grade**

- A/B/C from stream health (quote age, trade age, disconnect, feed type) — not OCR miss rates.

**Touch skew (detail row only)**

- `bid_size / max(ask_size, ε)` at NBBO. Label UI “touch skew”, not “L2 imbalance”.

### 4.4 Terminal layout (target)

```
┌─ Alpaca Flow · {IEX|SIP} · {session} · stream OK ──────────────────────┐
│  {SYM}  GO LONG · confidence ●●● (3/3) · {held} · data A                 │
│         trend ▲  tape ▲  vwap ▲                                          │
│  mid …  bid×sz  ask×sz  spr …  touch skew …                              │
│  tape 60s: dom …  sided n/m  pace …                                      │
│  last prints: …                                                          │
│  playbook: LEAN LONG — … (no wall claims)                                │
│  movers: …   watched: …                                                  │
│  keys: focus / ticker · q quit                                           │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.5 Module layout

```
flow_core/                 # pure math, no network
  book.py                  # L2Book depth-1 (from l2_core)
  vwap.py                  # SessionVWAP
  tape.py                  # side, dominance, tape_gate_ok
  trend.py                 # history, seed, trend_pct
  confidence.py            # confidence(), LongView
  quality.py               # stream grade
  playbook.py              # wall-free playbook text

flow_monitor/
  stream.py                # StockDataStream multi-symbol
  screener.py              # movers / most-actives
  render.py                # Rich UI
  main.py                  # entrypoint
  config.json
```

### 4.6 Config sketch

```json
{
  "feed": "IEX",
  "symbols_max": 30,
  "trend_window": 300,
  "trend_min_coverage": 0.6,
  "tape_dom_min": 0.25,
  "tape_min_sided": 4,
  "tape_sided_share": 0.5,
  "vwap_min_age": 900,
  "long_confirm_secs": 20,
  "max_spread_pct": 1.0,
  "alert_on": "banner",
  "alert_cooldown": 30,
  "screener": true,
  "screener_sec": 45,
  "csv_log": true,
  "sound": false,
  "toast": false
}
```

---

## 5. Mobile Trader / bridge impact

| Field / behavior | Change |
|---|---|
| `book.bids` / `book.asks` | Length 1 each (NBBO) |
| `book.imbalance` | Touch skew; consider renaming in API later to `touch_skew` (v1 may keep key for less client churn, document meaning change) |
| `walls` | Always `[]` or omit; UI must not invent walls |
| Stance / confidence | Prefer same pillars once tape is available to bridge; v1 bridge may ship quote-only trend/bias first, then add tape metrics |
| Playbook | Rewrite reasons without ask_break/bid_crack from multi-level walls |

**Provider work:** `trade_bridge/providers/alpaca.py`

- `AlpacaBroker` — wrap `TradingClient` (mirror patterns in `alpaca_trader.py`).
- `AlpacaMarketData` — latest quote → depth-1 `L2Book`; poll or stream.
- Branch in `providers/__init__.py` for `"alpaca"`.
- Smoke: `scripts/alpaca_smoke.py`; tests: `tests/test_alpaca_bridge.py`.

---

## 6. Implementation phases (execution order)

Do not reorder deletions ahead of landmine relocation. Do not implement Phase 4 before Phase 1 paper is trusted unless explicitly requested.

### Phase 1 — Broker/data provider parity

**Goal:** Mobile Trader works against Alpaca paper with zero changes to engine/routes beyond provider.

1. Read `providers/base.py` + `providers/webull.py`.
2. Implement `providers/alpaca.py` (broker + market data depth-1).
3. Wire `"alpaca"` in `providers/__init__.py`.
4. Credentials from existing Alpaca env vars.
5. `scripts/alpaca_smoke.py` (read-only account/positions/quote).
6. `tests/test_alpaca_bridge.py`.
7. Paper E2E: flip config provider to alpaca; exercise account/orders/positions.

**Exit criteria:** smoke green; place/cancel paper order; positions/account views correct; book shows valid bid/ask.

### Phase 2 — Landmine relocation

1. Create `flow_core/` with pure math extracted from `webull-l2/l2_core.py` (split modules optional; single module OK for first cut).
2. Update `trade_bridge/l2.py` imports; remove `sys.path` hack.
3. Temporary re-export from old path if needed so OCR still runs until Phase 5.
4. Tests that import bridge still pass.

**Exit criteria:** bridge + existing tests green; no runtime dependency on OCR packages for bridge.

### Phase 2b — Pillar redesign (product already locked in §1)

1. Confidence = trend + tape + VWAP only.
2. WallTracker/BookFlow: stop driving stance; empty walls in API.
3. Playbook text wall-free.
4. Document depth-1 limitation in UI strings if any.

**Exit criteria:** no UI/API path claims multi-level walls; confidence still produces LONG/BEAR/NEUTRAL.

### Phase 3 — Flow Monitor v1 (Flow-A → Flow-B)

**Flow-A (prototype)**

1. `StockDataStream` one symbol; print mid + last trade.
2. Pillars + minimal Rich banner.
3. CSV optional.

**Flow-B (usable replacement)**

1. Multi-symbol subscribe (cap `symbols_max`).
2. Session VWAP warm across watchlist.
3. Full Rich layout §4.4.
4. Screener movers strip.
5. Banner-transition alerts; optional sound/toast.
6. Focus symbol from CLI / keys / shared active-ticker if already available.
7. Optional: show Alpaca position via existing position feed pattern.

**Exit criteria:** run through RTH without Webull Desktop open; banner updates; tape not permanently dark on liquid names (SIP preferred); CSV written.

### Phase 4 — Retire OCR + Webull GUI automation

Only after Phase 1 paper OK and Flow-B usable (or user accepts temporary gap with bridge-only).

1. Delete OCR-only: `l2_signal.py`, `tape_core.py`, `calibrate.py`, `score_confidence.py` (or move scorer under `flow_monitor` later), region caches, webull-l2 config/README as needed.
2. Remove `WatchlistReader` OCR; keep pure helpers if any (`top_movers` sort) relocated.
3. Remove `workflow_add_wb` and agent call sites; keep TradingView workflows.
4. Momentum hotkeys: drop Webull leg.
5. `position_windows.py`: drop Webull L2 Monitor entry.
6. Launch scripts: drop `l2_signal` pane; launch `flow_monitor` instead.
7. Grep: no remaining imports of deleted OCR modules.

**Exit criteria:** `rg 'webull-l2|watchlist_ocr|l2_signal|workflow_add_wb'` clean (except docs/history); momentum_signal runs; flow_monitor is the human monitor.

### Phase 5 — Config/dependency cleanup

Only after Phase 1 trusted in real use.

1. Remove Webull credential defaults from bridge config.
2. Remove `webull-openapi-python-sdk` when unused.
3. Drop `cv2`/`mss`/`pytesseract` if no remaining consumers (check `tv-monitor/` first).
4. Delete or archive `providers/webull.py` after one release unreachable.
5. `.gitignore` cleanup for webull SDK logs.

### Phase 6 — Alpaca-only feature backlog (priority locked D11)

| Pri | Feature | Notes |
|---|---|---|
| P0 done by Phases 1–4 | Provider + Flow Monitor + OCR gone | Migration core |
| P1 | **Bracket / OCO orders** | Attach TP/SL at entry; survives engine death. Map from existing STOP_LOSS/TAKE_PROFIT. Verify `order_class` in current alpaca-py. |
| P1 | **Screener movers** | Already in Flow-B; also feed momentum if useful |
| P2 | **Trailing stop** (broker) | Alert-mode trail recipe; single-order trail first |
| P2 | **Alpaca trade stream for signal_engine** | Drop Finnhub free-tier cap; separate project if large |
| P3 | **Order update stream** | Fill reconciliation without poll |
| P3 | **News stream** | Optional 4th pillar or dashboard feed |
| P4 | Crypto 24/7 / options multi-leg | New strategy surfaces; not migration-blocking |

---

## 7. What you gain (Alpaca advantages used by this plan)

| Capability | Used in |
|---|---|
| Paper ≡ live API | Phase 1, existing trader |
| Notional fractional orders | Already live |
| Quote + trade websocket | Flow Monitor, bridge market data |
| Screener movers / most-actives | Flow-B, OCR movers replacement |
| Bracket / OCO / trailing stop | Phase 6 P1–P2 |
| SIP feed | Production tape quality |
| No Desktop/OCR ops | Phase 4 |
| (Later) news, crypto, options | Phase 6 P3–P4 |

**Honest loss:** multi-level depth and wall/BookFlow research. Do not fake them.

---

## 8. Explicit non-goals (this migration)

- Rebuilding multi-level L2 from third-party depth vendors (unless user later expands scope).
- Rewriting `signal_engine` strategy logic.
- Removing Discord OCR ticker source.
- Removing TradingView automation.
- Live money cutover without paper soak (user must opt into live).
- Implementing crypto/options as part of “migration done.”

---

## 9. Verification checklist (global)

### Phase 1

- [ ] `scripts/alpaca_smoke.py` against paper
- [ ] Provider `alpaca` account/positions/orders
- [ ] Depth-1 book mid/spread sensible for a liquid name
- [ ] Paper order place + cancel

### Phase 2 / 2b

- [ ] Bridge imports `flow_core` (or relocated module)
- [ ] Existing bridge/unit tests pass
- [ ] No wall-driven stance; walls empty

### Phase 3

- [ ] Flow monitor RTH session without Webull app
- [ ] Pillars vote/abstain sanely; VWAP matures after 15m+
- [ ] Symbol hop resets focus state without crash
- [ ] Disconnect → grade C
- [ ] CSV rows written

### Phase 4

- [ ] Grep clean of OCR entrypoints
- [ ] Launch scripts start flow monitor
- [ ] Agents do not open Webull for add-ticker

### Phase 5

- [ ] No Webull SDK required to run default stack
- [ ] Requirements trimmed safely

### Phase 6 (as implemented)

- [ ] Bracket order on paper attaches both legs
- [ ] Engine kill still leaves protection (if brackets used)

---

## 10. Risk register

| Risk | Mitigation |
|---|---|
| Delete `webull-l2` too early | Phase 2 landmine first |
| Fake L2 from one quote | Locked D1; empty walls |
| IEX tape too thin | SIP for production; show tape dark |
| 30-symbol WS limit (basic plan) | `symbols_max` / max engines |
| Engine exits only in process memory | Phase 6 brackets |
| User expects old wall banner | UI copy: “NBBO + tape — not Level 2” |
| tv-monitor still needs OCR deps | Check before dropping packages |
| Secrets in config.json (finnhub in webull-l2 config) | Prefer env; don’t copy secrets into flow config |

---

## 11. Suggested first implementation session (when user says go)

1. Phase 1 only: `providers/alpaca.py` + factory branch + smoke + tests.  
2. Paper flip + Mobile Trader smoke.  
3. Stop and confirm before Phase 2–4 bulk deletion.  
4. Then landmine + Flow-A prototype in a second session.  
5. Flow-B + OCR delete when Flow-A proves tape/pillars.

Do **not** start with deleting `webull-l2/`.

---

## 12. Document control

| Item | Value |
|---|---|
| Planning completed | 2026-07-20 |
| Supersedes | Conflicts with `ALPACA_MIGRATION_ROADMAP.md` resolve **in favor of this file** |
| Implementation status | **On `feature/alpaca-migration`:** Webull removed; Phase 2b empty walls; Flow-B monitor (tape+VWAP+screener+banner alerts); **bracket exits** in `alpaca_trader` via `BRACKET_EXITS=auto`. |
| Next user action | Paper-soak Mobile Trader + flow monitor; try paper buy with brackets (`STOP_LOSS`/`TAKE_PROFIT` set, RTH); optional trailing stops next |

---

## 13. One-paragraph brief

Retire every Webull surface. Keep Alpaca for execution (already live). Add an Alpaca bridge provider so Mobile Trader is paper-capable without Webull. Replace the Desktop L2 OCR monitor with an **Alpaca Flow Monitor** that streams quotes and trades, runs the existing trend/tape/VWAP confidence model on honest depth-1 data, and uses Alpaca’s screener for movers. Delete walls, BookFlow, and OCR. Later, add broker-side brackets and trails so risk survives process death, and optionally collapse Finnhub into Alpaca’s stream. Paper first; delete OCR only after the flow path works.
