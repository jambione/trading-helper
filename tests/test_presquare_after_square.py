"""Pre-square refused for N minutes after a full square (operator 2026-10-14). Pure state, no market data."""
import ai_entry_watch as A

CFG = {"ai_watch_presquare_after_square_min": 10}


def _rec(fast, slow):
    return {"indicator": {"pctr": fast, "pctr_slow": slow}}


def test_refuses_after_a_square_and_expires(monkeypatch):
    A._LAST_SQUARE.clear()
    monkeypatch.setattr(A, "classify_exh_seat", lambda rec, cfg: ("square", 1.0) if rec["indicator"]["pctr"] >= -20
                        and rec["indicator"]["pctr_slow"] >= -20 else ("pre_square", 5.0))
    A.after_square_record("X", _rec(-30, -28), 1000.0, CFG)
    assert A.after_square_refusal("X", 1000.0, CFG) is None            # rising from below: allowed
    A.after_square_record("X", _rec(-12, -15), 1060.0, CFG)            # square
    A.after_square_record("X", _rec(-15, -21), 1120.0, CFG)            # slid back into the band
    assert A.after_square_refusal("X", 1120.0, CFG) == "presquare_after_square"
    for t in range(1180, 1700, 60):                                     # keep polling, no square
        A.after_square_record("X", _rec(-30, -28), float(t), CFG)
    assert A.after_square_refusal("X", 1661.0, CFG) is None            # > 10 min after the square


def test_off_by_default_and_gap_resets(monkeypatch):
    A._LAST_SQUARE.clear()
    monkeypatch.setattr(A, "classify_exh_seat", lambda rec, cfg: ("square", 1.0))
    A.after_square_record("Y", _rec(-10, -10), 100.0, {})
    assert A.after_square_refusal("Y", 100.0, {}) is None and "Y" not in A._LAST_SQUARE
    A.after_square_record("Y", _rec(-10, -10), 100.0, CFG)
    monkeypatch.setattr(A, "classify_exh_seat", lambda rec, cfg: ("far", 30.0))
    A.after_square_record("Y", _rec(-60, -50), 400.0, CFG)              # 300 s polling gap: memory restarts
    assert A.after_square_refusal("Y", 400.0, CFG) is None
