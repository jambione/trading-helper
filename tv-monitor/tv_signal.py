"""TradingView indicator monitor (star / heart / fire, optional check).

Reads the calibrated panel regions ~1x/second by pixel position,
applies your hierarchy (heart = regime, fire = LazyBear squeeze momentum
for strength + context, star = timing; check/MACD fills the strength
slot instead if the layout still has it) and shows one verdict.

Setup: python tv_calibrate.py   (once, with the chart visible)
Run:   python tv_signal.py

The chart must stay VISIBLE on screen - a hidden browser tab can't be read.
"""
from __future__ import annotations

import csv
import json
import sys
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

from tv_core import (LeaderTracker, Trail, bullish_score, combine,
                     grid_cells, master_verdict, read_check, read_heart,
                     read_squeeze, read_star, trend5)

# shared session clock (repo root) — optional: monitor runs fine without it
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from session_clock import session_line
except Exception:                                       # noqa: BLE001
    session_line = None

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "tv_config.json"

def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    tp = cfg.get("tesseract_path",
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    import os
    if tp and os.path.exists(tp):
        pytesseract.pytesseract.tesseract_cmd = tp
    return cfg


class SqueezeTracker:
    """Adds the one piece of memory raw read_squeeze() readings lack:
    black->gray on the zero line means the squeeze just FIRED - that's
    LazyBear's entry signal."""

    FIRED_SHOW = 180.0   # keep announcing a fire for this many seconds
    ARM_READS = 3        # consecutive 'on' reads before a release counts
    CANCEL_MOM = 0.5     # momentum through zero the other way kills it

    def __init__(self):
        self.state: str | None = None
        self.on_reads = 0
        self.fired_dir: str | None = None
        self.fired_at = 0.0

    def update(self, sq: dict | None, now: float) -> dict | None:
        """read_squeeze() fields + 'fired'/'fired_ago' (for combine)."""
        if sq is None:
            return None
        if sq["squeeze"] == "on":
            if self.state != "on":
                self.fired_dir, self.fired_at = None, 0.0
            self.state = "on"
            self.on_reads += 1
        elif sq["squeeze"] == "off":
            if self.state == "on" and self.on_reads >= self.ARM_READS:
                self.fired_dir = "LONG" if sq["mom"] > 0 else "SHORT"
                self.fired_at = now
            self.state = "off"
            self.on_reads = 0
        # a fired call is only alive while momentum stays on its side
        if ((self.fired_dir == "LONG" and sq["mom"] < -self.CANCEL_MOM)
                or (self.fired_dir == "SHORT"
                    and sq["mom"] > self.CANCEL_MOM)):
            self.fired_dir = None
        out = dict(sq)
        out["fired"] = out["fired_ago"] = None
        if self.fired_dir and now - self.fired_at < self.FIRED_SHOW:
            out["fired"] = self.fired_dir
            out["fired_ago"] = int(now - self.fired_at)
        return out

    @staticmethod
    def label(info: dict | None) -> str | None:
        if info is None:
            return None
        parts = []
        if info["fired"]:
            parts.append(f"FIRED {info['fired']} {info['fired_ago']}s ago")
        elif info["squeeze"] == "on":
            parts.append("squeeze ON - coiling")
        parts.append(f"mom {info['mom']:+.1f}% "
                     + ("building" if info["building"] else "fading"))
        return ", ".join(parts)


# ------------------------------------------------------- auto locate --------

_BROWSERS = ("chrome", "brave", "msedge", "firefox", "opera", "vivaldi")


# TradingView chart tabs title like "NVDA 204.12 ▲ +3.65% Unnamed" -
# symbol + price, NOT the word TradingView. Match both.
_TICKER_TITLE = __import__("re").compile(r"^[A-Z]{1,6}\s+\d+\.\d+")


def find_tv_windows() -> list[dict]:
    """All visible browser windows, best-first: titles that look like a
    TradingView chart (ticker+price or the word itself) sort ahead. The
    caller confirms by actually locating the indicator panels."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    results: list[tuple] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        exe = buf.value.rsplit("\\", 1)[-1].lower() if ok else ""
        if not any(b in exe for b in _BROWSERS):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w < 600 or hh < 400:
            return True
        score = (0 if "tradingview" in title.lower()
                 else 1 if _TICKER_TITLE.match(title)
                 else 2)
        results.append((score, -w * hh,
                        {"left": r.left, "top": r.top,
                         "width": w, "height": hh}, title))
        return True

    user32.EnumWindows(cb, 0)
    return [(r, t) for _, _, r, t in
            sorted(results, key=lambda x: (x[0], x[1]))]


# chart-furniture words the legend OCR must never mistake for a ticker
_NOT_TICKERS = {"O", "H", "L", "C", "D", "W", "M", "VOL", "OHLC", "USD",
                "NYSE", "NASDAQ", "CBOE", "ONE", "BATS", "ARCA", "AMEX",
                "OTC", "UTC", "EST", "EDT", "AM", "PM", "ET", "ADD",
                "COMPARE", "UNNAMED"}


def _ocr_symbol(strip: np.ndarray) -> str | None:
    """OCR an already-cropped legend strip for a ticker token."""
    gray = cv2.cvtColor(np.ascontiguousarray(strip), cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    txt = pytesseract.image_to_string(
        th, config="--psm 7 -c tessedit_char_whitelist="
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for tok in txt.split():
        if 1 <= len(tok) <= 6 and tok not in _NOT_TICKERS:
            return tok
    return None


def detect_slot_symbol(img: np.ndarray) -> str | None:
    """Ticker of one chart cell, read from its legend (top-left strip):
    TradingView titles cells like 'NVDA · 1 · Cboe One'. The window
    title can't help here - it only names the ACTIVE chart of a grid."""
    h, w = img.shape[:2]
    if h < 20 or w < 80:
        return None
    return _ocr_symbol(img[:min(44, h), :int(w * 0.45)])


def legend_rect(rect: dict) -> dict:
    """Screen region of a cell's legend strip, for cheap re-OCR without
    grabbing the whole cell."""
    return {"left": rect["left"], "top": rect["top"],
            "width": max(80, int(rect["width"] * 0.45)), "height": 44}


def _assign_panels(toks: list) -> dict | None:
    """toks = [(y, value)] of axis labels sorted top->bottom. Uses the
    known scales: star 100..0, heart 0..-100. check and fire (squeeze)
    have dynamic scales, so they come from the divider lines instead;
    the 1..-1 probe below only matches the legacy fire widget.
    Returns {name: (top_y, bottom_y)} or None."""
    def first(pred, start=0):
        for i in range(start, len(toks)):
            if pred(toks[i][1]):
                return i
        return None

    def prev(pred, start):
        for i in range(start, -1, -1):
            if pred(toks[i][1]):
                return i
        return None

    # anchor on heart's unique -100 label (price panels never have it),
    # then walk UP: heart-top zero, star-bottom zero, star's ~100.
    # Immune to stocks trading near $100.
    i_hbot = first(lambda v: v <= -95)
    if i_hbot is None:
        return None
    i_htop = prev(lambda v: abs(v) < 0.5, i_hbot - 1)
    if i_htop is None:
        return None
    i_sbot = prev(lambda v: abs(v) < 0.5, i_htop - 1)
    if i_sbot is None:
        return None
    i_star = prev(lambda v: 95 <= v <= 105, i_sbot - 1)
    if i_star is None:
        return None
    i_ftop = first(lambda v: 0.95 <= v <= 1.05, i_hbot + 1)
    i_fbot = (first(lambda v: -1.05 <= v <= -0.95, i_ftop + 1)
              if i_ftop is not None else None)

    out = {"star": (toks[i_star][0], toks[i_sbot][0]),
           "heart": (toks[i_htop][0], toks[i_hbot][0])}
    if i_ftop is not None and i_fbot is not None:   # legacy 1..-1 fire
        out["fire"] = (toks[i_ftop][0], toks[i_fbot][0])
        mid = toks[i_hbot + 1:i_ftop]
        if len(mid) >= 2:
            out["check"] = (mid[0][0], mid[-1][0])
        return out
    # check (MACD) and fire (squeeze) both have dynamic +x..0..-x
    # scales: each panel's labels run positive to negative, so a
    # negative label followed by a positive one marks the boundary.
    # A single group means only the squeeze is on the chart (the MACD
    # panel was dropped from the layout).
    mid = toks[i_hbot + 1:]
    split = None
    for i in range(len(mid) - 1):
        if mid[i][1] < 0 and mid[i + 1][1] > 0:
            split = i + 1
            break
    if split is not None and split >= 2 and len(mid) - split >= 2:
        out["check"] = (mid[0][0], mid[split - 1][0])
        out["fire"] = (mid[split][0], mid[-1][0])
    elif len(mid) >= 2:
        out["fire"] = (mid[0][0], mid[-1][0])
    return out


def _panel_dividers(gray: np.ndarray, y0: int, y1: int, x0: int,
                    x1: int) -> list[int]:
    """TradingView separates panels with thin uniform horizontal lines
    slightly brighter than the background. Return their row centers."""
    strip = gray[y0:y1, x0:x1].astype(float)
    if strip.size == 0:
        return []
    means = strip.mean(axis=1)
    stds = strip.std(axis=1)
    bg = float(np.median(means))
    mask = (stds < 7) & (means > bg + 6)
    centers, run = [], []
    for i, m in enumerate(mask):
        if m:
            run.append(i)
        elif run:
            centers.append(y0 + run[len(run) // 2])
            run = []
    if run:
        centers.append(y0 + run[len(run) // 2])
    # merge centers closer than 12px
    merged = []
    for c in centers:
        if merged and c - merged[-1] < 12:
            continue
        merged.append(c)
    return merged


def locate_tv_panels(win_img: np.ndarray, rect: dict) -> dict | None:
    """Find the indicator column by its -100 axis label (only the heart
    panel has one), read star/heart bounds from their fixed scales, then
    find check/fire boundaries from the panel divider lines. Works with
    sidebars/watchlists to the right of the chart."""
    H, W = win_img.shape[:2]
    gray_full = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    x_crop = int(W * 0.5)
    gray = gray_full[:, x_crop:]
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    d = pytesseract.image_to_data(
        th, config="--psm 11 -c tessedit_char_whitelist=0123456789.-",
        output_type=pytesseract.Output.DICT)
    toks = []
    for i, txt in enumerate(d["text"]):
        txt = txt.strip().rstrip(".")
        if not txt or len(txt) > 9:
            continue
        try:
            v = float(txt)
        except ValueError:
            continue
        toks.append((d["top"][i] + d["height"][i] / 2,
                     x_crop + d["left"][i], v))

    # anchor: a <=-95 token whose x-column also contains star's 100..0
    assign = anchor_x = None
    for ay, ax, av in sorted(t for t in toks if t[2] <= -95):
        col = sorted((y, v) for y, x, v in toks if abs(x - ax) < 60)
        a = _assign_panels(col)
        if a and "star" in a and "heart" in a:
            assign, anchor_x = a, ax
            break
    if assign is None:
        return None

    axis_x = int(anchor_x) - 8
    px1 = axis_x - 4
    # wide region: the newest bar can sit far left of the axis when the
    # time scale extends into future/overnight hours
    px0 = max(0, px1 - 1600)
    if px1 - px0 < 60:
        return None

    def region(ty, by):
        return {"left": rect["left"] + px0, "top": rect["top"] + int(ty),
                "width": px1 - px0, "height": int(by - ty)}

    panels = {"star": region(*assign["star"]),
              "heart": region(*assign["heart"])}

    # check/fire: axis-label extents snapped outward to the enclosing
    # divider lines. Charts show extra/double dividers (8 seen on a
    # 4-panel layout), so consecutive-divider counting is unreliable -
    # the labels say WHICH panel, the nearest divider says its edge.
    y_start = int(assign["heart"][1]) + 6
    divs = _panel_dividers(gray_full, y_start, H - 40, px0, px1)

    def refine(ty, by):
        top = max((d for d in divs if d <= ty + 8), default=ty - 6)
        bot = min((d for d in divs if d >= by - 8), default=by + 6)
        return region(top + 3, bot - 3)

    if "check" in assign or "fire" in assign:
        for k in ("check", "fire"):
            if k in assign:
                panels[k] = refine(*assign[k])
    elif len(divs) >= 2:   # no usable labels at all
        panels["check"] = region(divs[0] + 3, divs[1] - 3)
        if len(divs) >= 3:
            panels["fire"] = region(divs[1] + 3, divs[2] - 3)

    return {k: v for k, v in panels.items() if v["height"] >= 15} or None


class Slot:
    """One chart cell: its screen regions plus the per-symbol memory
    (trails, squeeze arming) that was global back when the monitor
    watched a single chart."""

    def __init__(self, idx: int, rect: dict | None, panels: dict,
                 pin: str | None = None):
        self.idx = idx
        self.rect = rect          # cell screen rect (None = manual regions)
        self.panels = panels
        self.pin = pin            # config-pinned symbol, beats OCR
        self.ocr_sym: str | None = None
        self.symbol: str | None = pin
        self.trails = {"star": Trail(), "heart": Trail(), "gap": Trail(),
                       "mom": Trail()}
        self.squeeze = SqueezeTracker()
        # newest reads, kept for render / state / log
        self.star = self.heart = self.check = None
        self.sq: dict | None = None
        self.res: dict | None = None
        self.trend: dict | None = None
        self.score = 0.0

    @property
    def mismatch(self) -> bool:
        """Pinned symbol disagrees with what the legend OCR sees."""
        return bool(self.pin and self.ocr_sym and self.pin != self.ocr_sym)

    def set_symbol(self, sym: str | None):
        """New ticker in this cell -> its history is someone else's."""
        if sym and sym != self.symbol:
            if self.symbol is not None:
                self.trails = {k: Trail() for k in self.trails}
                self.squeeze = SqueezeTracker()
            self.symbol = sym


class TVTracker:
    """Auto-locates chart cells and their panels; falls back to the
    tv_calibrate regions. One TradingView window may carry 1, 2, or 4
    charts - each candidate grid is confirmed by actually finding the
    indicator axis labels inside its cells."""

    MISS_LIMIT = 5
    _LAYOUTS = {"1x1": (1, 1), "1x2": (1, 2), "2x1": (2, 1), "2x2": (2, 2)}
    _AUTO_ORDER = ("2x2", "1x2", "2x1", "1x1")

    def __init__(self, cfg: dict, console=None):
        self.grid = str(cfg.get("grid", "auto"))
        self.manual = self._manual_slots(cfg)
        self.console = console
        self.cached: list[dict] | None = None  # [{rect, panels, symbol}]
        self.win_rect = None
        self.misses = 0
        self.title_symbol: str | None = None

    @staticmethod
    def _manual_slots(cfg: dict) -> list[dict] | None:
        sp = cfg.get("slots_panels") or {}
        if sp:
            return [{"rect": None, "panels": p, "symbol": None}
                    for _, p in sorted(sp.items())]
        if cfg.get("panels"):
            return [{"rect": None, "panels": cfg["panels"], "symbol": None}]
        return None

    def report(self, ok: bool):
        self.misses = 0 if ok else self.misses + 1

    def _locate_slots(self, win_img: np.ndarray, rect: dict) -> list[dict]:
        """Try grid layouts (most charts first) and keep the first where
        enough cells carry the panel fingerprint - 3 of 4 counts (one
        cell may be an empty/odd chart), 2 of 2, or the whole window."""
        H, W = win_img.shape[:2]
        layouts = ([self.grid] if self.grid in self._LAYOUTS
                   else self._AUTO_ORDER)
        for name in layouts:
            rows, cols = self._LAYOUTS[name]
            slots = []
            for cell in grid_cells(W, H, rows, cols):
                sub = np.ascontiguousarray(
                    win_img[cell["top"]:cell["top"] + cell["height"],
                            cell["left"]:cell["left"] + cell["width"]])
                sub_rect = {"left": rect["left"] + cell["left"],
                            "top": rect["top"] + cell["top"],
                            "width": cell["width"],
                            "height": cell["height"]}
                panels = locate_tv_panels(sub, sub_rect)
                if panels:
                    slots.append({"rect": sub_rect, "panels": panels,
                                  "symbol": detect_slot_symbol(sub)})
            n = rows * cols
            need = 1 if n == 1 else max(2, -(-n * 3 // 4))  # ceil(0.75n)
            if len(slots) >= need:
                return slots
        return []

    def get(self, sct) -> list[dict] | None:
        cands = find_tv_windows()
        if not cands:
            return self.cached or self.manual
        # the active ticker leads the chart tab's title ("NVDA 204.12 ...")
        for _, title in cands:
            m = _TICKER_TITLE.match(title)
            if m:
                self.title_symbol = m.group(0).split()[0]
                break
        if (self.cached and self.win_rect in [r for r, _ in cands]
                and self.misses < self.MISS_LIMIT):
            return self.cached
        # confirm candidates by actually finding the panel axis labels -
        # that fingerprint only exists on the TradingView chart
        for rect, _ in cands[:3]:
            img = np.asarray(sct.grab(rect))
            slots = self._locate_slots(img, rect)
            if slots:
                if self.console and (self.cached is None
                                     or len(slots) != len(self.cached)):
                    self.console.print(
                        f"[dim]TV grid located: {len(slots)} chart(s)[/dim]")
                self.cached, self.win_rect = slots, rect
                self.misses = 0
                return self.cached
        self.misses = 0
        return self.cached or self.manual


L2_STATE = HERE.parent / "l2_state.json"
TV_STATE = HERE.parent / "tv_state.json"

TREND_WIN = 300.0    # the "5m" trend lookback
TREND_MIN = 240.0    # history needed before a trail may vote


def drift5(tr: Trail) -> float | None:
    return tr.slope(TREND_WIN) if tr.span() >= TREND_MIN else None


def read_l2_state() -> dict | None:
    """Latest state published by the Webull L2 monitor (if running)."""
    try:
        d = json.loads(L2_STATE.read_text())
        if time.time() - d.get("ts", 0) > 10:
            return None   # stale - L2 monitor stopped or losing reads
        return d
    except Exception:
        return None


def _sym_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True   # unknown symbol -> don't block
    a, b = a.upper(), b.upper()
    return a == b or a.startswith(b) or b.startswith(a)


def fmt(v, spec=".0f", none="—"):
    return format(v, spec) if v is not None else none


def banner(res, master: dict | None = None) -> Panel:
    """The one thing you see from across the room. The MASTER verdict
    (chart + order flow) takes over when the L2 monitor is running."""
    if master:
        v = master["verdict"]
        if v == "EXECUTE BUY":
            txt, style, border = "▲ ▲ ▲   E X E C U T E   B U Y   ▲ ▲ ▲", \
                "bold black on green", "green"
        elif v == "EXECUTE SELL":
            txt, style, border = "▼ ▼ ▼   E X E C U T E   S E L L   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v == "EXIT NOW":
            txt, style, border = "▼ ▼ ▼   E X I T   N O W   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v.startswith("TAKE PROFIT"):
            txt, style, border = "$ $ $   T A K E   P R O F I T   $ $ $", \
                "bold black on gold1", "gold1"
        elif v.startswith("TIGHTEN"):
            txt, style, border = f"◆  {v} — protect the position", \
                "bold black on dark_orange", "dark_orange"
        elif v.startswith("HOLD"):
            txt, style, border = f"◈  {v}", \
                "bold black on green", "green"
        elif v.startswith("CONFLICT"):
            txt, style, border = "◆  CONFLICT — chart vs tape — HOLD", \
                "bold black on dark_orange", "dark_orange"
        elif v.startswith("BUY SETUP"):
            txt, style, border = "◉  BUY SETUP — waiting for order flow", \
                "bold black on cyan", "cyan"
        elif v.startswith("SELL"):
            txt, style, border = "◉  SELL LEANING — flow not confirming", \
                "bold white on grey35", "red"
        else:
            txt, style, border = "—   W A I T   —", \
                "bold yellow on grey23", "yellow"
        return Panel(Align.center(f"[{style}]  {txt}  [/]"),
                     border_style=border, padding=(1, 1))
    if not res:
        txt, style, border = "SCANNING …", "bold white on grey30", "grey50"
    else:
        v = res["verdict"]
        if v == "STRONG BUY":
            txt, style, border = "▲ ▲ ▲   S T R O N G   B U Y   ▲ ▲ ▲", \
                "bold black on green", "green"
        elif "BUY" in v:
            txt, style, border = ("▲ ▲   B U Y   ▲ ▲" if v == "BUY"
                                  else "▲  lean buy  ▲"), \
                "bold black on green", "green"
        elif "SELL" in v:
            txt, style, border = "▼ ▼ ▼   S E L L   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v.startswith("WATCH"):
            txt, style, border = "◉  GET READY — exhaustion zone", \
                "bold black on cyan", "cyan"
        else:
            txt, style, border = "—   W A I T   —", \
                "bold yellow on grey23", "yellow"
    return Panel(Align.center(f"[{style}]  {txt}  [/]"),
                 border_style=border, padding=(1, 1))


def squeeze_text(sq: dict | None) -> str:
    """Color-coded squeeze reading: direction by color, conviction by
    brightness, badges for the coil / fire states."""
    if not sq:
        return "[dim]no read[/dim]"
    mc = "green" if sq["mom"] > 0 else "red" if sq["mom"] < 0 else "yellow"
    if sq["building"]:
        mom = f"mom [bold {mc}]{sq['mom']:+.1f}%[/bold {mc}] " \
              f"[{mc}]building[/{mc}]"
    else:
        mom = f"mom [dim {mc}]{sq['mom']:+.1f}% fading[/dim {mc}]"
    if sq["fired"]:
        fc = "green" if sq["fired"] == "LONG" else "red"
        tc = "black" if fc == "green" else "white"
        return (f"[bold {tc} on {fc}] FIRED {sq['fired']} [/] "
                f"[dim]{sq['fired_ago']}s ago[/dim]  {mom}")
    if sq["squeeze"] == "on":
        return f"[bold black on yellow] SQUEEZE ON [/] coiling  {mom}"
    return mom


def leader_strip(lead: str | None, score: float | None,
                 l2_sym: str | None) -> Panel:
    """Which chart deserves the Webull window right now."""
    if not lead:
        return Panel(Align.center(
            "[dim]no bullish leader on the grid[/dim]"),
            border_style="grey50", padding=(0, 1))
    txt = f"[bold cyan]🎯 LEADER  {lead}[/bold cyan]"
    if score is not None:
        txt += f"  [green]{score:+.1f}[/green]"
    if l2_sym and not _sym_match(l2_sym, lead):
        txt += (f"    [bold black on yellow]  SWITCH WEBULL → {lead}  [/]"
                f"  [dim]L2 is on {l2_sym}[/dim]")
    elif l2_sym:
        txt += "    [dim]webull on target[/dim]"
    return Panel(Align.center(txt), border_style="cyan", padding=(0, 1))


def slots_table(slots: list[Slot], lead: str | None,
                l2_sym: str | None) -> Table:
    """One row per chart cell - the grid at a glance."""
    t = Table(expand=False)
    t.add_column("Slot"), t.add_column("Symbol"), t.add_column("Verdict")
    t.add_column("5m"), t.add_column("Score", justify="right")
    t.add_column("")
    for s in slots:
        v = s.res["verdict"] if s.res else "—"
        c = ("green" if "BUY" in v else "red" if "SELL" in v
             else "cyan" if v.startswith("WATCH") else "yellow")
        tr = s.trend["dir"] if s.trend else None
        ta = {"up": "[green]▲ up[/green]", "down": "[red]▼ down[/red]",
              "flat": "[yellow]→ flat[/yellow]"}.get(tr, "[dim]…[/dim]")
        tags = []
        if s.symbol and lead and _sym_match(s.symbol, lead):
            tags.append("🎯")
        if s.symbol and l2_sym and _sym_match(l2_sym, s.symbol):
            tags.append("⚡L2")
        if s.mismatch:
            tags.append(f"[red]pin {s.pin} ≠ ocr {s.ocr_sym}[/red]")
        t.add_row(str(s.idx), f"[bold cyan]{s.symbol or '?'}[/bold cyan]",
                  f"[{c}]{v}[/{c}]", ta, f"{s.score:+.1f}", " ".join(tags))
    return t


def detail_table(s: Slot | None, hz: float, misses: int,
                 l2: dict | None = None, master: dict | None = None,
                 l2_note: str | None = None,
                 symbol: str | None = None) -> Table:
    """Per-indicator readout for the focus slot (the chart the Webull
    window is on, else the leader)."""
    star = s.star if s else None
    heart = s.heart if s else None
    check = s.check if s else None
    sq = s.sq if s else None
    res = s.res if s else None
    trend = s.trend if s else None
    symbol = (s.symbol if s else None) or symbol
    t = Table(title=f"TradingView Monitor [bold cyan]{symbol or '?'}"
                    f"[/bold cyan]   {hz:.1f} reads/s   miss:{misses}",
              expand=False)
    t.add_column("Indicator"), t.add_column("Reading", justify="left")

    if session_line:
        t.add_row("⏱ Session", session_line())
    if master:
        mc = ("green" if "BUY" in master["verdict"]
              else "red" if "SELL" in master["verdict"]
              else "dark_orange" if "CONFLICT" in master["verdict"]
              else "yellow")
        t.add_row("MASTER (chart+flow)",
                  f"[bold {mc}]{master['verdict']}[/bold {mc}]  "
                  f"[dim]{master['why']}[/dim]")
    if l2:
        lc = ("green" if l2.get("bias", 0) >= 25
              else "red" if l2.get("bias", 0) <= -25 else "yellow")
        px = l2.get("price")
        pj = l2.get("proj")
        pxs = ""
        if px and pj:
            pc = "green" if pj > px else "red" if pj < px else "yellow"
            pxs = f"  px {px:.3f}→[{pc}]{pj:.3f}[/{pc}]"
        t.add_row("⚡ Order flow (L2)",
                  f"[{lc}]bias {l2.get('bias', 0):+.0f}[/{lc}]  "
                  f"imb {l2.get('imbalance', '—')}  "
                  f"play {l2.get('play', '—')}{pxs}  "
                  f"[cyan]{l2.get('symbol') or '?'}[/cyan]"
                  + (f"  [bold red]{l2_note}[/bold red]" if l2_note
                     else ""))
    else:
        t.add_row("⚡ Order flow (L2)",
                  "[dim]L2 monitor not running / stale[/dim]")
    if res:
        v = res["verdict"]
        c = ("green" if "BUY" in v
             else "red" if "SELL" in v
             else "cyan" if v.startswith("WATCH") else "yellow")
        t.add_row("VERDICT (chart)", f"[bold {c}]{v}[/bold {c}]")
        t.add_row("", f"[dim]{'; '.join(res['reasons'])}[/dim]")

    if star:
        sc = {"red": "red", "green": "green"}.get(star["color"], "white")
        t.add_row("★ RSI (timing)",
                  f"[{sc}]{star['value']:.0f}[/{sc}] "
                  f"[dim](rising=buy, falling=sell; superseded by ♥)[/dim]")
    else:
        t.add_row("★ RSI", "[dim]no read[/dim]")
    if heart:
        hc = {"red": "green", "blue": "red"}.get(heart["shade"], "yellow")
        t.add_row("♥ Exhaustion (regime)",
                  f"w {fmt(heart['w'])}  b {fmt(heart['b'])}  "
                  f"shade [{hc}]{heart['shade']}[/{hc}] "
                  f"[dim](-100 = exhausted/buy-zone)[/dim]")
    else:
        t.add_row("♥ Exhaustion", "[dim]no read[/dim]")
    if check:   # only on layouts that still carry a MACD panel
        gc = "green" if check["gap"] > 0 else "red"
        t.add_row("✓ MACD (strength)",
                  f"[{gc}]gap {check['gap']:+.1f}%[/{gc}] "
                  f"[dim](signal vs MA; wider = stronger)[/dim]")
    t.add_row("🔥 Squeeze (strength+context)", squeeze_text(sq))
    if trend:
        tc = {"up": "green", "down": "red"}.get(trend["dir"], "yellow")
        arrow = {"up": "▲", "down": "▼"}.get(trend["dir"], "→")
        det = "  ".join(f"{k} {v:+.1f}" for k, v in
                        (("heart", trend["heart"]), ("mom", trend["mom"]),
                         ("rsi", trend["star"])) if v is not None)
        t.add_row("📈 Trend (5m)",
                  f"[bold {tc}]{arrow} {trend['dir'].upper()}"
                  f"[/bold {tc}]  [dim]{det}[/dim]")
    else:
        t.add_row("📈 Trend (5m)", "[dim]warming up (needs ~5 min "
                                   "on this symbol)[/dim]")
    return t


def open_log():
    """tv_log.csv, now with a symbol column; a pre-grid file (old header)
    is rotated aside rather than mixed."""
    path = HERE / "tv_log.csv"
    try:
        if path.exists():
            head = path.open().readline()
            if head and "symbol" not in head:
                path.replace(HERE / "tv_log.old.csv")
    except OSError:
        pass
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if f.tell() == 0:
        w.writerow(["time", "symbol", "star", "star_color", "heart_w",
                    "heart_b", "shade", "gap", "fire", "verdict"])
    return f, w


def _beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 180)
        except Exception:
            pass


def sync_slots(slots: list[Slot], found: list[dict],
               pins: dict[str, str]) -> list[Slot]:
    """Bind Slot state to the located cells. A changed cell count means
    the layout changed - start fresh; otherwise keep each slot's trails
    and just refresh its regions."""
    if len(slots) != len(found):
        slots = []
        for i, f in enumerate(found):
            s = Slot(i + 1, f.get("rect"), f["panels"],
                     pin=pins.get(str(i + 1)))
            s.ocr_sym = f.get("symbol")
            slots.append(s)
    else:
        for s, f in zip(slots, found):
            s.rect, s.panels = f.get("rect"), f["panels"]
            # locate-time OCR is consumed once; the periodic legend
            # re-read owns ocr_sym afterwards
            seed = f.pop("symbol", None)
            if seed:
                s.ocr_sym = seed
    return slots


def refresh_slot_symbols(slots: list[Slot], sct):
    """Re-OCR each cell's legend strip (slow cadence, off the hot path)."""
    for s in slots:
        if s.rect is None:
            continue
        try:
            sym = _ocr_symbol(np.asarray(sct.grab(legend_rect(s.rect))))
        except Exception:
            continue
        if sym:
            s.ocr_sym = sym


def read_slot(s: Slot, sct, t0: float) -> bool:
    """Grab this cell's panels and refresh its verdict/trend/score.
    Returns True when anything was readable."""
    star = heart = check = sq_info = None
    p = s.panels
    try:
        if "star" in p:
            star = read_star(np.asarray(sct.grab(p["star"])))
        if "heart" in p:
            heart = read_heart(np.asarray(sct.grab(p["heart"])))
        if "check" in p:
            check = read_check(np.asarray(sct.grab(p["check"])))
        if "fire" in p:
            sq_info = s.squeeze.update(
                read_squeeze(np.asarray(sct.grab(p["fire"]))), t0)
    except Exception:
        pass

    if star:
        s.trails["star"].add(star["value"], t0)
    if heart:
        hv = heart["w"] if heart["w"] is not None else heart["b"]
        s.trails["heart"].add(hv, t0)
    if check:
        s.trails["gap"].add(check["gap"], t0)
    if sq_info:
        s.trails["mom"].add(sq_info["mom"], t0)

    s.trend = trend5(drift5(s.trails["heart"]), drift5(s.trails["mom"]),
                     drift5(s.trails["star"]))
    s.res = combine(star, heart, check,
                    s.trails["star"].slope(10),
                    s.trails["heart"].slope(30),
                    s.trails["gap"].slope(15),
                    sq_info, s.trend)
    s.star, s.heart, s.check, s.sq = star, heart, check, sq_info
    s.score = bullish_score(s.res["verdict"], s.trend, s.sq)
    return bool(star or heart or check or sq_info)


def main():
    cfg = load_config()
    interval = cfg.get("poll_interval", 1.0)
    sym_refresh = float(cfg.get("symbol_refresh", 20.0))
    pins = {str(k): str(v).upper()
            for k, v in (cfg.get("pins") or {}).items()}
    beep_on = bool(cfg.get("leader_beep", True))
    console = Console()
    tracker = TVTracker(cfg, console)
    leader = LeaderTracker(float(cfg.get("leader_margin", 1.5)),
                           int(cfg.get("leader_confirm_reads", 5)))

    slots: list[Slot] = []
    logf, logw = open_log()
    misses = 0
    stamps: list[float] = []
    last_sym = 0.0
    prev_lead: str | None = None

    console.print("[bold]TradingView monitor - Ctrl+C to stop.[/bold] "
                  "Keep the chart(s) visible on screen.")
    with mss.MSS() as sct, Live(console=console,
                                refresh_per_second=2) as live:
        while True:
            t0 = time.time()
            found = tracker.get(sct) or []
            if not found:
                live.update(Group(
                    banner(None, None),
                    detail_table(None, 0.0, misses,
                                 symbol=tracker.title_symbol)))
                time.sleep(2.0)
                continue
            slots = sync_slots(slots, found, pins)
            single = len(slots) == 1
            if t0 - last_sym >= sym_refresh:
                last_sym = t0
                refresh_slot_symbols(slots, sct)
            for s in slots:
                s.set_symbol(s.pin or s.ocr_sym
                             or (tracker.title_symbol if single else None))

            ok_any = False
            for s in slots:
                ok_any = read_slot(s, sct, t0) or ok_any
            tracker.report(ok_any)
            if not ok_any:
                misses += 1

            # leader election: who deserves the Webull window
            scores = {s.symbol: s.score
                      for s in slots if s.symbol and s.res}
            lead = leader.update(scores)
            if beep_on and lead and lead != prev_lead:
                _beep()
            prev_lead = lead

            # bridge: the slot the Webull window is on gets the MASTER
            # verdict (entry + position/exit logic); anywhere else the
            # leader strip tells you where to point it
            l2 = read_l2_state()
            l2_sym = l2.get("symbol") if l2 else None
            l2_slot = (next((s for s in slots if s.symbol
                             and _sym_match(l2_sym, s.symbol)), None)
                       if l2_sym else None)
            lead_slot = (next((s for s in slots if s.symbol
                               and _sym_match(s.symbol, lead)), None)
                         if lead else None)
            focus = l2_slot or lead_slot or (slots[0] if single else None)
            master = (master_verdict(l2_slot.res["verdict"], l2)
                      if l2_slot and l2_slot.res else None)
            l2_note = (f"L2 on {l2_sym or '?'} — not in grid"
                       if l2 and not l2_slot else None)

            symbols_out = {}
            for s in slots:
                if s.symbol and s.res:
                    symbols_out[s.symbol] = {
                        "slot": s.idx, "verdict": s.res["verdict"],
                        "score": round(s.score, 2),
                        "trend": s.trend["dir"] if s.trend else None}
            try:
                TV_STATE.write_text(json.dumps(
                    {"ts": t0, "leader": lead, "l2_symbol": l2_sym,
                     "symbols": symbols_out,
                     # back-compat: the focus slot mirrors the old
                     # single-symbol fields
                     "symbol": focus.symbol if focus else lead,
                     "verdict": (focus.res["verdict"]
                                 if focus and focus.res else None)}))
            except Exception:
                pass

            for s in slots:
                if not s.res:
                    continue
                logw.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    s.symbol or "",
                    fmt(s.star and s.star["value"], ".1f", ""),
                    s.star["color"] if s.star else "",
                    fmt(s.heart and s.heart["w"], ".1f", ""),
                    fmt(s.heart and s.heart["b"], ".1f", ""),
                    s.heart["shade"] if s.heart else "",
                    fmt(s.check and s.check["gap"], ".2f", ""),
                    SqueezeTracker.label(s.sq) or "", s.res["verdict"]])
            logf.flush()

            stamps.append(t0)
            stamps[:] = [x for x in stamps if t0 - x <= 5]
            parts = [banner(focus.res if focus else None, master)]
            if not single or (lead and l2_sym
                              and not _sym_match(l2_sym, lead)):
                parts.append(leader_strip(lead, scores.get(lead), l2_sym))
            if not single:
                parts.append(slots_table(slots, lead, l2_sym))
            parts.append(detail_table(focus, len(stamps) / 5.0, misses,
                                      l2, master, l2_note))
            live.update(Group(*parts))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
