import ai_entry_watch as ew
import ai_positions as cp
import config


def _pos():
    return {"symbol": "AAA", "last_seen_price": 10.0, "exh_was_overbought": True,
            "indicator": {"pctr": -5.0, "pctr_slow": -8.0}}


def _cfg(engine_only):
    return {"ai_exit_left_overbought": True, "ai_exit_triangle_engine_only": engine_only}


def _patch(monkeypatch, calls):
    monkeypatch.setattr(ew, "left_overbought_exit_enabled", lambda cfg: True)
    monkeypatch.setattr(ew, "exh_square_arm_enabled", lambda cfg: True)

    def fake_live(rec, px, cfg, now):
        calls.append(rec["symbol"])
        rec["indicator"] = {"pctr": -100.0, "pctr_slow": -8.0}     # the raw clock_range jump
        return True
    monkeypatch.setattr(ew, "apply_live_exhaustion", fake_live)


def test_engine_only_never_reads_the_live_recompute(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    hold, why = cp.trail_yields_to_triangle(_pos(), _cfg(True), now=1000.0)
    assert calls == []
    assert hold and why == "still_dual_ob"           # judged on the engine dual stamped on the row


def test_default_still_uses_the_live_recompute(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    hold, why = cp.trail_yields_to_triangle(_pos(), _cfg(False), now=1000.0)
    assert calls == ["AAA"]
    assert why != "still_dual_ob"                    # the raw -100 jump took it out of overbought


def test_default_is_off():
    assert config.DEFAULT_CONFIG["ai_exit_triangle_engine_only"] is False
