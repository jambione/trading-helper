"""Arming refuses when the signal engine's heartbeat is stale (2026-09-23)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402
from tests.test_ai_entry_watch import _armable_rec, _last_cfg  # noqa: E402


def _heartbeat(tmp_path, age):
    hb = tmp_path / "signal_state.json"
    hb.write_text("{}")
    t = hb.stat().st_mtime
    os.utime(hb, (t - age, t - age))
    return hb, t


def test_age_reads_the_file_mtime(tmp_path):
    hb, now = _heartbeat(tmp_path, 90)
    assert 89 <= ew.engine_heartbeat_age(now=now, path=hb) <= 91


def test_missing_heartbeat_is_none(tmp_path):
    assert ew.engine_heartbeat_age(path=tmp_path / "nope.json") is None


def test_stale_engine_refuses_the_arm(tmp_path, monkeypatch):
    hb, now = _heartbeat(tmp_path, 300)
    monkeypatch.setattr(ew, "ENGINE_HEARTBEAT", hb)
    cfg = _last_cfg(ai_watch_engine_stale_max_sec=60.0)
    ok, why = ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=cfg, now=now)
    assert (ok, why) == (False, "engine_stale")


def test_missing_engine_refuses_the_arm(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ENGINE_HEARTBEAT", tmp_path / "gone.json")
    cfg = _last_cfg(ai_watch_engine_stale_max_sec=60.0)
    ok, why = ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=cfg)
    assert (ok, why) == (False, "engine_stale")


def test_fresh_engine_does_not_refuse(tmp_path, monkeypatch):
    hb, now = _heartbeat(tmp_path, 5)
    monkeypatch.setattr(ew, "ENGINE_HEARTBEAT", hb)
    cfg = _last_cfg(ai_watch_engine_stale_max_sec=60.0)
    _ok, why = ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=cfg, now=now)
    assert why != "engine_stale"


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "ENGINE_HEARTBEAT", tmp_path / "gone.json")
    _ok, why = ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=_last_cfg())
    assert why != "engine_stale"
