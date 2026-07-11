"""Webull Level-2 OCR signal monitor.

Captures the L2 order-book region of your screen, OCRs it, computes
imbalance/spread/wall signals, and alerts via console dashboard, sound,
and Windows toast. Logs every snapshot to CSV.

Region modes (config.json "region_mode"):
    "auto"   - finds the Webull window and locates the L2 panel by its
               header row; re-anchors when the window moves/resizes.
    "manual" - uses the fixed coordinates from calibrate.py.

Usage:
    python l2_signal.py

Requires: Tesseract-OCR installed (https://github.com/UB-Mannheim/tesseract/wiki)
and `pip install -r requirements.txt`.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np
import pytesseract
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from l2_core import (L2Book, LongView, PaperTrader, Signal, SignalEngine,
                     Trade, WallTracker, market_bias, parse_l2_text,
                     playbook, project_price)

# shared session clock (repo root) — optional: monitor runs fine without it
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from session_clock import session_line
except Exception:                                       # noqa: BLE001
    session_line = None

# Make this process DPI-aware so pygetwindow's window coordinates and
# mss's captured pixels are in the SAME coordinate space. Without this,
# Windows display scaling (125%/150%) makes the auto-located region land
# in the wrong place entirely.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
POS_STATE = HERE.parent / "position_state.json"   # scripts/position_feed.py

TESS_CFG = "--psm 6 -c tessedit_char_whitelist=0123456789.,K"


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    tp = cfg.get("tesseract_path")
    if tp and os.path.exists(tp):
        pytesseract.pytesseract.tesseract_cmd = tp
    return cfg


def read_real_position(symbol: str | None) -> dict | None:
    """The broker position for `symbol` from the position feed, or None.

    None also covers feed missing/stale/error — callers must not treat
    that as 'flat', so sync only happens when the feed is fresh and ok."""
    if not symbol:
        return None
    try:
        d = json.loads(POS_STATE.read_text())
        if not d.get("ok") or time.time() - d.get("ts", 0) > 30:
            return None
        sym = symbol.upper()
        for p in d.get("positions", []):
            ps = str(p.get("symbol", "")).upper()
            if ps == sym or ps.startswith(sym) or sym.startswith(ps):
                return p
        return {}   # feed fresh, symbol not held -> broker flat for us
    except Exception:
        return None


def preprocess(img: np.ndarray) -> np.ndarray:
    """Dark-theme screenshot -> clean black-text-on-white for Tesseract.

    Adaptive (local) threshold instead of global Otsu: the L2 rows have
    colored depth bars behind the numbers, and a global threshold merges
    text into the bars. Local thresholding separates each character from
    whatever is directly behind it."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:      # dark theme -> invert
        gray = 255 - gray
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 41, 15)


def ocr_book(sct, region: dict) -> L2Book | None:
    raw = np.asarray(sct.grab(region))
    # image_to_data gives word boxes; rebuild rows with explicit spaces so
    # narrow column gaps can't merge numbers together.
    d = pytesseract.image_to_data(preprocess(raw), config=TESS_CFG,
                                  output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list] = {}
    for i, word in enumerate(d["text"]):
        word = word.strip()
        if not word:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append((d["left"][i], word))
    text = "\n".join(
        " ".join(w for _, w in sorted(words))
        for _, words in sorted(lines.items())
    )
    book = parse_l2_text(text)
    if book is None:
        book = _ocr_by_columns(preprocess(raw))
    return book


def _ocr_by_columns(th: np.ndarray) -> L2Book | None:
    """Fallback when columns merge: locate the 4 columns via pixel
    projection, OCR each strip separately, then recombine rows."""
    ink = (th < 128).sum(axis=0)          # black-pixel count per x
    min_gap = max(8, th.shape[1] // 30)
    segs, start = [], None
    for x, v in enumerate(ink):
        if v and start is None:
            start = x
        elif not v and start is not None:
            segs.append([start, x]); start = None
    if start is not None:
        segs.append([start, th.shape[1]])
    merged = []
    for s in segs:                        # merge digit-level gaps
        if merged and s[0] - merged[-1][1] < min_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    if len(merged) < 4:
        return None
    merged = sorted(merged, key=lambda s: s[0] - s[1])[:4]  # 4 widest
    merged.sort(key=lambda s: s[0])
    cols = []
    for a, b in merged:
        pad = 6
        strip = th[:, max(0, a - pad):min(th.shape[1], b + pad)]
        txt = pytesseract.image_to_string(strip, config=TESS_CFG)
        cols.append([ln.strip() for ln in txt.splitlines() if ln.strip()])
    n = min(len(c) for c in cols)
    if n < 3:
        return None
    text = "\n".join(" ".join(c[i] for c in cols) for i in range(n))
    return parse_l2_text(text)


# ------------------------------------------------------- auto region --------

# UI words that look like tickers but aren't
_NOT_TICKERS = {"BID", "ASK", "SIZE", "VWAP", "MACD", "ATR", "EMA", "SMA",
                "RSI", "VOL", "NEWS", "EXT", "AUTO", "OPEN", "TAV", "EDT",
                "EST", "UTC", "AM", "PM", "NA", "OTC", "USD", "INC", "CO",
                "LTD", "LLC", "BUY", "SELL", "DAY", "VS", "TT", "HOD", "LOD"}


def _guess_symbol(raw_words: list[str]) -> str | None:
    """The active ticker appears several times on screen (chart header,
    quotes panel, order line); watchlist tickers only once. Pick the most
    frequent 2-5 letter uppercase token."""
    import collections
    import re as _re
    cands = [w for w in raw_words
             if _re.fullmatch(r"[A-Z]{2,5}", w) and w not in _NOT_TICKERS]
    if not cands:
        return None
    return collections.Counter(cands).most_common(1)[0][0]


def detect_symbol(win_img: np.ndarray) -> str | None:
    """OCR the Webull window and guess the active ticker symbol."""
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    d = pytesseract.image_to_data(th, config="--psm 11",
                                  output_type=pytesseract.Output.DICT)
    return _guess_symbol([w.strip() for w in d["text"] if w.strip()])

def find_webull_window() -> dict | None:
    """Bounding box of the (largest) visible window owned by the Webull
    PROCESS (Webull.exe). Matching by process instead of window title
    avoids grabbing unrelated windows that merely mention 'Webull' in
    their title (this console, chat apps, browsers, etc.)."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    results: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFO
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        if not ok or "webull" not in buf.value.rsplit("\\", 1)[-1].lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w > 500 and hh > 300:
            results.append({"left": r.left, "top": r.top,
                            "width": w, "height": hh})
        return True

    user32.EnumWindows(cb, 0)
    if not results:
        return None
    return max(results, key=lambda r: r["width"] * r["height"])


def locate_l2_region(win_img: np.ndarray, win_rect: dict,
                     debug_dir: Path | None = None) -> dict | None:
    """Find the L2 header row ('Size  Bid ... Ask  Size') inside a screenshot
    of the Webull window and return the screen region of the rows below it.

    Tries BOTH polarities (normal + inverted) because bright overlays can
    fool a single mean-based inversion decision. On failure, writes the
    detected bid/ask/size words to debug_words.txt when debug_dir is given.

    Decoys like 'Buy $800 @ASK' don't match because tokens must equal
    'bid'/'ask' exactly, sit close together, and have 'Size' on the row.
    """
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    scale = 2
    gray = cv2.resize(gray, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)
    dark_theme = np.median(gray) < 127
    variants = [255 - gray, gray] if dark_theme else [gray, 255 - gray]

    diag = []
    best = None
    for g in variants:
        _, th = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        d = pytesseract.image_to_data(th, config="--psm 11",
                                      output_type=pytesseract.Output.DICT)
        words = [(d["text"][i].strip().lower(), d["left"][i], d["top"][i],
                  d["width"][i], d["height"][i])
                 for i in range(len(d["text"])) if d["text"][i].strip()]
        bids = [w for w in words if w[0] == "bid"]
        asks = [w for w in words if w[0] == "ask"]
        sizes_all = [w for w in words if w[0] == "size"]
        diag.append((bids, asks, sizes_all))

        for b in bids:
            for a in asks:
                gap = a[1] - b[1]
                if gap <= 0 or abs(a[2] - b[2]) > b[4]:
                    continue  # ask must be right of bid, on the same row
                if gap > th.shape[1] // 4:
                    continue  # header Bid/Ask sit close together
                sizes = [w for w in words
                         if w[0] == "size" and abs(w[2] - b[2]) <= b[4]
                         and b[1] - 5 * gap < w[1] < a[1] + 5 * gap]
                if not sizes:
                    continue  # a real header has 'Size' flanking Bid/Ask
                score = len(sizes)
                if best is None or score > best[2]:
                    best = (b, a, score, sizes)
        if best:
            break

    if best is None:
        if debug_dir:
            try:
                with open(debug_dir / "debug_words.txt", "w") as f:
                    f.write("locator failed; words found per variant "
                            "(coords are at 2x window scale):\n")
                    for i, (bb, aa, ss) in enumerate(diag):
                        f.write(f"variant {i}: bid={bb}\n"
                                f"           ask={aa}\n"
                                f"           size={ss}\n")
            except Exception:
                pass
        return None
    b, a, _, sizes = best

    h = b[4]                      # text height at 2x
    gap = a[1] - b[1]
    lefts = [w[1] for w in sizes] + [b[1] - gap]
    rights = [w[1] + w[3] for w in sizes] + [a[1] + a[3] + gap]
    left, right = min(lefts) - h, max(rights) + h
    if right - left > 10 * gap:   # sanity: panel is only a few gaps wide
        left = max(left, b[1] - 3 * gap)
        right = min(right, a[1] + a[3] + 3 * gap)
    top = b[2] + int(h * 2.0)     # just below the header row
    height = int(h * 1.9 * 11)    # ~10-11 data rows
    # if the Trade/TurboTrader/Ladder tabs are visible below the header,
    # stop above them (short panels show fewer rows)
    tabs = [w[2] for w in words
            if w[0] in ("trade", "turbotrader", "ladder") and w[2] > b[2]]
    if tabs:
        height = min(height, min(tabs) - top - h // 2)

    region = {
        "left": win_rect["left"] + max(0, left // scale),
        "top": win_rect["top"] + top // scale,
        "width": (right - left) // scale,
        "height": height // scale,
    }
    # clamp inside the window
    region["width"] = min(region["width"],
                          win_rect["left"] + win_rect["width"] - region["left"])
    region["height"] = min(region["height"],
                           win_rect["top"] + win_rect["height"] - region["top"])
    if region["width"] < 60 or region["height"] < 40:
        return None
    return region


class RegionTracker:
    """Keeps the capture region pointed at the L2 panel even when the
    Webull window moves or resizes. Re-anchors when the window rect
    changes or several consecutive reads fail."""

    MISS_LIMIT = 5
    BAD_ANCHOR_LIMIT = 3     # anchors that produced zero good reads
    MANUAL_HOLD = 60         # seconds to sit on manual region before retrying auto

    def __init__(self, cfg: dict, console=None):
        self.mode = cfg.get("region_mode", "auto")
        self.manual = cfg.get("region")
        self.debug = cfg.get("debug", True)
        self.console = console
        self.region = self.manual if self.mode == "manual" else None
        self.win_rect = None
        self.misses = 0
        self.ok_since_anchor = 0
        self.bad_anchors = 0
        self.manual_until = 0.0
        self.symbol: str | None = None

    def report(self, ok: bool):
        if ok:
            self.misses = 0
            self.ok_since_anchor += 1
        else:
            self.misses += 1

    def _say(self, msg: str):
        if self.console:
            self.console.print(f"[dim]{msg}[/dim]")

    def get(self, sct) -> dict | None:
        if self.mode == "manual":
            return self.manual
        now = time.time()
        if now < self.manual_until:
            return self.manual
        rect = find_webull_window()
        if rect is None:
            return self.manual  # window not found; try manual as fallback
        if (self.region and rect == self.win_rect
                and self.misses < self.MISS_LIMIT):
            return self.region
        # window moved/resized or reads failing -> re-anchor
        if self.region is not None and self.ok_since_anchor == 0:
            self.bad_anchors += 1
        else:
            self.bad_anchors = 0
        if self.bad_anchors >= self.BAD_ANCHOR_LIMIT and self.manual:
            self._say("auto-locate keeps failing -> using calibrated region "
                      f"for {self.MANUAL_HOLD}s (check debug_anchor.png; "
                      "run calibrate.py to refresh the fallback)")
            self.manual_until = now + self.MANUAL_HOLD
            self.bad_anchors = 0
            self.misses = 0
            return self.manual
        img = np.asarray(sct.grab(rect))
        found = locate_l2_region(img, rect,
                                 HERE if self.debug else None)
        try:
            sym = detect_symbol(img)
            if sym:
                self.symbol = sym
        except Exception:
            pass
        if self.debug:
            dbg = np.ascontiguousarray(img[:, :, :3])
            if found:
                x = found["left"] - rect["left"]
                y = found["top"] - rect["top"]
                cv2.rectangle(dbg, (x, y),
                              (x + found["width"], y + found["height"]),
                              (0, 255, 0), 2)
            cv2.imwrite(str(HERE / "debug_anchor.png"), dbg)
        self.win_rect = rect
        self.misses = 0
        self.ok_since_anchor = 0
        if found:
            if found != self.region:
                self._say(f"L2 panel located at {found}")
            self.region = found
        elif self.region is None:
            self.region = self.manual
        return self.region


# ---------------------------------------------------------------- alerts ----

# distinct tones per event: high = get in / take money, low = get out
TONES = {"BUY": (1200, 200), "TAKE_PROFIT": (1500, 180),
         "TRAIL_EXIT": (1000, 220), "SELL": (500, 350), "STOP": (400, 450)}


def beep(action: str):
    try:
        import winsound
        freq, dur = TONES.get(action, (800, 250))
        for _ in range(3):
            winsound.Beep(freq, dur)
    except Exception:
        print("\a", end="", flush=True)


def toast(sig: Signal):
    try:
        from plyer import notification
        notification.notify(
            title=f"L2 {sig.action} signal @ {sig.price:.3f}",
            message=sig.reason,
            timeout=8,
        )
    except Exception:
        pass  # toast is best-effort


def alert(sig: Signal, cfg: dict):
    if cfg.get("sound", True):
        threading.Thread(target=beep, args=(sig.action,), daemon=True).start()
    if cfg.get("toast", True):
        threading.Thread(target=toast, args=(sig,), daemon=True).start()


# ------------------------------------------------------------------- log ----

class CsvLog:
    FIELDS = ["time", "best_bid", "best_ask", "spread_pct", "bid_size",
              "ask_size", "imbalance", "signal", "reason"]

    def __init__(self, path: Path):
        new = not path.exists()
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.FIELDS)

    def write(self, book: L2Book, sig: Signal | None):
        self.w.writerow([
            datetime.now().isoformat(timespec="milliseconds"),
            f"{book.best_bid:.4f}", f"{book.best_ask:.4f}",
            f"{book.spread_pct:.3f}", int(book.bid_size), int(book.ask_size),
            f"{book.imbalance:.3f}",
            sig.action if sig else "", sig.reason if sig else "",
        ])
        self.f.flush()


class TradeLog:
    """One row per completed round trip -> trades.csv. Review this to see
    which setups actually made money, then tune config thresholds."""
    FIELDS = ["entry_time", "entry", "exit_time", "exit", "pnl_pct", "reason"]

    def __init__(self, path: Path):
        new = not path.exists()
        self.f = open(path, "a", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.FIELDS)

    def write(self, tr: Trade):
        self.w.writerow([
            datetime.fromtimestamp(tr.entry_ts).isoformat(timespec="seconds"),
            f"{tr.entry:.4f}",
            datetime.fromtimestamp(tr.exit_ts).isoformat(timespec="seconds"),
            f"{tr.exit:.4f}", f"{tr.pnl_pct:+.2f}", tr.reason,
        ])
        self.f.flush()


# ------------------------------------------------------------- dashboard ----

def lv_banner(lv: dict | None) -> Panel:
    """The longer-view stance, big and unmissable at the top."""
    if not lv:
        txt, style, border = "WARMING UP …", "bold white on grey30", "grey50"
    else:
        held = lv["held"]
        dur = (f"{int(held // 60)}m{int(held % 60):02d}s"
               if held >= 60 else f"{int(held)}s")
        if lv["stance"] == "LONG":
            txt, style, border = f"▲ ▲   H O L D   L O N G   ({dur})   ▲ ▲", \
                "bold black on green", "green"
        elif lv["stance"] == "BEAR":
            txt, style, border = f"▼ ▼   S T A Y   O U T   ({dur})   ▼ ▼", \
                "bold white on red", "red"
        else:
            txt, style, border = f"→   N O   E D G E   ({dur})", \
                "bold yellow on grey23", "yellow"
        if lv.get("pending"):
            txt += f"   → {lv['pending']} in {int(lv['pending_left'])}s?"
    return Panel(Align.center(f"[{style}]  {txt}  [/]"),
                 border_style=border, padding=(1, 1))


def sparkline(vals: list[float], width: int = 40) -> str:
    """Unicode mini-chart of recent mid-prices."""
    if len(vals) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    vals = vals[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return "▄" * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in vals)


def _bias_meter(score: float, width: int = 21) -> str:
    """bearish ──────●───┼────────── bullish gauge, -100..+100."""
    pos = int(round((max(-100.0, min(100.0, score)) + 100) / 200 * (width - 1)))
    cells = ["─"] * width
    cells[width // 2] = "┼"
    cells[pos] = "●"
    return "".join(cells)


def _trend_cell(pct: float | None) -> str:
    if pct is None:
        return "…"
    arrow = "▲" if pct > 0.05 else "▼" if pct < -0.05 else "→"
    c = "green" if pct > 0.05 else "red" if pct < -0.05 else "yellow"
    return f"[{c}]{arrow} {pct:+.2f}%[/{c}]"


def render(book: L2Book | None, last_sig: Signal | None, reads: int,
           misses: int, hz: float, trader: PaperTrader | None = None,
           session_pnl: float = 0.0, n_trades: int = 0,
           trend1: float | None = None, trend5: float | None = None,
           spark: str = "", symbol: str | None = None,
           wall_events: list | None = None,
           lv: dict | None = None, proj: tuple | None = None) -> Group:
    t = Table(title=f"Webull L2 Monitor [bold cyan]{symbol or '?'}[/bold cyan]"
                    f"   {hz:.1f} reads/s   ok:{reads} miss:{misses}",
              expand=False)
    t.add_column("Metric"), t.add_column("Value", justify="right")
    if session_line:
        t.add_row("⏱ Session", session_line())
    if book:
        # one-glance verdict, always the first thing you see
        score, label, why = market_bias(book, trend1, trend5)
        bc = ("green" if label == "BULLISH"
              else "red" if label == "BEARISH" else "yellow")
        arrow = ("▲" if label == "BULLISH"
                 else "▼" if label == "BEARISH" else "→")
        t.add_row("MARKET BIAS",
                  f"[bold {bc}]{arrow} {label} ({score:+.0f})[/bold {bc}]")
        t.add_row("", f"[{bc}]{_bias_meter(score)}[/{bc}]  [dim]{why}[/dim]")

        # the daily-use playbook: 3 checks -> 1 action
        pb = playbook(book, trend1, trend5, wall_events or [])
        c1 = ("[green]✓ arrows agree ▲[/green]" if pb["trend_up"]
              else "[red]✓ arrows agree ▼[/red]" if pb["trend_dn"]
              else "[dim]✗ arrows disagree[/dim]")
        c2 = ("[green]✓ imbalance confirms[/green]" if pb["imb_up"]
              else "[red]✓ imbalance confirms[/red]" if pb["imb_dn"]
              else "[dim]✗ imbalance neutral[/dim]")
        if pb["ask_break"]:
            c3 = ("[green]● ask wall breaking @ "
                  + ", ".join(f"{p:.3f}" for p in pb["ask_break"]) + "[/green]")
        elif pb["bid_crack"]:
            c3 = ("[red]● support cracking @ "
                  + ", ".join(f"{p:.3f}" for p in pb["bid_crack"]) + "[/red]")
        else:
            c3 = "[dim]walls holding[/dim]"
        t.add_row("Checklist", f"{c1}   {c2}   {c3}")
        plays = {
            "LONG_TRIGGER": "[bold green]GO LONG — trend+flow aligned and "
                            "the wall above is breaking[/bold green]",
            "LONG_SETUP": "[green]LONG SETUP — aligned; wait for the ask "
                          "wall to break[/green]",
            "BEAR_CRACK": "[bold red]GET OUT — support cracking with "
                          "bearish flow[/bold red]",
            "BEARISH": "[red]BEARISH — stand aside / protect longs[/red]",
            "STAND_ASIDE": "[yellow]STAND ASIDE — checks don't line "
                           "up[/yellow]",
        }
        t.add_row("Play", plays[pb["verdict"]])

        if lv:
            held = lv["held"]
            dur = (f"{int(held // 60)}m{int(held % 60):02d}s"
                   if held >= 60 else f"{int(held)}s")
            stances = {
                "LONG": ("green", "▲ HOLD LONG"),
                "BEAR": ("red", "▼ STAY OUT / PROTECT"),
                "NEUTRAL": ("yellow", "→ NO EDGE"),
            }
            lc, ltxt = stances[lv["stance"]]
            drivers = (f"60s imb {lv['med_imb']:.2f}, "
                       f"5m {(lv['t5'] or 0):+.2f}%")
            if lv["ask_breaks"] or lv["bid_cracks"]:
                drivers += (f", walls {lv['ask_breaks']}▲/"
                            f"{lv['bid_cracks']}▼")
            line = f"[bold {lc}]{ltxt}[/bold {lc}]  ({dur})  [dim]{drivers}[/dim]"
            if lv["pending"]:
                pc, ptxt = stances[lv["pending"]]
                line += (f"   [dim]→ turning[/dim] [{pc}]{ptxt.split(' ', 1)[1]}"
                         f"[/{pc}] [dim]in {int(lv['pending_left'])}s "
                         f"if it holds[/dim]")
            t.add_row("Longer view", line)
        imb = book.imbalance
        color = "green" if imb > 1.3 else "red" if imb < 0.7 else "yellow"
        t.add_row("Best bid / ask", f"{book.best_bid:.3f} / {book.best_ask:.3f}")
        t.add_row("Spread", f"{book.spread:.3f} ({book.spread_pct:.2f}%)")
        t.add_row("Bid size (10 lvl)", f"{int(book.bid_size):,}")
        t.add_row("Ask size (10 lvl)", f"{int(book.ask_size):,}")
        t.add_row("Imbalance", f"[{color}]{imb:.2f}[/{color}]")
        walls = book.walls()
        t.add_row("Walls", "; ".join(f"{s} {p:.3f} x{int(z):,}"
                                     for s, p, z in walls) or "none")
        t.add_row("Move 1m / 5m",
                  f"{_trend_cell(trend1)}   {_trend_cell(trend5)}")
        if proj:
            mid, target, note = proj
            chg = 100.0 * (target - mid) / mid if mid else 0.0
            pc = ("green" if chg > 0.05 else "red" if chg < -0.05
                  else "yellow")
            t.add_row("Price → proj (5m)",
                      f"{mid:.3f} → [{pc}]{target:.3f} ({chg:+.2f}%)[/{pc}]"
                      + (f"  [dim]{note}[/dim]" if note else "")
                      + "  [dim]at current pace[/dim]")
        if spark:
            t.add_row("Price (recent)", spark)
        if trader and trader.in_position:
            up = trader.unrealized_pct(book)
            pc = "green" if up >= 0 else "red"
            tag = ("REAL LONG" if getattr(trader, "real", False)
                   else "paper LONG")
            t.add_row("Position",
                      f"[{pc}]{tag} @ {trader.entry:.3f}   "
                      f"uPnL {up:+.2f}%   peak {trader.peak_pct():+.2f}%[/{pc}]")
        else:
            t.add_row("Position", "flat")
        if n_trades:
            sc = "green" if session_pnl >= 0 else "red"
            t.add_row("Session (paper)",
                      f"[{sc}]{session_pnl:+.2f}% over {n_trades} trades[/{sc}]")
    else:
        t.add_row("Status", "[red]no clean OCR read yet[/red]")
    return Group(lv_banner(lv), t)


# ------------------------------------------------------------------ main ----

def main():
    cfg = load_config()
    engine = SignalEngine(cfg)
    trader = PaperTrader(cfg)
    log = CsvLog(HERE / "l2_log.csv") if cfg.get("csv_log", True) else None
    tlog = TradeLog(HERE / "trades.csv") if cfg.get("csv_log", True) else None
    interval = cfg.get("poll_interval", 0.35)
    console = Console()

    reads = misses = 0
    last_sig: Signal | None = None
    book: L2Book | None = None
    stamps: list[float] = []
    session_pnl = 0.0
    n_trades = 0
    walltrack = WallTracker(cfg.get("wall_multiple", 4.0))
    recent_events: list = []   # [(ts, event)] kept ~10s so you can react
    longview = LongView(cfg)
    lv: dict | None = None
    proj: tuple | None = None

    console.print("[bold]Starting — Ctrl+C to stop.[/bold] "
                  "Keep the L2 panel visible and unobstructed.")

    # real-position feed runs as a daemon thread so it lives and dies
    # with this window; exits then anchor to actual broker fills
    sys.path.insert(0, str(HERE.parent))
    try:
        from webull_bridge.position_feed import start_daemon
        if start_daemon():
            console.print("[dim]position feed on — exits anchor to real "
                          "broker fills[/dim]")
    except Exception as e:
        console.print(f"[dim]position feed unavailable: {e}[/dim]")
    tracker = RegionTracker(cfg, console)

    def _symbol_refresher():
        # keeps the displayed ticker current when you switch stocks
        with mss.MSS() as s2:
            while True:
                time.sleep(cfg.get("symbol_refresh", 30))
                rect = tracker.win_rect
                if not rect:
                    continue
                try:
                    sym = detect_symbol(np.asarray(s2.grab(rect)))
                    if sym:
                        tracker.symbol = sym
                except Exception:
                    pass

    threading.Thread(target=_symbol_refresher, daemon=True).start()
    with mss.MSS() as sct, Live(render(None, None, 0, 0, 0),
                                console=console, refresh_per_second=4) as live:
        while True:
            t0 = time.time()
            region = tracker.get(sct)
            if region is None:
                live.update(render(None, last_sig, reads, misses, 0.0))
                time.sleep(1.0)
                continue
            b = ocr_book(sct, region)
            tracker.report(b is not None)
            if b:
                reads += 1
                book = b
                sig = engine.update(b)
                # anchor exits to the real broker position when the feed
                # is fresh (None = feed unknown -> leave the trader alone)
                real = read_real_position(tracker.symbol)
                if real is not None:
                    trader.sync_real(real or None, b)
                exit_sig, trade = trader.update(b, sig)
                # exit alerts take priority (they're anchored to your entry)
                show = exit_sig or sig
                if exit_sig:
                    last_sig = exit_sig
                    alert(exit_sig, cfg)   # no-op unless re-enabled in config
                elif sig and not (sig.action == "SELL" and trade):
                    last_sig = sig
                    alert(sig, cfg)        # no-op unless re-enabled in config
                evs = walltrack.update(b)
                for ev in evs:
                    recent_events.append((t0, ev))
                recent_events[:] = [(ts, e) for ts, e in recent_events
                                    if t0 - ts <= 10]
                lv = longview.update(
                    b, engine.trend_pct(cfg.get("trend_window", 300)),
                    evs, t0)
                # projection: current pace extended 5 min, with the wall
                # standing in its way (if any) noted
                proj = None
                pp = project_price(engine.history,
                                   cfg.get("projection_minutes", 5.0))
                if pp:
                    mid, target = pp
                    note = ""
                    wm = cfg.get("wall_multiple", 4.0)
                    if target > mid:
                        blockers = [p for s, p, _ in b.walls(wm)
                                    if s == "ASK" and mid < p < target]
                        if blockers:
                            note = f"ask wall {min(blockers):.3f} in the way"
                    elif target < mid:
                        blockers = [p for s, p, _ in b.walls(wm)
                                    if s == "BID" and target < p < mid]
                        if blockers:
                            note = f"bid wall {max(blockers):.3f} may hold"
                    proj = (mid, target, note)

                # publish live state so the TradingView monitor can build
                # a combined chart+orderflow master verdict
                try:
                    score, _, _ = market_bias(b, engine.trend_pct(60),
                                              engine.trend_pct(300))
                    pb = playbook(b, engine.trend_pct(60),
                                  engine.trend_pct(300),
                                  [e for _, e in recent_events])
                    (HERE.parent / "l2_state.json").write_text(json.dumps({
                        "ts": t0, "symbol": tracker.symbol,
                        "bias": round(score, 1), "play": pb["verdict"],
                        "imbalance": round(b.imbalance, 2),
                        "price": round(proj[0], 4) if proj else None,
                        "proj": round(proj[1], 4) if proj else None,
                        "stance": lv["stance"] if lv else None,
                        "pos": ({"real": trader.real,
                                 "entry": round(trader.entry, 4),
                                 "upnl": round(trader.unrealized_pct(b), 2),
                                 "peak": round(trader.peak_pct(), 2)}
                                if trader.in_position else None)}))
                except Exception:
                    pass
                if trade:
                    session_pnl += trade.pnl_pct
                    n_trades += 1
                    if tlog:
                        tlog.write(trade)
                if log:
                    log.write(b, show)
            else:
                misses += 1
                if cfg.get("debug", True) and misses % 25 == 0:
                    # dump what the failing region looks like + raw OCR text
                    raw = np.asarray(sct.grab(region))
                    cv2.imwrite(str(HERE / "debug_region_raw.png"),
                                np.ascontiguousarray(raw[:, :, :3]))
                    th = preprocess(raw)
                    cv2.imwrite(str(HERE / "debug_region_th.png"), th)
                    txt = pytesseract.image_to_string(th, config=TESS_CFG)
                    (HERE / "debug_region_text.txt").write_text(
                        f"region={region}\n--- OCR text ---\n{txt}")
            stamps.append(t0)
            stamps[:] = [s for s in stamps if t0 - s <= 5]
            mids = [(bk.best_bid + bk.best_ask) / 2
                    for bk in list(engine.history)[-40:]]
            live.update(render(book, last_sig, reads, misses,
                               len(stamps) / 5.0, trader,
                               session_pnl, n_trades,
                               engine.trend_pct(60),
                               engine.trend_pct(cfg.get("trend_window", 300)),
                               sparkline(mids), tracker.symbol,
                               [e for _, e in recent_events], lv, proj))
            time.sleep(max(0.0, interval - (time.time() - t0)))


# ------------------------------------------------------ optional LLM hook ---
# To add periodic LLM commentary later (cfg["llm"]["enabled"] = true):
# every cfg["llm"]["interval"] seconds, send the last ~30 rows of l2_log.csv
# to the Anthropic API (model claude-haiku) with a prompt like:
#   "Given these L2 snapshots (bid/ask/sizes/imbalance over time), describe
#    order-book behavior in 2 sentences and flag accumulation/distribution."
# Run it in a background thread so it never blocks the OCR loop.

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
