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
    # Anchored on the poll's own line: the knob name now appears at
    # admission too (test_seeding_refuses_a_capped_name), and index() would
    # find that one first.
    i = src.index("tries = _entries_today(sym)")
    body = src[i - 400:i + 1100]
    assert "_entries_today(sym)" in body
    # The cap DROPS the name rather than skipping it — see
    # test_the_attempt_cap_drops_rather_than_parks below.
    assert "drop_watch_symbols([sym])" in body


def test_the_blocker_has_a_label():
    """An unlabelled block code renders as a raw string on the desk."""
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert "attempt_cap" in L


# ── a strike is a POSITION, not an attempt ─────────────────────────────────
#
# 2026-08-28: GAP wore four strikes for two actual trades. Two of its BUY rows
# were entry limits that never filled and were cancelled at the 30s TTL,
# logged as "no position". The name had not churned — it had failed to get a
# fill — and the cap was punishing bad luck with the book while GAP passed
# every signal gate (macd_exh_confluence, overbought_macd_armed, cm_rsi_off).

def _log(tmp_path, rows):
    import datetime as dt
    import json
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    out = []
    for i, (action, note) in enumerate(rows):
        out.append({"action": action, "ticker": "GAP", "note": note,
                    "time": (now + dt.timedelta(seconds=i)).astimezone(
                        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    (tmp_path / "alpaca_trade_log.json").write_text(json.dumps(out))
    ew._ENTRIES_TODAY.update({"day": "", "counts": {}, "ts": 0.0})


def test_an_unfilled_limit_does_not_burn_a_strike(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _log(tmp_path, [
        ("BUY", "local_stop_only"),
        ("CANCELED", "no position (canceled 1 open order)"),
        ("BUY", "local_stop_only"),
        ("CANCELED", "no position (canceled 0 open orders)"),
        ("BUY", "local_stop_only"), ("SELL", "close_out"),
        ("BUY", "local_stop_only"), ("SELL", "close_out"),
    ])
    assert ew._entries_today("GAP") == 2, "two fills, two non-fills"


def test_a_filled_entry_still_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _log(tmp_path, [("BUY", ""), ("SELL", "close_out")] * 3)
    assert ew._entries_today("GAP") == 3


def test_an_open_position_counts_before_it_closes(tmp_path, monkeypatch):
    """A fill that has not exited yet is still a strike — otherwise the cap
    only applies to names that have already finished trading."""
    monkeypatch.chdir(tmp_path)
    _log(tmp_path, [("BUY", ""), ("SELL", "close_out"), ("BUY", "")])
    assert ew._entries_today("GAP") == 2


def test_a_cancel_cannot_drive_the_count_negative(tmp_path, monkeypatch):
    """A stray cancel with no matching BUY must not hand out free strikes."""
    monkeypatch.chdir(tmp_path)
    _log(tmp_path, [
        ("CANCELED", "no position (canceled 0 open orders)"),
        ("CANCELED", "no position (canceled 0 open orders)"),
        ("BUY", ""), ("SELL", "close_out"),
    ])
    assert ew._entries_today("GAP") == 1


def test_a_cancel_that_is_not_a_non_fill_is_ignored(tmp_path, monkeypatch):
    """Only the 'no position' cancel proves the entry never filled."""
    monkeypatch.chdir(tmp_path)
    _log(tmp_path, [("BUY", ""), ("CANCELED", "replaced working sell")])
    assert ew._entries_today("GAP") == 1


# ── a capped name leaves the book ───────────────────────────────────────────

def test_the_attempt_cap_drops_rather_than_parks():
    """A name that cannot be opened again today is not a watch candidate.
    Parking it spends a row, a quote and a poll slot on something with no
    reachable outcome, and reads to the operator as a setup that might still
    fire. Same treatment dead_reentry already gets."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("tries = _entries_today(sym)")
    body = src[i:i + 900]
    assert "drop_watch_symbols([sym])" in body
    assert 'reason="attempt_cap"' in body


# ── the two staleness faults are named apart ────────────────────────────────
#
# "stale quote" is a print we can SEE is old — a quiet tape, normal on a thin
# name. "no quote age" is a print we cannot TIME at all, which is plumbing:
# decision_price returns an age on demand while the record carries None, and
# an untimed price does not trip the staleness guard, it disables it. GAP
# carried that on 2026-08-28 while the operator was reading "stale quote".

def test_a_provably_old_print_says_stale_quote():
    got = ew.derive_blocker({"last_ask_src": "rest", "last_ask_age_sec": 9999.0})
    assert got[0] == "stale_quote"


def test_an_untimed_print_says_no_quote_age():
    got = ew.derive_blocker({"last_ask_src": "rest", "last_ask_age_sec": None})
    assert got == ("no_quote_age", "no quote age")


def test_a_dead_tape_is_still_a_stale_quote():
    """stale_tape/none already mean 'no usable print' — that is not the
    plumbing fault this label exists to surface."""
    for src in ("stale_tape", "none", ""):
        got = ew.derive_blocker({"last_ask_src": src, "last_ask_age_sec": None})
        assert got[0] == "stale_quote", src


def test_stale_quote_clears_when_tape_is_fresh_again(monkeypatch):
    """A leftover poller stale_quote must not stick after the print recovers.

    Live 2026-09-01: stream + age 24–32s under a 60s ceiling still showed
    block_code=stale_quote because derive_blocker returned the stored refuse
    after _row_tape_stale had already gone false.

    The ceiling is pinned here rather than inherited: _row_tape_stale looks
    ai_watch_decision_max_age_sec up from the live config and ignores any
    decision_max_age_sec on the record, so an age chosen to sit under the
    operator's current setting makes the test pass or fail on bot_config.json
    instead of on the sticky-refuse bug it exists to catch.
    """
    monkeypatch.setattr(
        ew, "_push_cfg", lambda: {"ai_watch_decision_max_age_sec": 60.0})
    got = ew.derive_blocker({
        "last_ask_src": "stream",
        "last_ask_age_sec": 24.0,
        "block_code": "stale_quote",
        "block_reason": "stale quote",
        "block_detail": "tape age unknown or old",
        "entry_low": 10.0,
        "entry_high": 11.0,
        "last_ask": 10.5,
        "status": "watching",
    })
    assert got[0] != "stale_quote", got


def test_the_new_label_is_display_only():
    """Both refuse identically. This names which is which; it must not add a
    gate — should_arm_buy does not consult tape staleness, so an early skip
    here would block rows that currently arm."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    assert '_skip("no_quote_age"' not in src


def test_the_label_is_registered():
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert L.get("no_quote_age") == "no quote age"


# ── the cap has to hold at ADMISSION, not just in the poll ──────────────────
#
# The poll drops a capped name, but seeding runs on its own cadence and put it
# straight back. BULL was dropped for attempt_cap at 12:14:02, :16, :28, :40
# and :53 on 2026-08-28 — five times in under a minute, 137 admit/drop/entry
# events across the session. Two mechanisms fighting each other, spending
# quotes, poll slots and log lines to stay exactly where they started.

def test_seeding_refuses_a_capped_name():
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("candidates, rejected = apply_inclusion_gate(candidates, cfg)")
    body = src[i:i + 1400]
    assert "ai_watch_max_entries_per_symbol_day" in body, (
        "the cap must be applied where candidates are admitted")
    assert "_entries_today(sym)" in body
    assert '"reason": "attempt_cap"' in body, "a refusal must be logged, not silent"


def test_a_zero_cap_admits_everything():
    """The knob is a switch: 0 must leave seeding exactly as it was."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("candidates, rejected = apply_inclusion_gate(candidates, cfg)")
    body = src[i:i + 1400]
    assert "if cap > 0:" in body


# ── the engine's %R is the only %R ─────────────────────────────────────────
#
# ensure_live_exhaustion tried the engine wire first and fell back to a LOCAL
# recompute whenever the engine's bars were not realtime-fresh. That fallback
# is not the engine's %R with older data — it is a different indicator: a
# rolling window of ai_watch_exh_bars against the engine's wr_length,
# recomputed off the live print.
#
# Measured on AREN 2026-08-28 at one instant:
#     engine  pctr -64.60  ->  EXH 35.4%   rising
#     local   pctr -16.67  ->  EXH 83.3%   flat        <- displayed AND gated
#
# 48 points apart and pointing opposite ways, while the MACD beside them came
# from the engine — so the confluence rule was combining two indicators
# computed on different bars over different windows.

def test_engine_only_refuses_the_local_substitute():
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("if refresh_engine_exh(rec, sig, cfg, now):")
    body = src[i:i + 1400]
    assert "ai_watch_exhaustion_engine_only" in body
    assert "return False" in body, "no %R at all beats a confident wrong one"


def test_the_engine_is_still_tried_first():
    """The knob removes the FALLBACK, not the engine read — reversing those
    would leave the record with no %R at all."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("def ensure_live_exhaustion")
    body = src[i:i + 2200]
    assert body.index("refresh_engine_exh(") < body.index(
        "ai_watch_exhaustion_engine_only")


def test_off_by_default_keeps_the_fallback():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_exhaustion_engine_only"] is False


def test_the_engine_stamps_the_label_the_guard_asks_for():
    """require_live_pctr demands pctr_src == "live", and the ENGINE path is
    what stamps it — so the two may be on together.

    This test used to assert the opposite: that engine_only forces
    require_live_pctr off, on the reading that "live" was the local
    computation's own label. It is not. refresh_engine_exh stamps
    pctr_src="live" from the engine's own reading once stream_bars has 21
    minutes of history; the fallbacks are what carry "alpaca",
    "clock_range" and "sparse_window", and refusing those is the whole
    point of the knob. The ledger says so directly — with engine_only and
    require_live_pctr both true, 9/42 fills on 2026-08-31 pm and 29/42 on
    2026-09-01 logged pctr_src="live", so the pair admits names rather than
    refusing every one.

    What is worth pinning is that the producer and the gate keep naming the
    same label. If either side is renamed alone, the gate silently refuses
    everything and this fails.
    """
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("def refresh_engine_exh")
    body = src[i:i + 2600]
    assert 'ind["pctr_src"] = "live"' in body, (
        "the engine path must stamp the label require_live_pctr gates on")
    j = src.index('cfg.get("ai_watch_require_live_pctr"')
    gate = src[j:j + 400]
    assert 'src != "live"' in gate, (
        "the gate must still ask for the label refresh_engine_exh stamps")
