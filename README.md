# Trading Helper — Brasfield Momentum Tools

This repository contains the dashboard, signal scanner, indicator library,
and optional Alpaca execution helpers used to run the Brasfield momentum
workflow locally or on a server.

Quick links:
- Dashboard & API: `dashboard.py` (FastAPI + WebSocket)
- Signal engine: `signal_engine.py`
- Indicators: `signals.py`
- Alpaca execution: `alpaca_trader.py`
- Config: `config.py`, `bot_config.json`, `secrets.example.json`

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
cp secrets.example.json secrets.json
# edit secrets.json (do NOT commit this file)
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

Tests & CI
- Add unit tests under `tests/` and enable CI. A secret-scan workflow
    and a pre-commit config are included to prevent accidental key commits.

If you want, I can update this README with deployment or Docker instructions.
