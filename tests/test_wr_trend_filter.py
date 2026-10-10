"""wr_trend_entry_prereg.json: the %R trend filter and its random control (both default off)."""
import ai_entry_watch as ew

CFG = {"ai_watch_wr_trend_min_rise": 15}


def rec(pctr, src="engine"):
    return {"indicator": {"pctr": pctr, "pctr_src": src}}


def feed(sym, values, t0=1000.0, step=10.0, src="engine", cfg=CFG):
    ew._WR_HIST.pop(sym, None)
    for k, v in enumerate(values):
        ew.wr_trend_record(sym, rec(v, src), t0 + k * step, cfg)
    return t0 + (len(values) - 1) * step


def test_off_by_default_records_nothing_and_never_refuses():
    ew._WR_HIST.pop("AAA", None)
    ew.wr_trend_record("AAA", rec(-50), 1000.0, {})
    assert "AAA" not in ew._WR_HIST and ew.wr_trend_refusal("AAA", rec(-10), 2000.0, {}) is None
    assert ew.wr_rand_refusal("AAA", 2000.0, {}) is None


def test_rising_15_points_over_10_minutes_passes_flat_is_refused():
    t = feed("UP", [-60 + k * 0.5 for k in range(61)])          # +30 over 600 s
    assert ew.wr_trend_refusal("UP", rec(-30), t, CFG) is None
    t = feed("FLAT", [-30.0] * 61)
    assert ew.wr_trend_refusal("FLAT", rec(-29), t, CFG) == "wr_not_trending"


def test_no_history_and_source_mismatch_refuse():
    t = feed("NEW", [-50.0] * 20)                                # only 190 s of history
    assert ew.wr_trend_refusal("NEW", rec(-20), t, CFG) == "wr_no_history"
    t = feed("SRC", [-60.0] * 61, src="engine")
    assert ew.wr_trend_refusal("SRC", rec(-20, src="live"), t, CFG) == "wr_src_mismatch"


def test_history_restarts_after_a_gap():
    ew._WR_HIST.pop("GAP", None)
    ew.wr_trend_record("GAP", rec(-80), 1000.0, CFG)
    ew.wr_trend_record("GAP", rec(-20), 1000.0 + 600, CFG)      # 600 s gap > 180 s: restart
    assert len(ew._WR_HIST["GAP"]) == 1


def test_rand_refuses_whole_episodes_at_roughly_the_share():
    cfg = {"ai_watch_wr_rand_refuse_share": 0.5}
    refused = 0
    for i in range(400):
        sym = f"R{i}"
        ew._WR_EPISODE.pop(sym, None)
        first = ew.wr_rand_refusal(sym, 1_791_500_000.0, cfg)
        again = ew.wr_rand_refusal(sym, 1_791_500_030.0, cfg)    # same episode: same answer
        assert first == again
        refused += first is not None
    assert 150 < refused < 250
