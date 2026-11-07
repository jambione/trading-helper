import ai_positions as ap


def _pos(entry=100.0, peak=100.28, last=100.20):
    return {"entry_price": entry, "peak_price": peak, "last_seen_price": last,
            "stop_price": entry * 0.95, "risk_per_share": entry * 0.05, "local_stop_price": entry}


def test_off_by_default():
    assert ap._peak_lock_stop(_pos(), {}) is None


def test_locks_half_of_peak():
    cfg = {"ai_local_trail_peak_lock_frac": 0.5}
    assert ap._peak_lock_stop(_pos(), cfg) == 100.14


def test_inactive_below_from_bp():
    cfg = {"ai_local_trail_peak_lock_frac": 0.5}
    assert ap._peak_lock_stop(_pos(peak=100.10, last=100.08), cfg) is None


def test_shelf_never_below_lock():
    cfg = {"ai_local_trail_peak_lock_frac": 0.5, "ai_local_trail_arm_r": 0.06, "ai_local_trail_be_at_pct": 0.15,
           "ai_local_trail_peak_give_pct": 0.35, "ai_local_trail_give_r": 0.12}
    out = ap.local_profit_stop(_pos(), cfg, now=1e9)
    assert out is not None and out >= 100.14


def _notes(monkeypatch):
    got = []
    monkeypatch.setattr(ap, "_dc_note", lambda n: got.append(n))
    return got


def test_trend_hold_does_not_block_a_peak_lock_stop(monkeypatch):
    got = _notes(monkeypatch)
    pos = {"entry_price": 20.0, "stop_price": 19.0, "entry_stop_price": 19.0, "local_stop_price": 20.01, "peak_price": 20.04}
    cfg = {"ai_trail_exit_trend_hold": True, "ai_local_trail_peak_lock_frac": 0.5}
    assert ap.trail_exit_trend_hold(pos, "T", 20.01, cfg=cfg) is False
    assert got == ["trail:peak_lock"]


def test_initial_floor_works_without_the_stored_field(monkeypatch):
    got = _notes(monkeypatch)
    pos = {"entry_price": 20.0, "stop_price": 19.0, "entry_stop_price": 19.0, "risk_per_share": 1.0}
    cfg = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_floor_initial": True}
    init = ap.initial_local_stop(20.0, 1.0, cfg, spread_r=None)
    assert ap.trail_exit_trend_hold(pos, "T", init - 0.01, cfg=cfg) is False
    assert got == ["trail:initial_floor"]
