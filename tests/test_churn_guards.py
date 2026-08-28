"""Three guards against the 2026-08-28 churn: 33 round trips, median hold 79s.

  confirm ticks   MACD is computed on the FORMING minute bar, so its gap moves
                  with every trade and macd_gap_falling flips inside a single
                  bar. Firing on one reading samples noise at the positions
                  tick and calls it a thesis break: GAP liquidated 6s after
                  entry, ASST at 9s and 11s, PURR at 11s.

  breakeven       be_at_pct 0.15% is 1.4 cents on a $9.65 name that trades in
                  half-cent ticks, so three ticks up pinned the shelf to the
                  fill and the next tick down closed it. BULL did exactly that
                  three times, the last in 38 seconds on a one-cent peak.

  attempt cap     Seven names produced thirty of thirty-three trades. The
                  cooldown spaces attempts out; it never stops them.
"""
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

cp = pytest.importorskip("ai_positions")
import ai_entry_watch as ew  # noqa: E402


def _wire(monkeypatch, **sig):
    monkeypatch.setitem(sys.modules, "ai_entry_watch", types.SimpleNamespace(
        _engine_indicator_map=lambda: {"AAA": dict(sig)}))


def _on(monkeypatch, ticks=3):
    monkeypatch.setattr(cp, "_cfg_flag",
                        lambda k, d=False: k == "ai_exit_macd_liquidate")
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_exit_macd_confirm_ticks": ticks,
        "ai_exit_macd_hard_sell_sep": 1.0,
        "ai_watch_macd_max_age_sec": 30.0})


_RT = {"macd_src": "realtime", "macd_age_sec": 0.3}
_THIN = dict(_RT, macd_gap=0.003, macd_sep_ratio=0.8, macd_gap_falling=True)


# ── confirmation ─────────────────────────────────────────────────────────

def test_one_reading_no_longer_liquidates(monkeypatch):
    _on(monkeypatch, ticks=3)
    _wire(monkeypatch, **_THIN)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_it_fires_on_the_third_consecutive_reading(monkeypatch):
    _on(monkeypatch, ticks=3)
    _wire(monkeypatch, **_THIN)
    pos = {}
    assert cp.macd_thesis_broken("AAA", pos)[0] is False
    assert cp.macd_thesis_broken("AAA", pos)[0] is False
    fire, why = cp.macd_thesis_broken("AAA", pos)
    assert fire and why == "macd_thin_and_closing"


def test_a_flicker_resets_the_streak(monkeypatch):
    """The whole point — a gap that stops falling has not broken."""
    _on(monkeypatch, ticks=3)
    pos = {}
    _wire(monkeypatch, **_THIN)
    cp.macd_thesis_broken("AAA", pos)
    cp.macd_thesis_broken("AAA", pos)
    _wire(monkeypatch, **dict(_RT, macd_gap=0.02, macd_sep_ratio=2.0,
                              macd_gap_rising=True))
    assert cp.macd_thesis_broken("AAA", pos)[0] is False
    _wire(monkeypatch, **_THIN)
    assert cp.macd_thesis_broken("AAA", pos)[0] is False, "streak restarted"


def test_switching_reason_restarts_the_count(monkeypatch):
    """A thin-and-closing streak must not be cashed in as a cross."""
    _on(monkeypatch, ticks=3)
    pos = {}
    _wire(monkeypatch, **_THIN)
    cp.macd_thesis_broken("AAA", pos)
    cp.macd_thesis_broken("AAA", pos)
    _wire(monkeypatch, **dict(_RT, macd_gap=-0.01))
    assert cp.macd_thesis_broken("AAA", pos)[0] is False


def test_one_tick_restores_the_old_behaviour(monkeypatch):
    _on(monkeypatch, ticks=1)
    _wire(monkeypatch, **_THIN)
    assert cp.macd_thesis_broken("AAA", {})[0] is True


def test_a_crossed_gap_still_exits_within_seconds(monkeypatch):
    """Confirmation delays a real break by ticks, not minutes: 3 x 3s."""
    _on(monkeypatch, ticks=3)
    _wire(monkeypatch, **dict(_RT, macd_gap=-0.01))
    pos = {}
    outs = [cp.macd_thesis_broken("AAA", pos)[0] for _ in range(3)]
    assert outs == [False, False, True]


# ── breakeven now needs a real move ──────────────────────────────────────

def test_breakeven_no_longer_arms_on_three_ticks():
    """BULL: $9.645 fill, peak $9.655. At 0.15% the floor armed on a 1.4c
    move and pinned the trade flat; at 0.4% that peak does not reach it."""
    import config
    pct = float(config.load_config()["ai_local_trail_be_at_pct"])
    entry, peak = 9.645, 9.655
    gain = (peak - entry) / entry * 100.0
    assert gain < pct, (
        f"a {gain:.3f}% peak still arms a {pct}% breakeven floor")


def test_breakeven_still_arms_inside_a_normal_move():
    """It has to protect something. Median peak across the book is ~+0.31%
    on quiet days and higher on movers, so the floor must sit under that."""
    import config
    assert float(config.load_config()["ai_local_trail_be_at_pct"]) <= 0.5


# ── three strikes ────────────────────────────────────────────────────────

def test_the_cap_counts_todays_fills(tmp_path, monkeypatch):
    import datetime as dt
    import json
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = dt.datetime.now(et)
    old = (now - dt.timedelta(days=2)).astimezone(dt.timezone.utc)
    log = [{"action": "BUY", "ticker": "ASST",
            "time": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
           for _ in range(3)]
    log.append({"action": "BUY", "ticker": "ASST",
                "time": old.strftime("%Y-%m-%dT%H:%M:%SZ")})
    log.append({"action": "SELL", "ticker": "ASST",
                "time": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    (tmp_path / "alpaca_trade_log.json").write_text(json.dumps(log))
    monkeypatch.chdir(tmp_path)
    ew._ENTRIES_TODAY.update({"day": "", "counts": {}, "ts": 0.0})
    assert ew._entries_today("ASST") == 3, "today's BUYs only"
    assert ew._entries_today("NONE") == 0


def test_an_unreadable_log_does_not_block_the_desk(tmp_path, monkeypatch):
    """Failing open here is right: the cap is a churn guard, not protection,
    and a missing log must not stop every entry."""
    monkeypatch.chdir(tmp_path)
    ew._ENTRIES_TODAY.update({"day": "", "counts": {}, "ts": 0.0})
    assert ew._entries_today("ASST") == 0


def test_the_cap_is_wired_into_the_poll():
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    assert "ai_watch_max_entries_per_symbol_day" in src
    i = src.index("ai_watch_max_entries_per_symbol_day")
    body = src[i:i + 400]
    assert "_entries_today(sym)" in body
    assert '_skip("attempt_cap"' in body


def test_the_blocker_has_a_label():
    """An unlabelled block code renders as a raw string on the desk."""
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert "attempt_cap" in L
