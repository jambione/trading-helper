# Benchmarks — Excellence Loop Results

Append-only record. Every config change to the live engine should cite a row here.

---

## 2026-06-12 — A/B series #1 (3-indicator strategy, `tools/ab_bench.py`)

**Method:** pooled trades across ticker sets, 2mo of 1-min RTH bars (Alpaca IEX,
split-adjusted), signal-on-close → fill-next-open + adverse slippage,
end-of-data positions excluded. Anti-curve-fit: split-half robustness (edge
must hold on both halves of the ticker pool). 17 variants.
Raw CSVs: `benchmarks/ab_bench_2026-06-12*.csv`.

### Run 1 — Discord alert pool (11 microcaps, 25bps slip)
VSME EDHL PIII SLE WOK ORGN AUUD QUCY TOPS TWAV IONZ

| verdict | detail |
|---|---|
| ❌ ALL 17 variants negative | best: B2 −0.45%/trade; production config (SL1/TP2, exit=any): **−0.63%/trade, 19.7% WR, PF 0.33** |
| Stop-loss exits dominate | 73 of 127 baseline exits were the −1% stop; 0% of stop exits won |
| No config robust | both ticker halves negative everywhere |

**Conclusion: the 3-indicator entry has NO standalone edge on alert-pool
microcaps.** Caveat: IEX bars on these names are very sparse (some tickers
~300 bars in 2 months), which degrades indicator quality — but the direction
is consistent across 17 configs and both halves.

### Run 2 — Liquid pool (AAPL MSFT NVDA TSLA AMD META SPY QQQ, 5bps slip)

| verdict | detail |
|---|---|
| 🔑 `exit_mode=any` is the bug | 913/931 baseline exits = instant "reversal"; avg loss ≈ round-trip slippage. One of the 3 reversal conditions is ~always true in the backward window right after entry |
| ✅ `exit_mode=all` flips expectancy positive | C6 (all, SL1, no TP): **+0.31%/trade, PF 1.51, robust ✓** · C4 (all+sep1.5, SL1/TP2): **+0.17%/trade, 48.8% WR, PF 1.34, DD 9.3%, robust ✓** |

### Run 3 — Out-of-sample liquid pool (GOOG AMZN NFLX AVGO JPM XOM COST INTC, 5bps)

| verdict | detail |
|---|---|
| ⚠️ C-family edge shrinks to ~breakeven | C3 PF 0.99 · C4 PF 0.96 · C6 +0.28%/trade PF 1.40 but not split-half robust |
| `all` ≫ `any` confirmed everywhere | baseline PF 0.57 vs exit=all PF 0.98 on the same data |

### Run 4 — Momentum strategy excellence loop (`backtest_v2.py`, IONZ WOK QUCY, 2mo)

| verdict | detail |
|---|---|
| Baselines ~breakeven | PF 0.91–0.94, WR ~30% on train |
| One validated bright spot | IONZ held-out: hc=1, rsi<75, SL1/TP3.5, RVOL on → WR 48.1%, PF 1.94, DD 3.9% — single-ticker, treat as anecdote not edge |

## Changes applied from this series

1. `THREE_IND_EXIT_MODE=all` — beat `any` on every dataset tested (the single
   clearest result of the whole series)
2. `THREE_IND_REQUIRE_HOT=1` — catalyst gate: 3ind buys now require a mention
   burst; indicator-only entries lost in all 17 microcap configs
3. Mention bursts now archived to `benchmarks/mention_bursts.jsonl`
   (dashboard `_archive_burst`) — the missing dataset for replaying
   catalyst-gated entries in a future loop iteration
4. Env-loader bug fixed: inline ` # comments` on env values were being read
   into credentials (broke Alpaca auth in backtests; latent in the engine)

## Open items for the next loop iteration

- After ~2–4 weeks of burst archive + paper trades: replay catalyst-gated
  entries against bars fetched around each burst timestamp (the test we could
  not run today — no historical burst data existed)
- Paper-trade exit-reason breakdown (`paper_report.py --daily`) vs these
  backtest numbers — live fills on microcaps will be worse than 25bps on bad
  days; verify the gap
- If catalyst-gated trades remain negative in paper after ≥30 trades, the
  small-consistent-profit mission is better served on liquid names (C4-style
  config) than on the alert pool

---

## 2026-08-11 — Hybrid edge mode (AI Watch) + learning-loop ops

**Session postmortem (sim):** overbought-only arm + `left_overbought` off
(`exhaustion_scalp` arm gate, continuation-style exit). Fixture:
`tests/fixtures/sim_2026-08-11/`. Estimate: live −$117.60 → hybrid +$109.34.

**Forward-test ops (shipped 2026-08-12):**

1. Regime stamps on outcomes / trades / shadow / rejects:
   `edge_mode`, `exit_left_overbought`, `git_version`, `config_fp`, `paper`
   (`learn_stamps.py`).
2. EOD roll-up: `tools/daily_learn.py` → `ai_reports/daily/YYYY-MM-DD.{md,json}`
   + append `ai_reports/daily_ledger.jsonl`.
3. Watchdog: hourly `instrumentation_check` after `ai_watch_start_time`;
   once-daily `daily_learn` after 16:05 ET.

Read the ledger each morning: `tail ai_reports/daily_ledger.jsonl`.
One day remains a check, not a trend.

---

## 2026-08-15 — Replay tuner (`tools/replay_ab.py`)

Nightly (via `daily_learn` / watchdog) ranks a *declared* set of overlays on
the same shadow + outcome tape, by counterfactual session $:

| overlay | what it changes |
|---|---|
| `hybrid-exit` | overbought-only arm, `left_overbought` off (08-11 hypothesis) |
| `continuation` | heating\|overbought arm, `left_overbought` off |
| `flatten-vs-hold` | clock flatten vs hold to T1/stop/dead on remaining shadow |
| `heat-floor` | `heat_min` sweep — signal quality only, never a $ candidate |

Promote nothing automatically. `candidate` requires `min_n` (30) **and** the
same sign on both chronological halves. Anything smaller is a `hypothesis`.
The tool never writes `bot_config.json`.

Pinned 08-11 fixture still has to reproduce through `tools/sim_repro.py`.
On that tape the tuner must report hybrid-exit ≈ +$226.94 vs live and mark
it underpowered (n=14).

**Tape pack + overnight search:** `tools/desk_tape.py pack` freezes
shadow/outcomes/rejects (and optional trades / position_shadow) into
`ai_reports/tapes/<label>/`. Point `AI_REPORT_DIR` at that folder and every
existing sim can reuse the same rows. `replay_ab.py --search` walks the
declared grid in `tools/replay_experiments.json` (`search` key), writes one
jsonl line per cell, and ranks by held-out session $ when more than one day
is packed. Watchdog runs `pack --days 10` then `--search --days 10` after
`daily_learn`.

    venv/bin/python tools/desk_tape.py pack --days 10
    venv/bin/python tools/replay_ab.py --search --days 10
    venv/bin/python tools/replay_ab.py --day 2026-08-11
    venv/bin/python tools/sim_repro.py
