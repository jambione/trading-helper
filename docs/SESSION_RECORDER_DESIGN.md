# Session recorder + replay harness + shadow mode (design only)

Goal: after the close, run **real new code** on the day’s **actual inputs**
(prices, sources, failures) with a simulated clock — and optionally run a new
book server in **shadow** beside live for the first hour with no orders.

---

## 1. Session recorder

### What to record (append-only, per ET day)

| Stream | Fields (min) | Hook |
|---|---|---|
| `sources.jsonl` | ts, symbol, source (movers/trending/research/…), payload snippet (pct, rvol, price) | movers_screener / trending write; research publish; admit seed |
| `prints.jsonl` | ts, symbol, price, src (finnhub_ws / finnhub_rest / alpaca_trade / alpaca_quote / engine_rt), age_sec if known | dashboard price merge; `live_print` inputs |
| `quotes_meta.jsonl` | ts, process, kind (batch_trades / per_sym_quote / fetch_bars), n_symbols, ok / 429 / empty | alpaca_api.warn_429 + dashboard/engine call sites |
| `bars_1m.jsonl` or daily pickle | symbol, minute_ts, ohlcv, feed | signal_engine after bar fetch (or copy from runway cache writer) |
| `engine_tick.jsonl` | ts, symbol, pctr, pctr_slow, rising flags, rt_price, rt_age | signal_engine publish to signal_state |
| `book_events.jsonl` | existing events.jsonl + decision_ledger (already) | keep; ensure config_fp + git SHA on each |
| `config_snap.json` | full bot_config + git SHA + fingerprint at 09:25 / each change | watchdog or dashboard on config mtime |

### Format / size (estimate)

- JSONL, one event per line, UTC unix ts + et label.
- Busy day ballpark: prints ~50 sym × 4 updates/min × 390 min ≈ **80k lines** (~40–80 MB);
  sources negligible; bars 50 × 390 ≈ 20k rows; quotes_meta a few thousand.
- Budget **&lt; 500 MB/day** uncompressed; gzip overnight.
- Path: `ai_reports/sessions/YYYY-MM-DD/{streams…}`.

### Hooks (where)

1. **dashboard.py** — after Finnhub/Alpaca merge publishes a print; on 429.
2. **signal_engine.py** — after indicator compute / rt_price write; bar fetch result.
3. **ai_entry_watch.py** — already rich (events + decision_ledger); add
   `session_id` and ensure every admit/arm skip includes price_ts + src.
4. **alpaca_api.py** — single choke point for 429 + request counts (all processes).

Do **not** record secrets. Cap symbol set to book ∪ source ∪ positions.

---

## 2. Replay harness

### Idea

A **simulated clock** advances from 09:30 → 16:00. At each tick it feeds the
recorder streams into the same functions the live desk uses:

1. Source nominations → book server `admit` / rank / seat.  
2. Prints → one-price/one-clock update.  
3. Engine ticks → %R / mid_rise arm.  
4. Fake broker: fills at next print ± slip model; no Alpaca.

### API sketch

```text
ReplayClock(session_dir)
  .advance(to_ts)
  .inject_print / inject_source   # or auto from files
BookServer.replay_tick(clock)
Arm.eval(clock)
FakeBroker.on_order(...)
Assert: compare shadow decisions to recorded live decisions (optional)
```

### Verify

- Deterministic given a session dir + code SHA.  
- `rehearse_open`-style pass bars on the replayed book.  
- Diff live `events.jsonl` vs replay would-have events (shadow accuracy).

### Cost

CPU-bound; no network if session complete. Missing prints → same failure modes
as live (data-blocked), which is the point.

---

## 3. Shadow mode (first hour)

- Start a second process: `book_server_shadow.py` (or flag on ai_entry_watch)
  with **new** admit/rank code.
- Same market inputs (read dashboard state / shared recorder bus).
- **Orders disabled**; log `shadow_decision.jsonl`: would_admit, would_arm,
  would_skip, score, reasons.
- After 10:30, scorecard: shadow armable count, would-have opens vs live,
  overlap with names that later ran (from bars).
- Promote only if shadow hits pass bars without exploding 429s.

### Risk controls

- Hard `shadow_orders=false`.  
- Separate log dir; never write `entry_watch_state.json` live file.  
- CPU niceness; Finnhub subscribe **read-only** (no extra subs beyond live).

---

## 4. Implementation order (when scheduled)

1. `quotes_meta` + print recorder (unblocks Alpaca budget forensics).  
2. Session dir packing at 16:05.  
3. Replay harness driving book+arm+fake broker on one stored day.  
4. Shadow flag for book-server rewrite week.

*Design only — not implemented in this commit.*
