"""
tv_capture_mac.py — window discovery and capture for macOS.

The Windows path enumerates browser windows with EnumWindows and screengrabs
their screen rect through mss. Neither survives the port. find_tv_windows()
returns [] off win32, and a screen-rect grab captures whatever is stacked on
top of that rect — which on this desk is routinely the monitor's own terminal
sitting over the browser.

Quartz fixes both. CGWindowListCopyWindowInfo enumerates windows with titles
and bounds; CGWindowListCreateImage captures a window *by id*, including the
parts another window is covering. The chart does not have to be frontmost,
unobstructed, or even fully on screen.

Two things worth knowing before touching this:

  • Scale. Window bounds come back in logical points (1470x956 on this
    display) while the captured image is Retina pixels — 2x here. Everything
    downstream (tv_core's colour masks, the panel constants in tv_signal) was
    written against 1:1 Windows captures, so capture() downscales to logical
    and the rest of the pipeline never sees the 2x.

  • Byte order. CGWindowListCreateImage returns 32Little + alphaFirst, which
    is B,G,R,A in memory — exactly what tv_core.color_mask expects. Verified
    against CGImageGetBitmapInfo rather than assumed. Do not add a swap.

Self-test:  python tv_capture_mac.py
"""
from __future__ import annotations

import sys
import time

import numpy as np

try:
    import Quartz.CoreGraphics as CG
    _IMPORT_ERR: str | None = None
except Exception as e:                                     # noqa: BLE001
    CG = None
    _IMPORT_ERR = str(e)

AVAILABLE = CG is not None and sys.platform == "darwin"

# Same browser set the Windows path matches on, minus the .exe assumption.
BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera",
            "vivaldi", "safari")

# A TradingView chart window is never this small; anything under is a popup,
# a picture-in-picture, or a devtools pane.
MIN_W, MIN_H = 600, 400


def _cg_windows() -> list[dict]:
    opts = (CG.kCGWindowListOptionOnScreenOnly
            | CG.kCGWindowListExcludeDesktopElements)
    return list(CG.CGWindowListCopyWindowInfo(opts, CG.kCGNullWindowID) or [])


def find_windows(browsers: tuple[str, ...] = BROWSERS,
                 min_w: int = MIN_W,
                 min_h: int = MIN_H) -> list[tuple[dict, str]]:
    """Visible browser windows as (rect, title), best-first.

    Shape matches find_tv_windows() on Windows so TVTracker needs no changes,
    with one addition: rect carries an "id" the capture side needs. mss ignores
    unknown keys, and dict equality on the id makes the tracker's rect-unchanged
    check strictly more correct — moving a window now invalidates the cache even
    if it lands on identical bounds.

    Ordering matches the Windows scorer: a title holding the word tradingview
    first, then one that looks like a chart tab ("NVDA 204.12 …"), then the
    rest; ties broken by area, largest first.
    """
    if not AVAILABLE:
        return []

    scored: list[tuple[int, int, dict, str]] = []
    for w in _cg_windows():
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if not any(b in owner for b in browsers):
            continue
        b = w.get("kCGWindowBounds") or {}
        width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
        if width < min_w or height < min_h:
            continue
        title = w.get("kCGWindowName") or ""
        rect = {"left": int(b.get("X", 0)), "top": int(b.get("Y", 0)),
                "width": width, "height": height,
                "id": int(w.get("kCGWindowNumber", 0))}
        low = title.lower()
        score = 0 if "tradingview" in low else (1 if looks_like_chart(title) else 2)
        scored.append((score, -(width * height), rect, title))

    scored.sort(key=lambda x: (x[0], x[1]))
    return [(r, t) for _, _, r, t in scored]


def looks_like_chart(title: str) -> bool:
    """'STKH 2.71 ▼ −24.72% Unnamed' — symbol then price, TradingView's tab
    title. Kept local so this module stays importable without tv_signal.

    Public because it doubles as the cheap gate on whether reading is worth
    attempting at all: the title tracks the ACTIVE tab, so a browser sitting
    on some other page fails this in about a millisecond, rather than costing
    a capture and a 200ms tesseract pass to discover there are no panels.
    """
    parts = title.split()
    if len(parts) < 2:
        return False
    sym = parts[0]
    if not (1 <= len(sym) <= 6 and sym.isalpha() and sym.isupper()):
        return False
    try:
        float(parts[1])
    except ValueError:
        return False
    return True


def capture(window_id: int, *, logical: bool = True) -> np.ndarray | None:
    """One window's content as a BGRA array, or None if it could not be read.

    Includes regions covered by other windows. `logical=True` downscales a
    Retina capture back to the window's point size so the pixel constants in
    tv_core / tv_signal keep the meaning they were written with.
    """
    if not AVAILABLE:
        return None
    img = CG.CGWindowListCreateImage(
        CG.CGRectNull, CG.kCGWindowListOptionIncludingWindow, int(window_id),
        CG.kCGWindowImageBoundsIgnoreFraming)
    if img is None:
        return None
    width, height = CG.CGImageGetWidth(img), CG.CGImageGetHeight(img)
    if width == 0 or height == 0:
        return None

    provider = CG.CGImageGetDataProvider(img)
    data = CG.CGDataProviderCopyData(provider)
    if data is None:
        return None
    stride = CG.CGImageGetBytesPerRow(img) // 4
    arr = np.frombuffer(data, dtype=np.uint8).reshape(height, stride, 4)
    arr = np.ascontiguousarray(arr[:, :width, :])          # drop row padding

    if logical:
        scale = _retina_scale(window_id, width)
        if scale > 1:
            arr = _downscale(arr, scale)
    return arr


def _retina_scale(window_id: int, pixel_width: int) -> int:
    """Captured pixels per logical point, from this window's own bounds."""
    for w in _cg_windows():
        if int(w.get("kCGWindowNumber", -1)) != int(window_id):
            continue
        pts = int((w.get("kCGWindowBounds") or {}).get("Width", 0))
        if pts > 0:
            return max(1, round(pixel_width / pts))
        break
    return 1


def _downscale(arr: np.ndarray, factor: int) -> np.ndarray:
    """Area-average down by an integer factor.

    cv2 when available (faster, and INTER_AREA is the right filter for
    downsampling), else a reshape-and-mean that gives the same result. Nearest-
    neighbour would be wrong here: a 1px indicator line landing on a dropped
    row would vanish, and a vanished line reads as "no data" rather than a
    wrong value — quieter, but still a lie.
    """
    if factor <= 1:
        return arr
    h, w = arr.shape[0] // factor, arr.shape[1] // factor
    try:
        import cv2
        return cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
    except Exception:                                      # noqa: BLE001
        trimmed = arr[:h * factor, :w * factor]
        return (trimmed.reshape(h, factor, w, factor, 4)
                       .mean(axis=(1, 3)).astype(np.uint8))


class WindowCapture:
    """Duck-types mss for tv_signal: .grab(rect) -> BGRA ndarray.

    Captures the whole target window once per `ttl` and serves every rect as a
    crop of that single frame. Two reasons beyond saving screengrabs: all three
    indicator panels then come from the *same instant* (mss reads them
    milliseconds apart, so a fast tape can tear one panel against another), and
    a covered window still reads correctly.

    Rects are screen coordinates, matching what tv_signal computes today; they
    are translated to window-relative before cropping.
    """

    def __init__(self, ttl: float = 0.25):
        self.ttl = float(ttl)
        self.window_id: int | None = None
        self.origin: tuple[int, int] = (0, 0)
        self.ok = False
        self.last_error: str | None = None
        self._frame: np.ndarray | None = None
        self._at = 0.0

    # ── target ──────────────────────────────────────────────────────────────

    def set_target(self, rect: dict) -> None:
        """Point at a window using a rect from find_windows()."""
        wid = int(rect.get("id", 0))
        if wid != self.window_id:
            self._frame, self._at = None, 0.0
        self.window_id = wid
        self.origin = (int(rect.get("left", 0)), int(rect.get("top", 0)))

    # ── capture ─────────────────────────────────────────────────────────────

    def refresh(self, now: float | None = None, force: bool = False) -> bool:
        now = time.time() if now is None else now
        if not force and self._frame is not None and now - self._at < self.ttl:
            return True
        if self.window_id is None:
            self.ok, self.last_error = False, "no target window"
            return False
        frame = capture(self.window_id)
        if frame is None:
            # Do not keep serving the previous frame: a window that closed or
            # a capture that started failing must look like a miss, not like a
            # chart that stopped moving.
            self._frame, self.ok = None, False
            self.last_error = f"capture failed for window {self.window_id}"
            return False
        self._frame, self._at = frame, now
        self.ok, self.last_error = True, None
        return True

    def grab(self, rect: dict) -> np.ndarray:
        """Crop `rect` (screen coords) out of the cached window frame.

        Returns a black array of the requested size when the window cannot be
        read, so the colour-mask readers find nothing and return None — the
        same miss path a failed mss grab produces. Check .ok to tell a failed
        capture from a genuinely empty panel.
        """
        w = max(1, int(rect.get("width", 1)))
        h = max(1, int(rect.get("height", 1)))

        # A rect carrying an "id" came straight from find_windows(), so it
        # names the window to read. Panel rects derived downstream have no id
        # and are served from whatever window is already targeted. Retargeting
        # here keeps this a drop-in for mss.grab() — no call site has to know
        # which platform it is on.
        if rect.get("id") and int(rect["id"]) != self.window_id:
            self.set_target(rect)

        if not self.refresh() or self._frame is None:
            return np.zeros((h, w, 4), dtype=np.uint8)

        x = int(rect.get("left", 0)) - self.origin[0]
        y = int(rect.get("top", 0)) - self.origin[1]
        fh, fw = self._frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        if x1 <= x0 or y1 <= y0:
            self.last_error = "rect outside window"
            return np.zeros((h, w, 4), dtype=np.uint8)

        crop = self._frame[y0:y1, x0:x1]
        if crop.shape[0] == h and crop.shape[1] == w:
            return crop
        # Partially off-window: pad rather than hand back a differently-sized
        # array, since the readers convert row index to indicator value using
        # the panel height they were told to expect.
        out = np.zeros((h, w, 4), dtype=np.uint8)
        out[y0 - y:y0 - y + crop.shape[0], x0 - x:x0 - x + crop.shape[1]] = crop
        return out

    def frame(self) -> np.ndarray | None:
        """The whole cached window frame (for grid/panel location)."""
        return self._frame if self.refresh() else None

    # mss parity so callers can use it in a `with` block
    def __enter__(self) -> "WindowCapture":
        return self

    def __exit__(self, *exc) -> None:
        self._frame = None


def _selftest() -> int:
    if not AVAILABLE:
        print(f"NOT AVAILABLE on {sys.platform}"
              + (f" — Quartz import failed: {_IMPORT_ERR}" if _IMPORT_ERR else ""))
        return 1
    wins = find_windows()
    print(f"browser windows: {len(wins)}")
    for rect, title in wins:
        print(f"  id={rect['id']:<6} {rect['width']}x{rect['height']} "
              f"@{rect['left']},{rect['top']}  {title[:58]!r}")
    if not wins:
        print("no candidate windows — open a TradingView chart in Brave/Chrome")
        return 1

    rect, title = wins[0]
    cap = WindowCapture()
    cap.set_target(rect)
    frame = cap.frame()
    if frame is None:
        print(f"capture FAILED: {cap.last_error}")
        return 1
    print(f"\ncaptured {frame.shape[1]}x{frame.shape[0]} "
          f"(window is {rect['width']}x{rect['height']} pts)")
    lit = int((frame[:, :, :3].sum(axis=2) > 30).sum())
    print(f"non-black: {lit}/{frame.shape[0]*frame.shape[1]}")
    b, g, r = (int(frame[:, :, i].mean()) for i in range(3))
    print(f"channel means  B={b} G={g} R={r}")

    sub = cap.grab({"left": rect["left"] + 10, "top": rect["top"] + 10,
                    "width": 120, "height": 80})
    print(f"sub-crop 120x80 -> {sub.shape[1]}x{sub.shape[0]}  ok={cap.ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
