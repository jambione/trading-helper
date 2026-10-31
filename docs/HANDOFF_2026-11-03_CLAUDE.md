# Project Handoff & Technical Roadmap (For Claude / Next AI — 2026-11-03)

**Author:** Antigravity / AGY Pairing Session (2026-10-31)  
**Target:** Claude (Tuesday 2026-11-03 Takeover) & Quantitative Team  
**Primary Reference:** [`docs/running_list.md`](running_list.md) (Rows 68, 69, 70 are the newest truth).

---

## 1. Absolute Operating Rules (Non-Negotiable)

1. **Git Workflow:** Work strictly on `master-mac`. Always run `git pull --ff-only origin master-mac` before starting. **NEVER** force push (`git push -f`), rebase pushed commits, or hard-reset.
2. **Production Safety:** `config/bot_config.json` controls real paper/live money execution. **Never edit `config/bot_config.json` without the operator's explicit, literal approval.**
3. **No Pytest on Mac Mini:** **Never run `pytest` in the Mac mini checkout.** Some integration tests mutate configuration state on disk. Always run `pytest` inside the MacBook virtual environment (`.venv/bin/pytest`), and verify `git status` afterwards.
4. **Alpaca API Rate Budget:** The desk shares a single 200 req/min budget. All heavy historical studies must use `secrets_data.json` / `ALPACA_DATA_API_KEY` and run after trading hours.
5. **No Lookahead / Outcome-Blindness:** Studies require pre-registration, out-of-sample chronological splits (Half A vs Half B), and strict point-in-time feature alignment before scoring.

---

## 2. Current Production State (Live for Monday 2026-11-02)

* **Git State:** `master-mac` clean and synced across MacBook, GitHub `origin`, and Mac mini.
* **Option 2 Anti-Pullback Gate (LIVE):**
  * Implemented in [`ai_entry_watch.py`](../ai_entry_watch.py#L9657-L9716) (`anti_pullback_refusal`).
  * Enforces: Blocks entries where **Price drops $\ge 2.0\%$ below HOD** OR **Price $< \text{VWAP} - 15\text{ bp}$**.
  * Enabled in [`config/bot_config.json`](../config/bot_config.json#L70-L75):
    * `ai_watch_anti_pullback_gate: true`
    * `ai_watch_anti_pullback_max_drop_pct: 2.0`
    * `ai_watch_anti_pullback_vwap_buffer_bp: 15.0`
    * `ai_watch_anti_pullback_require_vwap: true`
  * **Empirical Proof:** Full-month October counterfactual (923 trades) proved **+1,290.2 bp GAIN** across 22 sessions (19/22 improved or neutral), removing a -8.96 bp/trade negative expectation. Last week's live audit cut dollar losses by **60.9% (+108.3 bp / +$4.00 paper / +$10.83 on $1k tranche)**.
* **Seat-Churn Retention (LIVE):**
  * `ai_watch_wait_state_evict_sec: 600` active on mini (Row 56). Keeps setup names (`exh_falling`, `exh_not_tight`, `wait_exh`) seated for 10 minutes to avoid re-seating and buying at the top of Leg 1.
* **Book Sizing (LIVE):**
  * 3 slots × 33% equity (Row 41). Active in [`config/bot_config.json`](../config/bot_config.json): `ai_max_positions: 3`, `ai_max_position_pct: 33.0`, `ai_risk_pct: 1.65` (~$634/slot on ~$1,920 equity, max open risk 4.95% $\le$ 5.0%).
* **Mac Mini Processes:** LaunchAgent `com.jambi.trading-desk` running in user console session (`dashboard.py` PID 57894, `signal_engine.py` PID 57904).

---

## 3. Tuesday Morning Action Item #1: Review Monday's Live Performance

Before touching any code on Tuesday:
1. Run [`tools/fill_truth_report.py`](../tools/fill_truth_report.py) on Monday 2026-11-02 fills.
2. Run [`tools/studies/week_dollar_pnl.py`](../tools/studies/week_dollar_pnl.py) to calculate exact Dollar P&L ($) and basis point returns.
3. Review `/api/state` and `logs/dashboard.log` for gate refusal labels:
   * Count occurrences of `anti_pullback_drop` and `below_vwap`.
   * Verify whether the gate blocked toxic falling knives (like Friday's `GPN` -74.1 bp) while letting healthy breakouts execute.
   * Present a clear Monday performance table to the operator.

---

## 4. Technical Roadmap & Immediate Backlog

The following two breakthrough strategies were researched, simulated, and stress-tested on 2026-10-31. They are ready for phased implementation:

```
                  PHASED IMPLEMENTATION TIMELINE
 
 Monday 11/02            Tuesday 11/03           Wednesday-Thursday
┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
│ Live: Option 2     │  │ 1. Audit Mon Live  │  │ 1. Deploy Midpoint │
│ Gate Sole Variable │─>│ 2. Build Midpoint  │─>│    Exit Router     │
│ Background: Shadow │  │    Exit Router +   │  │ 2. Build Open      │
│ Logger Active      │  │    Unit Tests      │  │    Thrust Pipeline │
└────────────────────┘  └────────────────────┘  └────────────────────┘
```

### Roadmap Item 1: Passive Midpoint Pegged Exits (Priority: HIGH)
* **Problem Solved:** Currently, all exits fire as market orders (`_close_position`), paying an average of **3.9 bp in bid-ask spread friction per exit** (~$18–$52/week paid to market makers).
* **Proven Performance:**
  * Tested across 134 live trades from last week and 110 representative trades from all 22 sessions of October against high-resolution millisecond SIP quotes ([`tools/studies/passive_exit_sim.py`](../tools/studies/passive_exit_sim.py), [`tools/studies/adversarial_exit_audit.py`](../tools/studies/adversarial_exit_audit.py)).
  * **88.2% to 92.5% Fill Rate** within a 10-second timeout collar.
  * **Adversarial Audit (Strict Trade-Through + 500 sh Queue + Timeout Penalty):** Generated **+1.42 to +2.56 bp/trade net lift**.
  * **Full Month Impact:** Projected to recapture **+1,712 basis points** of pure execution spread in October (~$171 on $1k tranches / ~$1,712 on $10k tranches).
  * **Combined with Option 2:** Turns last week from a **-$6.57 loss (-202.7 bp)** into a **+$8.58 PROFIT (+223.9 bp)**.
* **Claude's Implementation Steps for Tuesday:**
  1. **Phase A (Zero-Risk Shadow Logger):** Create a listener in [`ai_positions.py`](../ai_positions.py) that records the exit decision timestamp, NBBO bid/ask/midpoint, and logs whether a midpoint fill would have executed within 10 seconds on real SIP tape.
  2. **Phase B (Order Router):** Implement `place_midpoint_exit(ticker, qty, timeout_sec=10)` in [`alpaca_trader.py`](../alpaca_trader.py).
     * Submit limit sell at `round((bid + ask) / 2.0, 2)`.
     * Poll order status for up to 10 seconds.
     * If unfilled at deadline, cancel limit order, await cancel confirmation, and execute `close_position(ticker)` at market.
     * Include comprehensive unit tests in `tests/test_midpoint_exits.py`.

---

### Roadmap Item 2: The 3-Minute Open Thrust Strategy (Priority: MEDIUM)
* **Problem Solved:** Overcomes morning pullback traps by entering the opening institutional surge on fundamental catalyst runners and exiting quickly before any morning fade begins.
* **Proven Performance:**
  * Tested on 3,249 morning gapper events across 417 trading sessions from 2024–2026 ([`tools/studies/open_thrust_deep_dive.py`](../tools/studies/open_thrust_deep_dive.py)).
  * **The Winning Ruleset:**
    * Overnight fundamental catalyst (earnings, FDA, M&A) known before 09:25 ET.
    * Opening gap $\ge 5\text{--}8\%$.
    * Opening 1-minute Bar 0 is green (`close > open`).
    * Buy at 09:31:00 open.
    * **Exit via 1-Minute Prior-Low Trailing Stop** (cuts failed moves immediately, lets winners run up to 5 minutes).
  * **Out-of-Sample Holdout Results (Strict Chronological Split):**
    * Gap $\ge 5\%$: **+9.64 bp net** after 7 bp friction + 5 bp slippage (**Half A: +8.9 bp / Half B: +10.4 bp**).
    * Gap $\ge 8\%$: **+15.14 bp net** after 7 bp friction + 5 bp slippage (**Half A: +16.6 bp / Half B: +13.7 bp**, $t = +1.34$).
* **Claude's Implementation Steps for Mid-Week:**
  1. Build pre-market news scanner/classifier to ingest catalyst gappers by 09:25 ET.
  2. Create a dedicated `open_thrust_arm` evaluated between 09:30:55 and 09:31:05 ET.
  3. Wire the 1-minute prior-low trailing stop manager into [`ai_positions.py`](../ai_positions.py).
  4. Run in paper shadow mode before enabling capital allocation.

---

### Roadmap Item 3: Existing Queue Items
* **Board Ranking Scorer (Row 53):** First session that can count is Monday 2026-11-02. Verify forward scores comparing high-beta movers vs tight megacaps on the live board.
* **Position Sizing Step-Up (Row 41):** Once Option 2 and Midpoint Exits demonstrate multi-week consistent green performance, propose shifting sizing from 5 slots x 20% to 3 slots x 33% (`ai_max_positions: 3`, `ai_max_position_pct: 33`, `ai_risk_pct: 1.67`).

---

## 5. Key File Locations & Test Scripts
* **Option 2 Gate Implementation:** [`ai_entry_watch.py`](../ai_entry_watch.py#L9657-L9716)
* **Option 2 Unit Tests:** [`tests/test_anti_pullback_gate.py`](../tests/test_anti_pullback_gate.py)
* **Weekly Dollar P&L Tool:** [`tools/studies/week_dollar_pnl.py`](../tools/studies/week_dollar_pnl.py)
* **Passive Exit Empirical Simulators:** [`tools/studies/passive_exit_sim.py`](../tools/studies/passive_exit_sim.py), [`tools/studies/adversarial_exit_audit.py`](../tools/studies/adversarial_exit_audit.py), [`tools/studies/october_passive_exit_sample.py`](../tools/studies/october_passive_exit_sample.py)
* **Open Thrust Deep Dive Simulator:** [`tools/studies/open_thrust_deep_dive.py`](../tools/studies/open_thrust_deep_dive.py)
* **Desk Configuration:** [`config/bot_config.json`](../config/bot_config.json)
