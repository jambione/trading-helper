# Trading Helper — Brasfield Momentum Tools

This repository contains the dashboard, signal scanner, indicator library,
and optional Alpaca execution helpers used to run the Brasfield momentum
workflow locally or on a server.

Quick links:
- Dashboard & API: `dashboard.py` (FastAPI + WebSocket)
- Signal engine: `signal_engine.py`
- Indicators: `signals.py`
- Alpaca execution: `alpaca_trader.py`
- Config: `config.py`, `config/bot_config.json`, `config/secrets.example.json`

Docs (see `docs/`)
- Onboarding guide: `docs/ONBOARDING.md`
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

Discord alert source (OCR — optional, macOS)
- An optional second ticker producer that reads trading-alert messages straight
  off the on-screen Discord window — no Discord login, no token, ToS-safe (it
  only reads pixels). It runs ALONGSIDE the audio transcriber and POSTs to the
  same `/api/tickers/add` seam, so all downstream behaviour is identical.
- Pieces: `discord_ocr.swift` → `discord_ocr` (Apple Vision OCR of the Discord
  window) and `discord_source.py` (polls the helper, parses alert lines, POSTs
  new mentions). Ticker validation is shared with the transcriber via
  `ticker_extract.is_valid_ticker`.
- Setup:
  1. Build the helper once: `swiftc discord_ocr.swift -o discord_ocr`
  2. Keep the Discord window with the alert channel visible (not minimized).
  3. Enable in `config/bot_config.json`: `"discord_ocr_enabled": true`
     (optional: `discord_ocr_poll_sec`, `discord_window_owner`,
     `discord_window_title`).
  4. `start_all.py` then launches it automatically (logs prefixed `[discord]`);
     or run it standalone with `python discord_source.py`.
  - First run prompts for Screen Recording permission (same as audio capture).

Tests & CI
- Add unit tests under `tests/` and enable CI. A secret-scan workflow
    and a pre-commit config are included to prevent accidental key commits.

If you want, I can update this README with deployment or Docker instructions.
