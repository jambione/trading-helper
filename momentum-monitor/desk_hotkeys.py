"""Cross-platform keyboard + focus for the momentum desk monitor.

Windows: msvcrt
macOS / Linux: termios cbreak + select (non-blocking)
"""
from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import desk_actions as actions


class DeskHotkeys:
    """
    Keys:
      1-9   focus + load that row into TradingView
      SPACE focus + load top (newest) row
      T     focus = whatever symbol the TradingView chart is on
      B     buy focused symbol (Alpaca paper/live)
      S     sell / close focused symbol
      F     focus only (no TV load) — optional future
    """

    SPACE = " "

    def __init__(self, trade_enabled: bool = True):
        self.tv_ok = actions.tv_load_available()
        self.trade_ok = trade_enabled
        # Hotkeys on for Mac or Windows when we have either TV or trade
        self.enabled = sys.platform in ("darwin", "win32") and (
            self.tv_ok or self.trade_ok)
        self.focus: Optional[str] = None
        self._by_key: dict[str, str] = {}
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

    def update(self, ordered: list[str]):
        with self._lock:
            self._by_key = {str(i + 1): s for i, s in enumerate(ordered[:9])}
            self._top = ordered[0] if ordered else None

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
        # normalize
        if key in ("\r", "\n"):
            return
        low = key.lower()

        with self._lock:
            if self._busy:
                return
            if low == "b":
                sym = self.focus
                action = "buy"
            elif low == "s":
                sym = self.focus
                action = "sell"
            elif low == "t":
                sym = None            # resolved from TradingView in _grab_tv
                action = "grab_tv"
            elif key == self.SPACE or key == " ":
                sym = self._top
                action = "load"
            elif key in self._by_key:
                sym = self._by_key[key]
                action = "load"
            else:
                return
            if action != "grab_tv" and not sym:
                self._status = f"{datetime.now():%H:%M:%S}  no symbol"
                return
            self._busy = True

        try:
            if action == "load":
                self._load(sym)
            elif action == "buy":
                self._buy(sym)
            elif action == "sell":
                self._sell(sym)
            elif action == "grab_tv":
                self._grab_tv()
        finally:
            with self._lock:
                self._busy = False

    def _load(self, sym: str):
        self._set_focus(sym)
        self._set(f"FOCUS {sym} · loading TradingView …")
        tv_ok = False
        if self.tv_ok:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    tv_ok = bool(actions.load_tv(sym))
            except Exception as e:
                self._set(f"{sym}: TV error {e}")
                return
        else:
            self._set(f"FOCUS {sym} (TV load unavailable on this box)")
            return
        self._set(f"{sym}: TV{'✓' if tv_ok else '✗'}  FOCUS set")

    def _buy(self, sym: str):
        self._set(f"BUY {sym} …")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                msg = actions.desk_buy(sym)
        except Exception as e:
            msg = f"BUY error: {e}"
        self._set(msg)

    def _sell(self, sym: str):
        self._set(f"SELL {sym} …")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                msg = actions.desk_sell(sym)
        except Exception as e:
            msg = f"SELL error: {e}"
        self._set(msg)

    def _grab_tv(self):
        self._set("grab TradingView symbol …")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sym = actions.tv_focus_symbol()
        except Exception as e:
            self._set(f"TV grab error: {e}")
            return
        if not sym:
            self._set("TV: no chart symbol (open the TradingView tab in Brave/Chrome)")
            return
        self._set_focus(sym)
        self._set(f"FOCUS {sym} — from TradingView chart")
