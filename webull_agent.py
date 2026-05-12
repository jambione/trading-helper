#!/usr/bin/env python3
"""
webull_agent.py — Webull Automated Trading Agent

Watches signal_log.json (written by signal_engine.py) for new BUY and SELL
signals and places the corresponding market orders on Webull.

This agent is completely decoupled from the signal engine — it only reads
the log file.  Any other signal source that writes the same JSON format can
drive it too.

FIRST-TIME SETUP (run once)
────────────────────────────
    python webull_agent.py --setup

    This walks through Webull login + MFA and saves your session tokens to
    webull_tokens.json so the agent can run unattended afterward.

NORMAL OPERATION
────────────────
    python webull_agent.py

    The agent is DISABLED by default.  Set ENABLED=true in webull_agent.env
    to activate it.  While disabled it still watches the log and prints what
    it WOULD have done — useful for verifying signals before going live.

PAPER vs LIVE TRADING
─────────────────────
    PAPER_TRADING=true  (default) — all orders go to your Webull paper account.
    PAPER_TRADING=false            — real money.  Test thoroughly on paper first.

HOW IT WORKS
────────────
1. Every second, reads signal_log.json and processes any entries added since
   the last check (tracked by cursor saved to webull_cursor.json).
2. On BUY signal:
     - Calculates shares = floor(TRADE_AMOUNT / signal_price)
     - Places a market BUY order on Webull
     - Records the position in webull_positions.json
3. On SELL signal:
     - Finds all open positions for that ticker
     - Places a market SELL order for the total shares held
     - Closes out the position record
4. All actions are logged to webull_trade_log.json regardless of enabled/disabled.

CONFIG (webull_agent.env)
──────────────────────────
    ENABLED=false              # set true to place real orders
    PAPER_TRADING=true         # true = paper account, false = live account
    TRADE_AMOUNT=500           # dollars to spend per BUY signal
    WEBULL_EMAIL=              # your Webull login email
    WEBULL_PASS=               # your Webull password
    WEBULL_TRADING_PIN=        # your 6-digit trading PIN
    WEBULL_DEVICE_NAME=SignalEngine
    SIGNAL_LOG=signal_log.json # path to signal engine log (relative to this file)
    POLL_INTERVAL=1            # seconds between log checks
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent

# ── Load webull_agent.env ─────────────────────────────────────────────────────

def _load_env_file(path: Path):
    """Read KEY=VALUE lines into os.environ — shell environment always wins."""
    if not path.exists():
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = val.strip()

_load_env_file(_HERE / "webull_agent.env")

# ── Configuration ─────────────────────────────────────────────────────────────

ENABLED         = os.getenv("ENABLED",          "false").lower() == "true"
PAPER_TRADING   = os.getenv("PAPER_TRADING",    "true").lower()  == "true"
TRADE_AMOUNT    = float(os.getenv("TRADE_AMOUNT",  "500"))   # $ per BUY
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL",   "1"))     # seconds

WEBULL_EMAIL    = os.getenv("WEBULL_EMAIL",       "")
WEBULL_PASS     = os.getenv("WEBULL_PASS",        "")
WEBULL_PIN      = os.getenv("WEBULL_TRADING_PIN", "")
WEBULL_DEVICE   = os.getenv("WEBULL_DEVICE_NAME", "SignalEngine")

SIGNAL_LOG_PATH = _HERE / os.getenv("SIGNAL_LOG", "signal_log.json")

# State files — persist across restarts
CURSOR_FILE     = _HERE / "webull_cursor.json"      # last processed signal index
POSITIONS_FILE  = _HERE / "webull_positions.json"   # open positions
TRADE_LOG_FILE  = _HERE / "webull_trade_log.json"   # full trade history
TOKENS_FILE     = _HERE / "webull_tokens.json"      # Webull session tokens


# ── Webull client wrapper ─────────────────────────────────────────────────────

class WebullClient:
    """
    Thin wrapper around the 'webull' Python library.
    Handles login, token persistence, and order placement.
    Supports both paper and live trading via a single flag.
    """

    def __init__(self):
        try:
            from webull import webull as _webull
            self._wb = _webull()
        except ImportError:
            print(
                "\n❌  The 'webull' Python library is not installed.\n"
                "    Run:  pip install webull\n"
                "    Then re-run this script.\n"
            )
            sys.exit(1)

        self.logged_in  = False
        self.paper      = PAPER_TRADING

    # ── Login ─────────────────────────────────────────────────────────────────

    def login_from_tokens(self) -> bool:
        """
        Attempt to restore a previous session from webull_tokens.json.
        Returns True if the session is valid, False if a fresh login is needed.
        """
        if not TOKENS_FILE.exists():
            return False
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            self._wb.api_login(
                access_token  = tokens["access_token"],
                refresh_token = tokens["refresh_token"],
                token_expire  = tokens["token_expire"],
                uuid          = tokens["uuid"],
            )
            # Verify the session is still alive
            acct = self._wb.get_paper_account() if self.paper else self._wb.get_account()
            if acct:
                print(f"[WB]  Session restored from tokens ✓  ({'paper' if self.paper else 'LIVE'})")
                self.logged_in = True
                return True
        except Exception as e:
            print(f"[WB]  Token restore failed ({e}) — need fresh login")
        return False

    def login_with_credentials(self) -> bool:
        """
        Full login using email + password + MFA code.
        Saves tokens to webull_tokens.json for future restarts.
        """
        if not WEBULL_EMAIL or not WEBULL_PASS:
            print("❌  WEBULL_EMAIL and WEBULL_PASS must be set in webull_agent.env")
            return False

        print(f"\n[WB]  Logging in as {WEBULL_EMAIL}…")
        try:
            # Step 1: request MFA code (sent to your email/phone by Webull)
            self._wb.get_mfa(WEBULL_EMAIL)
            mfa_code = input("  Enter the MFA code Webull sent you: ").strip()

            # Step 2: full login
            result = self._wb.login(
                username    = WEBULL_EMAIL,
                password    = WEBULL_PASS,
                device_name = WEBULL_DEVICE,
                mfa         = mfa_code,
            )

            if not result or result.get("accessToken") is None:
                print(f"❌  Login failed: {result}")
                return False

            # Step 3: save tokens for future restarts
            tokens = {
                "access_token":  self._wb._access_token,
                "refresh_token": self._wb._refresh_token,
                "token_expire":  self._wb._token_expire,
                "uuid":          self._wb._uuid,
            }
            TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
            print(f"[WB]  Tokens saved to {TOKENS_FILE.name} ✓")

            self.logged_in = True
            return True

        except Exception as e:
            print(f"❌  Login error: {e}")
            return False

    def connect(self) -> bool:
        """Try token restore first, fall back to full credential login."""
        if self.login_from_tokens():
            return True
        return self.login_with_credentials()

    # ── Order placement ───────────────────────────────────────────────────────

    def place_buy(self, ticker: str, shares: int) -> dict:
        """
        Place a market BUY order.
        Returns a result dict with order_id (or error message).
        """
        if not self.logged_in:
            return {"ok": False, "error": "not logged in"}
        try:
            if self.paper:
                result = self._wb.paper_place_order(
                    stock     = ticker,
                    action    = "BUY",
                    orderType = "MKT",
                    quant     = shares,
                    enforce   = "DAY",
                )
            else:
                # Live orders require the trading PIN to be re-submitted
                self._wb.get_trade_token(WEBULL_PIN)
                result = self._wb.place_order(
                    stock     = ticker,
                    action    = "BUY",
                    orderType = "MKT",
                    quant     = shares,
                    enforce   = "DAY",
                )
            order_id = (result or {}).get("orderId") or (result or {}).get("data", {}).get("orderId")
            return {"ok": True, "order_id": str(order_id), "raw": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def place_sell(self, ticker: str, shares: int) -> dict:
        """
        Place a market SELL order.
        Returns a result dict with order_id (or error message).
        """
        if not self.logged_in:
            return {"ok": False, "error": "not logged in"}
        try:
            if self.paper:
                result = self._wb.paper_place_order(
                    stock     = ticker,
                    action    = "SELL",
                    orderType = "MKT",
                    quant     = shares,
                    enforce   = "DAY",
                )
            else:
                self._wb.get_trade_token(WEBULL_PIN)
                result = self._wb.place_order(
                    stock     = ticker,
                    action    = "SELL",
                    orderType = "MKT",
                    quant     = shares,
                    enforce   = "DAY",
                )
            order_id = (result or {}).get("orderId") or (result or {}).get("data", {}).get("orderId")
            return {"ok": True, "order_id": str(order_id), "raw": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_account_summary(self) -> str:
        """Return a one-line account summary for the startup banner."""
        try:
            acct = self._wb.get_paper_account() if self.paper else self._wb.get_account()
            cash = acct.get("accountMembers", [{}])[0].get("value", "?")
            return f"cash available: ${cash}"
        except Exception:
            return "(account info unavailable)"


# ── Cursor (position in signal_log.json) ─────────────────────────────────────

def _load_cursor() -> int:
    """Load the index of the last signal we processed."""
    if CURSOR_FILE.exists():
        try:
            return int(json.loads(CURSOR_FILE.read_text()).get("index", 0))
        except Exception:
            pass
    return 0


def _save_cursor(index: int):
    CURSOR_FILE.write_text(json.dumps({"index": index}))


# ── Position tracking ─────────────────────────────────────────────────────────

def _load_positions() -> dict:
    """
    Load open positions from disk.
    Format: { "AAPL": [ {shares, entry_price, entry_time, signal_time}, … ] }
    Allows multiple entries per ticker (stacking enabled).
    """
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_positions(positions: dict):
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))


# ── Trade logging ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_trade_log(entry: dict):
    entries = []
    if TRADE_LOG_FILE.exists():
        try:
            entries = json.loads(TRADE_LOG_FILE.read_text())
        except Exception:
            pass
    entries.append(entry)
    TRADE_LOG_FILE.write_text(json.dumps(entries, indent=2))


# ── Main agent ────────────────────────────────────────────────────────────────

class WebullAgent:
    """
    Core agent loop.

    Polls signal_log.json every POLL_INTERVAL seconds.
    Processes new BUY/SELL signals since the last cursor position.
    Places orders via WebullClient.
    Tracks open positions in webull_positions.json.
    """

    def __init__(self, client: WebullClient):
        self.client    = client
        self.cursor    = _load_cursor()
        self.positions = _load_positions()

    # ── Signal reading ─────────────────────────────────────────────────────────

    def _read_new_signals(self) -> list[dict]:
        """
        Read signal_log.json and return any entries added since the last cursor.
        Updates the cursor and persists it to disk.
        """
        if not SIGNAL_LOG_PATH.exists():
            return []
        try:
            all_signals = json.loads(SIGNAL_LOG_PATH.read_text())
        except Exception:
            return []

        new_signals = all_signals[self.cursor:]
        if new_signals:
            self.cursor = len(all_signals)
            _save_cursor(self.cursor)
        return new_signals

    # ── BUY execution ─────────────────────────────────────────────────────────

    def _handle_buy(self, signal: dict):
        """
        Process a BUY signal:
          1. Calculate share count from TRADE_AMOUNT and signal price.
          2. Place a market BUY order (or simulate if disabled).
          3. Record the position and log the action.
        """
        ticker      = signal.get("ticker", "")
        signal_price= float(signal.get("price", 0))
        signal_time = signal.get("time", _now_iso())

        if not ticker or signal_price <= 0:
            print(f"[WB]  ⚠️  Invalid BUY signal — missing ticker or price: {signal}")
            return

        # Calculate shares — round down so we never overspend
        shares = math.floor(TRADE_AMOUNT / signal_price)
        if shares < 1:
            print(f"[WB]  ⚠️  {ticker}: TRADE_AMOUNT=${TRADE_AMOUNT:.0f} too small "
                  f"to buy even 1 share at ${signal_price:.2f} — skipping")
            return

        est_cost = shares * signal_price

        if ENABLED:
            print(f"\n[WB]  🟢 BUY  {ticker}  {shares} shares @ ~${signal_price:.2f}  "
                  f"(est. ${est_cost:.2f})  [{'paper' if PAPER_TRADING else 'LIVE'}]")
            result = self.client.place_buy(ticker, shares)
        else:
            # Dry-run mode — log what WOULD happen without placing an order
            print(f"\n[WB]  🟢 BUY (disabled — dry run)  {ticker}  "
                  f"{shares} shares @ ~${signal_price:.2f}  (est. ${est_cost:.2f})")
            result = {"ok": True, "order_id": "DRY_RUN", "dry_run": True}

        # Record the position regardless of enabled/disabled
        entry = {
            "shares":       shares,
            "signal_price": signal_price,
            "signal_time":  signal_time,
            "order_id":     result.get("order_id", ""),
            "order_ok":     result.get("ok", False),
        }
        if ticker not in self.positions:
            self.positions[ticker] = []
        self.positions[ticker].append(entry)
        _save_positions(self.positions)

        # Append to trade log
        _append_trade_log({
            "action":       "BUY",
            "ticker":       ticker,
            "shares":       shares,
            "signal_price": signal_price,
            "est_cost":     round(est_cost, 2),
            "signal_time":  signal_time,
            "order_id":     result.get("order_id", ""),
            "order_ok":     result.get("ok", False),
            "error":        result.get("error", ""),
            "paper":        PAPER_TRADING,
            "dry_run":      not ENABLED,
            "agent_time":   _now_iso(),
        })

        if not result.get("ok"):
            print(f"[WB]  ❌ BUY order failed: {result.get('error')}")
        else:
            print(f"[WB]  ✓  Order submitted  id={result.get('order_id')}")

    # ── SELL execution ─────────────────────────────────────────────────────────

    def _handle_sell(self, signal: dict):
        """
        Process a SELL signal:
          1. Find all open positions for this ticker.
          2. Sum total shares held and place a market SELL.
          3. Calculate P&L, log the trade, close the position record.
        """
        ticker      = signal.get("ticker", "")
        signal_price= float(signal.get("price", 0))
        signal_time = signal.get("time", _now_iso())

        if not ticker:
            print(f"[WB]  ⚠️  Invalid SELL signal — missing ticker: {signal}")
            return

        open_lots = self.positions.get(ticker, [])
        if not open_lots:
            print(f"[WB]  ℹ️  SELL {ticker}: no open positions tracked — skipping")
            return

        total_shares = sum(lot["shares"] for lot in open_lots)
        avg_entry    = (sum(lot["shares"] * lot["signal_price"] for lot in open_lots)
                        / total_shares)
        pnl_pct      = ((signal_price - avg_entry) / avg_entry * 100
                        if avg_entry > 0 else 0)
        pnl_dollar   = total_shares * (signal_price - avg_entry)

        if ENABLED:
            print(f"\n[WB]  🔴 SELL {ticker}  {total_shares} shares @ ~${signal_price:.2f}  "
                  f"P&L {'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}% "
                  f"({'+' if pnl_dollar>=0 else ''}${pnl_dollar:.2f})  "
                  f"[{'paper' if PAPER_TRADING else 'LIVE'}]")
            result = self.client.place_sell(ticker, total_shares)
        else:
            print(f"\n[WB]  🔴 SELL (disabled — dry run)  {ticker}  "
                  f"{total_shares} shares @ ~${signal_price:.2f}  "
                  f"P&L {'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%")
            result = {"ok": True, "order_id": "DRY_RUN", "dry_run": True}

        # Close all lots for this ticker
        del self.positions[ticker]
        _save_positions(self.positions)

        # Append to trade log
        _append_trade_log({
            "action":       "SELL",
            "ticker":       ticker,
            "shares":       total_shares,
            "signal_price": signal_price,
            "avg_entry":    round(avg_entry, 4),
            "pnl_pct":      round(pnl_pct, 3),
            "pnl_dollar":   round(pnl_dollar, 2),
            "lots_closed":  len(open_lots),
            "signal_time":  signal_time,
            "order_id":     result.get("order_id", ""),
            "order_ok":     result.get("ok", False),
            "error":        result.get("error", ""),
            "paper":        PAPER_TRADING,
            "dry_run":      not ENABLED,
            "agent_time":   _now_iso(),
        })

        if not result.get("ok"):
            print(f"[WB]  ❌ SELL order failed: {result.get('error')}")
        else:
            print(f"[WB]  ✓  Order submitted  id={result.get('order_id')}")

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
        mode_str  = "PAPER" if PAPER_TRADING else "⚠️  LIVE"
        state_str = "ENABLED" if ENABLED else "DISABLED (dry-run only)"

        print("=" * 60)
        print("  Webull Trading Agent")
        print(f"  Status        : {state_str}")
        print(f"  Account mode  : {mode_str}")
        print(f"  Trade amount  : ${TRADE_AMOUNT:.0f} per BUY")
        print(f"  Signal log    : {SIGNAL_LOG_PATH}")
        print(f"  Cursor        : entry #{self.cursor} (last processed)")
        print(f"  Open positions: {sum(len(v) for v in self.positions.values())} lots "
              f"across {len(self.positions)} ticker(s)")
        print("=" * 60)

        if ENABLED and self.client.logged_in:
            print(f"  Account: {self.client.get_account_summary()}")
        print()

        if not ENABLED:
            print("  ℹ️  Running in dry-run mode — no orders will be placed.")
            print("     Set ENABLED=true in webull_agent.env to activate trading.\n")

        print("  Watching for signals…\n")

        while True:
            try:
                signals = self._read_new_signals()
                for sig in signals:
                    action = sig.get("action", "").upper()
                    if action == "BUY":
                        self._handle_buy(sig)
                    elif action == "SELL":
                        self._handle_sell(sig)
                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                print(f"[WB]  Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)


# ── First-time setup wizard ───────────────────────────────────────────────────

def _direct_send_code(email: str) -> bool:
    """
    Send a Webull login verification code directly via their API.
    Bypasses the webull library's get_mfa() which is unreliable.
    Returns True if the request succeeded.
    """
    import uuid as _uuid
    did = _uuid.uuid4().hex   # random device ID for this request

    headers = {
        "app":          "global",
        "appid":        "webull-desktop",
        "ver":          "1",
        "os":           "web",
        "did":          did,
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0",
    }
    payload = {
        "account":     email,
        "accountType": "2",   # 2 = email address
        "codeType":    "5",   # 5 = login verification code
    }
    try:
        resp = requests.post(
            "https://userapi.webull.com/api/user/v1/sendLoginCode",
            json=payload,
            headers=headers,
            timeout=10,
        )
        data = resp.json()
        # Webull returns {"success": true} or {"msg": "...", "code": ...}
        if data.get("success") or resp.status_code == 200:
            return True
        print(f"  Webull response: {data}")
        return False
    except Exception as e:
        print(f"  Request error: {e}")
        return False


def run_setup():
    """
    Interactive setup wizard — run once to log in and save tokens.
    After setup, the agent can restart without MFA codes.

    Uses Webull's API directly to send the email verification code —
    more reliable than the library's built-in get_mfa() method.
    Falls back to browser token extraction if login still fails.
    """
    print("=" * 60)
    print("  Webull Agent — First-Time Setup")
    print("=" * 60)
    print()
    print("  This will log you into Webull and save your session tokens")
    print(f"  to {TOKENS_FILE.name} so the agent can run unattended.\n")

    try:
        from webull import webull as _webull
    except ImportError:
        print("❌  Run:  pip install webull  then try again.")
        return

    email    = WEBULL_EMAIL or input("  Webull email address : ").strip()
    password = WEBULL_PASS  or input("  Webull password      : ").strip()
    pin      = WEBULL_PIN   or input("  6-digit trading PIN  : ").strip()

    wb = _webull()

    # ── Step 1: Send verification code via direct API call ────────────────────
    print(f"\n  Sending verification code to {email}…")
    sent = _direct_send_code(email)

    if sent:
        print("  ✓  Code sent — check your email inbox (and spam folder).")
    else:
        print("  ⚠️  Automatic send may have failed.")
        print("      Try opening the Webull app and tapping 'Forgot Password'")
        print("      or logging in via https://app.webull.com to trigger a code,")
        print("      then come back here and enter it below.")

    mfa = input("\n  Enter the 6-digit verification code: ").strip()
    if not mfa:
        print("  No code entered — setup cancelled.")
        return

    # ── Step 2: Login via webull library ──────────────────────────────────────
    print("  Logging in…")
    result = None
    error  = None
    try:
        result = wb.login(
            username    = email,
            password    = password,
            device_name = WEBULL_DEVICE,
            mfa         = mfa,
        )
    except Exception as e:
        error = str(e)

    if result and result.get("accessToken"):
        _finish_setup(wb, email, pin)
        return

    # ── Step 3: Fallback — browser token extraction ───────────────────────────
    print(f"\n  ⚠️  Login failed{': ' + error if error else ''}.")
    print(f"      Raw response: {result}")
    print()
    print("  BROWSER TOKEN FALLBACK")
    print("  ──────────────────────")
    print("  1. Open https://app.webull.com in Chrome and log in normally.")
    print("  2. Press F12  →  Application tab  →  Local Storage")
    print("     →  click 'https://app.webull.com'")
    print("  3. Find and copy these four values:")
    print("       access_token")
    print("       refresh_token")
    print("       token_expire")
    print("       uuid")
    print()
    go = input("  Paste tokens manually? [y/N]: ").strip().lower()
    if go != "y":
        print("  Setup cancelled.")
        return

    tokens = {
        "access_token":  input("  access_token  : ").strip(),
        "refresh_token": input("  refresh_token : ").strip(),
        "token_expire":  input("  token_expire  : ").strip(),
        "uuid":          input("  uuid          : ").strip(),
    }
    if not tokens["access_token"]:
        print("  No token entered — setup cancelled.")
        return

    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    print(f"\n  ✓  Tokens saved to {TOKENS_FILE.name}")
    print("  Run the agent with:  python webull_agent.py")


def _finish_setup(wb, email: str, pin: str):
    """Save tokens and print next steps after a successful login."""
    tokens = {
        "access_token":  wb._access_token,
        "refresh_token": wb._refresh_token,
        "token_expire":  wb._token_expire,
        "uuid":          wb._uuid,
    }
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

    print(f"\n  ✓  Logged in successfully!")
    print(f"  ✓  Tokens saved to {TOKENS_FILE.name}")
    print()
    print("  Make sure webull_agent.env has:")
    print(f"    WEBULL_EMAIL={email}")
    print(f"    WEBULL_PASS=<your password>")
    print(f"    WEBULL_TRADING_PIN={pin}")
    print(f"    ENABLED=false   ← change to true when ready to trade")
    print()
    print("  Run the agent:  python webull_agent.py")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Webull trading agent — watches signal_log.json and places orders."
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run the first-time login wizard to save session tokens.",
    )
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    # ── Normal operation ──────────────────────────────────────────────────────
    client = WebullClient()

    if ENABLED:
        # Only attempt login if we're actually going to place orders
        if not client.connect():
            print("❌  Could not connect to Webull.  Run:  python webull_agent.py --setup")
            sys.exit(1)
    else:
        print("[WB]  Disabled — skipping Webull login (dry-run mode)")

    agent = WebullAgent(client)
    agent.run()


if __name__ == "__main__":
    main()
