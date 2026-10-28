"""open_spread_replay knobs: off by default, only inside the open window, the variant's release/block, the pure actual
read (no _SIP_SPREAD_CACHE write, quotes <= t), and the two-sided random control."""
import datetime
import zoneinfo

import ai_entry_watch as ew

ET = zoneinfo.ZoneInfo("America/New_York")
T935 = datetime.datetime(2026, 10, 28, 9, 35, tzinfo=ET).timestamp()
T1000 = datetime.datetime(2026, 10, 28, 10, 0, tzinfo=ET).timestamp()
MAX = 0.15


def _rt(monkeypatch, val):
    monkeypatch.setattr(ew, "sip_spread_at", lambda sym, t, **k: val)
    monkeypatch.setattr(ew, "_OS_COUNTS", {})


def test_off_by_default_and_outside_the_window(monkeypatch):
    _rt(monkeypatch, 0.05)
    assert ew.open_spread_override("A", 0.5, MAX, T935, {}) is None
    on = {"ai_watch_open_spread_realtime_min": 16}
    assert ew.open_spread_override("A", 0.5, MAX, T1000, on) is None


def test_variant_releases_and_blocks_on_the_actual_spread(monkeypatch):
    on = {"ai_watch_open_spread_realtime_min": 16}
    _rt(monkeypatch, 0.05)
    assert ew.open_spread_override("A", 0.5, MAX, T935, on) == "pass"
    assert ew.open_spread_override("A", None, MAX, T935, on) == "pass"
    _rt(monkeypatch, 0.4)
    assert ew.open_spread_override("A", 0.05, MAX, T935, on) == "spread_wide"
    _rt(monkeypatch, None)
    assert ew.open_spread_override("A", 0.05, MAX, T935, on) == "spread_unknown"
    _rt(monkeypatch, 0.05)
    assert ew.open_spread_override("A", 0.05, MAX, T935, on) is None          # both agree: live decides


def test_modes(monkeypatch):
    _rt(monkeypatch, 0.4)
    rel = {"ai_watch_open_spread_realtime_min": 16, "ai_watch_open_spread_mode": "release"}
    assert ew.open_spread_override("A", 0.05, MAX, T935, rel) is None          # release-only never blocks
    skip = {"ai_watch_open_spread_realtime_min": 16, "ai_watch_open_spread_mode": "skip"}
    assert ew.open_spread_override("A", 0.9, MAX, T935, skip) == "pass"


def test_counters_count_outside_the_paint_only(monkeypatch):
    _rt(monkeypatch, 0.05)
    on = {"ai_watch_open_spread_realtime_min": 16}
    ew.open_spread_override("A", 0.5, MAX, T935, on)
    ew._MID_RISE_PEEK.on = True
    try:
        ew.open_spread_override("A", 0.5, MAX, T935, on)
    finally:
        ew._MID_RISE_PEEK.on = False
    assert ew._OS_COUNTS == {"released_variant": 1}


def test_random_control_is_two_sided_and_salted(monkeypatch):
    monkeypatch.setattr(ew, "_OS_COUNTS", {})
    rel = {"ai_watch_open_spread_window_min": 16, "ai_watch_open_spread_rand_release_share": 1.0,
           "ai_watch_open_spread_rand_salt": "OS-1"}
    assert ew.open_spread_override("A", 0.5, MAX, T935, rel) == "pass"
    blk = {"ai_watch_open_spread_window_min": 16, "ai_watch_open_spread_rand_block_share": 1.0,
           "ai_watch_open_spread_rand_salt": "OS-1"}
    assert ew.open_spread_override("A", 0.05, MAX, T935, blk) == "spread_wide"
    half = {"ai_watch_open_spread_window_min": 16, "ai_watch_open_spread_rand_release_share": 0.5,
            "ai_watch_open_spread_rand_salt": "OS-2"}
    hits = sum(ew.open_spread_override(f"S{i}", 0.5, MAX, T935, half) == "pass" for i in range(2000))
    assert 900 < hits < 1100


def test_actual_read_is_pure_and_never_looks_ahead(monkeypatch):
    monkeypatch.setattr(ew, "_OS_RT_CACHE", {})
    monkeypatch.setattr(ew, "_SIP_SPREAD_CACHE", {"A": (0.9, 1.0)})
    seen = []

    def fetch(sym, t):
        seen.append(t)
        return [(100.0, 100.1)]

    v = ew.sip_spread_at("A", T935, fetch=fetch)
    assert abs(v - 0.09995) < 1e-3 and seen == [T935]
    assert ew._SIP_SPREAD_CACHE == {"A": (0.9, 1.0)}
    assert ew.sip_spread_at("A", T935 + 100, fetch=lambda s, t: [(100.0, 100.5)]) != v   # its own second, own value
