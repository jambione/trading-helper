"""TradingView 4-indicator monitor (star / heart / check / fire).

Reads the calibrated panel regions ~1x/second by pixel position + OCR,
applies your hierarchy (heart = regime, check = strength, star = timing,
fire = context) and shows one verdict.

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

from tv_core import (Trail, combine, master_verdict, read_check, read_heart,
                     read_star, rightmost_data_x)

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

FIRE_WORDS = {"BULL": "BULL UP", "REVERSE": "REVERSE", "SHORT": "SHORT "
              "SELLERS CONTROL"}


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    tp = cfg.get("tesseract_path",
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    import os
    if tp and os.path.exists(tp):
        pytesseract.pytesseract.tesseract_cmd = tp
    return cfg


def read_fire(img: np.ndarray) -> str | None:
    """OCR the whole fire panel: the TREND STRENGTH widget (the actual
    big picture) plus the newest signal label."""
    import re
    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(th, config="--psm 11").upper()

    parts = []
    if "STRONG BULLISH" in text:
        parts.append("STRONG BULLISH")
    elif "BULLISH" in text and "BEARISH" not in text:
        parts.append("BULLISH")
    elif "WEAK" in text or "BEARISH" in text:
        parts.append("WEAK/BEARISH")
    m = re.search(r"(\d)\s*/\s*4", text)
    if m:
        parts.append(f"{m.group(1)}/4 trends up")
    if "WITH TREND" in text:
        parts.append("with trend")
    elif "AGAINST" in text:
        parts.append("against trend")
    # newest signal bubble, if any is legible
    for pat, label in (("BULL UP", "BULL UP"), ("REVERSE UP", "rev up"),
                       ("REVERSE DOWN", "rev down"),
                       ("SHORT SELLER", "short sellers ctl")):
        if pat in text:
            parts.append(label)
            break
    return ", ".join(parts) or None


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


def _assign_panels(toks: list) -> dict | None:
    """toks = [(y, value)] of axis labels sorted top->bottom. Uses the
    known scales: star 100..0, heart 0..-100, fire 1..-1, check between
    heart and fire. Returns {name: (top_y, bottom_y)} or None."""
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
    mid = toks[i_hbot + 1:i_ftop] if i_ftop is not None \
        else toks[i_hbot + 1:]
    if len(mid) >= 2:
        out["check"] = (mid[0][0], mid[-1][0])
    if i_ftop is not None and i_fbot is not None:
        out["fire"] = (toks[i_ftop][0], toks[i_fbot][0])
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

    # check/fire: between divider lines below the heart panel
    y_start = int(assign["heart"][1]) + 6
    divs = _panel_dividers(gray_full, y_start, H - 40, px0, px1)
    if len(divs) >= 2:
        panels["check"] = region(divs[0] + 3, divs[1] - 3)
        if len(divs) >= 3:
            panels["fire"] = region(divs[1] + 3, divs[2] - 3)
        else:
            panels["fire"] = region(divs[1] + 3,
                                    min(H - 45, divs[1] + 3 +
                                        (divs[1] - divs[0])))
    elif "check" in assign:
        panels["check"] = region(*assign["check"])

    return {k: v for k, v in panels.items() if v["height"] >= 15} or None


class TVTracker:
    """Auto-locates the panels; falls back to tv_calibrate regions."""

    MISS_LIMIT = 5

    def __init__(self, cfg: dict, console=None):
        self.manual = cfg.get("panels") or None
        self.console = console
        self.cached: dict | None = None
        self.win_rect = None
        self.misses = 0
        self.symbol: str | None = None

    def report(self, ok: bool):
        self.misses = 0 if ok else self.misses + 1

    def get(self, sct) -> dict | None:
        cands = find_tv_windows()
        if not cands:
            return self.cached or self.manual
        # the active ticker leads the chart tab's title ("NVDA 204.12 ...")
        for _, title in cands:
            m = _TICKER_TITLE.match(title)
            if m:
                self.symbol = m.group(0).split()[0]
                break
        if (self.cached and self.win_rect in [r for r, _ in cands]
                and self.misses < self.MISS_LIMIT):
            return self.cached
        # confirm candidates by actually finding the panel axis labels -
        # that fingerprint only exists on the TradingView chart
        for rect, _ in cands[:3]:
            img = np.asarray(sct.grab(rect))
            panels = locate_tv_panels(img, rect)
            if panels:
                if panels != self.cached and self.console:
                    self.console.print(
                        f"[dim]TV panels located: {sorted(panels)}[/dim]")
                self.cached, self.win_rect = panels, rect
                self.misses = 0
                return self.cached
        self.misses = 0
        return self.cached or self.manual


L2_STATE = HERE.parent / "l2_state.json"
TV_STATE = HERE.parent / "tv_state.json"


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


def render(star, heart, check, fire_label, res, hz, misses,
           symbol: str | None = None, l2: dict | None = None,
           master: dict | None = None, mism: bool = False) -> Group:
    t = Table(title=f"TradingView Monitor [bold cyan]{symbol or '?'}"
                    f"[/bold cyan]   {hz:.1f} reads/s   miss:{misses}",
              expand=False)
    t.add_column("Indicator"), t.add_column("Reading", justify="left")

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
                  + ("  [bold red]SYMBOL MISMATCH[/bold red]" if mism
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
    if check:
        gc = "green" if check["gap"] > 0 else "red"
        t.add_row("✓ MACD (strength)",
                  f"[{gc}]gap {check['gap']:+.1f}%[/{gc}] "
                  f"[dim](signal vs MA; wider = stronger)[/dim]")
    else:
        t.add_row("✓ MACD", "[dim]no read[/dim]")
    t.add_row("🔥 Big Picture (context)", fire_label or "[dim]quiet[/dim]")
    return Group(banner(res, master), t)


def main():
    cfg = load_config()
    interval = cfg.get("poll_interval", 1.0)
    console = Console()
    tracker = TVTracker(cfg, console)

    trails = {"star": Trail(), "heart": Trail(), "gap": Trail()}
    logf = open(HERE / "tv_log.csv", "a", newline="")
    logw = csv.writer(logf)
    if logf.tell() == 0:
        logw.writerow(["time", "star", "star_color", "heart_w", "heart_b",
                       "shade", "gap", "fire", "verdict"])

    misses = 0
    stamps: list[float] = []
    fire_label = None
    fire_next = 0.0

    console.print("[bold]TradingView monitor - Ctrl+C to stop.[/bold] "
                  "Keep the chart visible on screen.")
    with mss.mss() as sct, Live(console=console,
                                refresh_per_second=2) as live:
        while True:
            t0 = time.time()
            panels = tracker.get(sct) or {}
            if not panels:
                live.update(render(None, None, None, None, None, 0.0,
                                   misses, tracker.symbol))
                time.sleep(2.0)
                continue
            star = heart = check = None
            try:
                if "star" in panels:
                    star = read_star(np.asarray(sct.grab(panels["star"])))
                if "heart" in panels:
                    heart = read_heart(np.asarray(sct.grab(panels["heart"])))
                if "check" in panels:
                    check = read_check(np.asarray(sct.grab(panels["check"])))
                if "fire" in panels and t0 >= fire_next:
                    fire_label = read_fire(
                        np.asarray(sct.grab(panels["fire"])))
                    fire_next = t0 + cfg.get("fire_interval", 5.0)
            except Exception:
                misses += 1

            if star:
                trails["star"].add(star["value"], t0)
            if heart:
                hv = heart["w"] if heart["w"] is not None else heart["b"]
                trails["heart"].add(hv, t0)
            if check:
                trails["gap"].add(check["gap"], t0)
            ok = bool(star or heart or check)
            tracker.report(ok)
            if not ok:
                misses += 1

            res = combine(star, heart, check,
                          trails["star"].slope(10),
                          trails["heart"].slope(30),
                          trails["gap"].slope(15),
                          fire_label)

            # bridge: merge with live L2 order flow when available
            l2 = read_l2_state()
            mism = bool(l2 and not _sym_match(l2.get("symbol"),
                                              tracker.symbol))
            master = (None if (l2 is None or mism)
                      else master_verdict(res["verdict"], l2))
            try:
                TV_STATE.write_text(json.dumps(
                    {"ts": t0, "symbol": tracker.symbol,
                     "verdict": res["verdict"]}))
            except Exception:
                pass

            logw.writerow([
                datetime.now().isoformat(timespec="seconds"),
                fmt(star and star["value"], ".1f", ""),
                star["color"] if star else "",
                fmt(heart and heart["w"], ".1f", ""),
                fmt(heart and heart["b"], ".1f", ""),
                heart["shade"] if heart else "",
                fmt(check and check["gap"], ".2f", ""),
                fire_label or "", res["verdict"]])
            logf.flush()

            stamps.append(t0)
            stamps[:] = [s for s in stamps if t0 - s <= 5]
            live.update(render(star, heart, check, fire_label, res,
                               len(stamps) / 5.0, misses, tracker.symbol,
                               l2, master, mism))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
