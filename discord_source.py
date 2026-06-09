#!/usr/bin/env python3
"""
discord_source.py — read trading-alert messages off the Discord window via OCR
and feed them into the dashboard as ticker mentions.

ToS-safe by design: this NEVER logs into Discord or touches its servers. It only
reads pixels already on your own screen. A small Swift/Vision helper
(`discord_ocr`) screenshots the Discord app window and prints the recognized
text; this poller parses the alert lines and POSTs each new alert's ticker to
the same dashboard seam the audio transcriber uses:

    POST http://localhost:8888/api/tickers/add   {"ticker": "NVDA", "count": 1}

So everything downstream (mention tracking → burst detection → signal engine →
toasts/charts) is driven identically to transcription — this is just a cleaner,
second producer that runs ALONGSIDE the transcriber.

Alert format (ticker is always the first token, line contains ">>>>>"):
    INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83, ...
    SPY  NEW WEEKLY LOW          >>>>> Price: $739.20 | Bar Low: ...
    DXF  New Daily High          >>>>> Current Price = 0.6379

Setup:
  1. Build the OCR helper (one time):   swiftc discord_ocr.swift -o discord_ocr
  2. Keep the Discord window with the alert channel visible (not minimized).
  3. Enable in config/bot_config.json:   "discord_ocr_enabled": true
  4. Run standalone for testing:         python discord_source.py
     or let start_all.py launch it automatically when enabled.

The first run will request Screen Recording permission (same as the audio
capture) — grant it to your terminal / launcher.

Config keys (config/bot_config.json), all optional with sane defaults:
  discord_ocr_enabled   bool   default false  (gate used by start_all.py)
  discord_ocr_poll_sec  float  default 2.5    (seconds between OCR captures)
  discord_window_owner  str    default "Discord"
  discord_window_title  str    default ""     (substring filter, optional)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path

# Pure-stdlib ticker validation — single source of truth for the NASDAQ/NYSE
# universe (shared with the audio transcriber). Imports in milliseconds.
sys.path.insert(0, str(Path(__file__).parent / "transcription"))
from ticker_extract import is_valid_ticker  # noqa: E402

ROOT          = Path(__file__).parent
OCR_BINARY    = ROOT / "discord_ocr"
OCR_SCRIPT    = ROOT / "discord_ocr.swift"
CONFIG_FILE   = ROOT / "config" / "bot_config.json"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8888")

# An alert line carries this arrow marker between the ticker/headline and the
# data payload. Two-or-more ">" tolerates OCR dropping a couple of arrows.
_ALERT_MARKER = re.compile(r">>+")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


# ── Alert parsing ───────────────────────────────────────────────────────────

def parse_alert_line(line: str) -> str | None:
    """Return the ticker for an alert line, or None if the line isn't a parseable
    alert. The ticker is the first real word of the line (after any leading
    emoji/symbol); if that word isn't a valid ticker we reject the whole line —
    we never scan deeper, so mid-line words like NEW/LOW can't false-positive."""
    if not _ALERT_MARKER.search(line):
        return None
    for tok in line.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok)
        if not alpha:
            continue  # leading emoji / symbol — look at the next token
        sym = alpha.upper()
        if 2 <= len(sym) <= 5 and is_valid_ticker(sym):
            return sym
        return None   # first real word isn't a valid ticker → not an alert
    return None


def _signature(line: str) -> str:
    """Stable de-dupe key: collapse to lower-case alphanumerics so OCR jitter in
    spacing/punctuation doesn't make the same on-screen alert look 'new'. The
    embedded price keeps successive same-ticker alerts distinct (so repeated
    spikes are each counted, which is what drives burst detection)."""
    return re.sub(r"[^A-Za-z0-9]", "", line).lower()


# ── OCR + delivery ────────────────────────────────────────────────────────────

def _ocr_command(cfg: dict) -> list[str]:
    owner = str(cfg.get("discord_window_owner") or "Discord")
    title = str(cfg.get("discord_window_title") or "").strip()
    if OCR_BINARY.exists():
        cmd = [str(OCR_BINARY)]
    elif OCR_SCRIPT.exists():
        # Fallback: run via the swift interpreter (slower startup).
        cmd = ["swift", str(OCR_SCRIPT)]
    else:
        print("[discord] ERROR: discord_ocr not found. Build it:", flush=True)
        print("  swiftc discord_ocr.swift -o discord_ocr", flush=True)
        raise SystemExit(1)
    cmd += ["--owner", owner]
    if title:
        cmd += ["--title", title]
    return cmd


def _run_ocr(cmd: list[str]) -> list[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print("[discord] OCR timed out", flush=True)
        return []
    if out.returncode != 0:
        msg = (out.stderr or "").strip().splitlines()
        print(f"[discord] OCR failed: {msg[-1] if msg else out.returncode}", flush=True)
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _post_ingest(alerts: list[dict]) -> None:
    """POST this poll's newly-captured alerts (and an implicit heartbeat) to the
    dashboard. Sent every poll even when empty so the dashboard knows the source
    is alive. Each alert drives the mention system + the live feed downstream."""
    try:
        body = json.dumps({"alerts": alerts}).encode()
        req  = urllib.request.Request(
            f"{DASHBOARD_URL}/api/discord/ingest",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
        for a in alerts:
            print(f"  → {a['ticker']}", flush=True)
    except Exception as e:
        if alerts:
            print(f"  → {[a['ticker'] for a in alerts]}  (API error: {e})", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

# Bounded set of recently-emitted alert signatures so messages that linger on
# screen across polls aren't re-counted, while messages that scroll in are.
_SEEN_CAP = 800


def check() -> int:
    """One-shot self-test: capture once, report what OCR saw and parsed, and give
    a verdict. No priming, no POST, no loop. Returns a shell exit code so it's
    scriptable.  Run:  python discord_source.py --check"""
    cfg   = _load_config()
    cmd   = _ocr_command(cfg)
    lines = _run_ocr(cmd)
    alerts = [(parse_alert_line(ln), ln) for ln in lines]
    alerts = [(t, ln) for t, ln in alerts if t]

    print(f"[discord] OCR read {len(lines)} text line(s) from the Discord window.")
    if not lines:
        print("[discord] VERDICT: ✗ window not captured — is Discord open, on this "
              "display/Space, and NOT minimized? (Grant Screen Recording if prompted.)")
        return 1
    if not alerts:
        print("[discord] VERDICT: ⚠ window captured, but no alert line is visible. "
              "Scroll the alert channel so the latest '>>>>>' alerts are on screen.")
        return 2
    print(f"[discord] parsed {len(alerts)} alert(s):")
    for t, ln in alerts:
        print(f"   {t:6}  <=  {ln[:75]}")
    print("[discord] VERDICT: ✓ working — these tickers would post as they newly appear.")
    return 0


def main() -> None:
    cfg      = _load_config()
    poll_sec = float(cfg.get("discord_ocr_poll_sec", 2.5) or 2.5)
    cmd      = _ocr_command(cfg)

    print(f"[discord] OCR source started — polling every {poll_sec:g}s", flush=True)
    print(f"[discord] command: {' '.join(cmd)}", flush=True)
    print(f"[discord] posting alerts → {DASHBOARD_URL}/api/discord/ingest", flush=True)

    seen: "OrderedDict[str, None]" = OrderedDict()
    # First scan is special: the channel already shows several alerts. We surface
    # each VISIBLE ticker once (so the watchlist populates immediately) but never
    # re-post the same on-screen lines, and we collapse to one-per-ticker so a
    # screen full of repeats (e.g. MTEN ×7) can't fake a startup burst. After the
    # first scan, every genuinely new alert line is posted as it appears.
    primed = False

    while True:
        t0 = time.time()
        new_alerts: list[dict] = []
        first_frame_tickers: "OrderedDict[str, str]" = OrderedDict()   # ticker → line
        for line in _run_ocr(cmd):
            ticker = parse_alert_line(line)
            if not ticker:
                continue
            sig = _signature(line)
            if sig in seen:
                continue
            seen[sig] = None
            if len(seen) > _SEEN_CAP:
                seen.popitem(last=False)
            if primed:
                new_alerts.append({"ticker": ticker, "line": line})
            else:
                first_frame_tickers.setdefault(ticker, line)
        if not primed:
            primed = True
            new_alerts = [{"ticker": t, "line": ln} for t, ln in first_frame_tickers.items()]
            print(f"[discord] startup: surfacing {len(new_alerts)} visible ticker(s); "
                  "watching for new alerts…", flush=True)
        # POST every poll (even with no new alerts) — it doubles as a heartbeat
        # so the dashboard can show the source is alive on a quiet market.
        _post_ingest(new_alerts)
        elapsed = time.time() - t0
        time.sleep(max(0.0, poll_sec - elapsed))


if __name__ == "__main__":
    if "--check" in sys.argv or "--once" in sys.argv:
        raise SystemExit(check())
    try:
        main()
    except KeyboardInterrupt:
        print("\n[discord] stopped.", flush=True)
