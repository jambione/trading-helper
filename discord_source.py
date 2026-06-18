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
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, asdict
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

# A standard alert line carries this arrow marker between the ticker/headline and
# the data payload. Two-or-more ">" tolerates OCR dropping a couple of arrows.
_ALERT_MARKER = re.compile(r">>+")

# A "Squeeze Potential Alert" body reads e.g. "ATHE ww close over 6.78/7/7.50".
# These have no arrow marker; "close over" is the signature. We treat them as a
# strong catalyst → fire the burst toast immediately (see ingest, "burst" flag).
_SQUEEZE_MARKER = re.compile(r"(?i)\bclose\s+over\b")


# ── Sentiment extraction ──────────────────────────────────────────────────────
# Three non-alert content types in the channel become a rolling sentiment signal:
# bot market-direction alerts, human chat, and TradingView chart labels.

@dataclass
class SentimentEvent:
    ticker: str | None
    score:  float
    source: str      # "hv_alert" | "chat" | "tv_chart"
    raw:    str
    ts:     float


_HV_RE = re.compile(
    r'(?P<bull>🟢|HIGH\s+VOLUME\s+BULLISH)|(?P<bear>🔴|HIGH\s+VOLUME\s+BEARISH)',
    re.IGNORECASE,
)
_CHAT_RE = re.compile(
    r'^\[?\d{1,2}:\d{2}\s*[AP]M\]?'
    r'(?P<user>[^:]{2,60})'
    r':\s*(?P<body>.+)$',
    re.IGNORECASE,
)
_TV_TREND_RE    = re.compile(r'TREND\s*:\s*(?P<trend>LONG|SHORT)', re.IGNORECASE)
_TV_STRATEGY_RE = re.compile(r'STRATEGY\s*:\s*(?P<strat>BULLISH|BEARISH)', re.IGNORECASE)

_CHAT_SKIP = frozenset({"HOD", "LOD", "PDT", "ATH", "AM", "PM", "EST", "ETF",
                        "IPO", "HALT", "SEC", "FDA", "CEO", "BULL", "BEAR"})

_BULLISH_KW = {
    "breakout": 1.0, "breaking out": 1.0, "ripping": 1.0, "mooning": 1.0,
    "red to green": 0.9, "new high": 0.9, "bounce": 0.7, "bouncing": 0.7,
    "hod": 0.8, "green": 0.6, "buying": 0.6, "long": 0.7, "bullish": 1.0,
    "calls": 0.8, "holding": 0.4, "support": 0.4, "running": 0.7,
    "flat to green": 0.8, "red to flat": 0.5, "loading": 0.6, "strong": 0.5,
}
_BEARISH_KW = {
    "breakdown": -1.0, "dumping": -1.0, "tanking": -1.0, "bearish": -1.0,
    "all out": -0.7, "selling": -0.7, "loss": -0.6, "sold": -0.6,
    "choppy": -0.5, "chop": -0.4, "red": -0.5, "down": -0.4,
    "fading": -0.6, "rejected": -0.7, "failed": -0.6, "stopped": -0.5,
    "short": -0.8, "puts": -0.8, "dump": -1.0, "lod": -0.8, "weak": -0.5,
}
_EMOJI_SCORES = {
    "🚀": 1.0, "📈": 0.9, "🟢": 0.6, "🔥": 0.7, "💃": 0.5, "🙌": 0.5,
    "📉": -0.9, "🔴": -0.6, "💀": -0.7, "😱": -0.5,
}
# Sorted longest-first so multi-word phrases match before their component words.
_KW_SORTED = sorted({**_BULLISH_KW, **_BEARISH_KW}.items(), key=lambda kv: -len(kv[0]))


class _TvState:
    def __init__(self):
        self.pending_trend  = None
        self.pending_ticker = None


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


# ── Alert parsing ───────────────────────────────────────────────────────────

_PRICE_RE  = re.compile(r'(?:High|Current|Bar High|Bar Low|Close|Last)?\s*Price\s*[=:]\s*\$?([\d,]+\.?\d*)', re.I)
_VOLUME_RE = re.compile(r'(?:Total\s+)?Volume\s*[=:]\s*([\d,]+)', re.I)


def _parse_regular_meta(ticker: str, line: str) -> dict:
    """Extract alert_type, price, and volume from a standard >>>>> alert line."""
    parts = re.split(r'>>+', line, maxsplit=1)
    headline     = parts[0].strip()
    data_section = parts[1].strip() if len(parts) > 1 else ""

    # Strip leading ticker (and any stray punctuation/whitespace) from headline
    alert_type = re.sub(rf'(?i)^\W*{re.escape(ticker)}\s*', '', headline).strip()
    alert_type = re.sub(r'[!?.,]+$', '', alert_type).strip().title()

    price = None
    m = _PRICE_RE.search(data_section)
    if m:
        try:
            price = float(m.group(1).replace(',', ''))
        except ValueError:
            pass

    volume = None
    m = _VOLUME_RE.search(data_section)
    if m:
        try:
            volume = int(m.group(1).replace(',', ''))
        except ValueError:
            pass

    return {"alert_type": alert_type, "price": price, "volume": volume}


def _parse_squeeze_levels(line: str) -> list[float]:
    """Extract slash-separated price levels from a 'close over X/Y/Z' line."""
    m = re.search(r'(?i)close\s+over\s+([\d./]+)', line)
    if not m:
        return []
    levels = []
    for part in m.group(1).split('/'):
        try:
            levels.append(float(part.strip()))
        except ValueError:
            pass
    return levels


def parse_alert_line(line: str) -> tuple[str, str, dict] | tuple[None, None, dict]:
    """Parse one OCR line into (ticker, kind, meta), or (None, None, {}) if it
    isn't an alert. kind is "squeeze" or "alert". meta contains:
      - regular: {alert_type, price, volume}
      - squeeze: {levels: [float, ...]}
    """
    is_squeeze = bool(_SQUEEZE_MARKER.search(line))
    if not (is_squeeze or _ALERT_MARKER.search(line)):
        return None, None, {}
    for tok in line.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok)
        if not alpha:
            continue
        sym = alpha.upper()
        if 2 <= len(sym) <= 5 and is_valid_ticker(sym):
            if is_squeeze:
                meta = {"levels": _parse_squeeze_levels(line)}
            else:
                meta = _parse_regular_meta(sym, line)
            return sym, ("squeeze" if is_squeeze else "alert"), meta
        return None, None, {}
    return None, None, {}


def _first_valid_ticker(text: str) -> str | None:
    for tok in text.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok).upper()
        if 2 <= len(alpha) <= 5 and alpha not in _CHAT_SKIP and is_valid_ticker(alpha):
            return alpha
    return None


def _score_text(body: str, original_line: str) -> float:
    body_l  = body.lower()
    raw_sum = 0.0
    for phrase, val in _KW_SORTED:
        if phrase in body_l:
            raw_sum += val
            body_l   = body_l.replace(phrase, " ")   # consume so sub-words can't re-match
    for emoji, val in _EMOJI_SCORES.items():
        if emoji in original_line:
            raw_sum += val
    return max(-1.0, min(1.0, raw_sum / 3.0))


def parse_market_signal(line: str) -> tuple[str | None, float, str]:
    m = _HV_RE.search(line)
    if not m:
        return None, 0.0, ""
    score = 1.0 if m.group("bull") else -1.0
    return "SPY", score, ""


def parse_chat_sentiment(line: str) -> tuple[str | None, float, str]:
    m = _CHAT_RE.match(line)
    if not m:
        return None, 0.0, ""
    body = m.group("body")
    if ">>>>>" in body or "bb live alert" in body.lower():
        return None, 0.0, ""
    ticker = _first_valid_ticker(body)
    score  = _score_text(body, line)
    if abs(score) < 0.15:
        return None, 0.0, ""
    return ticker, score, body.strip()


def _update_tv_state(line: str, tv_state: "_TvState",
                     new_sentiment: list, seen: dict, t0: float) -> None:
    """Pair TREND + STRATEGY chart labels across consecutive OCR lines into a
    tv_chart sentiment event. The ticker comes from the chart header line that
    precedes the labels. Cross-scan dedupe is gated on the STRATEGY line's
    signature so a chart visible across polls only fires once."""
    tkr = _first_valid_ticker(line)
    if tkr:
        tv_state.pending_ticker = tkr
    if _TV_TREND_RE.search(line):
        tv_state.pending_trend = True
        return
    sm = _TV_STRATEGY_RE.search(line)
    if sm and tv_state.pending_trend is not None:
        sig = _signature(line)
        if sig not in seen:
            seen[sig] = t0
            score = 1.0 if sm.group("strat").upper() == "BULLISH" else -1.0
            new_sentiment.append(
                SentimentEvent(tv_state.pending_ticker, score, "tv_chart", line.strip(), t0))
        tv_state.pending_trend = None


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
        # Warn when the source file is newer than the compiled binary.
        if OCR_SCRIPT.exists() and OCR_SCRIPT.stat().st_mtime > OCR_BINARY.stat().st_mtime:
            print("[discord] WARNING: discord_ocr.swift is newer than the compiled binary — "
                  "rebuild with:  bash scripts/build_ocr.sh", flush=True)
        cmd = [str(OCR_BINARY)]
    elif OCR_SCRIPT.exists():
        # Fallback: run via the swift interpreter (slower startup).
        cmd = ["swift", str(OCR_SCRIPT)]
    else:
        print("[discord] ERROR: discord_ocr not found. Build it:", flush=True)
        print("  bash scripts/build_ocr.sh", flush=True)
        raise SystemExit(1)
    cmd += ["--owner", owner]
    if title:
        cmd += ["--title", title]
    return cmd


def _run_ocr(cmd: list[str]) -> tuple[list[str], bool]:
    """Run the OCR binary once. Returns (lines, ok); ok=False means a process-level
    failure (window not found, binary crashed, timeout) — distinct from a successful
    capture that returned zero lines (quiet Discord channel)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print("[discord] OCR timed out", flush=True)
        return [], False
    if out.returncode != 0:
        lines = [ln for ln in (out.stderr or "").strip().splitlines() if ln.strip()]
        if not lines:
            detail = str(out.returncode)
        elif len(lines) <= 2:
            detail = " | ".join(lines)
        else:
            detail = f"{lines[0]} | … | {lines[-1]}"
        print(f"[discord] OCR failed (rc={out.returncode}): {detail}", flush=True)
        return [], False
    return [ln for ln in out.stdout.splitlines() if ln.strip()], True


def _post_ingest(alerts: list[dict], sentiment: list) -> None:
    """POST this poll's newly-captured alerts (and an implicit heartbeat) to the
    dashboard. Sent every poll even when empty so the dashboard knows the source
    is alive. Each alert drives the mention system + the live feed downstream.
    sentiment carries SentimentEvent records (market/chat/chart direction)."""
    try:
        body = json.dumps({"alerts": alerts,
                           "sentiment": [asdict(e) for e in sentiment]}).encode()
        req  = urllib.request.Request(
            f"{DASHBOARD_URL}/api/discord/ingest",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        for a in alerts:
            print(f"  → {a['ticker']}", flush=True)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError) or "timed out" in str(e).lower():
            print(f"[discord] POST timeout — dashboard slow? ({e})", flush=True)
        elif alerts:
            print(f"  → {[a['ticker'] for a in alerts]}  (API error: {e})", flush=True)
        else:
            print(f"[discord] heartbeat POST failed: {e}", flush=True)
    except Exception as e:
        if alerts:
            print(f"  → {[a['ticker'] for a in alerts]}  (API error: {e})", flush=True)
        else:
            print(f"[discord] heartbeat POST failed: {e}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

# Signatures expire after one full trading session so early-session alerts can
# never be evicted by sheer volume and accidentally re-fire later in the day.
_SESSION_TTL = 8 * 3600   # 8 hours

# Backoff caps how long we wait between retries when the Discord window can't be
# found (e.g. Discord is closed or on a different Space).
_MAX_BACKOFF_SEC = 60.0


def check() -> int:
    """One-shot self-test: capture once, report what OCR saw and parsed, and give
    a verdict. No priming, no POST, no loop. Returns a shell exit code so it's
    scriptable.  Run:  python discord_source.py --check"""
    cfg          = _load_config()
    cmd          = _ocr_command(cfg)
    lines, ok    = _run_ocr(cmd)
    if not ok:
        print("[discord] VERDICT: ✗ OCR process failed — see error above.")
        return 1
    alerts = []
    for ln in lines:
        tkr, kind, _ = parse_alert_line(ln)
        if tkr:
            alerts.append((tkr, kind, ln))

    print(f"[discord] OCR read {len(lines)} text line(s) from the Discord window.")
    if not lines:
        print("[discord] VERDICT: ✗ window not captured — is Discord open, on this "
              "display/Space, and NOT minimized? (Grant Screen Recording if prompted.)")
        return 1
    if not alerts:
        print("[discord] VERDICT: ⚠ window captured, but no alert line is visible. "
              "Scroll the alert channel so the latest alerts are on screen.")
        return 2
    print(f"[discord] parsed {len(alerts)} alert(s):")
    for tkr, kind, ln in alerts:
        tag = "🔥burst" if kind == "squeeze" else "mention"
        print(f"   {tkr:6} {tag:8} <=  {ln[:70]}")
    print("[discord] VERDICT: ✓ working — these tickers would post as they newly appear "
          "(squeeze alerts fire the burst toast).")
    return 0


def main() -> None:
    cfg      = _load_config()
    poll_sec = float(cfg.get("discord_ocr_poll_sec", 2.5) or 2.5)
    cmd      = _ocr_command(cfg)

    print(f"[discord] OCR source started — polling every {poll_sec:g}s", flush=True)
    print(f"[discord] command: {' '.join(cmd)}", flush=True)
    print(f"[discord] posting alerts → {DASHBOARD_URL}/api/discord/ingest", flush=True)

    # sig → first-seen timestamp; entries expire after _SESSION_TTL seconds.
    seen: dict[str, float] = {}
    # First scan is special: the channel already shows several alerts. We surface
    # each VISIBLE ticker once (so the watchlist populates immediately) but never
    # re-post the same on-screen lines, and we collapse to one-per-ticker so a
    # screen full of repeats (e.g. MTEN ×7) can't fake a startup burst. After the
    # first scan, every genuinely new alert line is posted as it appears.
    primed      = False
    fail_streak = 0   # consecutive OCR process failures (window not found, crash)

    while True:
        t0 = time.time()

        # Expire stale signatures from previous sessions.
        expired = [s for s, ts in seen.items() if t0 - ts > _SESSION_TTL]
        for s in expired:
            del seen[s]

        lines, ok = _run_ocr(cmd)

        if not ok:
            # Exponential backoff so we don't spam logs when Discord is closed.
            fail_streak += 1
            backoff = min(poll_sec * (2 ** (fail_streak - 1)), _MAX_BACKOFF_SEC)
            time.sleep(backoff)
            continue

        fail_streak = 0
        new_alerts: list[dict] = []
        new_sentiment: list[SentimentEvent] = []
        tv_state = _TvState()   # resets each scan
        first_frame: "OrderedDict[str, dict]" = OrderedDict()   # ticker → alert dict
        for line in lines:
            sig = _signature(line)
            ticker, kind, meta = parse_alert_line(line)
            if ticker:
                if sig in seen:
                    continue
                seen[sig] = t0
                tv_state.pending_ticker = ticker
                # A squeeze breakout is a strong catalyst → ask the dashboard to
                # fire the burst toast immediately (simulate a mention burst).
                alert = {"ticker": ticker, "line": line, "burst": kind == "squeeze", **meta}
                if primed:
                    new_alerts.append(alert)
                else:
                    first_frame.setdefault(ticker, alert)
                continue

            # Non-alert line: try the three sentiment parsers.
            m_ticker, m_score, _ = parse_market_signal(line)
            if m_score != 0.0:
                if sig in seen:
                    continue
                seen[sig] = t0
                new_sentiment.append(
                    SentimentEvent(m_ticker, m_score, "hv_alert", line.strip(), t0))
                continue

            _update_tv_state(line, tv_state, new_sentiment, seen, t0)

            c_ticker, c_score, c_raw = parse_chat_sentiment(line)
            if c_score != 0.0:
                if sig in seen:
                    continue
                seen[sig] = t0
                new_sentiment.append(
                    SentimentEvent(c_ticker, c_score, "chat", c_raw, t0))
        if not primed:
            primed = True
            new_alerts = list(first_frame.values())
            print(f"[discord] startup: surfacing {len(new_alerts)} visible ticker(s); "
                  "watching for new alerts…", flush=True)
        # POST every poll (even with no new alerts) — it doubles as a heartbeat
        # so the dashboard can show the source is alive on a quiet market.
        _post_ingest(new_alerts, new_sentiment)
        elapsed = time.time() - t0
        time.sleep(max(0.0, poll_sec - elapsed))


if __name__ == "__main__":
    if "--check" in sys.argv or "--once" in sys.argv:
        raise SystemExit(check())
    try:
        main()
    except KeyboardInterrupt:
        print("\n[discord] stopped.", flush=True)
