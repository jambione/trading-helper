"""11/19 operator: 'all the checks are live' - the arm reads the live day change and volume, never admission stamps."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_entry_watch as ew  # noqa: E402


def test_day_change_is_live_not_admit(monkeypatch):
    monkeypatch.setattr(ew, "_desk_pct_change", lambda s: None)
    assert ew._arm_day_change({"symbol": "HOOD", "pct_change": -1.54, "admit_pct_change": 3.0}) == -1.54
    assert ew._arm_day_change({"symbol": "HOOD", "admit_pct_change": 3.0}) is None
    monkeypatch.setattr(ew, "_desk_pct_change", lambda s: 2.2)
    assert ew._arm_day_change({"symbol": "HOOD", "admit_pct_change": 3.0}) == 2.2


def test_rvol_is_live_not_admit(monkeypatch):
    monkeypatch.setattr(ew, "_desk_rvol", lambda s: None)
    assert ew._arm_rvol({"symbol": "HZO", "rvol": 0.82, "admit_rvol": 2.0}) == 0.82
    assert ew._arm_rvol({"symbol": "HZO", "admit_rvol": 2.0}) is None


def test_still_moving_gate_is_in_the_arm_and_wired_to_config():
    import inspect
    import config
    src = inspect.getsource(ew.should_arm_buy)
    assert "ai_watch_arm_min_day_chg" in src and '"not_moving"' in src and '"day_chg_unknown"' in src
    assert config.DEFAULT_CONFIG.get("ai_watch_arm_min_day_chg", 0.0) == 0.0 if hasattr(config, "DEFAULT_CONFIG") else True
    assert ew.format_blocker("not_moving") == "not moving today"


def test_live_price_band_and_live_only_ranking():
    import inspect
    src = inspect.getsource(ew.should_arm_buy)
    assert "ai_watch_arm_live_price_band" in src and '"above_max_price"' in src and '"below_min_price"' in src
    assert ew._rank_rvol({"symbol": "X", "admit_rvol": 3.0}, live_lookup=lambda s: None) is None
    assert ew._rank_rvol({"symbol": "X", "rvol": 1.4, "admit_rvol": 3.0}, live_lookup=lambda s: None) == 1.4


class _Cp:
    @staticmethod
    def log_event(kind, **kw):
        return {"kind": kind, **kw}


class _Gt:
    def __init__(self, held=()):
        self.held = set(held)

    def has_open_position(self, sym):
        return sym in self.held


def test_not_moving_seat_is_released_after_the_window(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    cfg = {"ai_watch_not_moving_evict_sec": 120.0}
    rec, ev = {"status": "watching", "block_code": "not_moving", "pct_change": 0.05}, []
    kw = dict(sym="HZO", cfg=cfg, events=ev, cp=_Cp, gt=_Gt())
    assert ew._maybe_not_moving_evict(rec, now=1000.0, **kw) is False
    assert ew._maybe_not_moving_evict(rec, now=1110.0, **kw) is False
    assert ew._maybe_not_moving_evict(rec, now=1121.0, **kw) is True
    assert dropped == ["HZO"] and ev[-1]["reason"] == "not_moving_seat"


def test_not_moving_release_resets_and_never_drops_a_position(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    cfg = {"ai_watch_not_moving_evict_sec": 120.0}
    rec = {"status": "watching", "block_code": "not_moving"}
    kw = dict(sym="X", cfg=cfg, events=[], cp=_Cp, gt=_Gt())
    ew._maybe_not_moving_evict(rec, now=1000.0, **kw)
    rec["block_code"] = "exh_falling"                      # moving again: clock resets
    assert ew._maybe_not_moving_evict(rec, now=1200.0, **kw) is False and "not_moving_since" not in rec
    held = {"status": "watching", "block_code": "not_moving", "not_moving_since": 1.0}
    assert ew._maybe_not_moving_evict(held, now=1000.0, **dict(kw, gt=_Gt({"X"}))) is False
    assert ew._maybe_not_moving_evict(held, now=1000.0, **dict(kw, cfg={})) is False   # off by default
    assert dropped == []


def test_admission_uses_the_live_moving_floor():
    import inspect
    src = inspect.getsource(ew.passes_inclusion)
    assert "ai_watch_arm_min_day_chg" in src and '"not_moving"' in src
