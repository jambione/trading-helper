"""Native macOS Discord window capture + Vision OCR (no external OCR binary).

Capture strategy (in order):
  1. CGWindowListCreateImage for the Discord window id (preferred — captures
     that window's pixels even when another window overlaps its bounds).
  2. CGDisplayCreateImage + crop to the window rect (fast when it works; on
     some macOS builds it hangs/returns None despite Screen Recording).
  3. Fallback: /usr/sbin/screencapture -R <window rect> (uses the launcher
     app's Screen Recording — Ghostty/Terminal).

OCR: Apple Vision via the system Vision.framework (loaded with PyObjC).

Permission must be granted to:
  - the Terminal/Ghostty app that starts the stack, and/or
  - the Python binary that runs discord_source.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from typing import Any

_READY: bool | None = None
_VISION_LOADED = False
_REQUESTED_ACCESS = False


def available() -> bool:
    """True when Quartz + Vision can load on this Mac."""
    global _READY, _VISION_LOADED
    if _READY is not None:
        return _READY
    if sys.platform != "darwin":
        _READY = False
        return False
    try:
        from Quartz import (  # noqa: F401
            CGDisplayCreateImage,
            CGMainDisplayID,
            CGWindowListCopyWindowInfo,
        )
        import objc  # noqa: F401

        if not _VISION_LOADED:
            objc.loadBundle(
                "Vision",
                globals(),
                bundle_path="/System/Library/Frameworks/Vision.framework",
            )
            _VISION_LOADED = True
        _READY = "VNRecognizeTextRequest" in globals()
    except Exception:
        _READY = False
    return _READY


def _permission_hint() -> str:
    exe = sys.executable
    return (
        "Screen Recording is blocked for this process.\n"
        f"  Python: {exe}\n"
        "  System Settings → Privacy & Security → Screen & System Audio Recording\n"
        "  Enable: Ghostty (or Terminal) AND this Python if listed.\n"
        "  Then re-run:  python discord_source.py --check"
    )


def _active_displays() -> list[int]:
    from Quartz import CGGetActiveDisplayList, CGMainDisplayID

    try:
        err, displays, count = CGGetActiveDisplayList(32, None, None)
        if err == 0 and displays:
            return list(displays)[: int(count or 0)]
    except Exception:
        pass
    return [CGMainDisplayID()]


def _display_containing(x: float, y: float) -> int:
    from Quartz import CGDisplayBounds, CGMainDisplayID

    for did in _active_displays():
        db = CGDisplayBounds(did)
        if (db.origin.x <= x < db.origin.x + db.size.width
                and db.origin.y <= y < db.origin.y + db.size.height):
            return did
    return CGMainDisplayID()


def find_window(owner: str = "Discord", title: str = "") -> dict[str, Any] | None:
    """Largest on-screen window for owner (optional title substring)."""
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
    )

    wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    needle = (owner or "Discord").lower()
    title_n = (title or "").lower().strip()
    best: dict[str, Any] | None = None
    best_area = 0.0

    for w in wins or []:
        o = str(w.get("kCGWindowOwnerName") or "")
        if needle not in o.lower():
            continue
        t = str(w.get("kCGWindowName") or "")
        if title_n and title_n not in t.lower():
            continue
        b = w.get("kCGWindowBounds") or {}
        ww = float(b.get("Width") or 0)
        hh = float(b.get("Height") or 0)
        if ww < 200 or hh < 200:
            continue
        area = ww * hh
        if area > best_area:
            best_area = area
            best = {
                "id": int(w.get("kCGWindowNumber") or 0),
                "owner": o,
                "title": t,
                "x": float(b.get("X") or 0),
                "y": float(b.get("Y") or 0),
                "w": ww,
                "h": hh,
            }
    return best


def _capture_by_window_id(win: dict[str, Any]):
    """Preferred path: capture the specific window by id.

    CGDisplayCreateImage is flaky on recent macOS (returns None after ~5s even
    when CGPreflightScreenCaptureAccess is true). Window-list capture works when
    Screen Recording is granted to the launching terminal / Python.
    """
    from Quartz import (
        CGImageGetHeight,
        CGImageGetWidth,
        CGPreflightScreenCaptureAccess,
        CGRectNull,
        CGWindowListCreateImage,
        kCGWindowImageBoundsIgnoreFraming,
        kCGWindowImageDefault,
        kCGWindowListOptionIncludingWindow,
    )

    # Do NOT call CGRequestScreenCaptureAccess here — it can block tens of
    # seconds on a dialog and re-prompt endlessly when denied.
    if not CGPreflightScreenCaptureAccess():
        return None

    wid = int(win.get("id") or 0)
    if wid <= 0:
        return None

    for flags in (kCGWindowImageBoundsIgnoreFraming, kCGWindowImageDefault):
        try:
            img = CGWindowListCreateImage(
                CGRectNull,
                kCGWindowListOptionIncludingWindow,
                wid,
                flags,
            )
        except Exception:
            continue
        # None / empty = denied or unavailable; cold start can take 10–20s and
        # still succeed, so do not time-out a valid image.
        if img is None:
            continue
        try:
            w = CGImageGetWidth(img)
            h = CGImageGetHeight(img)
        except Exception:
            continue
        if w >= 50 and h >= 50:
            return img
    return None


def _capture_cgdisplay(win: dict[str, Any]):
    """Fallback: full-display grab + crop. Often fails on recent macOS."""
    from Quartz import (
        CGDisplayBounds,
        CGDisplayCreateImage,
        CGImageCreateWithImageInRect,
        CGImageGetHeight,
        CGImageGetWidth,
        CGPreflightScreenCaptureAccess,
        CGRectMake,
    )

    if not CGPreflightScreenCaptureAccess():
        return None

    cx = win["x"] + win["w"] / 2.0
    cy = win["y"] + win["h"] / 2.0
    did = _display_containing(cx, cy)

    t0 = time.time()
    full = CGDisplayCreateImage(did)
    # If macOS is going to hang/deny, bail quickly for fallback.
    if full is None or (time.time() - t0) > 2.0:
        return None

    db = CGDisplayBounds(did)
    scale_x = CGImageGetWidth(full) / db.size.width
    scale_y = CGImageGetHeight(full) / db.size.height

    left = max(win["x"], float(db.origin.x))
    top = max(win["y"], float(db.origin.y))
    right = min(win["x"] + win["w"], float(db.origin.x + db.size.width))
    bottom = min(win["y"] + win["h"], float(db.origin.y + db.size.height))
    if right - left < 50 or bottom - top < 50:
        return None

    local = CGRectMake(
        (left - db.origin.x) * scale_x,
        (top - db.origin.y) * scale_y,
        (right - left) * scale_x,
        (bottom - top) * scale_y,
    )
    crop = CGImageCreateWithImageInRect(full, local)
    if crop is None or CGImageGetWidth(crop) < 50 or CGImageGetHeight(crop) < 50:
        return None
    return crop


def _capture_screencapture(win: dict[str, Any]) -> str | None:
    """Fallback: region PNG via system screencapture. Returns path or None."""
    # Integer global points; -R is x,y,w,h
    x = int(round(win["x"]))
    y = int(round(win["y"]))
    w = int(round(win["w"]))
    h = int(round(win["h"]))
    if w < 50 or h < 50:
        return None

    fd, path = tempfile.mkstemp(prefix="discord_ocr_", suffix=".png")
    os.close(fd)
    try:
        # 6s hard cap — never block the poller on a TCC dialog.
        r = subprocess.run(
            ["/usr/sbin/screencapture", "-x", f"-R{x},{y},{w},{h}", path],
            capture_output=True,
            timeout=6,
        )
        if r.returncode != 0 or not os.path.isfile(path) or os.path.getsize(path) < 2000:
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        return path
    except (subprocess.TimeoutExpired, OSError):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _cgimage_from_png(path: str):
    from Foundation import NSURL
    from Quartz import CGImageSourceCreateImageAtIndex, CGImageSourceCreateWithURL

    url = NSURL.fileURLWithPath_(path)
    src = CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return CGImageSourceCreateImageAtIndex(src, 0, None)


def capture_window_image(win: dict[str, Any]):
    """Capture Discord window → CGImage. Raises RuntimeError if all paths fail."""
    img = _capture_by_window_id(win)
    if img is not None:
        return img

    img = _capture_cgdisplay(win)
    if img is not None:
        return img

    path = _capture_screencapture(win)
    if path:
        try:
            img = _cgimage_from_png(path)
            if img is not None:
                return img
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    raise RuntimeError(_permission_hint())


def ocr_cgimage(cg_image, accurate: bool = False) -> list[str]:
    """Run Apple Vision on a CGImage; return text lines top→bottom."""
    if not available():
        raise RuntimeError("Vision framework not available")

    req = VNRecognizeTextRequest.alloc().init()  # type: ignore[name-defined]
    req.setRecognitionLevel_(1 if accurate else 0)
    req.setUsesLanguageCorrection_(False)
    try:
        req.setRecognitionLanguages_(["en-US"])
    except Exception:
        pass

    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(  # type: ignore[name-defined]
        cg_image, None
    )
    performed = handler.performRequests_error_([req], None)
    if isinstance(performed, tuple):
        ok = bool(performed[0])
        err = performed[1] if len(performed) > 1 else None
    else:
        ok = bool(performed)
        err = None
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")

    results = req.results() or []

    def mid_y(obs) -> float:
        bb = obs.boundingBox()
        return float(bb.origin.y + bb.size.height / 2.0)

    lines: list[str] = []
    for obs in sorted(results, key=mid_y, reverse=True):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        s = str(cands[0].string() or "").strip()
        if s:
            lines.append(s)
    return lines


def capture_and_ocr(
    owner: str = "Discord",
    title: str = "",
    accurate: bool = False,
) -> list[str]:
    """Find window → capture → OCR. Raises RuntimeError on failure."""
    if not available():
        raise RuntimeError("native OCR unavailable (need macOS + PyObjC Quartz/Vision)")

    win = find_window(owner=owner, title=title)
    if not win:
        raise RuntimeError(
            f"no on-screen window found for owner '{owner}' — "
            "is Discord open and visible (not minimized)?"
        )
    img = capture_window_image(win)
    return ocr_cgimage(img, accurate=accurate)


if __name__ == "__main__":
    t0 = time.time()
    try:
        lines = capture_and_ocr()
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    dt = time.time() - t0
    print(f"OK {len(lines)} lines in {dt:.2f}s", file=sys.stderr)
    for ln in lines:
        print(ln)
