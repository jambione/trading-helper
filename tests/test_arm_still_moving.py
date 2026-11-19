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
