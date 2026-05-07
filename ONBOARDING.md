# Signal Scanner — Onboarding Guide

## What it is

Signal Scanner is a real-time trading dashboard that runs on your local machine. It monitors a watchlist of stock tickers, calculates multi-indicator signals (Williams %R, CM RSI-2, OBV), streams live prices, and transcribes financial audio to detect tickers mentioned on air.

The app has two distinct parts:

| Part | Where it runs | What it does |
|------|--------------|--------------|
| **Backend** | Your local machine | Price feeds, signal calculation, watchlist file, transcription |
| **Frontend** | Browser (local or hosted) | Dashboard UI, TradingView chart, login |

Both parts currently live in this repo. The goal is to eventually host the frontend on a service like Vercel while the backend continues to run locally — connected via Cloudflare Tunnel.

---

## Prerequisites

- Python 3.11+
- A modern browser (Chrome / Edge recommended for TradingView)
- API keys:
  - **Alpaca** — for bar data and price fallback (free paper account works)
  - **Finnhub** — for real-time WebSocket price stream (free tier works)

---

## Installation

```bash
# 1. Clone / navigate to the project folder
cd trading-helper

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Mac / Linux
.venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## First-time Setup

### 1. Create your secrets file

Copy the example and fill in your values:

```bash
cp secrets.example.json secrets.json
```

Edit `secrets.json`:

```json
{
  "api_key":     "your-alpaca-api-key",
  "secret_key":  "your-alpaca-secret-key",
  "finnhub_key": "your-finnhub-api-key"
}
```

> `secrets.json` is never committed to git. Keep it out of any repo you push to.

**Login is disabled by default.** You can open `http://localhost:8888` without a password. No `require_auth`, `dashboard_user`, `dashboard_pass`, or `jwt_secret` fields are needed for local use.

### 2. Start the server

```bash
python dashboard.py
```

The terminal will print:

```
Signal Scanner  —  http://localhost:8888
Ctrl+C to stop
```

### 3. Open the dashboard

Open `http://localhost:8888` in your browser. The dashboard loads directly — no login required when running locally.

---

## Dashboard Layout

The dashboard is a three-column layout:

```
┌─────────────────┬──────────────────┬──────────────────────────────┐
│  Live Transcript│    Watchlist     │      TradingView Chart        │
│  (left, fixed)  │  (center, resize)│      (right, fills space)    │
└─────────────────┴──────────────────┴──────────────────────────────┘
```

Drag the handle between the watchlist and chart to resize. The widths are saved to localStorage.

### Left — Live Transcript

Captures real-time speech from a selected audio device (loopback or microphone). As tickers are spoken, they are:

- Highlighted in bold in the transcript
- Automatically added to the watchlist
- Highlighted in the watchlist row with an amber border

**Controls**
- **Start / Stop Transcription** — toggle the speech capture subprocess
- **Clear** — wipe the in-memory transcript (does not affect the watchlist)
- Device selection is in **Settings → Audio**

### Center — Watchlist

Live table of tickers being tracked. Sorted by: recently mentioned → signal strength → price.

| Column | Meaning |
|--------|---------|
| Ticker | Symbol |
| Price | Last trade price (Finnhub WebSocket, Alpaca fallback) |
| Chg% | Day change percentage from open |
| Vol | Day volume (green = high relative volume) |
| Add | Opens Webull + TradingView workflow |
| ✕ | Removes ticker from watchlist |

**Row colors**

| Color | Signal status | Meaning |
|-------|--------------|---------|
| Green tint | BUY | All signal conditions met |
| Blue tint | ON_DECK | Setup building (oversold streak ≥ min bars) |
| Amber left border | — | Spoken in the last 30 seconds |

Click any row to load that ticker in the TradingView chart.

**Adding tickers manually** — type a symbol in the input box at the top of the watchlist and press Enter or click `+`.

### Right — TradingView Chart

Loads a TradingView widget for the selected ticker. Uses your saved chart layout if you configure one in **Settings → Data → TradingView Chart Layout URL**.

---

## The Watchlist File

Tickers are persisted to `transcription/wb_watchlist.json` — a JSON array of timestamped objects:

```json
[
  {"ticker": "AAPL", "added": "2026-05-07T09:30:00-04:00"},
  {"ticker": "NVDA", "added": "2026-05-07T09:31:15-04:00"}
]
```

**Auto-purge:** entries whose `added` timestamp is 15 minutes old or older are automatically removed from the file the next time the backend reads it (within 100 ms of expiry).

**Adding entries from external tools:** write an object with `ticker` and `added` (ISO 8601 with timezone). The old plain-string format is still accepted and migrated automatically on first read — those entries are treated as just-added and will expire 15 minutes later.

The backend polls this file every 100 ms by file mtime and broadcasts changes over the WebSocket. You can edit it directly from any text editor or script and the dashboard will update within a fraction of a second.

This is the primary integration point for the local → hosted split: your local tooling writes to this file; the dashboard reads from it in real time.

---

## Signal Logic

Every ticker on the watchlist is evaluated against three indicators:

| Indicator | Trigger condition |
|-----------|------------------|
| **Williams %R (fast/slow)** | Fast %R < oversold threshold for N consecutive bars |
| **CM RSI-2** (Larry Connors) | RSI(2) < oversold level AND price > 200-SMA AND price < 5-SMA |
| **OBV Oscillator** | On-balance volume trending up with a volume surge |

**Signal statuses** (shown as row color and badge):

| Status | Condition |
|--------|-----------|
| `BUY` | All conditions met |
| `ON_DECK` | Oversold streak ≥ min bars |
| `WARMING` | Fast %R < −60 (approaching zone) |
| `COLD` | Far from oversold |

---

## Settings Reference

Open settings with the **⚙ Settings** button in the header.

### API Keys tab

| Field | Description |
|-------|-------------|
| Alpaca API Key | Paper or live Alpaca key |
| Alpaca Secret Key | Corresponding secret |
| Finnhub API Key | Free key from finnhub.io |

### Data tab

| Field | Description |
|-------|-------------|
| Trading Strategy | `Multiple Oversold` (recommended) or legacy weighted scoring |
| TradingView Chart Layout URL | Paste a saved layout URL to load your indicators automatically |
| Bar Timeframe | Candle size for signal calculation (1 Min default) |
| Bar Count | Number of bars to fetch (300 default) |

### Signals tab

Fine-tune indicator thresholds. Defaults work well for intraday momentum setups.

| Group | Key fields |
|-------|-----------|
| %R Trend Exhaustion | OB Zone Edge (default 20), Min Bars in Zone (default 2) |
| OBV Oscillator | EMA Length, Volume Surge multiplier |
| CM RSI-2 | RSI Period (2), Oversold Level (25) |
| MACD | Fast / Slow / Signal periods |

### Audio tab

Select the audio input device for transcription. Devices marked `⟳ LOOPBACK` capture system audio (TV/web streams playing through your speakers) — this is the recommended mode for transcribing financial media.

### Connection tab

| Field | Description |
|-------|-------------|
| Backend URL | Leave empty when running locally. Set to your Cloudflare tunnel URL when the frontend is hosted remotely. |
| Sign Out | Clears your session token and returns to the login page. |

---

## Remote Access Setup (Cloudflare Tunnel)

This section covers how to make the dashboard accessible from anywhere while keeping all data processing local.

> **Enable login before exposing the backend publicly.** Add `"require_auth": true` (plus `dashboard_user`, `dashboard_pass`, and `jwt_secret`) to `secrets.json` before starting `cloudflared`.

### Step 1 — Install cloudflared

```bash
# Mac
brew install cloudflare/cloudflare/cloudflared

# Windows (winget)
winget install Cloudflare.cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/
```

### Step 2 — Start a quick tunnel

```bash
cloudflared tunnel --url http://localhost:8888
```

You will see output like:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-words-here.trycloudflare.com                                               |
+--------------------------------------------------------------------------------------------+
```

Copy that URL. It is your public backend URL.

> For a permanent URL instead of a random one, create a named tunnel with `cloudflared tunnel create <name>` and configure DNS routing. See the [Cloudflare docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

### Step 3 — Log in from anywhere

On any device, open `https://random-words-here.trycloudflare.com` in a browser.

You will see the login page. Enter:
- **Backend URL**: `https://random-words-here.trycloudflare.com`
- Your username and password from `secrets.json`

The dashboard will connect over the tunnel, streaming your local data to the remote browser.

---

## Hosting the Frontend (Optional)

If you want the dashboard HTML/CSS/JS hosted on a static platform (Vercel, Netlify, GitHub Pages) instead of served by the local backend:

1. Deploy the following files to your hosting service:
   - `dashboard.html`
   - `login.html`
   - `static/`

2. Visit your hosted URL (e.g. `https://my-dashboard.vercel.app`)

3. The login page will appear. Set:
   - **Backend URL**: your Cloudflare tunnel URL
   - Username and password as usual

All API calls and the WebSocket will go to your tunnel URL. The frontend is stateless — it has no backend of its own.

---

## Credentials and Security

Login is **disabled by default**. The dashboard is open to anyone who can reach the server.

To enable login (required before exposing the backend via Cloudflare Tunnel):

```json
{
  "require_auth":   true,
  "dashboard_user": "yourname",
  "dashboard_pass": "a-strong-password",
  "jwt_secret":     "a-long-random-string-at-least-32-chars"
}
```

Or use environment variables: `REQUIRE_AUTH=true`, `DASHBOARD_USER`, `DASHBOARD_PASS`, `JWT_SECRET`.

| What | Where | Notes |
|------|-------|-------|
| Auth on/off | `secrets.json` key `require_auth` | `false` = no login (local default). `true` = login required. |
| Dashboard login | `secrets.json` keys `dashboard_user` / `dashboard_pass` | Only used when `require_auth` is `true` |
| JWT signing secret | `secrets.json` key `jwt_secret` | Use a random 32+ character string. Rotating this invalidates all active sessions. |
| Alpaca / Finnhub keys | `secrets.json` (same file) | Never committed to git |
| Session token | Browser localStorage | Auto-expires after 24 hours |

---

## Troubleshooting

**Redirected to login on every page load**
- Check that `secrets.json` has a `jwt_secret` set. Without it, the backend uses an insecure default that changes if the process restarts, which invalidates all tokens.

**WebSocket shows disconnected (red dot)**
- Verify the backend is running: `python dashboard.py`
- If using a tunnel, confirm `cloudflared` is still running
- Check the browser console for close code `4001` (bad token — try logging out and back in)

**No prices showing in the watchlist**
- Confirm Finnhub and/or Alpaca keys are saved in Settings → API Keys
- Check the terminal for `[STARTUP] Alpaca connected` and `[STARTUP] Finnhub stream started`

**Transcription not starting**
- Only works on Windows with `pyaudiowpatch` installed (for loopback capture)
- On other platforms, start the transcriber manually and write output to the watchlist file

**Watchlist changes not appearing**
- The backend polls `transcription/wb_watchlist.json` every 100 ms by file mtime
- Confirm the file is valid JSON: `python -c "import json; json.load(open('transcription/wb_watchlist.json'))"`

---

## File Reference

| File | Purpose |
|------|---------|
| `dashboard.py` | FastAPI backend — prices, signals, WebSocket, REST API |
| `auth.py` | JWT token creation and verification |
| `config.py` | Config loading/saving (`bot_config.json` + `secrets.json`) |
| `signals.py` | Multi-indicator signal calculation |
| `alpaca_api.py` | Alpaca bar fetching and price lookup |
| `finnhub_stream.py` | Finnhub WebSocket price stream |
| `dashboard.html` | Main dashboard SPA |
| `login.html` | Login page |
| `static/js/app.js` | Frontend bootstrap, auth gate, store wiring |
| `static/js/api.js` | WebSocket + REST client, auth headers |
| `static/js/auth.js` | Token and backend URL storage |
| `static/js/tickers.js` | Watchlist table component |
| `static/js/tradingview.js` | TradingView chart widget |
| `static/js/transcription.js` | Transcript panel component |
| `static/js/config.js` | Settings drawer component |
| `static/css/styles.css` | All styles |
| `transcription/wb_watchlist.json` | Live watchlist — the shared data file |
| `secrets.json` | API keys and auth credentials (not in git) |
| `secrets.example.json` | Template for secrets.json |
| `bot_config.json` | Non-secret settings (timeframe, thresholds, etc.) |
