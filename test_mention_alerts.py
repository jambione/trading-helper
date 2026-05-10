#!/usr/bin/env python3
"""
test_mention_alerts.py — Test mention burst detection and alert thresholds.

Run while the trading server is running:
    python test_mention_alerts.py
    python test_mention_alerts.py --url http://localhost:8888
    python test_mention_alerts.py --ticker TSLA --count 7 --window 10

What it tests:
  1. Single mention → mention_count increments, no burst
  2. Rapid mentions (≥ threshold within window) → mention_burst = True
  3. Mentions taper off → burst clears after window expires
  4. Clear watchlist → mention data wiped
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

# ── CLI args ──────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Test mention burst alerts")
parser.add_argument("--url",     default="http://localhost:8888", help="Server base URL")
parser.add_argument("--ticker",  default="TESTX",  help="Ticker symbol to use (default: TESTX)")
parser.add_argument("--count",   type=int, default=6, help="Mentions to fire in burst (default: 6)")
parser.add_argument("--window",  type=int, default=10, help="Expected alert window in seconds (default: 10)")
parser.add_argument("--threshold", type=int, default=5, help="Expected alert threshold (default: 5)")
args = parser.parse_args()

BASE      = args.url.rstrip("/")
TICKER    = args.ticker.upper()
BURST_N   = args.count
WINDOW    = args.window
THRESHOLD = args.threshold

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
INFO = "\033[94mℹ\033[0m"

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def get_state() -> dict:
    with urllib.request.urlopen(BASE + "/api/state", timeout=5) as r:
        return json.loads(r.read())


def find_ticker(state: dict, ticker: str) -> dict | None:
    for row in state.get("tickers", []):
        if row.get("ticker") == ticker:
            return row
    return None


# ── Test helpers ──────────────────────────────────────────────────────────────

_passed = 0
_failed = 0

def check(label: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  {PASS}  {label}" + (f"  ({detail})" if detail else ""))
    else:
        _failed += 1
        print(f"  {FAIL}  {label}" + (f"  ({detail})" if detail else ""))


def section(title: str):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")


# ── Verify server is reachable ────────────────────────────────────────────────

print(f"\n{INFO}  Connecting to {BASE} ...")
try:
    with urllib.request.urlopen(BASE + "/api/meta", timeout=3) as r:
        meta = json.loads(r.read())
    print(f"  Server: OK  (auth_required={meta.get('auth_required', False)})")
except Exception as e:
    print(f"\033[91mERROR: Cannot reach server at {BASE}\033[0m")
    print(f"  Make sure the trading server is running first.")
    print(f"  Detail: {e}")
    sys.exit(1)


# ── Test 1: Clean slate ───────────────────────────────────────────────────────

section("Test 1 — Pre-test cleanup")

# Remove test ticker if already in watchlist
try:
    post("/api/tickers/remove", {"ticker": TICKER})
    print(f"  {INFO}  Removed {TICKER} from watchlist (if present)")
    time.sleep(0.3)
except Exception:
    pass

state  = get_state()
row    = find_ticker(state, TICKER)
check("Ticker not in watchlist before test", row is None)


# ── Test 2: Single mention ────────────────────────────────────────────────────

section(f"Test 2 — Single mention of {TICKER}")

post("/api/tickers/add", {"ticker": TICKER})
time.sleep(0.5)
state = get_state()
row   = find_ticker(state, TICKER)

check("Ticker appears in watchlist",          row is not None)
check("mention_count = 1",                    (row or {}).get("mention_count") == 1,
      f"got {(row or {}).get('mention_count')}")
check("mention_window = 1",                   (row or {}).get("mention_window") == 1,
      f"got {(row or {}).get('mention_window')}")
check("mention_burst = False (below threshold)", not (row or {}).get("mention_burst", False))


# ── Test 3: Burst — rapid mentions ───────────────────────────────────────────

section(f"Test 3 — Burst: {BURST_N} mentions within {WINDOW}s (threshold={THRESHOLD})")

print(f"  {INFO}  Firing {BURST_N} rapid mentions...")
for i in range(BURST_N - 1):   # -1 because we already sent 1 above
    post("/api/tickers/add", {"ticker": TICKER})
    time.sleep(0.05)   # ~50ms between mentions = fast burst

time.sleep(0.3)
state = get_state()
row   = find_ticker(state, TICKER)

total = BURST_N
check(f"mention_count = {total}",
      (row or {}).get("mention_count") == total,
      f"got {(row or {}).get('mention_count')}")
check(f"mention_window >= {THRESHOLD}",
      (row or {}).get("mention_window", 0) >= THRESHOLD,
      f"got {(row or {}).get('mention_window')}")
check("mention_burst = True",
      (row or {}).get("mention_burst") is True,
      f"got {(row or {}).get('mention_burst')}")

if row:
    print(f"\n  {INFO}  Burst state: window={row.get('mention_window')}  "
          f"daily={row.get('mention_count')}  burst={row.get('mention_burst')}")


# ── Test 4: Burst clears after window expires ─────────────────────────────────

section(f"Test 4 — Burst clears after {WINDOW}s window expires")

print(f"  {INFO}  Waiting {WINDOW + 2}s for window to expire...")
for remaining in range(WINDOW + 2, 0, -1):
    print(f"    {remaining}s remaining...  ", end="\r")
    time.sleep(1)
print()

state = get_state()
row   = find_ticker(state, TICKER)

check("mention_window drops to 0",
      (row or {}).get("mention_window", -1) == 0,
      f"got {(row or {}).get('mention_window')}")
check("mention_burst = False after window",
      not (row or {}).get("mention_burst", True),
      f"got {(row or {}).get('mention_burst')}")
check("mention_count (daily) preserved",
      (row or {}).get("mention_count", 0) == total,
      f"got {(row or {}).get('mention_count')} (expected {total})")


# ── Test 5: Clear watchlist wipes mention data ────────────────────────────────

section("Test 5 — Clear watchlist wipes mention data")

post("/api/ticker-log/clear", {})
time.sleep(0.5)
state = get_state()
row   = find_ticker(state, TICKER)

check("Ticker removed after clear",           row is None)
print(f"  {INFO}  mention_ts and mention_daily cleared on server (in-memory)")


# ── Summary ───────────────────────────────────────────────────────────────────

total_tests = _passed + _failed
print(f"\n{'═'*55}")
print(f"  Results: {_passed}/{total_tests} passed", end="")
if _failed:
    print(f"  \033[91m({_failed} failed)\033[0m")
else:
    print(f"  \033[92m— all good!\033[0m")
print(f"{'═'*55}\n")

sys.exit(0 if _failed == 0 else 1)
