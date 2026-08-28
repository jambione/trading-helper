"""Float as an admission filter, and the field it reads.

float_feed has been caching Finnhub's shareOutstanding in a file named
float_cache.json since it was written. Those are different numbers and not by
a little — AREN carries 47.6M shares outstanding against a 13.12M float, KXIN
1.56M against 1.42M — so a low-float screen run on the cached value would have
been screening the wrong quantity.

Why it is worth having, measured over 507 closed trades on 2026-08-28, each
joined to its arm-time features and its own max favourable excursion:

                  n     medMFE   reached +0.25R
  every trade    507    +0.041        10%
  float < 20M     33    +0.186        45%
  float >= 50M   438    +0.039         8%

438 of 507 trades this desk has ever taken were in names over 50M float, and
those names barely move.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import float_feed  # noqa: E402


# ── the feed reads the right field ───────────────────────────────────────

def test_the_fetch_captures_floating_share():
    src = (_ROOT / "float_feed.py").read_text(encoding="utf-8")
    assert '"floatingShare"' in src, "float must come from floatingShare"
    assert '"float_m": fl' in src


def test_shares_outstanding_is_kept_too():
    """universe_screen consumes shares_out and it is a valid separate number.
    This adds a field; it does not repurpose one."""
    src = (_ROOT / "float_feed.py").read_text(encoding="utf-8")
    assert '"shares_out": so' in src
    assert hasattr(float_feed, "shares_out")
    assert hasattr(float_feed, "float_shares")


# ── the filter ───────────────────────────────────────────────────────────

def _row(sym="AAA", **over):
    r = {"symbol": sym, "price": 5.0, "pct_change": 12.0, "rvol": 6.0,
         "criteria": ["mom_open"], "dollar_volume": 5e6}
    r.update(over)
    return r


def _cfg(**over):
    c = {"ai_watch_max_float_m": 20.0, "ai_watch_require_uptrend": False,
         "ai_watch_min_price": 1.0, "ai_min_dollar_volume": 0.0}
    c.update(over)
    return c


def test_a_big_float_is_refused(monkeypatch):
    monkeypatch.setattr(float_feed, "float_shares", lambda s: 480.0)
    ok, _met, why = ew.passes_inclusion(_row(), _cfg())
    assert ok is False and why == "float_too_big"


def test_a_small_float_passes(monkeypatch):
    monkeypatch.setattr(float_feed, "float_shares", lambda s: 1.4)
    ok, met, _why = ew.passes_inclusion(_row(), _cfg())
    assert ok is True
    assert "low_float" in met, "the criterion should be recorded, not just used"


def test_an_unknown_float_does_not_refuse(monkeypatch):
    """The lookup is a cached network call. An outage must not empty the book —
    this filter is a preference, not protection, and it fails OPEN."""
    monkeypatch.setattr(float_feed, "float_shares", lambda s: None)
    ok, met, _why = ew.passes_inclusion(_row(), _cfg())
    assert ok is True
    assert "low_float" not in met, "unknown is not a low float"


def test_a_raising_feed_does_not_refuse(monkeypatch):
    def boom(_s):
        raise RuntimeError("finnhub down")
    monkeypatch.setattr(float_feed, "float_shares", boom)
    assert ew.passes_inclusion(_row(), _cfg())[0] is True


def test_zero_disables_the_filter(monkeypatch):
    monkeypatch.setattr(float_feed, "float_shares", lambda s: 999.0)
    assert ew.passes_inclusion(_row(), _cfg(ai_watch_max_float_m=0))[0] is True


def test_it_is_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_max_float_m"] == 0.0


def test_the_knob_reaches_the_live_config():
    import config
    assert "ai_watch_max_float_m" in config.load_config()


def test_the_refusal_has_a_label():
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert L.get("float_too_big") == "float"
