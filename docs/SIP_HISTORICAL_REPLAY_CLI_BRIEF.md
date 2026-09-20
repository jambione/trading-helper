# CLI brief — Historical SIP square replay (Phase B buy/no-buy)

**Repo:** `jambione/trading-helper` · base `master-mac`  
**Why:** Gate 1 of `docs/SIP_FROM_250_PLAN.md`. Decide whether **~$99/mo Algo Trader Plus** is worth it **before** buying — using free **delayed historical SIP** bars already available (`StockBarsRequest` + `DataFeed.SIP`). Live SIP is not required for this study.

**Locked question:** On the names Phase B actually admits, does SIP give enough premarket bars by **09:20 ET** for the dual-%R **slow line (112 bars)** so the square can exist — and how often would square OB+tight have been true?

## Product / pass bars (pre-register — do not retune after seeing results)

Window: **last 10–15 RTH weekdays** with a Phase B ledger (or explicit `--days`).

For each day × admitted Phase B symbol (from `ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl` admit/seed rows; if thin, also soft-seed momentum∪research that Phase B would have taken):

| Metric | Meaning |
|--------|---------|
| `sip_bars_0415_0920` | 1‑min SIP bars with timestamp in [04:00, 09:20) ET |
| `iex_bars_0415_0920` | Same window on IEX (control) |
| `sip_clear_112` | `sip_bars >= rte_slow_native_length` (112) by 09:20 |
| `iex_clear_112` | same for IEX |
| `square_minutes` | minutes in [04:00,09:20) where dual OB+tight true on SIP bars (same thresholds as RTH: `rte_threshold`, `rte_confluence_max`) |
| `pre_square_minutes` | both ≥ −`ai_watch_exh_pre_thr` (35) and tight |

**Go (SIP worth buying for Phase B data):**
- ≥ **60%** of (day, symbol) admits have `sip_clear_112`
- Median `sip_bars` ≥ **150** on those that clear (liquidity, not one-tick wonders)
- IEX clear rate ≪ SIP clear rate (proves the choke is feed, not “no tape anywhere”)

**No-go / later:**
- SIP clear rate &lt; 40% on our admit set → universe is too thin; fix admits before paying
- SIP ≈ IEX → missing_print is elsewhere (Finnhub path); don’t buy SIP yet

**Not a pass bar:** positive expectancy / MFE. This study answers **computability**, not edge (§7 caveat: more data ≠ edge).

## Current gaps

1. One-off 2026-09-17 table exists in GO_LIVE §3.1; no multi-day tool or report.
2. Phase B ledger is kind/symbol/ts/source — **no indicators**; replay must rebuild from bars.
3. Helpers already know `--feed sip`: `tools/claim_ab_screen.py`, `tools/ab_bench.py`, `backtest.py` — reuse bar fetch, don’t invent a new Alpaca client.

## Implement

### A. New tool `tools/phase_b_sip_replay.py`

```text
.venv/bin/python tools/phase_b_sip_replay.py \
  --ledger-dir ai_reports/phase_b_ledger \
  --days 15 \
  --feed-sip sip --feed-iex iex \
  --out ai_reports/phase_b_sip_replay/
```

1. Collect admitted symbols per day from phase_b_ledger (kinds that mean seat/admit; document which kinds). Dedupe. Cap optional `--max-symbols-per-day` for API budget.
2. For each (day, sym): fetch 1‑min bars 04:00–09:30 ET that day on SIP and IEX (Alpaca historical; delayed SIP is fine).
3. Count bars with `ts < 09:20 ET`; flag `clear_112`.
4. Walk SIP bars in order; compute fast/slow %R with desk `rte_*` / `signals.compute_percent_r_exhaustion` (or shared helper). Tally square / pre_square minutes.
5. Write:
   - `ai_reports/phase_b_sip_replay/YYYY-MM-DD.json` per day
   - `ai_reports/phase_b_sip_replay/summary.md` + `summary.json` with go/no-go vs frozen bars above
6. Default feed labels match existing tools (`sip` / `iex`).

### B. Universe note

If ledger admits are almost empty (blind lane), add `--universe-fallback momentum,research` for symbols that appeared on those boards between 04:00–09:20 that day — labeled separately so we don’t pretend they were Phase B admits.

### C. Tests

- Fixture: 120 SIP bars → `clear_112` true; 50 bars → false.
- Fixture: dual OB+tight stretch → `square_minutes > 0`.
- Summary go/no-go deterministic on fixture folder.

### D. Ship / run

- PR the tool + fixtures (code).
- **Run on mini** (keys + ledgers live there): 10–15 day replay after merge.
- Paste `summary.md` go/no-go into chat + check the Gate 1 box in `docs/SIP_FROM_250_PLAN.md`.
- **Do not** subscribe in the same PR.

## Done when

1. Multi-day SIP vs IEX clear-112 rates published for our admit set.
2. Written **go / no-go / later** against the frozen bars above.
3. Linked from SIP_FROM_250_PLAN Gate 1.

## Explicit non-goals

- Purchasing Algo Trader Plus.
- Enabling live Phase B or Plan B burst.
- Retuning square thresholds to make SIP “look good.”
- Claiming edge from bar counts alone.

## Suggested commit title

`feat(tools): Phase B historical SIP square replay (buy/no-buy)`
