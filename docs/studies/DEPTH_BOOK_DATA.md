# Depth-book data: Databento XNAS.ITCH mbp-10, 2026-06-01 .. 2026-08-28

Downloaded 2026-10-04 for the stage-2 depth pre-registration
([`depth_book_prereg.json`](depth_book_prereg.json), commit 62fcbbc). Paid from the $125 Databento
new-account historical credit (card never charged). Raw data is **not** in git (the repo is public, and
Databento's licence forbids redistribution).

## Where it lives (Mac mini only)

`~/repo/trading-helper/data/databento/xnas_itch_mbp10_2026-06_08/` (git-ignored by `data/databento/`)

| path | what |
|---|---|
| `mbp10/{DAY}_{SYM}_{MMM}.dbn.zst` | one file per random entry: XNAS.ITCH **mbp-10**, `[signal-35 s, signal]` by `ts_recv`. `MMM` = minutes after 09:30 ET (signal = 09:30 ET + MMM min, on the minute). |
| `mbp10/status_{DAY}.dbn.zst` | XNAS.ITCH **status** (halts/pauses) 08:00-16:00 ET for that day's 40 names |
| `plan.json` | universe (40 largest $10+ gappers per session, same rule as `sip_breakout_study.universe`) and the 7,353 entries (seed 17, 3 per name-day, 09:45-15:29) |
| `spend_log.csv` | every Databento request with its `metadata.get_cost` quote and the running total |
| `quote35_per_entry.json` | pre-download exact quote per entry |
| `features.json` | OBI5 / OBI5_dm / OBI1 / ASK_THIN30 at signal and at the 1 s guard, plus drop counts |
| `sip_quotes.json` | Alpaca SIP NBBO cache `"SYM|t_ns" -> [bid, ask]` (via `exec_report.nbbo_at`) at signal+0.5 s and +60/120/300 s |
| `result.json` | the pre-registered scoring output |
| `scripts/` | copies of the probe script and quote helpers |

## Schema

mbp-10 rows (`DBNStore.to_df(pretty_ts=False, price_type="float")`): `ts_recv` (index; ns UTC), `ts_event`,
`rtype`, `publisher_id`, `instrument_id`, `action` (A/C/M/T/F…), `side`, `depth`, `price`, `size`, `flags`,
`ts_in_delta`, `sequence`, then for levels `00..09`: `bid_px_NN`, `ask_px_NN`, `bid_sz_NN`, `ask_sz_NN`,
`bid_ct_NN`, `ask_ct_NN`, `symbol`. Every row carries the full top-10 snapshot after that event, so the
book at time S is simply the last row with `ts_recv <= S` (no replay needed). Undefined prices are NaN.
This is Nasdaq's own book only (not consolidated).

status rows: `action` (7 = trading, 8 = halt, 9 = pause, 10 = suspend), `reason`, `trading_event`,
`is_trading` / `is_quoting` / `is_short_sell_restricted` ('Y'/'N'/'~'), `symbol`.

## Load

```python
import databento as db   # pip install databento (kept out of the desk venv; e.g. /tmp/dbn/venv)
df = db.DBNStore.from_file("data/databento/xnas_itch_mbp10_2026-06_08/mbp10/2026-07-16_VEEE_254.dbn.zst") \
       .to_df(pretty_ts=False, price_type="float").reset_index()
```

## Rerun

```
python3 -m venv /tmp/dbn/venv && /tmp/dbn/venv/bin/pip install databento pandas numpy
export WORK=~/repo/trading-helper/data/databento/xnas_itch_mbp10_2026-06_08
/tmp/dbn/venv/bin/python tools/studies/databento_markout_probe.py plan       # deterministic (seed 17)
/tmp/dbn/venv/bin/python tools/studies/databento_markout_probe.py fetch      # skips files already present; SPEND_CAP_USD (default 10)
/tmp/dbn/venv/bin/python tools/studies/databento_markout_probe.py features
REPO=$PWD .venv/bin/python tools/studies/databento_markout_probe.py outcomes  # Alpaca SIP, cached
REPO=$PWD .venv/bin/python tools/studies/databento_markout_probe.py score
```
`fetch --pipe` = the 1-name, 1-day pipe check. The key is read from `config/secrets.json.databento`
(`{"api_key": ...}`, git-ignored) and never printed. Only usage-priced historical `timeseries.get_range`
is used; no batch jobs, no live, no subscription. Each request is quoted with `metadata.get_cost` first
and refused if the cumulative quoted total would pass the cap.

## Cost

- Pre-download exact quote (`metadata.get_cost`, one per window): **$6.2560** for the 7,353 windows; status ≈ $0.006.
- Logged quoted total of everything requested: **$6.2686** over 7,423 requests (`spend_log.csv`; this includes
  7 conservative re-reservations of downloads interrupted by restarts, so the true bill should be ≤ this).
  Charged against the $125 credit; the card was never charged. Check the portal Billing page for the posted amount.
- Quote granularity note: `get_cost` prices in coarse time chunks (a 1 ms window quotes the same as 35 s; 35 s = 5 min),
  so very narrow windows have a floor of ~$0.0003-0.02 each depending on the name's activity.
- Full-RTH mbp-10 for all 2,451 name-days would have quoted $83.34 (reference).

Size on disk: ~92 MB. Survivors after book drops: 7,261 (drops: no_book 54, stale 28, halt 10).
Result of the pre-registered test: [`DEPTH_BOOK_RESULT_2026-10-04.md`](DEPTH_BOOK_RESULT_2026-10-04.md).

## Premarket set (2026-10-04, incomplete)

`data/databento/xnas_premarket_2026-06_08/`: XNAS.ITCH mbp-10 per name-day for the window [qualify−10 min, 09:41 ET]
(`book/{DAY}_{SYM}.dbn.zst`), status per day, plus the Alpaca raw daily, 30-min scan and 1-minute caches (`rawdaily.pkl`,
`scan/`, `minute/`), `plan.json`, `quote.json` and `spend_log.csv`. 106 of 797 name-days were fetched before Databento
returned 402 account_insufficient_funds; see [`PREMARKET_DEPTH_STATUS_2026-10-04.md`](PREMARKET_DEPTH_STATUS_2026-10-04.md).
