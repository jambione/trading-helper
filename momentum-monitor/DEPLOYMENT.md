# Momentum Monitor — Deployment Guide

Everything required to run the momentum desk monitor on a new macOS or Windows
machine.

The monitor is a terminal UI that polls a live symbol feed and gives you
single-key control: `1`–`9` load a symbol into TradingView and save it to your
watchlist, `B`/`S` place Alpaca orders for the focused symbol.

---

## 1. Requirements at a glance

| | Requirement | Required? | Without it |
|---|---|---|---|
| Runtime | Python **3.10+** | **Yes** | Won't start |
| Package | `rich` | **Yes** | Won't start |
| Package | `pyautogui` | Practically | Monitor displays, but `1`–`9` only set focus — no TradingView load, no watchlist save |
| Package | `alpaca-py` | No | `B`/`S` keys and the P&L panel are disabled |
| Package | `plyer` | No | No desktop toasts |
| Package | `pygetwindow` (Win) | No | Falls back to ctypes window discovery |
| Service | Dashboard feed | **Yes** | Symbol table stays empty |
| App | Brave or Chrome running | For hotkeys | `1`–`9` report "browser not running" |
| App | TradingView chart pinned at a known tab | For hotkeys | Keystrokes land on the wrong page |
| OS | macOS Accessibility permission | For hotkeys (macOS) | Keystrokes **silently discarded** |
| Shell | A real TTY / console | **Yes** | Hotkeys disabled — "run in Terminal.app" |
| Credentials | Alpaca API key + secret | No | Monitor runs read-only |

**Nothing in the credentials row is needed to run the monitor.** Verified: with
no `signal_engine.env` present and `alpaca-py` not installed,
`desk_actions.init_trader()` returns `"off"`, the monitor starts normally, the
table populates, and `1`–`9` still drive TradingView.

### Python floor is 3.10, not a preference

`alpaca_trader.py` uses `bool | None` in a `def` signature without
`from __future__ import annotations`. That is a **runtime** `TypeError` on 3.9
and earlier, raised at import. 3.10 is a hard minimum.

---

## 2. Install

### macOS / Linux

```bash
git clone <your-repo-url> trading-helper
cd trading-helper
bash scripts/setup_monitor.sh
```

### Windows

```powershell
git clone <your-repo-url> trading-helper
cd trading-helper
powershell -ExecutionPolicy Bypass -File scripts\setup_monitor.ps1
```

Both scripts create `.venv-monitor/`, install from
`momentum-monitor/requirements-monitor.txt`, scaffold `signal_engine.env` from
the example, and run preflight checks. Safe to re-run.

### Do not install the root `requirements.txt`

It covers the entire trading-helper project — fastapi, pandas, numpy, opencv,
pytesseract — none of which the monitor imports. It also pins `pywin32` with no
platform marker, and **pywin32 publishes Windows-only wheels**, so
`pip install -r requirements.txt` fails outright on macOS with
`No matching distribution found for pywin32`.

`requirements-monitor.txt` is the verified import closure of the monitor graph:

```
momentum_signal.py
  ├─ desk_actions.py ──── alpaca_trader.py   (alpaca imports are function-local)
  ├─ desk_hotkeys.py
  ├─ mac_agent.py / windows_agent.py         (platform-selected)
  └─ session_clock.py
```

Note `pywin32` is **not** in it. Nothing in the monitor graph imports
`win32api`/`win32gui`; `windows_agent.py` uses `ctypes.windll` from the stdlib.

---

## 3. Configuration

### `momentum-monitor/momentum_config.json` (tracked)

Behaviour and thresholds. The values that matter most on a new machine:

| Key | Default | Meaning |
|---|---|---|
| `trader_mode` | `paper` | `off` \| `paper` \| `live` — **`live` places real orders** |
| `trade_amount` | `1000` | Dollars per buy, sized into whole shares |
| `trade_enabled` | `true` | Master switch for the `B`/`S` keys |
| `extended_hours` | `true` | Allow pre/post-market orders |
| `poll_interval` | `2.0` | Seconds between feed polls |

Config wins over environment variables where both define the same setting.

### `signal_engine.env` (NOT tracked — create per machine)

Copy from `signal_engine.env.example` and fill in only what you need. Every
value is optional for the monitor.

| Variable | Unlocks |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | `B`/`S` keys, live P&L panel |
| `TRADER_MODE` | `off` \| `paper` \| `live`; auto-downgrades to `off` when keys are blank |
| `TRADE_AMOUNT`, `BUY_ORDER_STYLE`, `LIMIT_PAD_PCT`, `EXTENDED_HOURS` | Order sizing and style |
| `DASHBOARD_URL` | Feed location; already defaults correctly in code |
| `DASHBOARD_USER` / `DASHBOARD_PASS` | Only if the feed starts requiring auth — blank works today |

`FINNHUB_API_KEY` and `MASSIVE_API_KEY` belong to `finnhub_stream.py` and
`massive_client.py`. Confirmed absent from the monitor's import graph — omit
them unless you also run those services.

### Credentials hygiene

`signal_engine.env` must never be committed. Rotate any key that has been in a
tracked file, and confirm `.gitignore` covers it before your first commit on a
new machine.

---

## 4. The feed is a hard dependency

The monitor polls `DASHBOARD_URL/api/state` (default
`https://trading.jbrasfield.com`) for its symbol list. It computes nothing
locally. **If that endpoint is unreachable, the monitor runs but the table stays
empty** — no amount of local configuration fixes it.

On a new machine, confirm reachability before debugging anything else:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://trading.jbrasfield.com/api/state
```

Auth is currently optional — the working reference machine has no `.env` and no
dashboard credentials set.

---

## 5. macOS permissions

### Accessibility — the one that silently breaks everything

`pyautogui` synthesizes keystrokes and clicks. Without Accessibility, macOS
**discards those events without raising an error**. The monitor reports success
and nothing happens. This is the single most common failure on a new machine.

Grant it to **the application that launches the monitor** — `Terminal.app`,
iTerm, or whichever terminal you use. Do *not* try to grant it to the Python
binary: macOS attributes Accessibility to the *responsible process*, which is
the terminal app, not the interpreter it spawned.

```
System Settings → Privacy & Security → Accessibility → +  (add your terminal)
```

Trust is read **at process start**. Granting it to an already-running monitor
does nothing — quit and relaunch.

To verify from inside the exact interpreter that will run the monitor:

```bash
.venv-monitor/bin/python -c "
import ctypes
a = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
a.AXIsProcessTrusted.restype = ctypes.c_bool
print('Accessibility trusted:', a.AXIsProcessTrusted())"
```

`setup_monitor.sh` runs this check automatically.

### Must run in a real terminal window

Single-key input uses `termios`/`tty` cbreak mode and needs a TTY. Launched
without one, the monitor prints
`keys off (stdin not a tty — run in Terminal.app)` and every hotkey is dead.
IDE output panes and most task runners do not provide a TTY.

---

## 6. Windows notes

No special permissions are required. Run from a real console window (cmd or
PowerShell) — single-key input uses `msvcrt` and needs a real console, so IDE
panes will not work.

One caveat: `pyautogui` cannot send input to a window running at a higher
integrity level. If the browser was started as Administrator and the monitor was
not, keystrokes are dropped. Run both at the same level.

---

## 7. Browser setup

Both platforms drive the browser the same way:

1. Brave or Chrome must already be running — the agents look for
   `Brave Browser` first, then `Google Chrome`.
2. A TradingView chart must be open and pinned at the tab index in
   `BRAVE_TV_TAB` (default `1`).
3. The chart URL must contain `tradingview.com/chart`, or symbol read-back
   cannot find the tab.

The load sequence is: focus browser → switch tab → `Escape` → click the chart
canvas → type the ticker → `Enter` → save to watchlist.

---

## 8. Platform parity

Both platforms share the monitor, hotkeys, and Alpaca logic. The browser
automation differs:

| | macOS (`mac_agent.py`) | Windows (`windows_agent.py`) |
|---|---|---|
| Window discovery | AppleScript / System Events | `ctypes` + `user32` HWND |
| Tab switch | `Cmd`+`N` | `Ctrl`+`N` |
| Save to watchlist | `Option`+`W` | `Alt`+`W` |
| Chart click point | Derived from window bounds | Derived from window rect |
| Read chart symbol (`T` key) | Supported | **Not implemented** |
| Verifies symbol loaded before saving | Yes | **No — always reports success** |
| Desktop toasts | `BrasfieldNotifier.app` or `plyer` | `plyer` |

Two Windows gaps worth knowing:

- **`T` (focus = TradingView chart symbol) does nothing.** `windows_agent.py`
  has no `read_tv_symbol()`; `desk_actions.tv_focus_symbol()` returns `None`.
- **`workflow_add_tv()` returns `True` unconditionally.** It cannot detect that
  keystrokes went to the wrong window, so it may save the wrong symbol to your
  watchlist. The macOS path verifies the chart actually shows the requested
  ticker and aborts the save if not. Porting that check requires implementing
  `read_tv_symbol()` for Windows.

`BrasfieldNotifier.app` is macOS-only, gitignored, and built separately via
`scripts/build_notifier.sh`. It is used by `mac_agent.py`'s alert listener and
is **not required by the monitor**.

---

## 9. Launching

```bash
# macOS / Linux
.venv-monitor/bin/python momentum-monitor/momentum_signal.py

# Windows
.venv-monitor\Scripts\python.exe momentum-monitor\momentum_signal.py
```

### Keys

| Key | Action |
|---|---|
| `1`–`9` | Focus that row + load into TradingView + save to watchlist |
| `Space` | Same, for the newest row |
| `T` | Set focus from the current TradingView chart symbol (macOS only) |
| `B` | Buy focused symbol |
| `S` | Sell / close focused symbol |
| `Ctrl+C` | Stop |

The focused symbol is written to `active_symbol.json` in the repo root for other
tools to read.

### Double-click launcher

The reference machine uses a `trading.command` on the Desktop, which is **not in
the repo** — recreate it per machine:

```bash
#!/bin/bash
REPO="$HOME/repo/trading-helper"
cd "$REPO" || exit 1
"$REPO/.venv-monitor/bin/python" momentum-monitor/momentum_signal.py
echo; read -n1 -rsp $'Momentum desk exited. Press any key to close…\n'
```

Save it, then `chmod +x trading.command`. On Windows, the equivalent `.bat`
calls `.venv-monitor\Scripts\python.exe`.

---

## 10. Troubleshooting

**Table is empty.** Feed unreachable. Check `DASHBOARD_URL/api/state` with
`curl` before anything else.

**Hotkeys do nothing at all; no reaction.** Not a TTY. Launch from a real
terminal window.

**Browser focuses, then focus returns to the monitor, nothing is typed.** The
chart click is landing on the wrong window — usually the terminal itself. This
was a real bug: the macOS click point was hardcoded to 15% of *screen* width,
which assumed the browser occupied the left half of the display. It is now
derived from the browser window's actual bounds. If you see this again, confirm
the browser window is not fully occluded and that `_front_window_bounds()` is
returning sane values.

**Symbol loads but is not saved to the watchlist.** `Option`/`Alt`+`W` is
reaching the page but TradingView did not accept it. Confirm the shortcut still
maps to "add to watchlist" in your TradingView settings.

**`B`/`S` say "trader mode=off".** Credentials missing or blank. Expected on a
fresh install — fill in `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` and relaunch.

**macOS: everything looks fine but no keystrokes land.** Accessibility. Run the
verification snippet in §5 from the venv interpreter. Remember the grant applies
to your terminal app and is only read at process start.

**Windows: keystrokes dropped into the browser only.** Integrity-level mismatch.
Run the browser and the monitor at the same privilege level.
