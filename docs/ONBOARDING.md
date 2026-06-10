# Trading Helper — New Machine Setup Guide

This guide walks through setting up the Brasfield momentum trading dashboard on
a fresh macOS machine from scratch.

> **macOS only.** The Discord OCR source uses ScreenCaptureKit and Apple Vision —
> both macOS-only frameworks. The rest of the stack (dashboard, signal engine,
> Alpaca, Finnhub) runs on any platform, but the full pipeline requires a Mac.

---

## What the system does

Three processes run together and talk to each other:

| Process | File | Role |
|---|---|---|
| **Dashboard** | `dashboard.py` | FastAPI server — prices, signals, WebSocket UI, REST API |
| **Signal engine** | `signal_engine.py` | Scans watchlist tickers for multi-indicator setups |
| **Discord OCR** | `discord_source.py` | Screenshots Discord every 2.5 s, OCRs alert messages, POSTs tickers |

A fourth source feeds the dashboard from outside the machine:

| Source | Direction | Role |
|---|---|---|
| **TradingView webhook** | TV → dashboard | Pine Script squeeze indicator fires a burst alert when a setup confirms |

Everything flows into the same mention-tracking and burst-detection pipeline.
When a ticker appears in both the Discord OCR feed and the TradingView feed
within a short window, that is your highest-confidence signal.

---

## Prerequisites

| Requirement | Version | Install |
|---|---|---|
| macOS | 12.3 Monterey or later | — |
| Xcode Command Line Tools | latest | `xcode-select --install` |
| Python | 3.12 | `brew install python@3.12` |
| Homebrew | latest | [brew.sh](https://brew.sh) |
| cloudflared | latest | `brew install cloudflare/cloudflare/cloudflared` |
| Git | any | included with Xcode CLT |

---

## 1. Clone the repository

```bash
git clone https://github.com/jambione/trading-helper.git
cd trading-helper
git checkout ocr-only-source
```

---

## 2. Create the virtual environment

The startup scripts expect a `venv/` directory at the repo root.

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Verify the install:

```bash
python -c "import fastapi, uvicorn, pandas, alpaca; print('OK')"
```

---

## 3. Create your secrets file

```bash
cp secrets.example.json secrets.json
```

Edit `secrets.json` and fill in your keys:

```json
{
  "api_key":            "your-alpaca-api-key",
  "secret_key":         "your-alpaca-secret-key",
  "finnhub_key":        "your-finnhub-api-key",
  "require_auth":       false,
  "dashboard_user":     "admin",
  "dashboard_pass":     "changeme",
  "jwt_secret":         "replace-with-a-long-random-string",
  "push_contact_email": "mailto:you@example.com"
}
```

**Where to get the keys:**

| Key | Source | Notes |
|---|---|---|
| `api_key` / `secret_key` | [alpaca.markets](https://alpaca.markets) | Free paper account works |
| `finnhub_key` | [finnhub.io](https://finnhub.io) | Free tier works |
| `jwt_secret` | any random string | Run `openssl rand -hex 32` to generate one |
| `push_contact_email` | your own email | Used in VAPID push claims — format `mailto:you@example.com` |

The VAPID key pair (`push_vapid_public_key` / `push_vapid_private_key`) is
**generated automatically** on first startup and written back into
`secrets.json`. You do not need to create them manually.

> `secrets.json` is in `.gitignore` and must never be committed.
> Set `require_auth: true` before exposing the dashboard via Cloudflare Tunnel.

---

## 4. Build the Discord OCR binary

The Discord alert source uses a native Swift helper that screenshots the Discord
window and runs Apple Vision OCR. Compile it once:

```bash
bash scripts/build_ocr.sh
```

This produces `discord_ocr` in the repo root. The script is idempotent — it
skips recompilation if the binary is already up-to-date.

If you update `discord_ocr.swift` later, re-run the script. The dashboard will
warn you at startup if the source file is newer than the binary.

---

## 5. Configure Discord

The OCR source reads alerts from the Discord window on your screen. It never
logs into Discord or touches Discord's servers — it only reads pixels.

**Before starting the server each day:**

1. Open Discord and navigate to your trading alert channel
2. Keep the window visible on your primary display (not minimized, not on a
   hidden Space)
3. The OCR polls every 2.5 seconds; any alert line containing `>>>>>` or
   `close over` will be parsed and sent to the dashboard

**Config keys** (in `bot_config.json`):

| Key | Default | Description |
|---|---|---|
| `discord_ocr_enabled` | `true` | Set to `false` to disable without stopping the server |
| `discord_ocr_poll_sec` | `2.5` | Seconds between OCR captures |
| `discord_window_owner` | `"Discord"` | App name to capture |
| `discord_window_title` | `""` | Optional window title substring filter |

**Grant Screen Recording permission** the first time you run:
System Settings → Privacy & Security → Screen Recording → enable your terminal
or launcher app.

**Test it without starting the full server:**

```bash
source venv/bin/activate
python discord_source.py --check
```

Expected output when working:

```
[discord] OCR read 34 text line(s) from the Discord window.
[discord] parsed 3 alert(s):
   NVDA   mention  <=  NVDA Price Volatility Spike! >>>>> 1 Minute High...
[discord] VERDICT: ✓ working
```

---

## 6. Configure the TradingView webhook

TradingView is a second independent squeeze detector. When a Pine Script alert
fires, TradingView POSTs to the dashboard — confirming the same setup from a
completely different source.

### 6a. Set a webhook secret

Open `bot_config.json` and set a secret token:

```json
"tv_webhook_secret": "your-secret-here"
```

Use any string. This prevents random internet traffic from injecting fake alerts
into your public endpoint.

### 6b. Add the Pine Script to TradingView

1. Open TradingView → Pine Editor (bottom toolbar)
2. Paste the contents of `scripts/brasfield_squeeze_alert.pine`
3. Click **Save** then **Add to chart**

### 6c. Create the TradingView alert

1. Right-click the indicator pane → **Add Alert on Brasfield Squeeze Alert**
2. Condition: **Squeeze Fire Long**
3. Trigger: **Once Per Bar Close**
4. Enable **Webhook URL**, paste:
   ```
   https://trading.jbrasfield.com/api/tradingview/webhook?secret=your-secret-here
   ```
5. In the **Message** box, paste exactly:
   ```json
   {"ticker":"{{ticker}}","close":{{close}},"interval":"{{interval}}"}
   ```
6. Click **Create**

Repeat for each chart or watchlist you want to monitor.

**Test the endpoint** with the server running:

```bash
curl -X POST "https://trading.jbrasfield.com/api/tradingview/webhook?secret=your-secret-here" \
  -H "Content-Type: application/json" \
  -d '{"ticker":"NVDA","close":950.00,"interval":"5"}'
```

You should see a burst toast fire in the dashboard immediately.

---

## 7. Cloudflare Tunnel

The tunnel makes the dashboard reachable at `https://trading.jbrasfield.com`
so TradingView webhooks can reach it and you can access the UI from any device.

### First-time setup on a new machine

The tunnel is already created and DNS-configured in Cloudflare. You just need to
authenticate `cloudflared` and place the credentials file.

```bash
cloudflared tunnel login
```

This opens a browser. Authorise the `jambione` Cloudflare account. The
credentials file is saved to `~/.cloudflared/`.

Then update `cloudflared-config.yml` so the path matches your home
directory (the default has `/Users/jonathanbrasfield/`):

```yaml
credentials-file: /Users/YOUR_USERNAME/.cloudflared/56c84116-0ef0-47c7-bbea-25634d765487.json
```

### Start the tunnel manually

```bash
bash scripts/restart_cloudflare.sh
```

Or it starts automatically with `scripts/run_trading_server.sh` (see §8).

### Enable login before going public

Set `"require_auth": true` in `secrets.json` before the tunnel goes live.
Anyone with the tunnel URL can reach the login page — only valid credentials get
through.

---

## 8. Starting and stopping the server

### Quick start (manual)

```bash
source venv/bin/activate
python start_all.py
```

Launches dashboard, signal engine, and Discord OCR source together.
Press `Ctrl+C` to stop all three.

### Full startup with tunnel and caffeinate (recommended for trading days)

```bash
bash scripts/run_trading_server.sh
```

This script:
- Creates / repairs the venv automatically
- Starts the Cloudflare tunnel
- Runs `caffeinate -si` to keep the Mac awake on AC power
- Launches `start_all.py`
- Tears the tunnel down cleanly when the server exits

> **Update the hardcoded path** in `scripts/run_trading_server.sh` if your repo
> is not at `/Users/jonathanbrasfield/repo/trading-helper/trading-helper`:
> ```bash
> REPO="/Users/YOUR_USERNAME/path/to/trading-helper"
> ```
> Same change is needed in `scripts/evening_stop.sh`.

### Automated startup via launchd (optional)

The repo includes a launchd plist to start the server automatically at 4:00 AM
on weekdays:

```bash
cp com.trading.helper.start.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.trading.helper.start.plist
```

Review and update all paths in the plist file before loading it.

### Evening shutdown

```bash
bash scripts/evening_stop.sh
```

Quits Discord, then kills the server, Discord OCR source, signal engine,
dashboard, caffeinate, and cloudflared in the correct order.

---

## 9. Mobile push notifications (iPhone / Android)

The dashboard is a Progressive Web App (PWA). When installed to the home screen
it can deliver burst-alert notifications to your lock screen even when the screen
is off or the app is closed.

### 9a. Install to iPhone home screen (required for iOS push)

iOS push only works from a PWA installed via Safari — it does **not** work from
a browser tab.

1. Open `https://trading.jbrasfield.com` in **Safari** (not Chrome)
2. Tap the **Share** button (box with arrow) → **Add to Home Screen**
3. Tap **Add** — the dashboard icon appears on your home screen

### 9b. Grant notification permission

1. Open the dashboard from the **home screen icon** (not from Safari)
2. Tap the **bell icon** in the top-right header
3. iOS will prompt: *"trading.jbrasfield.com" Would Like to Send You Notifications* → tap **Allow**

The bell turns solid when permission is granted. Burst alerts will now arrive as
OS notifications with sound, even with the phone locked.

### 9c. Android

On Android, Chrome is fine — no PWA install required for push. Open the
dashboard, tap the bell icon, and allow notifications when prompted.

### 9d. Verify push is working

With notification permission granted, trigger a test burst from the terminal
while the browser tab is in the background:

```bash
# With the server running, inject 5 rapid mentions of a ticker
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8888/api/mention \
    -H "Content-Type: application/json" \
    -d '{"ticker":"NVDA"}' > /dev/null
done
```

You should receive a lock-screen notification within a few seconds.

---

## 10. Verifying everything is running

Open `http://localhost:8888` (or `https://trading.jbrasfield.com` remotely).

Check the source status indicators in the dashboard header:

| Indicator | Green means |
|---|---|
| **Discord OCR** | `discord_source.py` has polled within the last 15 seconds |
| **TradingView** | At least one webhook has been received this session |

Check the terminal output for these lines at startup:

```
[dashboard] Signal Scanner  —  http://localhost:8888
[engine]    Signal engine started
[discord]   OCR source started — polling every 2.5s
```

---

## 11. Dashboard layout

```
┌─────────────────┬──────────────────┬──────────────────────────┐
│  Discord Feed   │    Watchlist     │    TradingView Chart     │
│  + TV Alerts    │  (center panel)  │    (fills remaining)     │
└─────────────────┴──────────────────┴──────────────────────────┘
```

**Watchlist row colors:**

| Color | Meaning |
|---|---|
| Green tint | BUY — all signal conditions met |
| Blue tint | ON_DECK — setup building |
| Amber border | Mentioned in the last 30 seconds |

**Alert feed** (left panel) shows Discord OCR alerts and TradingView squeeze
alerts in a unified chronological feed. Squeeze bursts are marked 🔥.

Click any watchlist row to load that ticker in the TradingView chart panel.

---

## 12. Configuration reference

All non-secret settings live in `bot_config.json` and can be changed
from the dashboard Settings drawer without restarting.

**Key settings for the OCR + webhook pipeline:**

| Key | Default | Description |
|---|---|---|
| `discord_ocr_enabled` | `true` | Enable/disable the Discord OCR source |
| `discord_ocr_poll_sec` | `2.5` | Polling interval in seconds |
| `tv_webhook_secret` | `""` | Secret token for the TradingView webhook endpoint |
| `mention_alert_threshold` | `5` | Mentions needed to trigger a burst toast |
| `mention_alert_window` | `10` | Rolling window in seconds for burst detection |

---

## 13. Troubleshooting

**`[discord] OCR failed: no on-screen window found`**
Discord is closed, minimized, or on a different Space. Bring the Discord alert
channel window to your main display.

**Discord OCR source backing off / stops polling**
Consecutive OCR failures trigger exponential backoff (up to 60 s). Once Discord
is visible again it resumes automatically.

**TradingView webhook returning 401**
The secret in the webhook URL doesn't match `tv_webhook_secret` in
`bot_config.json`. They must be identical.

**No prices in the watchlist**
Confirm Finnhub and Alpaca keys are saved in Settings → API Keys. Check the
terminal for `[STARTUP] Finnhub stream started` and `[STARTUP] Alpaca connected`.

**Dashboard unreachable at `trading.jbrasfield.com`**
Run `bash scripts/restart_cloudflare.sh`. Check `tunnel.log` for errors.
Confirm cloudflared is authenticated: `cloudflared tunnel list`.

**`discord_ocr` binary not found or stale**
Run `bash scripts/build_ocr.sh`. Requires Xcode Command Line Tools.

**WebSocket shows disconnected (red dot in UI)**
- Local: confirm `dashboard.py` is still running
- Remote: confirm `cloudflared` is running and the tunnel is healthy

**Watchlist not updating**
Validate the file:
```bash
python -c "import json; json.load(open('transcription/wb_watchlist.json'))"
```

**No push notifications reaching the phone**
- Confirm `push_contact_email` is set in `secrets.json` (not the placeholder value)
- On iPhone: the dashboard must be opened from the **home screen icon**, not Safari
- Check that the bell icon is solid (permission granted), not outlined
- Confirm the browser/PWA has notification permission in iOS Settings → Notifications

**Push subscription not persisting across server restarts**
Check that `push_subscriptions.json` exists and is valid JSON. If corrupted,
delete it — the browser will re-subscribe on the next bell-icon tap.

---

## 14. File reference

| File | Purpose |
|---|---|
| `dashboard.py` | FastAPI backend — all REST endpoints, WebSocket, state |
| `signal_engine.py` | Scans watchlist tickers, writes signal state |
| `discord_source.py` | Discord OCR poller — screenshots window, parses alerts |
| `discord_ocr.swift` | Swift/Vision OCR helper (compiled to `discord_ocr`) |
| `transcription/ticker_extract.py` | NASDAQ/NYSE ticker validation, company-name expansion |
| `transcription/workflows.py` | Webull + TradingView GUI automation |
| `bot_config.json` | Non-secret runtime settings |
| `secrets.json` | API keys and auth credentials (**not in git**) |
| `secrets.example.json` | Template for `secrets.json` |
| `push_subscriptions.json` | Browser push subscriptions (auto-created, **not in git**) |
| `cloudflared-config.yml` | Cloudflare tunnel configuration |
| `scripts/build_ocr.sh` | Compile `discord_ocr.swift` → `discord_ocr` |
| `scripts/brasfield_squeeze_alert.pine` | TradingView Pine Script squeeze indicator |
| `scripts/run_trading_server.sh` | Full startup: venv + tunnel + caffeinate + server |
| `scripts/restart_cloudflare.sh` | Kill and restart the Cloudflare tunnel |
| `scripts/evening_stop.sh` | Clean shutdown of the full trading session |
| `start_all.py` | Launch dashboard + signal engine + Discord OCR |
| `stop_all.py` | Graceful shutdown of all processes |
| `dashboard.html` | Main dashboard SPA |
| `login.html` | Login page |
| `static/` | Frontend JS, CSS, assets |
| `static/sw.js` | Service worker — handles background push events |
| `tests/` | Test suite — `python -m pytest tests/ -q` |
