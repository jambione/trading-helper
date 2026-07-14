# Webull L2 OCR Signal Monitor

Watches the Level-2 order book on your Webull screen via OCR and alerts you when buy/sell pressure conditions trigger. Signals are computed with pure math (milliseconds); nothing waits on an API.

## Setup (once)

1. Install Tesseract-OCR: https://github.com/UB-Mannheim/tesseract/wiki (default path `C:\Program Files\Tesseract-OCR` matches config.json).
2. `pip install -r requirements.txt`

## Region tracking

Default is `"region_mode": "auto"` — the script finds the Webull window by title, OCR-locates the L2 header row ("Size Bid ... Ask Size"), and anchors the capture region below it. If you move or resize Webull, it re-anchors automatically (also after 5 failed reads in a row). No calibration needed.

Fallback: set `"region_mode": "manual"` in config.json and run `python calibrate.py` to drag-select the region — useful if auto-locate ever struggles with your theme/font size. The manual region is also used if the Webull window can't be found.

## Run

```
python l2_signal.py
```

Live terminal dashboard shows bid/ask, spread, 10-level sizes, imbalance, and detected walls. On a signal you get a triple beep (high = BUY, low = SELL), a Windows toast, and a CSV entry in `l2_log.csv`.

## The one signal to watch: 5m CONFIDENCE

The big banner at the top is the whole point — everything in the table below is supporting detail. It reads e.g. `GO / HOLD LONG · confidence ●●● (3/3) · 2m03s`.

Confidence is **agreement of independent, hard-to-spoof evidence**, not the magnitude of any single meter. Three pillars each vote long / short / neutral:

- **trend** — the 5-minute robust drift (where price actually went)
- **tape** — 60s executed buy/sell dominance from Time&Sales (real money; can't be spoofed). Votes on a **volume-aware** gate: it needs at least `tape_min_sided` (4) prints with a real side AND the sided volume to be at least `tape_sided_share` (0.5) of the flow — so a few large clearly-sided prints vote (catching fast moves the old 8-print-count gate missed), while a tape that's mostly unsided noise abstains.
- **vwap** — price above / below the session VWAP (who controls the day)

The direction is the majority; the dots show how many agree. **3/3 = size up, 2/3 = normal, split or <2 = STAND ASIDE** no matter how extreme one meter looks. Hysteresis (`long_confirm_secs`) holds the stance so it doesn't flicker, and the small `trend ▲ tape ▲ vwap ▲` line under it shows the one-glance why.

Imbalance is deliberately **not** a confidence pillar — it's resting displayed size, the one input spoofers fake. It stays a detail row, not your focus signal.

## Time & Sales (the tape)

Keep the Webull **Time&Sales** widget visible next to the L2 panel. A background thread locates it, OCRs the prints (tolerating the merged columns OCR produces), and sides each print by color, then by the quote rule (at/above ask = buy, at/below bid = sell) and tick rule for the uncolored ones. That feeds the tape pillar, the `Tape 1m/5m` rows, the true volume-weighted `VWAP` row, and a tape veto in the playbook. Toggle with `ts_enabled`; tune `ts_poll` (0.6s).

## How signals work

- **Imbalance** = total bid size / total ask size across visible levels.
- **BUY**: imbalance ≥ `imbalance_buy` (1.8) for `confirm_reads` (3) consecutive reads, spread ≤ `max_spread_pct`, no large ask wall at the best ask, price not falling, **and the executed tape isn't selling** (`tape_gate_entries`, veto when 60s tape dominance ≤ −`tape_dom_min` with ≥8 prints). The tape veto is the spoof filter the old imbalance-only entry was missing — it's what produced 52 stop-outs in the paper log.
- **SELL**: imbalance ≤ `imbalance_sell` (0.55) for 3 consecutive reads.
- **Walls**: any level ≥ `wall_multiple` (4×) the median size on its side.
- `alert_cooldown` (30s) prevents spam. Tune everything in `config.json`.

## Symbol switching & re-measuring

- When you switch tickers in Webull, all per-symbol state (trend history, streaks, walls, tape, any virtual position) is wiped so the new stock starts clean. Symbol detection needs two consecutive readings to switch, so one OCR misread of the ticker can't nuke your history.
- `trades.csv` and `l2_log.csv` now carry a `symbol` column (old files are auto-archived as `*-old-<stamp>.csv` on the schema change). Run `python trade_stats.py` after a few sessions to see win rate / avg PnL by exit reason, day, and symbol — with the glitchy `|pnl|>10%` rows split out. Use `--all` to include archived files.

## 5m view integrity

The 5-minute trend/projection drive the confidence signal, bias, and playbook, so they're guarded:

- **Coverage gate**: the 5m trend shows `…` (with a `warming: X.Xm of 5m` note) until `trend_min_coverage` (60%) of the window actually has data. No more calling 40 seconds of history a "5-minute trend" — the playbook stays STAND ASIDE until the read is real.
- **Robust endpoints**: trend and projection use median mids over a band at each end of the window instead of two raw samples, so one surviving OCR misread can't swing them.
- **Glitch gate**: a frame whose mid jumps more than `glitch_jump_pct` (1.5%) from the recent median is held back unless `glitch_confirm` (2) consecutive reads land there. Real spikes pass one frame later; single-frame garbage never enters history. Dropped frames show as `glitch:N` in the title.
- **Reference-price veto** (`ref_price_gate`): the internal gate above can be fooled by a *sustained* misread — two bad frames in a row make it rebase onto the garbage level, which is how the 139→6 and 6.81→23.74 rows entered the old paper log. So each frame is also checked against the real last-trade price from the Finnhub stream: if the OCR mid disagrees by more than `glitch_ref_tol_pct` (15%), the frame is vetoed outright. The reference is only used when it's a *fresh* print (< `ref_max_age`, 15s), so it can never fight a genuine fast move, and the whole check is a no-op when there's no Finnhub key or `websockets`. Needs the key via `FINNHUB_API_KEY` env var or `finnhub_key` in config.json.
- **5m sparkline**: the price row charts median mids per time bucket across the whole trend window (the shape you're trading), not just the last ~15 seconds of reads.
- **Frame skip**: when the captured panel's pixels are byte-identical to the previous poll, Tesseract is skipped and the last parse is re-stamped — a large CPU saving on quiet tape, which keeps the effective read rate up.

## Measuring the signal (does it actually work?)

`score_confidence.py` grades the monitor's calls against what price did next, on your own logged data — so "how much can I trust this" becomes a number instead of a vibe.

```
python score_confidence.py           # scores l2_log.csv
python score_confidence.py --all     # include archived l2_log-old-*.csv
python score_confidence.py --stride 20 --horizons 60,120,300
```

For every logged moment it measures the forward return at +1/+2/+5 min and reports, per predictor, the **hit-rate** (share of calls price moved the called way; 50% = coin flip) and **median edge** (median of return × direction; >0 = real forward edge). Hit-rate and median are used as the headline because they shrug off the OCR glitches that wreck a mean.

What it scores:

- **5m BANNER stance** — the real three-pillar confidence call. Only appears once stance logging is on (the CSV now carries `stance,agree,total,tape_live`); older logs don't have it, so it's skipped until you've run a session with the new build.
- **trend pillar** — reconstructed from the logged mid series exactly as `SignalEngine.trend_pct` computes it. The one pillar recoverable from price history alone.
- **imbalance** and the **logged BUY/SELL signal** — for comparison (imbalance having ~no forward edge is *why* it's kept out of the confidence signal).

The glitch count it prints (`[N glitchy]`, moves > 25% in the window) is itself a health check: a high count means the OCR feed is dirty and the reference-price veto above should be doing more work.

## Tuning tips

- Low-float movers like SKYQ: try `imbalance_buy` 2.0–2.5 and `confirm_reads` 4 to cut false positives from spoofed size.
- `poll_interval` 0.35s targets ~2–3 reads/sec; raise it if your CPU-usage is high or OCR misses climb.
- If the dashboard shows mostly misses: re-run `calibrate.py`, make sure Windows display scaling isn't blurring the region, and keep the L2 panel unobstructed.

## Adding the LLM later

The hook is stubbed at the bottom of `l2_signal.py`: a background thread sends recent `l2_log.csv` rows to claude-haiku every 45s for order-flow commen