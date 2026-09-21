"""A price cell that never asks what time it is.

2026-09-21, 06:00 ET premarket: the board showed USDE at 10.33 while it
traded 11.87. Not a bad quote — the last trade IEX itself saw, from Friday
16:10, 62 hours earlier. Eleven of sixteen watchlist names were in the same
state, every one of them rendered as a live price, because the price cell
asked whether a price EXISTED and never how old it was. The engine's
admission gate was not fooled (it reads the dated rt_* pair and refused all
15 candidates with stale_tape_admit); only the display was.

The clock was already on the row — ``price_age_sec``, stamped in the merge
from the trade's own timestamp. These tests pin the verdict that reads it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as d   # noqa: E402


# ── the verdict ───────────────────────────────────────────────────────────

def test_a_print_older_than_the_ceiling_is_stale():
    assert d.price_is_stale(222_400.0) is True      # USDE, Friday 16:10
    assert d.price_is_stale(d.BOARD_PRICE_STALE_SEC + 1) is True


def test_a_young_print_is_not_stale():
    assert d.price_is_stale(0.0) is False
    assert d.price_is_stale(14.0) is False
    assert d.price_is_stale(d.BOARD_PRICE_STALE_SEC) is False


def test_unknown_age_is_neither():
    """None means the winning source could not date its quote — the Finnhub
    REST path never can. Unknown must not read as stale (half a normal
    session would grey out, burying the real case) nor be claimed as fresh
    anywhere else."""
    assert d.price_is_stale(None) is False
    assert d.price_is_stale("") is False
    assert d.price_is_stale("not-a-number") is False


# ── the row ───────────────────────────────────────────────────────────────

def _snapshot_rows(monkeypatch, tickers: dict):
    monkeypatch.setattr(d, "load_tickers", lambda: list(tickers))
    monkeypatch.setattr(d, "load_trending", lambda: [])
    monkeypatch.setattr(d, "_ai_book_symbols", lambda: [])
    monkeypatch.setattr(d, "_ticker_src_map", lambda: {})
    monkeypatch.setattr(d, "_build_mention_rank", lambda syms: {})
    monkeypatch.setattr(d, "refresh_ticker_timestamps", lambda syms: None)
    with d.STATE.lock:
        d.STATE.tickers.clear()
        d.STATE.tickers.update(tickers)
    return {r["ticker"]: r for r in d._snapshot()["tickers"]}


def test_stale_row_is_marked_and_loses_its_percentage(monkeypatch):
    rows = _snapshot_rows(monkeypatch, {
        "USDE": {"price": 10.33, "price_age_sec": 222_400.0, "day_open": 10.20},
    })
    r = rows["USDE"]
    assert r["price"] == 10.33          # still shown — it is the best estimate
    assert r["price_stale"] is True
    # Friday's close against today's open is not this morning's move.
    assert r["pct_change"] is None


def test_live_row_keeps_its_percentage(monkeypatch):
    """A fresh price against an open stamped with today's session."""
    monkeypatch.setattr(d, "_et_day_str", lambda now=None: "2026-09-21")
    rows = _snapshot_rows(monkeypatch, {
        "HOOD": {"price": 124.85, "price_age_sec": 8.0,
                 "day_open": 119.82, "day_open_day": "2026-09-21"},
    })
    r = rows["HOOD"]
    assert r["price_stale"] is False
    assert r["pct_change"] == 4.20
    assert r["pct_change_basis"] == "open"


def test_undated_row_is_left_alone(monkeypatch):
    """A Finnhub REST quote carries no trade clock. It must keep behaving
    exactly as it did before this change existed."""
    rows = _snapshot_rows(monkeypatch, {
        "SNAP": {"price": 5.52, "price_age_sec": None, "prev_close": 5.54},
    })
    r = rows["SNAP"]
    assert r["price_stale"] is False
    assert r["pct_change"] == -0.36


def test_row_with_no_price_is_not_called_stale(monkeypatch):
    """Absent is not stale: the scanner-snapshot path owns that cell."""
    rows = _snapshot_rows(monkeypatch, {
        "OIG": {"price": None, "price_age_sec": None},
    })
    assert rows["OIG"]["price_stale"] is False


# ── what the percentage is measured against ───────────────────────────────
#
# Same morning, one field over. The price cell had just learned to check its
# clock, and the two rows that WERE printing then showed HOOD +9.67% and
# RKLB -4.21%. Both were measured from `day_open`, which premarket still
# holds the PREVIOUS session's open — 113.34 and 68.43, matching Friday's
# RTH opens to the cent. The real premarket moves were +4.2% and +1.8%.

def test_premarket_falls_back_to_the_prior_close(monkeypatch):
    """No open exists before 09:30, and the feed's `o` is last session's."""
    row = {"day_open": 113.34, "day_open_day": "", "prev_close": 119.82}
    assert d.pct_change_basis(row) == ("prev_close", 119.82)


def test_an_open_stamped_today_wins(monkeypatch):
    monkeypatch.setattr(d, "_et_day_str", lambda now=None: "2026-09-21")
    row = {"day_open": 120.10, "day_open_day": "2026-09-21", "prev_close": 119.82}
    assert d.pct_change_basis(row) == ("open", 120.10)


def test_yesterdays_open_is_never_the_basis(monkeypatch):
    """The whole defect in one assertion: an open from another session must
    not be measured against, even mid-session."""
    monkeypatch.setattr(d, "_et_day_str", lambda now=None: "2026-09-21")
    row = {"day_open": 113.34, "day_open_day": "2026-09-18", "prev_close": 119.82}
    assert d.pct_change_basis(row) == ("prev_close", 119.82)


def test_an_unstamped_open_is_not_a_reference():
    """Rows written before day_open carried a session stamp. Unknown which
    session they belong to means unusable, not assumed-today — that
    assumption is the whole bug."""
    assert d.pct_change_basis({"day_open": 113.34}) == ("", None)


def test_no_reference_at_all_yields_no_percentage():
    assert d.pct_change_basis({"day_open": 0, "prev_close": 0}) == ("", None)
    assert d.pct_change_basis({}) == ("", None)


def test_the_row_publishes_the_premarket_move_and_names_its_basis(monkeypatch):
    rows = _snapshot_rows(monkeypatch, {
        "HOOD": {"price": 124.85, "price_age_sec": 8.0,
                 "day_open": 113.34, "day_open_day": "", "prev_close": 119.82},
    })
    r = rows["HOOD"]
    assert r["pct_change"] == 4.20          # not 9.67 off Friday's open
    assert r["pct_change_basis"] == "prev_close"


def test_a_stale_price_still_publishes_no_percentage(monkeypatch):
    """The staleness rule outranks the basis rule: a reference cannot rescue
    a price that is two days old."""
    rows = _snapshot_rows(monkeypatch, {
        "USDE": {"price": 10.33, "price_age_sec": 222_400.0, "prev_close": 10.22},
    })
    assert rows["USDE"]["pct_change"] is None


# ── where the prior close comes from ──────────────────────────────────────
#
# Not the quote feed. Measured 2026-09-21 06:40 ET, premarket, Finnhub's
# /quote for HOOD was the whole of the LAST COMPLETED session — c=119.82 and
# o=113.34 (Friday's close and open) — with pc=109.81, which is THURSDAY's
# close. A daily bar is dated, so "the last bar before today" cannot drift a
# session no matter what the feed means by previous.

def _daily(rows):
    """A daily-bar frame indexed in ET, shaped like fetch_daily returns."""
    import pandas as pd
    idx = pd.to_datetime([d for d, _ in rows]).tz_localize("America/New_York")
    return pd.DataFrame({"close": [c for _, c in rows]}, index=idx)


class _FakeFunnel:
    """Stands in for tools.morning_funnel: same two entry points _vol_loop uses."""
    def __init__(self, frames):
        self.frames = frames
        self.calls = 0

    def fetch_daily(self, client, tickers, cfg):
        self.calls += 1
        return {t: self.frames[t] for t in tickers if t in self.frames}

    @staticmethod
    def _et_index(df):
        return df.index


def _clear_prev_close_cache():
    d._PREV_CLOSE_CACHE.clear()
    d._PREV_CLOSE_DATE = ""


def test_prior_close_is_the_last_session_before_today():
    """Friday's close for a Monday session — never Thursday's, and never a
    bar from today."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _clear_prev_close_cache()
    now_et = datetime(2026, 9, 21, 6, 40, tzinfo=ZoneInfo("America/New_York"))
    mf = _FakeFunnel({"HOOD": _daily([
        ("2026-09-17", 109.44),      # Thursday — what Finnhub's `pc` gave us
        ("2026-09-18", 119.82),      # Friday — the answer
    ])})
    with d.STATE.lock:
        d.STATE.tickers.clear()
    d._publish_prior_closes(mf, object(), ["HOOD"], {}, now_et)
    with d.STATE.lock:
        row = dict(d.STATE.tickers["HOOD"])
    assert row["prev_close"] == 119.82
    assert row["prev_close_day"] == "2026-09-18"


def test_todays_own_bar_is_never_the_prior_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _clear_prev_close_cache()
    now_et = datetime(2026, 9, 21, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    mf = _FakeFunnel({"HOOD": _daily([
        ("2026-09-18", 119.82),
        ("2026-09-21", 126.10),      # today, still forming
    ])})
    with d.STATE.lock:
        d.STATE.tickers.clear()
    d._publish_prior_closes(mf, object(), ["HOOD"], {}, now_et)
    with d.STATE.lock:
        assert d.STATE.tickers["HOOD"]["prev_close"] == 119.82


def test_a_name_with_no_history_is_cached_as_a_miss():
    """The watchlist turns over all day; without a negative entry a fresh
    listing is re-requested every 60s forever."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _clear_prev_close_cache()
    now_et = datetime(2026, 9, 21, 6, 40, tzinfo=ZoneInfo("America/New_York"))
    mf = _FakeFunnel({})
    with d.STATE.lock:
        d.STATE.tickers.clear()
    d._publish_prior_closes(mf, object(), ["NEWCO"], {}, now_et)
    d._publish_prior_closes(mf, object(), ["NEWCO"], {}, now_et)
    assert mf.calls == 1
    with d.STATE.lock:
        assert "prev_close" not in d.STATE.tickers.get("NEWCO", {})
