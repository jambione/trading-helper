"""Tape-only fix knobs (operator 2026-10-14): per-name print-gap ceiling and quote age ceilings. Defaults unchanged."""
import ai_entry_watch as A


def test_name_ceiling_off_by_default_and_follows_the_median_gap():
    A._PRINT_GAPS.clear()
    for i in range(8):                                   # prints every 30 s
        A.note_print_gap("XYZ", 1000.0 + 30 * i, 0.0)
    assert A.name_tape_ceiling("XYZ", {}) == 0.0
    assert A.name_tape_ceiling("XYZ", {"ai_watch_tape_gap_mult": 1.5}) == 45.0
    assert A.name_tape_ceiling("XYZ", {"ai_watch_tape_gap_mult": 3, "ai_watch_tape_gap_max_sec": 60}) == 60.0
    assert A.name_tape_ceiling("NEW", {"ai_watch_tape_gap_mult": 1.5}) == 0.0   # < 5 gaps: no stretch


def test_same_print_seen_twice_is_not_a_gap():
    A._PRINT_GAPS.clear()
    A.note_print_gap("Q", 100.0, 0.0)
    A.note_print_gap("Q", 110.0, 10.0)                   # same print (ts 100) polled again
    assert A._PRINT_GAPS["Q"][1] == []


def test_decision_price_uses_the_stretched_ceiling(monkeypatch):
    A._PRINT_GAPS.clear()
    for i in range(6):
        A.note_print_gap("ABC", 1000.0 + 40 * i, 0.0)
    monkeypatch.setattr(A, "live_print", lambda s: (10.0, 30.0))
    monkeypatch.setattr(A, "cross_checked_quote", lambda *a, **k: None)
    cfg = {"ai_watch_decision_max_age_sec": 15.0}
    assert A.decision_price("ABC", cfg, now=1300.0)[1] == "stale_tape"
    cfg["ai_watch_tape_gap_mult"] = 1.0                  # median gap 40 s -> ceiling 40 s
    assert A.decision_price("ABC", cfg, now=1300.0)[1] == "stream"
