# Grok CLI brief: AI catalyst shadow logger (log only, no trading impact)

Written Sat 2026-09-26. Paste this whole file into Grok CLI.

You are building a small, log-only research feature for the trading-helper paper desk. Work in the MacBook repo `~/repo/trading-helper` on branch `master-mac`. The live desk runs on the Mac mini (`ssh mac-mini-away`, same repo path, `.venv/bin/python`). Always end an ssh command with `< /dev/null`.

## Why this exists
We want to know whether an AI model that reads a stock's fresh news can pick names that beat random over the next 1, 5 and 10 days. A historical backtest can't answer that, because the models already know how past news played out. So this feature must work **forward only**:
- Each trading morning (and once at midday), both models score the fresh news.
- Every answer is logged, append-only.
- A nightly scorer grades the rows as their forward returns come in.

**Nothing in this feature places an order.** Nothing reads or writes trading state, and nothing changes how the desk trades.

## Hard rules
- **The repo is PUBLIC.** Never commit, print or log API keys, tokens, account numbers or Keychain contents.
- **Zero trading impact:**
  - no orders and no broker trading calls;
  - no reads or writes of positions, book, watch or other desk state files;
  - no edits to trading-path code or `config/bot_config.json`.
  - The one edit to `ai_trader.py` is a guarded tick call, described below.
- **Keep it simple:** one module (`ai_catalyst.py`), one scorer (`tools/ai_catalyst_score.py`), one config key (`ai_catalyst_log_enabled`), one universe file. Put caps, times and batch sizes in module constants, not new config keys.
- **Reuse the existing model clients and their auth paths.** Do not add an xAI HTTP client or `XAI_API_KEY`. Do not add any new login flow.
- **Run tests on the MacBook only.** Never run pytest on the mini. A known test-hygiene bug rewrites the tracked `config/bot_config.json` during a suite run (see `docs/HANDOFF_2026-09-26.md`, Housekeeping).
  - After every suite run, check `git status config/bot_config.json`.
  - If the file shows as modified, run `git checkout config/bot_config.json`.
  - Never commit that file.
- **Don't touch the MacBook's uncommitted files.** Leave these alone and never stage them: `docs/PHASE1_AFTER_CLOSE_2026-09-21.md`, `docs/studies/*`, `tools/studies/edge*.py`, `tools/studies/entry_scorecard.py`, `tools/studies/book_names_study.py`, `tools/studies/entry_timing_study.py`. Stage files by explicit path. Never use `git add -A` or `git add .`.
- **Restart only the approved way.** The only allowed restart is `launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk`, run on the mini, outside 09:30-16:00 ET. Never run `./trading restart` or start desk processes over ssh.

## What already exists (read these first)
1. **`ai_suggest.py`: the model clients. Reuse them as they are.**
   - `call_grok_cli(prompt, *, model, timeout, max_turns, live_search, cli_bin, phase)`:
     - runs the Grok Build CLI headless on the grok.com subscription login (`~/.grok/auth.json`, a file, not Keychain);
     - strips `XAI_API_KEY`/`GROK_API_KEY` from the subprocess env;
     - runs in an empty temp workspace (`_cli_workspace()`);
     - `live_search=False` adds `--disable-web-search`.
   - `call_agy_cli(prompt, *, model, timeout, cli_bin, effort, phase)`:
     - runs the Antigravity CLI (`agy -p`, Gemini subscription);
     - **its login lives in the macOS login Keychain.**
   - `agy_auth_status(cli_bin)` is the login probe (`agy models`). `ai_trader.py` prints `[ai] agy_auth=ok method=session` at startup (see `ai_trader.py` around lines 1279-1296, and `_agy_auth_remedy()` around 634).
   - Both clients append usage (tokens, `total_cost_usd` for Grok) to `ai_reports/token_metrics.jsonl` via `_record_usage`, tagged with `phase`. **Pass `phase="ai_catalyst"`.**
   - Useful helpers: `parse_research_times`, `due_slot(now, times=..., weekdays_only=..., catchup_min=..., last_slot=...)`, and `_strip_fences`/`_iter_json_blobs` for pulling JSON out of model text.
   - Live config on the mini (read from `load_config()`, never hard-code):
     - Grok: `grok_backend="cli"`, `grok_cli_bin="grok"`, `grok_model="grok-4.6"`.
     - AGY: `claude_backend="agy"`, `claude_cli_bin="agy"`, `claude_model`/`agy_model="gemini-3.1-pro-high"`. The AGY slot still uses the historical `claude_*` key names; mirror `seed_rank.run_one`'s fallback chain `agy_* -> claude_*`.
   - The mini's `~/.gemini/antigravity-cli/settings.json` does not allow `read_url`, so headless AGY cannot browse. Good for blindness; do not change it.
2. **`seed_rank.py`: the template to copy.** It shows how a side job hangs off the ai_trader loop:
   - `tick(cfg, now)`, called from `ai_trader.main()`'s main loop (around line 1647: `started = seed_rank.tick(cfg, t0)`);
   - `due()` via `ai_suggest.due_slot` with its own schedule state;
   - `append_log` JSONL under `ai_paths.resolve_report_dir()`;
   - model settings resolved in `run_one()`.
   - Current AI schedule (ET): research at 08:30 and 14:30, seed-rank at 09:25, 12:00 and 15:00. 08:45 and 12:15 do not collide with these. However, the 08:30 AGY/Grok research may still be running at 08:45.
3. **Why this must run inside the launchd job.**
   - `com.jambi.trading-desk` (`~/Library/LaunchAgents/com.jambi.trading-desk.plist`) runs `./trading restart`. That script starts `ai_trader.py`, `tools/session_snapshot.py`, `tools/watchdog.py` and the rest with `nohup`, so all of them inherit the launchd GUI session.
   - **A process started over ssh cannot read the login Keychain, so AGY fails there.**
   - Therefore the logger must be launched as a child of `ai_trader.py` (the same way `session_snapshot.py` launches `tools/nightly.py` with `subprocess.Popen(..., start_new_session=True, stdin=DEVNULL)`). It must never be launched as a cron job or over ssh.
   - Grok's auth is a file, so a Grok-only dry run does work over ssh. AGY does not.
4. **`desk_io.py`: why the logger runs as a child process and not in a thread.** ai_trader runs with desk_io in "live" mode. That mode records every alpaca-py REST call (patched at `RESTClient._request`) and every `*.json` file it reads into `ai_reports/sessions/DAY/wire.jsonl.gz`. The nightly exact-replay acceptance check depends on that recording. If the logger's news and snapshot calls ran inside ai_trader, they would pollute the recording.
   - **Run all fetching and model calls in a separate child process.** In a fresh interpreter desk_io stays in its default "off" mode.
   - Inside ai_trader, the tick must read no `.json` files except `load_config()`. Keep the schedule state in a `.txt` file (desk_io's file channel only catches `*.json`; `config/` is already skipped).
5. **News today:**
   - `news_feed.py` refreshes `ai_reports/news_cache.json` through alpaca-py `NewsClient.get_news(NewsRequest(...))`. It stores only `{ts, headline}`: no ids, summaries or urls. It is called from `tools/watchdog.py`.
   - `tools/catalyst_screen.py` has the same fetch pattern.
   - `massive_client.fetch_news(symbol, limit)` exists (per-symbol, needs `MASSIVE_API_KEY`).
   - **Finnhub company-news is not used anywhere in the code.** `finnhub_stream.py` is quotes only.
   - **v1 uses Alpaca news only.** Call `NewsClient.get_news` directly with multi-symbol requests, `start`/`end`, `limit=50` and pagination. Keep `id`, `headline`, `summary`, `url`, `source`, `created_at`, `symbols`. Namespace ids as `alpaca:<id>`. Do not use `news_cache.json` (it lacks ids and summaries). Do not add Finnhub or Massive in v1.
6. **Credentials.** Alpaca keys live in `signal_engine.env` and are loaded with `desk_core.load_desk_env(ROOT / "signal_engine.env")` into `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`; see `movers_screener._keys()`. `tools/bars.py` reads `config/secrets.json`. **Never log any of them.**
7. **Pre-market names.**
   - `movers_screener.premarket_scan` runs 08:00-09:30. It only takes gap-UP names priced $2-20 (the desk's band) and writes `ai_reports/premarket_scan/DAY.jsonl`. **Do not reuse its selection.**
   - Reuse `movers_screener._load_scan_universe(api, sec)`, one assets call. If importing `movers_screener` has side effects you don't want in the child, copy its ~25 lines. Also reuse its snapshot pattern: `StockHistoricalDataClient.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=chunk_of_500, feed=DataFeed.IEX))`, reading `latest_trade.price`, `latest_trade.timestamp`, `previous_daily_bar.close` and `previous_daily_bar.volume`.
   - The asset list also gives company names (`a.name`).
8. **Desk seed boards (read-only, repo root; each has `rows[]` with `symbol`):**
   - `movers_stocks.json` (`ts`);
   - `trending_stocks.json` (`updated`);
   - `grok_suggestions.json`;
   - `ai_paths.AGY_SUGGESTIONS_FILE` (`agy_suggestions.json`, which is **missing on the mini**; fall back to the legacy `claude_suggestions.json`);
   - `seed_rank_gx.json`.
   - Only use a board whose timestamp is today (ET). Tag each name with its board.
9. **`tools/bars.py`:**
   - `client()` (Alpaca data client);
   - `fetch(sym, day)` / `fetch_many(syms, day)` for 1-minute RTH closes (SIP historical, free when more than 15 minutes old);
   - `index_at` and `forward_return`.
   - It has no daily-bar helper. Add a small daily-close fetch inside the scorer (`TimeFrame.Day`, SIP), not in bars.py.
10. **`tools/nightly.py`:**
   - It is started in the background by `tools/session_snapshot.py` at 16:05 ET (so it is also under launchd).
   - It runs steps in sequence through `run(cmd, log, timeout)`. A failed step does not stop the next one.
   - It writes `ai_reports/nightly/DAY/{nightly.json,summary.md}`.
11. **`config.py`:** defaults dict (for example, `"ai_seed_rank_enabled": False` around line 1377) and `load_config()` (defaults <- bot_config.json <- secrets.json).
12. **`tests/conftest.py`:** it already redirects `AI_REPORT_DIR` to a temp dir, so anything written through `ai_paths.resolve_report_dir()` is safe in tests.

## Files to touch (exact)
- **NEW `config/liquid_universe.json`:** the 100-symbol liquid universe. It is copied from the uncommitted study list `LIQUID` in `tools/studies/edge_fetch.py`: 100 names, not 108, and it is not in edge_common.py. Format:
  ```json
  {"version": 1, "source": "tools/studies/edge_fetch.py LIQUID, 2026-09-26",
   "etfs": ["SPY","QQQ","IWM","DIA","SMH","XLF","XLE","XLK","TLT","GLD"],
   "symbols": ["SPY","QQQ","IWM","DIA","SMH","XLF","XLE","XLK","TLT","GLD","AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","V","MA","UNH","XOM","LLY","JNJ","PG","HD","COST","ABBV","MRK","CVX","KO","PEP","WMT","BAC","ORCL","CRM","AMD","NFLX","ADBE","CSCO","TMO","ACN","MCD","ABT","LIN","DHR","WFC","TXN","QCOM","INTU","AMGN","PM","IBM","CAT","GE","HON","UNP","LOW","SPGI","GS","MS","BLK","AMAT","MU","ISRG","NOW","BKNG","PLTR","UBER","SBUX","DE","AXP","C","SCHW","MDT","ADP","GILD","LRCX","PANW","KLAC","MRVL","ADI","SNPS","CDNS","REGN","VRTX","TJX","CMCSA","T","VZ","PFE","DIS","NKE","BA","COP","MO","SO","DUK","CVS"]}
  ```
- **NEW `ai_catalyst.py`** (repo root, next to `seed_rank.py`): the scheduler tick, the run logic and the CLI.
- **NEW `tools/ai_catalyst_score.py`:** the forward-return scorer and `scorecard.md` writer.
- **EDIT `ai_trader.py`:** one guarded call in the main loop, right after the `seed_rank.tick` block (~line 1655):
  ```python
  try:
      import ai_catalyst
      started = ai_catalyst.tick(load_config(), t0)
      if started:
          print(f"[ai] ai_catalyst {started}", flush=True)
  except Exception as e:  # noqa: BLE001
      print(f"[ai] ai_catalyst tick failed: {e}", flush=True)
  ```
  Use `load_config()` fresh here, not the startup `cfg`, so the kill switch works with a config edit and needs no restart. Nothing else in ai_trader changes.
- **EDIT `config.py`:** add the default `"ai_catalyst_log_enabled": True` with a one-line comment ("log-only AI news catalyst logger; no trading impact"). Do NOT edit `config/bot_config.json`.
- **EDIT `tools/nightly.py`:** add a step `catalyst` that runs `tools/ai_catalyst_score.py` through `run()` with a 30-minute timeout.
  - Run it FIRST, before fidelity, because it is quick and the replays take hours.
  - Add `"catalyst"` to the `--only` choices.
  - Record `res["catalyst"] = {"rc": rc}` and add one line to `summary_md`.
- **NEW tests:** `tests/test_ai_catalyst.py`, `tests/test_ai_catalyst_score.py`, plus small fixtures under `tests/fixtures/ai_catalyst/`.

## Spec: `ai_catalyst.py`

### Constants (module level)
```
RUN_TIMES = ["08:45", "12:15"]   # ET, trading days only
CATCHUP_MIN = {"08:45": 40, "12:15": 60}
MAX_NAMES_PER_RUN = 60
BATCH_SIZE = 5                   # names per model call (cost cap); part of PROMPT_VERSION
MAX_CALLS_PER_MODEL_PER_RUN = 16 # 12 batches + retries, hard stop
MODEL_CONCURRENCY = 2            # parallel calls per model
CALL_TIMEOUT_SEC = 240
RUN_DEADLINE_SEC = 40 * 60       # the child exits (logging unscored names as "deadline") after this
GAP_MIN_ABS_PCT = 3.0
GAP_MIN_PRICE = 20.0
GAP_MIN_PREV_DOLLARS = 20e6
GAP_MAX_TRADE_AGE_SEC = 900
GAP_TOP = 25
MAX_HEADLINES_PER_NAME = 8
SUMMARY_MAX_CHARS = 400
PROMPT_VERSION = "cat-v1"
```

### `tick(cfg, now) -> str | None` (runs inside ai_trader; must be cheap and must never block)
1. If `cfg.get("ai_catalyst_log_enabled", True)` is false, return None.
2. **Dry-run request:** if `ai_reports/ai_catalyst/dryrun.request` exists (plain text containing a comma-separated symbol list):
   - rename it to `dryrun.taken`;
   - spawn the child with `--dry-run --symbols <list>`;
   - return `"dryrun started"`.
   - This is how AGY gets verified under the launchd context.
3. **Schedule:** `slot = ai_suggest.due_slot(now, times=parse_research_times(RUN_TIMES), weekdays_only=True, catchup_min=..., last_slot=<last_slot.txt>)`.
   - Use the per-slot catch-up. The simplest way: call `due_slot` once per time with its own catch-up and take the latest.
   - If a slot is due, write `last_slot.txt` first (so a crash can never double-fire), then spawn `python -u ai_catalyst.py --run <slot>`. Return `f"run started slot={slot}"`.
   - Keep the last slot in memory too. Read `last_slot.txt` once, lazily.
4. **Spawn** with `subprocess.Popen([ROOT/.venv/bin/python or sys.executable, "-u", str(ROOT/"ai_catalyst.py"), ...], cwd=ROOT, stdout=open(logs/ai_catalyst.log,"ab"), stderr=STDOUT, stdin=DEVNULL, start_new_session=True)`.
   - Never wait on the child.
   - Before spawning, if a previous child is still alive (keep the `Popen` handle; check `poll()`), skip and log `"skip: previous run alive"`.

### Child run (`--run SLOT`)
1. **Trading-day check:** Alpaca calendar (`TradingClient.get_calendar`). If today isn't a session, append a `{"kind":"skip","reason":"holiday"}` row to `runs.jsonl` and exit.
2. **Kill switch and auth:**
   - Re-check `ai_catalyst_log_enabled` from `load_config()`.
   - Probe AGY with `agy_auth_status()` and Grok with `cli_logged_in()`.
   - A model whose auth fails is skipped for this run and logged, but the other model still runs. Never attempt a login.
3. **News window:**
   - **08:45 run:** from the previous session's 16:00 ET (from the calendar, so Monday reads from Friday 16:00) to the run's start `ts`.
   - **12:15 run:** from the `news_end` of today's last completed run in `runs.jsonl` (or the previous 16:00 if there was none) to run start.
   - Only items with `created_at < run start` are allowed.
4. **Candidate names (union; remember each name's sources):**
   - `universe`: every symbol in `config/liquid_universe.json`.
   - `gapper`, 08:45 run only:
     - snapshot the scan universe (IEX);
     - keep names with price >= $20, |gap vs prior close| >= 3%, last trade within 15 minutes, and prior-day dollar volume >= $20M;
     - take the top 25 by |gap|;
     - save the list to `runs.jsonl` so the 12:15 run reuses it.
   - `seed_movers` / `seed_trending` / `seed_research_grok` / `seed_research_agy` / `seed_rank`: names on today's desk boards. These are exempt from the price and gap filters.
5. **News fetch** for all candidates. Use multi-symbol batches of about 50 symbols per request and paginate.
   - Drop items already scored: build a seen set of `(symbol, model, news_id)` from the last 5 day logs. A name/model pair is scored only on news ids it has never seen. **Never rescore.**
   - **Only names with at least one new item are eligible.**
   - Sort eligible names by their newest new item, newest first, and keep 60. Log how many were dropped.
6. **Prices at scoring time** (one snapshot call per ~100 symbols, IEX), for each name plus SPY:
   - `last_trade`, `last_trade_ts`, `age_sec`, `prior_close`;
   - `price_source = "alpaca_iex_snapshot"`.
7. **Model calls:**
   - Batch names in groups of `BATCH_SIZE`, in deterministic symbol order.
   - For each model: `call_grok_cli(prompt, model=grok_model, timeout=CALL_TIMEOUT_SEC, max_turns=1, live_search=False, cli_bin=grok_cli_bin, phase="ai_catalyst")` and `call_agy_cli(prompt, model=agy_model, timeout=CALL_TIMEOUT_SEC, cli_bin=agy_cli_bin, phase="ai_catalyst")`.
   - The two models get the **identical prompt** and are run independently. Neither ever sees the other's output.
   - Run through a `ThreadPoolExecutor(MODEL_CONCURRENCY)` per model, respecting `MAX_CALLS_PER_MODEL_PER_RUN` and `RUN_DEADLINE_SEC`.
   - Capture each CLI's version once per run (`grok --version`, `agy --version`, best effort, 10 s timeout).
8. **Parse and validate** (see Prompt).
   - On a parse or validation failure, retry once with the same prompt plus: `Your previous reply was not valid JSON matching the schema. Reply with ONLY the JSON object.` If only some tickers are missing or invalid, retry only those.
   - After the retry, log the name/model with `status` set to `parse_fail`, `timeout`, `call_error`, `missing`, `deadline` or `auth_skip`. Such names still count as scored: they are never retried in a later run.
9. **Append** one row per (name, model, run) to `ai_reports/ai_catalyst/YYYY-MM-DD.jsonl` (append-only, flush after each batch).
   - Append a run summary row to `ai_reports/ai_catalyst/runs.jsonl`: `slot`, `news_start`, `news_end`, candidate counts by source, eligible, dropped, calls per model, failures, `wall_sec`, and the sum of `total_cost_usd` from `token_metrics.jsonl` for `phase=ai_catalyst` since the run started.

### Prompt (`PROMPT_VERSION = "cat-v1"`, a module constant; hash it with sha256)
System-style header:
- "You classify news catalysts for US stocks. Use ONLY the information given below. Do not browse, search or use tools. Do not use any knowledge of price moves after the timestamps shown. The only price information allowed is what is given here."

Per name block:
- ticker;
- company name;
- prior close;
- last trade price with its ET time and age;
- that name's new headlines, newest first, at most 8. Each headline shows its ET timestamp, source, headline, and summary (truncated to 400 characters).

Required output: ONE JSON object, nothing else:
```json
{"results": [{"ticker": "AAPL",
  "catalyst_type": "earnings|guidance|analyst|M&A|FDA/regulatory|product|macro/sector|legal|management|other|none",
  "direction": "up|down|neutral",
  "materiality": 1,
  "expected_horizon": "intraday|days|weeks",
  "confidence": 0.0,
  "rationale": "one sentence"}]}
```

Validation:
- enums must match exactly (case-insensitive, normalized to the canonical spelling above);
- `materiality` is an int from 1 to 5;
- `confidence` is a float from 0 to 1;
- `rationale` is a non-empty string, cut to 300 characters;
- tickers outside the batch are dropped;
- a batch ticker missing from the reply gets `status=missing` after the retry.

### Row schema (one JSON line)
```
row_id (sha1 of day|slot|symbol|model), kind:"score", day, slot, run_ts, scored_ts,
symbol, company, sources:[...], model_family:"grok"|"agy", model_id, cli_version,
prompt_version, prompt_sha256, batch_id, batch_symbols,
news_ids:[...], news_urls:[...], headlines_sha256, n_news, newest_news_ts, oldest_news_ts,
price:{last, ts, age_sec, source}, prior_close, spy:{last, ts, age_sec, prior_close},
status:"ok"|"parse_fail"|"timeout"|"call_error"|"missing"|"deadline"|"auth_skip",
retry_used, raw_response (first attempt), raw_response_retry, error,
catalyst_type, direction, materiality, expected_horizon, confidence, rationale
```
Never put env values, keys or full CLI command lines in rows or logs.

### CLI
- `python ai_catalyst.py --run SLOT`: the scheduled run. Only the tick uses it.
- `python ai_catalyst.py --dry-run --symbols AAPL,NVDA,JPM [--models grok,agy] [--since-hours 72] [--out PATH]`:
  - does one scoring pass on just those names, with no dedupe skip and news from the last N hours;
  - prints the rows (pretty JSON) and writes them to `--out` if given (under `ai_reports/ai_catalyst/dryrun/`);
  - **never appends to the day log or `runs.jsonl`**. Tag its token_metrics rows `phase="ai_catalyst_dryrun"`.

## Spec: `tools/ai_catalyst_score.py`
Run: `.venv/bin/python tools/ai_catalyst_score.py [--asof YYYY-MM-DD]`. It is idempotent and read-only except for its own outputs.

**Inputs and cache:**
- It reads every `ai_reports/ai_catalyst/*.jsonl` day log (rows with `kind=score`, `status=ok`).
- It keeps a cache at `ai_reports/ai_catalyst/returns_cache.jsonl`, keyed by `row_id` and horizon. A horizon value, once matured, is frozen and never refetched.

**Entry reference (tradable, the same for every name):** `entry_ts` is the close of the first 1-minute RTH SIP bar that starts at or after `max(scored_ts, 09:30 ET)` on the scoring day D. So the 08:45 run enters on the 09:30 bar's close, and 12:15 enters on the 12:15/12:16 bar.

**Horizons:**
- `h_1550`: `entry_ts` to D's 15:50 1-minute close;
- `h_d1`: to the close of trading day D+1;
- `h_d5`: to the close of D+5;
- `h_d10`: to the close of D+10.
- Trading days come from the Alpaca calendar.
- Unmatured horizons stay null and are not counted.

**Benchmarks, over the same entry and exit instants:**
- `base_ew`: the equal-weight mean return of the 90 single stocks in the universe (ETFs excluded; missing bars are skipped, and the n used is recorded);
- `spy`: SPY.
- Use `bars.fetch_many` for the 1-minute legs (one request per ~100 symbols per day) and a daily-close fetch for the D+k legs.

**Signed excess:**
- `side = +1` for up and `-1` for down. Neutral rows are reported but excluded from directional slices.
- `excess_base = side*(r_name - r_base_ew)`; `excess_spy = side*(r_name - r_spy)`.
- `net = excess - 5 bp` (assumed round trip).

**Counting unit:**
- To avoid double counting, a slice counts each `(symbol, D)` once, using the first qualifying row of the day.
- The **date-clustered t**: average net excess within each scoring day, then `t = mean(daily) / (sd(daily)/sqrt(n_days))`.
- Report `n_days`, `n_names`, the mean net bp, the t, and the hit rate.
- State plainly in the scorecard that overlapping 5- and 10-day windows inflate t a little.

**`ai_reports/ai_catalyst/scorecard.md`**, rewritten on every run, with these sections:
1. **Header:** as-of date, trading days logged, rows ok and failed by model, and total `ai_catalyst` cost to date (from token_metrics).
2. **The pre-registered primary test**, verbatim from below, with its current numbers and a PASS / FAIL / NOT YET (fewer than 40 days) verdict.
3. **By model** (grok, agy) × horizon.
4. **By direction × materiality bucket** (1-2, 3, 4-5) and **by direction × confidence bucket** (<0.5, 0.5-0.7, >=0.7), per model.
5. **By agreement**, at the (symbol, run) level:
   - `agree_strong`: both ok, same non-neutral direction, both materiality >= 4;
   - `agree_weak`: same direction, but not both materiality >= 4;
   - `disagree`: both ok with different directions;
   - `single`: only one model ok.
6. **Reference rows:** all scored names (the news-having set) vs `base_ew`, and names by source tag (universe / gapper / seed_*).
7. **Split halves:** the first and second half of the logged days, for the primary slice.

### Pre-registered success bar (copy this text into `scorecard.md` and into the `ai_catalyst.py` module docstring)
> **Primary slice:** both models agree, direction = up, and both materiality >= 4 (`agree_strong` & up).
>
> **Test:** after >= 40 trading days of logging, the primary slice must beat the equal-weight liquid-universe baseline (`base_ew`) at the **5-trading-day horizon (`h_d5`)**:
> - by **more than 50 bp net** of an assumed 5 bp round trip;
> - with a **date-clustered t >= 2**;
> - with the mean net excess **also above 50 bp in each half** of the period, split chronologically by scoring day.
>
> If all three hold, the verdict is GO. Otherwise it is NO-GO.
>
> Every other slice (the SPY comparison, the other horizons, single models, confidence buckets, down calls) is secondary. Secondary slices are reported but never used for the go/no-go. The bar, the slice and the horizon are fixed as of 2026-09-26 and must not be changed after data starts arriving.

## Guardrails checklist (build them in)
- [ ] No imports of `ai_positions`, `ai_entry_watch` book/watch writers, or any order or broker trading call. `TradingClient` is used for `get_calendar` and `get_all_assets` only.
- [ ] The child writes only under `ai_reports/ai_catalyst/`, to `logs/ai_catalyst.log`, and to `token_metrics.jsonl` (through the existing clients).
- [ ] The kill switch `ai_catalyst_log_enabled` (default true) is checked in `tick` and again in the child.
- [ ] Every cap is enforced: names, calls per model, per-call timeout, run deadline.
- [ ] The tick never blocks (spawns only) and never reads `.json` state inside ai_trader (only `load_config()`).
- [ ] No keys are logged. There is a test for this (see below).
- [ ] A model failure or timeout never affects the other model or the desk.

## Tests (MacBook only: `.venv/bin/python -m pytest tests/test_ai_catalyst.py tests/test_ai_catalyst_score.py -q`, then the full suite)
- **Prompt building:** a fixture of 2 names with 3 headlines produces a stable prompt. Check that `prompt_sha256` is stable, headlines are ordered newest first, summaries are truncated, the max headlines per name is respected, and there is no text from outside the fixture.
- **JSON parsing and validation:**
  - valid input;
  - fenced input;
  - prose around the JSON;
  - a bad enum;
  - materiality out of range;
  - confidence as a string;
  - an extra ticker;
  - a missing ticker;
  - the retry path (a fake client returns junk, then valid JSON, and `retry_used` is true);
  - two failures giving `parse_fail`.
- **Dedupe:**
  - A name/news id already in a prior day log for the same model is not scored again.
  - The other model is still scored.
  - A name with no new items is ineligible.
  - Failed statuses count as seen.
  - The 12:15 news window starts at the morning run's `news_end`.
- **Selection:**
  - the gapper filter (price, |gap|, trade age, dollar volume, top-N);
  - the 60-name cap, ordered by newest news;
  - stale boards (not today) are ignored.
- **Schedule and tick:**
  - `due` fires at 08:45 and 12:15 on weekdays within the catch-up window, not on weekends, and not twice (`last_slot.txt`);
  - the kill switch false means no spawn;
  - the dry-run request file is renamed and spawns once.
  - Monkeypatch `subprocess.Popen`; never spawn for real.
- **Dry run:** it never appends to the day log or `runs.jsonl`.
- **No secrets:** with `ALPACA_API_KEY=sk_test_SENTINEL` in the env and a fake client, no written row or log line contains `SENTINEL`.
- **Scorer math on fixtures:**
  - synthetic 1-minute and daily closes for 3 names, the universe and SPY;
  - checks the entry bar choice (08:45 gives 09:30; 12:15 gives the 12:15 bar);
  - `h_1550`, `h_d1`, `h_d5` and `h_d10`;
  - signed excess for up and down calls, and the 5 bp net;
  - unmatured horizons stay null;
  - the (symbol, D) dedupe;
  - the date-clustered t against a hand-computed value;
  - the half-split;
  - the agreement buckets;
  - the verdict (NOT YET below 40 days; PASS or FAIL on constructed data);
  - cache freezing (a matured value is not recomputed).
- **Isolation:** no network (fake clients and monkeypatched fetchers). Pass explicit `cfg` dicts; never call `load_config()`/`save_config()` against the real `config/bot_config.json`. All writes go through `ai_paths.resolve_report_dir()` (conftest points it to a temp dir) or `tmp_path`. Assert that `config/bot_config.json`'s mtime is unchanged after the new test files run.
- **After the full suite:** check `git status config/bot_config.json`. If it is modified (the known bug), run `git checkout config/bot_config.json`. Do not try to fix that bug in this change.
- Lint: `ruff check ai_catalyst.py tools/ai_catalyst_score.py tests/test_ai_catalyst*.py`.

## Commit and deploy
1. **Commit on the MacBook:**
   - `git pull --rebase origin master-mac`;
   - `git add config/liquid_universe.json ai_catalyst.py tools/ai_catalyst_score.py ai_trader.py config.py tools/nightly.py tests/test_ai_catalyst.py tests/test_ai_catalyst_score.py tests/fixtures/ai_catalyst/`. Explicit paths only; confirm with `git status` that nothing else is staged.
   - Commit (for example `feat: AI catalyst shadow logger (log-only) + nightly scorer`), then `git push origin master-mac`.
2. **When:** Sunday 2026-09-27, or Monday 2026-09-28 before 08:30 ET. Never 09:30-16:00 ET.
3. **Pull on the mini:** `ssh mac-mini-away 'cd ~/repo/trading-helper && git pull --ff-only' < /dev/null`. If the pull refuses because of local runtime files, stop and report. Do not stash or reset.
4. **Grok-only dry run over ssh** (Grok auth is a file, so this works over ssh; AGY will not):
   `ssh mac-mini-away 'cd ~/repo/trading-helper && .venv/bin/python ai_catalyst.py --dry-run --symbols AAPL,NVDA,JPM --models grok --since-hours 72' < /dev/null`
   Expect up to 3 rows with `status=ok`, and `ai_reports/ai_catalyst/YYYY-MM-DD.jsonl` not created. If Grok reports that it isn't logged in, stop and report; never run `grok login` over ssh.
5. **Restart** (on the mini, outside RTH): `ssh mac-mini-away 'launchctl kickstart -k gui/$(id -u)/com.jambi.trading-desk' < /dev/null`. The single quotes make `$(id -u)` evaluate on the mini. Never run `./trading restart` over ssh.
6. **Check the restart:** `logs/ai_trader.log` must contain `desk_io recording` and `agy_auth=ok`. If `agy_auth=fail` appears, stop and report; do not log in over ssh.
7. **Both-model dry run under the launchd context:**
   `ssh mac-mini-away 'cd ~/repo/trading-helper && mkdir -p ai_reports/ai_catalyst && echo AAPL,NVDA,JPM > ai_reports/ai_catalyst/dryrun.request' < /dev/null`
   - Within about a minute, `logs/ai_trader.log` shows `[ai] ai_catalyst dryrun started`.
   - A few minutes later, `ai_reports/ai_catalyst/dryrun/*.json` has rows for both `grok` and `agy` with `status=ok`, and `logs/ai_catalyst.log` shows no traceback.
   - The day log must not exist yet.
8. **Monday 2026-09-28 at about 09:05 ET, read-only:**
   - `ai_reports/ai_catalyst/2026-09-28.jsonl` has rows for both models with `slot=2026-09-28T08:45`;
   - `runs.jsonl` has the run summary (candidates by source, eligible <= 60, failures, cost);
   - `ai_trader.log` shows `ai_catalyst run started slot=2026-09-28T08:45`.
   - After 12:30, check for the 12:15 rows and that no (symbol, model, news_id) repeats.
9. **Monday night:** `ai_reports/nightly/2026-09-28/summary.md` shows the catalyst step with `rc=0`, and `ai_reports/ai_catalyst/scorecard.md` exists (only `h_1550` matured, primary verdict NOT YET).
10. **Rollback:** set `"ai_catalyst_log_enabled": false` in the mini's `config/bot_config.json`. The tick re-reads config, so no restart is needed. Do this only with Jonathan's OK, because it is a live config edit.

## Acceptance checklist
- [ ] `config/liquid_universe.json` is committed with the 100 symbols above.
- [ ] `ai_catalyst.py` meets the spec: tick, child run, dry run, dedupe, caps, both models via the existing `call_grok_cli`/`call_agy_cli`, strict JSON with one retry, full row schema.
- [ ] `tools/ai_catalyst_score.py` meets the spec, and `scorecard.md` includes the pre-registered bar verbatim.
- [ ] `ai_trader.py` changed only by the guarded tick call. `config.py` changed only by the one default key. `tools/nightly.py` has the new step first.
- [ ] No trading-path change, no order code, no state reads or writes, no secrets in code, rows or logs.
- [ ] New tests pass, the full suite passes on the MacBook, and `config/bot_config.json` is unmodified and uncommitted.
- [ ] Only the listed files are committed and pushed to `origin/master-mac`; the uncommitted study files are untouched.
- [ ] Mini: pulled, kickstarted outside RTH, `desk_io recording` and `agy_auth=ok` present, Grok dry run OK over ssh, both-model dry run OK through `dryrun.request`.
- [ ] Monday: 08:45 and 12:15 rows present, the nightly catalyst step rc=0, `scorecard.md` written.

## Report back
- commit SHA(s) and the files changed;
- test counts;
- the mini's HEAD after the pull;
- the dry-run rows (a trimmed sample);
- the Monday 08:45 run summary (candidates by source, eligible, calls, failures, cost);
- anything you skipped or changed from this brief, and why.
