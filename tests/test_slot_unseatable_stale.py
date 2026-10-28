"""Slot-priority push filter: a cached spread or gap blocks a push only while the gate itself would still use it.

CRWV 2026-10-28: a 0.33% premarket spread read at 04:51 kept it off the engine (no tape, never re-measured) to 09:39.
"""
import ai_entry_watch as ew

CFG = {"ai_watch_gap_down_block_pct": 1.0, "ai_watch_max_sip_spread_pct": 0.15}
T = 1_793_190_000.0          # 2026-10-28 morning


def _clear(monkeypatch):
    monkeypatch.setattr(ew, "_SIP_SPREAD_CACHE", {})
    monkeypatch.setattr(ew, "_GAP_CACHE", {})


def test_fresh_wide_spread_blocks_and_stale_one_does_not(monkeypatch):
    _clear(monkeypatch)
    ew._SIP_SPREAD_CACHE["CRWV"] = (0.33, T - 60)
    assert ew._slot_unseatable("CRWV", CFG, now=T) is True
    ew._SIP_SPREAD_CACHE["CRWV"] = (0.33, T - ew.SIP_SPREAD_TTL_SEC - 1)
    assert ew._slot_unseatable("CRWV", CFG, now=T) is False


def test_tight_or_unknown_spread_never_blocks(monkeypatch):
    _clear(monkeypatch)
    ew._SIP_SPREAD_CACHE["A"] = (0.05, T - 10)
    ew._SIP_SPREAD_CACHE["B"] = (None, T - 10)
    assert not ew._slot_unseatable("A", CFG, now=T) and not ew._slot_unseatable("B", CFG, now=T)


def test_sip_gap_holds_all_day_iex_gap_only_briefly(monkeypatch):
    _clear(monkeypatch)
    ew._GAP_CACHE["HOOD"] = (-5.6, T - 3 * 3600, "sip")
    assert ew._slot_unseatable("HOOD", CFG, now=T) is True
    ew._GAP_CACHE["HOOD"] = (-5.6, T - 30, "iex")
    assert ew._slot_unseatable("HOOD", CFG, now=T) is True
    ew._GAP_CACHE["HOOD"] = (-5.6, T - ew.GAP_IEX_TTL_SEC - 1, "iex")
    assert ew._slot_unseatable("HOOD", CFG, now=T) is False


def test_yesterdays_gap_does_not_block(monkeypatch):
    _clear(monkeypatch)
    ew._GAP_CACHE["HOOD"] = (-5.6, T - 86400, "sip")
    assert ew._slot_unseatable("HOOD", CFG, now=T) is False


def test_gate_ttls_share_the_constants():
    import inspect
    assert inspect.signature(ew.sip_spread_pct).parameters["ttl"].default == ew.SIP_SPREAD_TTL_SEC
