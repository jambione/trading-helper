# Trading Helper — Brasfield Momentum Tools

This repository contains the dashboard, signal scanner, indicator library,
and optional Alpaca execution helpers used to run the Brasfield momentum
workflow locally or on a server.

Quick links:
- Dashboard & API: `dashboard.py` (FastAPI + WebSocket)
- Signal engine: `signal_engine.py`
- Indicators: `signals.py`
- Alpaca execution: `alpaca_trader.py`
- Swing screener: `swing_screener.py`
- Relative-strength screener: `rs_screener.py` (+ `rs_core.py`, `rs_cache.py`, `rs_fetch.py`)
- Config: `config.py`, `config/bot_config.json`, `config/secrets.example.json`

Docs (see `docs/`)
- Onboarding guide: `docs/ONBOARDING.md`
- Relative strength (formula, free-tier limits): `docs/RELATIVE_STRENGTH.md`
- Ticker recognition notes: `docs/TICKER_RECOGNITION_SUMMARY.md`,
  `docs/TICKER_RECOGNITION_IMPROVEMENTS.md`, `docs/CODX_FIX_SUMMARY.md`

Quickstart (local)
1. Create a virtualenv and install deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Copy secrets example and edit keys:

```bash
cp config/secrets.example.json config/secrets.json
# edit config/secrets.json (do NOT commit this file)
```

3. Run the dashboard (opens browser):

```bash
python dashboard.py
```

Then open http://localhost:8888 in your browser.

Security & secrets
- `secrets.json` MUST NOT be committed. This repo already includes
    `.gitignore` entry for `secrets.json`. If you accidentally committed
    keys, rotate them immediately.
- Use `secrets.example.json` as the template for required keys.

Recommended next steps
- Install the pre-commit hooks: `pip install pre-commit && pre-commit install`
- Enable the GitHub workflow secret-scan (provided in `.github/workflows/`)

Configuration
- Edit `bot_config.json` or use environment variables. See `config.py` for
    default keys and `SAFE_CONFIG_KEYS` that can be changed from the dashboard.

Discord alert source (OCR — macOS)
- The primary ticker producer. Reads trading-alert messages off the on-screen
  Discord window using Apple Vision OCR — no Discord login, no token, ToS-safe.
- Pieces: `discord_ocr.swift` → `discord_ocr` (compiled Swift binary) and
  `discord_source.py` (polls the binary, parses alert lines, POSTs new mentions).
- Setup:
  1. Build the binary once: `bash scripts/build_ocr.sh`
  2. Keep the Discord alert channel window visible (not minimized).
  3. Enable in `config/bot_config.json`: `"discord_ocr_enabled": true`
  4. `start_all.py` launches it automatically (logs prefixed `[discord]`).
  - First run prompts for Screen Recording permission — grant it.

TradingView webhook (second signal source)
- A Pine Script squeeze indicator (`scripts/brasfield_squeeze_alert.pine`) fires
  a webhook to `/api/tradingview/webhook` when a squeeze releases on the chart.
- Independent of Discord — two sources confirming the same ticker is a stronger signal.
- Set `tv_webhook_secret` in `config/bot_config.json` and point the TV alert at
  `https://trading.jbrasfield.com/api/tradingview/webhook?secret=<your_secret>`.
- See `docs/ONBOARDING.md` §6 for the full setup walkthrough.

Tests & CI
- Add unit tests under `tests/` and enable CI. A secret-scan workflow
    and a pre-commit config are included to prevent accidental key commits.

If you want, I can update this README with deployment or Docker instructions.
