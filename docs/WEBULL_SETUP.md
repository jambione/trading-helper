# Webull OpenAPI setup (Mobile Trader bridge)

How to switch the bridge from the mock simulator to your real Webull
account.

## 1. Prerequisites

- **App key + secret** from https://developer.webull.com (Individual
  application; review takes ~1–2 business days). You have these.
- **OpenAPI Advanced Quotes subscription** — required for Level-2 depth
  (`get_quotes` with `depth=10`). Subscribe on the Webull *Technology*
  website under your avatar → Advanced Quotes → **OpenAPI** Advanced
  Quotes. ⚠️ An Advanced Quotes / L2 subscription bought in the Webull
  app or desktop does **not** apply to OpenAPI — it's a separate,
  OpenAPI-specific subscription.

## 2. Install the SDK

```sh
venv/bin/pip install webull-openapi-python-sdk
```

## 3. Configure credentials

Never commit keys. Either export env vars where the dashboard runs:

```sh
export WEBULL_APP_KEY="..."
export WEBULL_APP_SECRET="..."
# optional — auto-detected from your account list if omitted:
export WEBULL_ACCOUNT_ID="..."
```

or put them in `config/webull_bridge.json` (git-ignored `config/` dir):

```json
{
  "webull_app_key": "...",
  "webull_app_secret": "...",
  "webull_account_id": ""
}
```

Note: the SDK stores a 2FA token under `conf/token.txt` in the working
directory (override with `WEBULL_OPENAPI_TOKEN_DIR`).

## 4. Smoke test (read-only — places no orders)

```sh
venv/bin/python scripts/webull_smoke.py AAPL
```

This prints your account list, balance, positions, and a depth-10 quote,
then confirms the depth payload parses into the L2 engine's book format.
If any section errors or the parse step fails, fix that before flipping
the provider (the raw JSON in the output shows what the parser saw).

Common issues:
- `get_quotes` returns an error / no levels → the OpenAPI Advanced
  Quotes subscription isn't active (step 1).
- Empty account list → the app key wasn't approved for trading, or the
  region is wrong (`WEBULL_REGION`, default `us`).

## 5. Flip the provider

In `config/webull_bridge.json`:

```json
{
  "provider": "webull",
  "max_order_value": 1000
}
```

Restart the dashboard. `GET /api/l2/watchlist` now reports
`"provider": "webull"`, and the iPhone app's Settings screen shows
**WEBULL** instead of MOCK.

## 6. Safety rails

- `max_order_value` (config) — server rejects any order whose notional
  exceeds this. Start small.
- The app's Buy is a **LIMIT at the ask** (no chasing); Sell is 100% of
  the position. Both are hold-to-confirm in the UI.
- Orders are `time_in_force: DAY`, regular trading session only
  (`support_trading_session: "N"`).
- First live test: market hours, a cheap liquid symbol, buy amount ~$20,
  then verify the fill in the Webull app before trusting it with size.

## Tuning

- `webull_poll_sec` (default 0.5) — depth poll interval. The stance
  engine was tuned for ~2–3 reads/sec on the OCR monitor; raise this if
  you hit API rate limits (watch the dashboard log for HTTP 429).
- `webull_depth_levels` (default 10) — matches the l2_core math, which
  medians sizes across visible levels.
