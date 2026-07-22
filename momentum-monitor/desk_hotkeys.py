"""Cross-platform keyboard + focus for the momentum desk monitor.

Windows: msvcrt
macOS / Linux: termios cbreak + select (non-blocking)

Trading (Alpaca B/S) and T=grab-TV were removed from this desk — letter keys
are reserved for Stocktwits trending load.
"""
from __future__ import annotations

import contextlib
import io
import sys
import threading
from datetime import datetime
from typing import Callable, Optional

import desk_actions as actions


class DeskHotkeys:
    """
    Keys:
      1-9   focus + load momentum row into TradingView
      SPACE focus + load top (newest) momentum row
      A-J   focus + load Stocktwits trending row (panel order)
            A = 1st ST row, B = 2nd, … J = 10th
    """

    SPACE = " "
    # Full A–J for ST panel (B/S/T free — no longer buy/sell/TV-grab)
    ST_LETTERS = tuple("abcdefghij")

    def __init__(self):
        self.tv_ok = actions.tv_load_available()
        # Keys only need a real terminal on Mac/Windows (TV load is optional)
        self.enabled = sys.platform in ("darwin", "win32")
        self.focus: Optional[str] = None
        self._by_key: dict[str, str] = {}       # "1".."9" → momentum symbol
        self._st_by_key: dict[str, str] = {}    # "a".."j" → Stocktwits symbol
        self._top: Optional[str] = None
        self._status = ""
        self._busy = False
        self._lock = threading.Lock()
        self._on_focus: Optional[Callable[[str], None]] = None
        if self.enabled:
            threading.Thread(target=self._reader, daemon=True,
                             name="desk-keys").start()

    def set_focus_callback(self, cb: Callable[[str], None]):
        self._on_focus = cb

    def update(self, ordered: list[str], st_ordered: Optional[list[str]] = None):
        """ordered = momentum 1-9; st_ordered = Stocktwits A-J map."""
        with self._lock:
            self._by_key = {str(i + 1): s for i, s in enumerate(ordered[:9])}
            self._top = ordered[0] if ordered else None
            self._st_by_key = {}
            if st_ordered:
                for letter, sym in zip(self.ST_LETTERS, st_ordered):
                    if sym:
                        self._st_by_key[letter] = str(sym).upper()

    def st_letter_for_index(self, i: int) -> str:
        """Letter shown in the ST panel for row index 0..9, or ''."""
        if 0 <= i < len(self.ST_LETTERS):
            return self.ST_LETTERS[i].upper()
        return ""

    def status(self) -> str:
        with self._lock:
            return self._status

    def focus_symbol(self) -> Optional[str]:
        with self._lock:
            return self.focus

    def _set(self, msg: str):
        with self._lock:
            self._status = f"{datetime.now():%H:%M:%S}  {msg}"

    def _set_focus(self, sym: str):
        with self._lock:
            self.focus = sym
        actions.publish_focus(sym)
        if self._on_focus:
            try:
                self._on_focus(sym)
            except Exception:
                pass

    def _reader(self):
        if sys.platform == "win32":
            self._reader_win()
        else:
            self._reader_unix()

    def _reader_win(self):
        try:
            import msvcrt
        except Exception:
            return
        while True:
            try:
                ch = msvcrt.getwch()
            except Exception:
                return
            self._handle_key(ch)

    def _reader_unix(self):
        """macOS/Linux: cbreak mode so single keypresses are available."""
        try:
            import select
            import termios
            import tty
        except Exception:
            self._set("keys off (no termios)")
            return
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:
            self._set("keys off (stdin not a tty — run in Terminal.app)")
            return
        try:
            tty.setcbreak(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.15)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch:
                    self._handle_key(ch)
        except Exception as e:
            self._set(f"keys error: {e}")
        finally:
            with contextlib.suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _handle_key(self, ch: str):
        if not ch:
            return
        key = ch
        if key in ("\r", "\n"):
            return
        low = key.lower()

        with self._lock:
            if self._busy:
                return
            tag = ""
            if key == self.SPACE or key == " ":
                sym = self._top
                action = "load"
            elif key in self._by_key:
                sym = self._by_key[key]
                action = "load"
            elif low in self._st_by_key:
                sym = self._st_by_key[low]
                action = "load"
                tag = "ST "
            else:
                return
            if not sym:
                self._status = f"{datetime.now():%H:%M:%S}  no symbol"
                return
            self._busy = True

        try:
            self._load(sym, tag=tag)
        finally:
            with self._lock:
                self._busy = False

    def _load(self, sym: str, tag: str = ""):
        self._set_focus(sym)
        prefix = tag or ""
        self._set(f"FOCUS {prefix}{sym} · loading TradingView …")
        if not self.tv_ok:
            self._set(f"FOCUS {sym} (TV load unavailable on this box)")
            return
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tv_ok = bool(actions.load_tv(sym))
        except Exception as e:
            self._set(f"{sym}: TV error {e}")
            return
        self._set(f"{prefix}{sym}: TV{'✓' if tv_ok else '✗'}  FOCUS set")
