"""turn_open_score: the pre-registered words. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import turn_open_score as T  # noqa: E402


def test_words():
    assert T.word(1.0, 2.0, 2.5, 5.0, 5.0, 1.6, 9, 500) == "PASS"
    assert T.word(1.0, 2.0, 2.5, 5.0, -1.0, 1.6, 9, 500) == "PROMISING"
    assert T.word(1.0, 2.0, 1.6, 5.0, 5.0, 3.0, 9, 500) == "PROMISING"
    assert T.word(-1.0, 2.0, 2.5, 5.0, 5.0, 3.0, 9, 500) == "FAIL"
    assert T.word(1.0, 2.0, 2.5, 5.0, 5.0, 3.0, 7, 500) == "UNDERPOWERED"
    assert T.word(1.0, 2.0, 2.5, 5.0, 5.0, 3.0, 9, 99) == "UNDERPOWERED"


def test_stop_exits():
    assert T.stop_exit({"reason": "local_trail"}) and T.stop_exit({"exit_reason": "stop"})
    assert not T.stop_exit({"reason": "eod_flatten"})


def test_halves_cover_the_sessions():
    assert len(T.HALF_A) + len(T.HALF_B) == 9 and not set(T.HALF_A) & set(T.HALF_B)
