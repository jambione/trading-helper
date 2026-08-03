# AI Trader Execution (Agreement Watch + Realtime Arming) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When both AIs agree on a name, keep it on a session watch queue and use Alpaca realtime quotes to buy when a WAIT turns into a ready zone — without depending only on research/open-bell clocks. Sells stay continuous mechanical management.

**Architecture:** Research remains the slow clock (thesis + agreement). A new `ai_entry_watch` module owns queue state, structured WAIT classification, zone arming, and poll ticks. `ai_trader` calls the poller every `ai_watch_poll_sec` during RTH. Entry structure still uses existing `evaluate_entry` / `place_scaled_entry` in `ai_positions.py`, with prompt + persistence fixes so WAIT can carry levels.

**Tech Stack:** Python 3, existing `ai_trader` / `ai_positions` / `ai_trading` / `ai_suggest`, Alpaca paper via `alpaca_trader` + quote helpers in `ai_trading`, pytest, JSON state under `claude_reports/`.

**Spec:** `docs/superpowers/specs/2026-08-03-ai-trader-execution-design.md`

## Global Constraints

- Paper trading only; do not enable live/non-paper paths.
- No short selling in v1.
- Prefer agreed (`agreement=true`) names; single-source watch off by default.
- Do not full-LLM-entry on every poll tick; structure calls are rate-limited.
- Keep `manage_open_positions` continuous and unchanged in spirit.
- Default expire unfilled watches at RTH close.
- TDD: failing test first for each task; run targeted pytest before commit.
- Follow existing patterns in `ai_positions.py` (paths, `log_event`, tmp_path monkeypatch in tests).
- Do not commit secrets (`config/secrets.json`, `signal_engine.env`).

## File map

| File | Responsibility |
|------|----------------|
| `ai_entry_watch.py` | **New.** Watch queue load/save, upsert from research rows, classify WAIT, arm rules, poll tick, EOD expire. |
| `ai_positions.py` | Entry prompt allows structured WAIT; `normalize_entry_decision`; always log full decision on skip/ok; keep place/manage. |
| `ai_suggest.py` | After research merge / entry path: call watch upsert; optional thinner use of `_place_qualifying_entries` when watch owns timing (flag). |
| `ai_trader.py` | RTH poll loop for `ai_entry_watch.poll_once`; rebuild queue after open-bell / book refresh. |
| `ai_trading.py` | Reuse `_latest_ask` / `_latest_bid` / `market_is_open` (no big rewrite). |
| `config.py` | New defaults: `ai_watch_*`, structure TTL, zone pad, structure call cap. |
| `tests/test_ai_entry_watch.py` | **New.** Unit tests for queue, arming, wait_kind, expire. |
| `tests/test_ai_positions.py` | Extend for structured WAIT parse + qualifies behavior. |
| Dashboard/API (Phase 3 optional) | Expose watch queue in positions/suggestions payload if cheap. |

---

### Task 1: Config knobs for watch execution

**Files:**
- Modify: `config.py` (`DEFAULT_CONFIG` and any allowlist of keys if present ~lines 160–180 and 330–350)
- Test: `tests/test_ai_entry_watch.py` (create file; config smoke only)

**Interfaces:**
- Produces: config keys with defaults:
  - `ai_watch_enabled`: `True`
  - `ai_watch_require_agreement`: `True`
  - `ai_watch_single_source`: `False`
  - `ai_watch_poll_sec`: `20.0`
  - `ai_structure_ttl_sec`: `5400.0`
  - `ai_watch_expire_at_close`: `True`
  - `ai_entry_zone_pad_pct`: `0.15`
  - `ai_max_structure_calls_per_hour`: `12`
  - `ai_persist_entry_decisions`: `True`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ai_entry_watch.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DEFAULT_CONFIG, load_config

def test_watch_config_defaults_present():
    for key in (
        "ai_watch_enabled",
        "ai_watch_require_agreement",
        "ai_watch_single_source",
        "ai_watch_poll_sec",
        "ai_structure_ttl_sec",
        "ai_watch_expire_at_close",
        "ai_entry_zone_pad_pct",
        "ai_max_structure_calls_per_hour",
        "ai_persist_entry_decisions",
    ):
        assert key in DEFAULT_CONFIG
    cfg = load_config()
    assert cfg["ai_watch_enabled"] is True
    assert cfg["ai_watch_require_agreement"] is True
    assert cfg["ai_watch_poll_sec"] == 20.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ai_entry_watch.py::test_watch_config_defaults_present -v`  
Expected: FAIL (keys missing from `DEFAULT_CONFIG`)

- [ ] **Step 3: Add keys to `DEFAULT_CONFIG` in `config.py`**

Place near other safety knobs (`ai_open_bell_*`). If `config.py` has an explicit env/secrets key list, add the new keys there too so overrides work.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ai_entry_watch.py::test_watch_config_defaults_present -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): add config defaults for entry watch poller

EOF
)"
```

---

### Task 2: Structured WAIT in entry prompt + normalize decision

**Files:**
- Modify: `ai_positions.py` — `_ENTRY_PROMPT_TEMPLATE` (~lines 78–145), add `normalize_entry_decision`, adjust `qualifies_as_entry` only if needed (BUY still requires numbers)
- Test: `tests/test_ai_positions.py`

**Interfaces:**
- Produces: `normalize_entry_decision(raw: dict | None) -> dict | None` with fields:
  - `decision`: `"BUY"` | `"WAIT"`
  - `wait_kind`: `"wait_for_zone"` | `"wait_setup"` | `"hard_no"` | `None` (None when BUY)
  - `entry_low`, `entry_high`, `stop_price`, `target_1`, `target_2`, `reward_risk`, `summary`, etc. preserved
- Inference: if WAIT and `entry_low>0` and `stop_price>0` and `target_1>0` → `wait_for_zone`; if WAIT and summary/decision hard no keywords or explicit `wait_kind` → honor; else WAIT without levels → `wait_setup`
- Prompt change: **remove** “If WAIT, set every numeric field to 0”. Replace with structured WAIT rules + `wait_kind`.

- [ ] **Step 1: Write failing tests**

```python
def test_normalize_wait_for_zone_infers_kind():
    d = cp.normalize_entry_decision({
        "decision": "WAIT",
        "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        "summary": "wait for pullback to 27-28.5",
    })
    assert d["wait_kind"] == "wait_for_zone"
    assert d["entry_low"] == 27.0

def test_normalize_wait_setup_without_levels():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "entry_low": 0, "stop_price": 0, "target_1": 0,
        "summary": "no clean setup",
    })
    assert d["wait_kind"] == "wait_setup"

def test_normalize_hard_no():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "wait_kind": "hard_no", "summary": "thesis broken",
    })
    assert d["wait_kind"] == "hard_no"

def test_qualifies_as_entry_still_rejects_wait():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
    })
    assert cp.qualifies_as_entry(d, min_reward_risk=3.0) is False
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_ai_positions.py::test_normalize_wait_for_zone_infers_kind tests/test_ai_positions.py::test_normalize_wait_setup_without_levels tests/test_ai_positions.py::test_normalize_hard_no tests/test_ai_positions.py::test_qualifies_as_entry_still_rejects_wait -v`

- [ ] **Step 3: Implement `normalize_entry_decision` + prompt text update**

Call `normalize_entry_decision` at end of `evaluate_entry` before return so all callers get `wait_kind`.

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add ai_positions.py tests/test_ai_positions.py
git commit -m "$(cat <<'EOF'
feat(ai): structured WAIT with wait_kind and levels

EOF
)"
```

---

### Task 3: Persist full entry decisions on every structure outcome

**Files:**
- Modify: `ai_positions.py` — helper `log_entry_decision(symbol, decision, **extra)` writing to events via `log_event` with full fields (decision, wait_kind, levels, summary truncated to ~300 chars)
- Modify: `ai_suggest.py` — `_place_qualifying_entries` where it logs `entry_not_qualified` (~1862–1866): pass full decision fields, not only `decision` key
- Test: `tests/test_ai_positions.py`

**Interfaces:**
- Produces: `log_entry_decision(symbol: str, decision: dict | None, *, reason: str, **extra) -> dict`
- Event kind: `entry_decision` (plus keep existing `entry_skip` for gates)

- [ ] **Step 1: Write failing test**

```python
def test_log_entry_decision_writes_levels(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(cp, "EVENTS_PATH", events)
    cp.log_entry_decision(
        "SMCI",
        {"decision": "WAIT", "wait_kind": "wait_for_zone",
         "entry_low": 27.0, "entry_high": 28.5, "stop_price": 25.0,
         "target_1": 35.0, "summary": "pullback"},
        reason="structure",
    )
    row = json.loads(events.read_text().strip().splitlines()[-1])
    assert row["kind"] == "entry_decision"
    assert row["symbol"] == "SMCI"
    assert row["wait_kind"] == "wait_for_zone"
    assert row["entry_low"] == 27.0
    assert "pullback" in row["summary"]
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement + wire into `_place_qualifying_entries` after `evaluate_entry`**

Always call `log_entry_decision` when a decision object exists (BUY or WAIT), gated by config `ai_persist_entry_decisions` default True (read via small cfg helper or always-on for tests).

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add ai_positions.py ai_suggest.py tests/test_ai_positions.py
git commit -m "$(cat <<'EOF'
feat(ai): persist full entry decision JSON on WAIT/BUY

EOF
)"
```

---

### Task 4: Watch queue state module — load/save/upsert

**Files:**
- Create: `ai_entry_watch.py`
- Test: `tests/test_ai_entry_watch.py`

**Interfaces:**
- `WATCH_STATE_PATH = REPORT_DIR / "entry_watch_state.json"` (same `claude_reports` dir as open-bell state; import path from `ai_positions.REPORT_DIR` or duplicate `ROOT / "claude_reports"`)
- `load_watch() -> dict[str, dict]`  # symbol -> record
- `save_watch(state: dict) -> None`  # atomic write like traffic_log
- `upsert_from_rows(rows: list[dict], *, cfg: dict, now: float) -> dict`  
  - If `ai_watch_require_agreement` and not row agreement → skip (unless `ai_watch_single_source`)
  - Create/update record: `symbol`, `reason`, `score`, `agreement`, `status` (`watching`), `updated_ts`, keep existing `structure` if symbol still eligible
- `drop_missing(state, active_symbols: set[str], now) -> dict` — invalidate symbols not in active set when research supersedes
- Record shape:

```python
{
  "symbol": "SMCI",
  "status": "watching",  # watching|armed|submitted|filled|invalidated|expired
  "agreement": True,
  "score": 8.2,
  "reason": "...",
  "structure": None | {decision, wait_kind, entry_low, ...},
  "structure_ts": 0.0,
  "last_poll_ts": 0.0,
  "last_ask": None,
  "updated_ts": 0.0,
}
```

- [ ] **Step 1: Write failing tests**

```python
def test_upsert_requires_agreement_by_default(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 8.2, "reason": "ai"},
        {"symbol": "HOOD", "agreement": False, "trending_score": 7.5, "reason": "x"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert "SMCI" in state
    assert "HOOD" not in state

def test_drop_missing_invalidates(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "SMCI": {"symbol": "SMCI", "status": "watching", "updated_ts": 1.0},
        "OLD": {"symbol": "OLD", "status": "watching", "updated_ts": 1.0},
    }
    out = ew.drop_missing(state, {"SMCI"}, now=2.0)
    assert out["SMCI"]["status"] == "watching"
    assert out["OLD"]["status"] == "invalidated"
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

- [ ] **Step 3: Implement `ai_entry_watch.py` load/save/upsert/drop_missing**

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add ai_entry_watch.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): entry watch queue state upsert and invalidation

EOF
)"
```

---

### Task 5: Zone arming pure functions (no broker)

**Files:**
- Modify: `ai_entry_watch.py`
- Test: `tests/test_ai_entry_watch.py`

**Interfaces:**
- `ask_in_zone(ask: float, entry_low: float, entry_high: float, pad_pct: float) -> bool`
- `spread_ok(bid: float | None, ask: float, max_spread_pct: float) -> bool`
- `should_arm_buy(record: dict, *, ask: float, bid: float | None, cfg: dict) -> tuple[bool, str]`  
  Returns `(True, "zone")` or `(False, reason)` e.g. `spread`, `above_zone`, `below_zone`, `hard_no`, `no_structure`, `wait_setup`, `not_watching`

Rules:
- status in `watching`/`armed` only
- structure.decision BUY with levels → arm if ask in zone (or ask <= entry_high and >= entry_low)
- wait_kind `wait_for_zone` → same
- `wait_setup` / `hard_no` → never arm for auto-buy
- spread using mid: `(ask-bid)/mid*100 <= ai_max_spread_pct`

- [ ] **Step 1: Write failing tests**

```python
def test_ask_in_zone_with_pad():
    import ai_entry_watch as ew
    assert ew.ask_in_zone(28.0, 27.0, 28.5, 0.15) is True
    assert ew.ask_in_zone(30.0, 27.0, 28.5, 0.15) is False

def test_should_arm_wait_for_zone(monkeypatch):
    import ai_entry_watch as ew
    rec = {
        "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 3.0}
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.95, cfg=cfg)
    assert ok and why == "zone"
    ok2, why2 = ew.should_arm_buy(rec, ask=32.0, bid=31.9, cfg=cfg)
    assert not ok2
```

- [ ] **Step 2–4: Fail → implement → pass**

- [ ] **Step 5: Commit**

```bash
git add ai_entry_watch.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): zone and spread arming rules for entry watch

EOF
)"
```

---

### Task 6: `poll_once` — quotes, place entry, rate-limited restructure

**Files:**
- Modify: `ai_entry_watch.py`
- Test: `tests/test_ai_entry_watch.py` with monkeypatched `ai_trading` / `ai_positions`

**Interfaces:**
- `poll_once(*, cfg: dict, now: float | None = None) -> list[dict]`  # list of event dicts
- Behavior:
  1. If not `ai_watch_enabled` or not market open → return `[]` or single skip event
  2. Load watch state; for each non-terminal symbol:
     - Refresh ask/bid via `ai_trading._latest_ask` / `_latest_bid`
     - If no structure or structure older than `ai_structure_ttl_sec` → call structure helper (see below) subject to `ai_max_structure_calls_per_hour`
     - If `should_arm_buy` → build decision dict for `place_scaled_entry` (need equity from `ai_trading.get_account()`), call `ai_positions.place_scaled_entry`, on ok set status `submitted`/`filled`, `ai_trading.record_external_buy`
  3. Save state; return events
- `ensure_structure(record, cfg, now) -> dict` uses `ai_positions.evaluate_entry` with book reason; stores normalized decision on record
- Structure call budget: module-level or state file ring of timestamps in last hour

- [ ] **Step 1: Write failing test with fakes**

```python
def test_poll_once_buys_when_in_zone(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    monkeypatch.setattr(gt, "market_is_open", lambda: True)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "_latest_ask", lambda s: 28.0)
    monkeypatch.setattr(gt, "_latest_bid", lambda s: 27.95)
    monkeypatch.setattr(gt, "has_open_position", lambda s: False)
    monkeypatch.setattr(gt, "can_open_new_position", lambda s: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"ok": True, "equity": 100_000})
    monkeypatch.setattr(gt, "buys_left_this_poll", lambda: 3)
    placed = []
    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}
    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)
    cfg = {
        "ai_watch_enabled": True,
        "ai_max_spread_pct": 1.0,
        "ai_entry_zone_pad_pct": 0.15,
        "ai_min_reward_risk": 3.0,
        "ai_structure_ttl_sec": 999999,
        "ai_max_structure_calls_per_hour": 12,
        "ai_max_price": 100.0,
        "ai_risk_pct": 1.0,
    }
    events = ew.poll_once(cfg=cfg, now=1e12 + 10)
    assert placed == ["SMCI"]
    assert any(e.get("kind") == "entry_ok" or e.get("symbol") == "SMCI" for e in events) or placed
```

- [ ] **Step 2–4: Fail → implement `poll_once` + budget → pass**

Also test: wide spread does not place; `wait_setup` does not place without new structure.

- [ ] **Step 5: Commit**

```bash
git add ai_entry_watch.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): poll_once arms zone and places scaled paper entries

EOF
)"
```

---

### Task 7: Wire queue rebuild from research + open bell

**Files:**
- Modify: `ai_suggest.py` — after successful research parse when `trading` (near `_place_qualifying_entries` call ~2009): also `upsert_from_rows` + `drop_missing` on merged/tag_agreement rows
- Modify: `ai_trader.py` — `_run_open_bell_entries`: after/before place, rebuild watch from `book.rows`; consider reducing open-bell to “structure + queue” while poller buys (keep calling `_place_qualifying_entries` once for immediate BUYs still OK)
- Test: unit test upsert called — monkeypatch in `tests/test_ai_entry_watch.py` or thin test in `tests/test_ai_suggest.py` if patterns exist

**Interfaces:**
- Produces: `rebuild_watch_from_book(rows, cfg, now) -> dict` convenience in `ai_entry_watch.py` combining upsert + drop_missing + save

- [ ] **Step 1: Write test for `rebuild_watch_from_book`**

```python
def test_rebuild_watch_from_book(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    rows = [{"symbol": "SOFI", "agreement": True, "trending_score": 7.8, "reason": "peg"}]
    state = ew.rebuild_watch_from_book(rows, cfg=cfg, now=100.0)
    assert "SOFI" in state and state["SOFI"]["status"] == "watching"
    assert ew.load_watch()["SOFI"]["symbol"] == "SOFI"
```

- [ ] **Step 2–4: Implement rebuild; call from `ai_suggest` trading path and `ai_trader` open-bell**

When `ai_watch_enabled`, after research entries, still allow immediate `_place_qualifying_entries` for names that already qualify as BUY (fast path). Watch covers WAIT. Document in code comment.

- [ ] **Step 5: Commit**

```bash
git add ai_entry_watch.py ai_suggest.py ai_trader.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): rebuild entry watch from research and open bell

EOF
)"
```

---

### Task 8: RTH poll loop inside `ai_trader.main`

**Files:**
- Modify: `ai_trader.py` main loop (~635–665)
- Test: optional small test of “poll interval elapsed” helper, or manual verification steps below

**Interfaces:**
- `last_watch_poll = 0.0`
- Each loop iteration, if trading and `ai_watch_enabled` and `(t0 - last_watch_poll) >= ai_watch_poll_sec`: call `ai_entry_watch.poll_once(cfg=cfg, now=t0)`; set `last_watch_poll = t0`
- On poll errors: print + `log_event("watch_poll_error", reason=...)`; do not crash loop
- Optional: if `ai_watch_expire_at_close` and market just closed, call `expire_open_watches(now)` once per day

- [ ] **Step 1: Add `expire_open_watches` + test**

```python
def test_expire_open_watches(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({"SMCI": {"symbol": "SMCI", "status": "watching"}})
    out = ew.expire_open_watches(now=1.0)
    assert out["SMCI"]["status"] == "expired"
```

- [ ] **Step 2: Implement expire + wire main loop poll**

Reload cfg each poll or reuse loaded cfg (match how open-bell reads cfg — currently load once at start; for watch flags, re-`load_config()` every poll is safer for live toggles, or load once like existing code).

- [ ] **Step 3: Run full unit suite for AI modules**

Run:  
`pytest tests/test_ai_entry_watch.py tests/test_ai_positions.py tests/test_ai_trading.py tests/test_ai_suggest.py -q --tb=line`  
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add ai_trader.py ai_entry_watch.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): run entry watch poller in ai_trader RTH loop

EOF
)"
```

---

### Task 9: Positions payload / operator visibility (Phase 3 light)

**Files:**
- Modify: `ai_trader.py` `_positions_payload` (or wherever positions JSON is built ~261–320)
- Test: small unit if payload is pure; else skip automated and verify keys manually

**Interfaces:**
- Add `"entry_watch": ai_entry_watch.public_snapshot()` → list of `{symbol, status, wait_kind, entry_low, entry_high, last_ask, score, agreement}`

- [ ] **Step 1: Implement `public_snapshot() -> list[dict]` + include in positions write**
- [ ] **Step 2: pytest for snapshot shape**
- [ ] **Step 3: Commit**

```bash
git add ai_entry_watch.py ai_trader.py tests/test_ai_entry_watch.py
git commit -m "$(cat <<'EOF'
feat(ai): expose entry watch queue on positions state

EOF
)"
```

---

### Task 10: Deploy verification checklist (manual on Mac mini)

No code — run after deploy when user asks.

- [ ] Confirm `ai_trader` starts without import errors; log line mentions watch poll if enabled
- [ ] With market open and an agreed name forced into `entry_watch_state.json` with `wait_for_zone` and current ask in zone → paper order appears (or use a dry mock if market closed)
- [ ] Confirm WAIT events in `claude_reports/events.jsonl` include `entry_low` / `summary`
- [ ] Confirm unfilled watches expire after close when `ai_watch_expire_at_close`
- [ ] Confirm sells still managed: open a paper position fixture path or existing stop logic unchanged

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Agreement ⇒ watch queue | 4, 7 |
| WAIT ⇒ poll not abandon | 5, 6, 8 |
| Structured WAIT + levels | 2, 3 |
| Alpaca-driven arming | 5, 6 |
| Research slow clock | 7 |
| Open bell kickstart | 7 |
| Continuous sells unchanged | 8 (no change to manage path) |
| Expire at close | 8 |
| Config knobs | 1 |
| Observability | 3, 9 |
| Rate-limit structure calls | 6 |
| No shorting | 6 (buy path only) |
| Phases 1–3 | Tasks 1–3 = P1; 4–8 = P2; 9–10 = P3 |

## Placeholder / consistency scan

- Names: `wait_kind` values `wait_for_zone` | `wait_setup` | `hard_no` used consistently.
- Module name: `ai_entry_watch` throughout.
- Placement always via `place_scaled_entry` + `record_external_buy`.
- No TBD steps remaining.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-03-ai-trader-execution.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks  
2. **Inline Execution** — Same session, batch with checkpoints  

Which approach do you want? (Or “start Task 1 only” if you prefer a smaller first bite.)
