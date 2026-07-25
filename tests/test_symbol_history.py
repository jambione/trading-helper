"""T0.2 — SymbolHistory ring buffer: bounds, ordering, None handling, prune."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from symbol_history import SymbolHistory  # noqa: E402

T0 = 1753449600.0


# ── bounds ───────────────────────────────────────────────────────────────────

def test_ring_never_exceeds_maxlen_and_evicts_oldest_first():
    h = SymbolHistory(maxlen=5)
    for i in range(20):
        h.push("AAAA", T0 + i, price=float(i))
    assert h.count("AAAA") == 5
    # Last five pushes survive; the first fifteen are gone.
    assert h.series("AAAA", "price") == [15.0, 16.0, 17.0, 18.0, 19.0]


def test_maxlen_comes_from_the_constructor_not_a_constant():
    small, big = SymbolHistory(maxlen=3), SymbolHistory(maxlen=50)
    for i in range(60):
        small.push("A", T0 + i, price=float(i))
        big.push("A", T0 + i, price=float(i))
    assert small.count("A") == 3
    assert big.count("A") == 50


def test_degenerate_maxlen_is_clamped_not_fatal():
    for bad in (0, -7, None, "nope"):
        h = SymbolHistory(maxlen=bad)
        h.push("A", T0, price=1.0)
        assert h.count("A") >= 1


# ── ordering ─────────────────────────────────────────────────────────────────

def test_series_is_oldest_to_newest():
    h = SymbolHistory(maxlen=10)
    for i, px in enumerate([3.0, 1.0, 2.0, 9.0]):
        h.push("AAAA", T0 + i, price=px)
    assert h.series("AAAA", "price") == [3.0, 1.0, 2.0, 9.0]



# ── None / bad-value handling ────────────────────────────────────────────────

def test_series_skips_none_samples():
    """A feed gap must not read as a move to zero."""
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0 + 0, price=1.0)
    h.push("AAAA", T0 + 1, price=None)
    h.push("AAAA", T0 + 2, price=3.0)
    assert h.series("AAAA", "price") == [1.0, 3.0]


def test_all_none_sample_is_still_recorded_for_its_timestamp():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0)
    h.push("AAAA", T0 + 4)
    assert h.count("AAAA") == 2
    assert h.series("AAAA", "price") == []


def test_non_numeric_and_nan_values_are_dropped():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0 + 0, price="not a number")
    h.push("AAAA", T0 + 1, price=float("nan"))
    h.push("AAAA", T0 + 2, price=float("inf"))
    h.push("AAAA", T0 + 3, price=True)          # bool is not a price
    h.push("AAAA", T0 + 4, price=2.5)
    assert h.series("AAAA", "price") == [2.5]


def test_numeric_strings_are_accepted():
    """The feed occasionally hands us stringified numbers."""
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0, price="3.41")
    assert h.series("AAAA", "price") == [3.41]


def test_bad_symbol_or_timestamp_is_a_noop():
    h = SymbolHistory(maxlen=10)
    h.push("", T0, price=1.0)
    h.push(None, T0, price=1.0)
    h.push("AAAA", None, price=1.0)
    h.push("AAAA", "bad-ts", price=1.0)
    assert h.symbols() == set()


def test_unknown_field_and_symbol_return_empty():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0, price=1.0)
    assert h.series("AAAA", "not_a_field") == []
    assert h.series("ZZZZ", "price") == []
    assert h.count("ZZZZ") == 0


def test_symbols_are_case_normalized():
    h = SymbolHistory(maxlen=10)
    h.push("aaaa", T0, price=1.0)
    h.push("AAAA", T0 + 1, price=2.0)
    assert h.symbols() == {"AAAA"}
    assert h.series("aAaA", "price") == [1.0, 2.0]


# ── all tracked fields ───────────────────────────────────────────────────────

def test_every_declared_field_round_trips():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0, price=3.41, mention_window=9, mention_velocity=7)
    assert h.series("AAAA", "price") == [3.41]
    assert h.series("AAAA", "mention_window") == [9.0]
    assert h.series("AAAA", "mention_velocity") == [7.0]


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_prune_evicts_symbols_absent_from_the_live_set():
    h = SymbolHistory(maxlen=10)
    for sym in ("AAAA", "BBBB", "CCCC"):
        h.push(sym, T0, price=1.0)
    h.prune({"AAAA", "CCCC"})
    assert h.symbols() == {"AAAA", "CCCC"}


def test_prune_keeps_history_for_surviving_symbols():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0 + 0, price=1.0)
    h.push("AAAA", T0 + 1, price=2.0)
    h.push("BBBB", T0 + 1, price=9.0)
    h.prune({"AAAA"})
    assert h.series("AAAA", "price") == [1.0, 2.0]


def test_prune_with_empty_live_set_clears_everything():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0, price=1.0)
    h.prune(set())
    assert h.symbols() == set()


def test_prune_tolerates_none_and_mixed_case_live_set():
    h = SymbolHistory(maxlen=10)
    h.push("AAAA", T0, price=1.0)
    h.prune(None)
    assert h.symbols() == set()
    h.push("BBBB", T0, price=1.0)
    h.prune({"bbbb"})
    assert h.symbols() == {"BBBB"}


def test_long_session_churn_does_not_leak_rings():
    """200 symbols churn through the feed; only the live ones are retained."""
    h = SymbolHistory(maxlen=120)
    for i in range(200):
        sym = f"S{i:03d}"
        h.push(sym, T0 + i, price=1.0)
        h.prune({sym})
    assert len(h.symbols()) == 1


# ── wiring: push_history() from the render loop ──────────────────────────────

def test_push_history_records_rows_and_prunes_in_one_call():
    from momentum_signal import push_history

    h = SymbolHistory(maxlen=10)
    rows = [
        {"ticker": "AAAA", "price": 3.41, "mention_window": 9,
         "signal_proximity": {"mention_velocity": 7}},
        {"ticker": "BBBB", "price": None, "mention_window": 0},
    ]
    push_history(h, rows, T0)
    assert h.symbols() == {"AAAA", "BBBB"}
    assert h.series("AAAA", "price") == [3.41]
    assert h.series("AAAA", "mention_velocity") == [7.0]
    assert h.series("BBBB", "price") == []

    # Next poll drops BBBB from the feed -> its ring is evicted.
    push_history(h, [rows[0]], T0 + 2.0)
    assert h.symbols() == {"AAAA"}


def test_push_history_survives_malformed_rows():
    from momentum_signal import push_history

    h = SymbolHistory(maxlen=10)
    rows = [
        {"ticker": None},
        {"ticker": "AAAA", "price": "junk", "signal_proximity": "not a dict"},
        {},
    ]
    push_history(h, rows, T0)          # must not raise
    assert h.symbols() == {"AAAA"}
    assert h.series("AAAA", "price") == []
