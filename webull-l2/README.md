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

## How signals work

- **Imbalance** = total bid size / total ask size across visible levels.
- **BUY**: imbalance ≥ `imbalance_buy` (1.8) for `confirm_reads` (3) consecutive reads, spread ≤ `max_spread_pct`, and no large ask wall sitting at the best ask.
- **SELL**: imbalance ≤ `imbalance_sell` (0.55) for 3 consecutive reads.
- **Walls**: any level ≥ `wall_multiple` (4×) the median size on its side.
- `alert_cooldown` (30s) prevents spam. Tune everything in `config.json`.

## Tuning tips

- Low-float movers like SKYQ: try `imbalance_buy` 2.0–2.5 and `confirm_reads` 4 to cut false positives from spoofed size.
- `poll_interval` 0.35s targets ~2–3 reads/sec; raise it if your CPU-usage is high or OCR misses climb.
- If the dashboard shows mostly misses: re-run `calibrate.py`, make sure Windows display scaling isn't blurring the region, and keep the L2 panel unobstructed.

## Adding the LLM later

The hook is stubbed at the bottom of `l2_signal.py`: a background thread sends recent `l2_log.csv` rows to claude-haiku every 45s for order-flow commen