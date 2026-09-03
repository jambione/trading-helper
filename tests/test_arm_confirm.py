"""The buy must survive as long as the sell does.

GAP, 2026-08-28, from the shadow log:

    13:31:33  arm=False  macd_bearish
    13:31:46  arm=True   last_overbought_macd_armed   <- bought
    13:33:14  arm=False  macd_bearish

Bearish either side, bullish for one thirteen-second poll. The position closed
79 seconds later on macd_negative, and the operator's chart shows the MACD
histogram red and both lines declining across the whole window.

Two defects, both introduced the same morning:

  * the flat-OB exemption fired at 80.7%. It exists because a reading PINNED
    at 100% cannot rise, so the level must stand in for the turn. At 80.7%
    there are nineteen points of headroom and "not rising" is a real refusal.

  * the hard sell had required ai_exit_macd_confirm_ticks agreeing reads since
    that morning; the entry required one. An exit held to a higher standard of
    evidence than the entry will always buy noise and sell signal.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import config as _config  # noqa: E402


def _rec(pctr):
    # pctr_src is the engine's own label for a real rolling reading; GAP's
    # was one. Without it ai_watch_require_live_pctr refuses the record
    # before the exemption is ever reached, and these tests stop being
    # about the exemption.
    return {"symbol": "X", "indicator": {
        "pctr": pctr, "pctr_rising": False, "pctr_falling": False,
        "pctr_src": "live",
        "macd_src": "realtime", "macd_age_sec": 1.0, "macd_gap": 0.02,
        "macd_sep_ratio": 2.0, "macd_bull": True,
        "macd_gap_rising": True, "macd_gap_falling": False}}


def _exh_cfg():
    """The operator's config with the EXH rules forced ON.

    These two tests assert what exhaustion_allows_buy decides. Reading
    ai_watch_exhaustion_rules off the live file makes them assert whether
    the desk currently consults EXH at all, which is a different question
    and one that flips with a knob (it went false on 2026-09-01).
    """
    c = dict(_config.load_config())
    c["ai_watch_exhaustion_rules"] = True
    return c


# ── the flat-OB exemption is for the ceiling only ────────────────────────

def test_a_pinned_reading_still_gets_the_exemption():
    c = _exh_cfg()
    assert ew.exhaustion_allows_buy(_rec(-0.0), c) == (
        True, "overbought_macd_armed")


def test_gap_at_eighty_percent_is_refused():
    """The trade that prompted this. Nineteen points of headroom and flat."""
    c = _exh_cfg()
    ok, why = ew.exhaustion_allows_buy(_rec(-19.3), c)
    assert ok is False
    assert why == "not_rising_overbought"


def test_the_threshold_is_configurable_and_near_the_ceiling():
    c = _config.load_config()
    assert c["ai_watch_ob_flat_min_pct"] >= 95.0, (
        "the exemption is for readings that cannot rise, not merely high ones")


def test_the_default_is_the_ceiling():
    assert _config.DEFAULT_CONFIG["ai_watch_ob_flat_min_pct"] == 99.0


# ── entry confirmation, mirroring the exit ───────────────────────────────

def test_a_single_yes_does_not_arm():
    rec = {}
    assert ew._arm_streak(rec, True) == 1


def test_consecutive_yes_accumulates():
    rec = {}
    assert [ew._arm_streak(rec, True) for _ in range(3)] == [1, 2, 3]


def test_any_no_resets_the_streak():
    """The whole point: bearish-bullish-bearish must not accumulate."""
    rec = {}
    ew._arm_streak(rec, True)
    ew._arm_streak(rec, True)
    assert ew._arm_streak(rec, False) == 0
    assert ew._arm_streak(rec, True) == 1


def test_the_streak_does_not_survive_a_restart():
    """After a restart the desk re-earns its evidence rather than acting on a
    count it cannot see the readings behind.

    The count now DOES persist to disk (it has to — see the rebuild test
    below), so "not written down" is no longer what enforces this. The poll
    sequence is: it restarts at 1 with the process, so a stored poll number
    from before the restart can never be "the immediately preceding poll".
    """
    rec = {"arm_streak": 4, "arm_streak_poll": 900}   # yesterday's process
    assert ew._arm_streak(rec, True, seq=1) == 1


def test_the_rebuild_must_not_eat_the_count():
    """IREN, 2026-09-03: 21 arm-YES verdicts, streak=1 on every one.

    sync_watch_from_source_panels rebuilds each row every 2s and carries over
    only a whitelist of keys. arm_streak was not on it, and the arm poll runs
    every ~13s, so the counter was wiped between every pair of polls and
    ai_watch_arm_confirm_ticks=2 could only ever be met by a name the sync
    happened to skip. Arming was a race, not a confirmation.
    """
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index('"block_code", "block_reason", "block_ts", "block_detail",')
    carried = src[i:i + 400]
    assert '"arm_streak"' in carried, "the 2s rebuild must carry the count"
    assert '"arm_streak_poll"' in carried, "...and the poll it was earned on"


def test_non_consecutive_polls_do_not_accumulate():
    """Two YES verdicts with a poll between them are not two in a row.

    This is what the poll number buys: the paths that leave the arm loop
    early (stale_quote, stream_required, tape_only, a batch-stage NO) do not
    all call the reset, and a count that trusted them would arm on evidence
    gathered minutes apart.
    """
    rec = {}
    assert ew._arm_streak(rec, True, seq=10) == 1
    assert ew._arm_streak(rec, True, seq=12) == 1   # poll 11 said something else
    assert ew._arm_streak(rec, True, seq=13) == 2   # now it is consecutive


def test_consecutive_polls_accumulate_to_the_bar():
    rec = {}
    got = [ew._arm_streak(rec, True, seq=s) for s in (5, 6, 7)]
    assert got == [1, 2, 3]


def test_one_tick_restores_the_old_behaviour():
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": 1}) == 1
    assert ew._arm_confirm_ticks({}) == 1
    assert ew._arm_confirm_ticks(None) == 1


def test_garbage_cannot_disable_the_gate():
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": "soon"}) == 1
    assert ew._arm_confirm_ticks({"ai_watch_arm_confirm_ticks": 0}) == 1


def test_the_poll_resets_on_refusal_and_gates_on_the_streak():
    # The window is a proxy for "these live in the same stretch of the poll",
    # not a budget. It grew from 3200 when the arm recheck and the confirm
    # streak gained their own event logging (_log_arm_recheck) -- the verdicts
    # between the shadow row and the entry gates used to be written nowhere,
    # so a name vetoed on the post-refresh check left no trace on disk.
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)")
    body = src[i:i + 4200]
    assert "_arm_streak(rec, False" in body, "a refusal must reset the count"
    assert "streak < need_arm" in body
    assert 'set_block_reason(rec, "arm_confirming"' in body


def test_the_entry_bar_is_not_looser_than_the_exit():
    """An exit held to a higher standard than the entry buys noise and sells
    signal. They need not be equal, but the entry must not be the weaker."""
    c = _config.load_config()
    assert c["ai_watch_arm_confirm_ticks"] >= 1
    assert c["ai_exit_macd_confirm_ticks"] >= 1


def test_the_confirming_state_has_a_label():
    from ai_entry_watch import _BLOCKER_LABELS as L
    assert L.get("arm_confirming") == "confirming"


# --- the arm recheck log -----------------------------------------------------
# should_arm_buy runs twice per buy-ready poll. Only the first reached
# shadow.jsonl, so a name that armed on the batch quote and was vetoed on the
# re-pull left no trace anywhere: GTLB did that 163 times on 2026-09-02 and
# nothing on disk could say why it never bought.

def test_snapshot_reads_indicator_first_then_the_record():
    rec = {
        "symbol": "GTLB",
        "indicator": {"cm_rsi": 31.0, "cm_rsi_rising": True},
        "pctr": -44.0,
    }
    snap = ew._arm_gate_snapshot(rec, 52.5)
    assert snap["ask"] == 52.5
    assert snap["cm_rsi"] == 31.0
    assert snap["cm_rsi_rising"] is True
    # not on indicator, so the record supplies it
    assert snap["pctr"] == -44.0
    # absent everywhere is None, never a coerced False
    assert snap["macd_src"] is None


def test_snapshot_survives_a_record_with_no_indicator():
    assert ew._arm_gate_snapshot({"symbol": "X"}, None)["cm_rsi"] is None
    assert ew._arm_gate_snapshot({}, 1.0)["cm_rsi_rising"] is None


def test_recheck_logs_the_verdict_and_names_what_moved():
    seen = []

    class _Cp:
        @staticmethod
        def log_event(kind, **fields):
            seen.append((kind, fields))

    before = ew._arm_gate_snapshot(
        {"indicator": {"cm_rsi": 31.0, "cm_rsi_rising": True}}, 52.50)
    after = ew._arm_gate_snapshot(
        {"indicator": {"cm_rsi": 30.4, "cm_rsi_rising": False}}, 52.55)
    ew._log_arm_recheck(
        _Cp, "GTLB", stage="refresh", ok=False, why="rsi_not_rising",
        why_first="rsi_turning_up", px_src="stream",
        before=before, after=after)

    assert len(seen) == 1
    kind, f = seen[0]
    assert kind == "arm_recheck"
    assert f["symbol"] == "GTLB"
    assert f["stage"] == "refresh"
    assert f["ok"] is False
    assert f["why"] == "rsi_not_rising"
    assert f["why_first"] == "rsi_turning_up"
    # the whole point: say which input moved between the two verdicts
    assert f["changed"] == ["cm_rsi", "cm_rsi_rising"]
    # ask changes on every re-pull and would drown the signal
    assert "ask" not in f["changed"]


def test_recheck_never_raises_into_the_trading_path():
    class _Boom:
        @staticmethod
        def log_event(kind, **fields):
            raise RuntimeError("disk full")

    # instrumentation must not be able to stop a trade
    ew._log_arm_recheck(
        _Boom, "X", stage="confirm", ok=False, why="arm_confirming",
        before={}, after={}, streak=1, need=2)


def test_the_poll_logs_both_dark_branches():
    """The post-refresh veto and the confirm streak each get an event."""
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)")
    body = src[i:i + 4200]
    assert 'stage="refresh"' in body, "post-repull veto must be logged"
    assert 'stage="confirm"' in body, "confirm streak must be logged"


# ── Package B: atomic confirm→submit slip gate ───────────────────────────────

def test_confirm_slip_ok_within_and_beyond_limits():
    import ai_entry_watch as ew
    cfg = {"ai_entry_confirm_max_slip_pct": 1.0,
           "ai_entry_confirm_max_slip_px": 0.10}
    ok, why = ew._confirm_slip_ok(17.52, 17.55, cfg)
    assert ok is True
    assert why == ""
    # FRVO-class jump 17.52 → 18.32
    ok, why = ew._confirm_slip_ok(17.52, 18.32, cfg)
    assert ok is False
    assert "jump_" in why
    # Absolute cents binds before pct on a cheap name
    ok, why = ew._confirm_slip_ok(5.00, 5.12, cfg)
    assert ok is False


def test_confirm_slip_disabled_legs():
    import ai_entry_watch as ew
    cfg = {"ai_entry_confirm_max_slip_pct": 0.0,
           "ai_entry_confirm_max_slip_px": 0.0}
    ok, why = ew._confirm_slip_ok(10.0, 12.0, cfg)
    assert ok is True
