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
