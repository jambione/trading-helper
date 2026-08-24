"""Share count is the one *cause* on the entry row, so it has to be honest.

Two properties matter. Unknown must stay unknown — a name nobody looked
up must never read as low-float, because low-float is the leg that admits
a trade. And the read path must never raise: it runs inside a two-second
entry poll and a share count is not worth stopping the desk for.
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ff = pytest.importorskip("float_feed")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    def _write(data):
        p = tmp_path / "float_cache.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(ff, "CACHE_PATH", p)
        ff._CACHE["mtime"] = None
        return p
    return _write


def test_a_known_small_company_reads_low_float(cache):
    cache({"CDTG": {"shares_out": 3.02, "ts": time.time()}})
    assert ff.shares_out("CDTG") == pytest.approx(3.02)
    assert ff.is_low_float("CDTG") is True


def test_a_known_large_company_reads_high(cache):
    cache({"AAPL": {"shares_out": 14687.36, "ts": time.time()}})
    assert ff.is_low_float("AAPL") is False


def test_an_unlooked_up_symbol_is_unknown_not_low(cache):
    """The property that decides trades. None must not read as small."""
    cache({"AAPL": {"shares_out": 14687.36, "ts": time.time()}})
    assert ff.shares_out("NOPE") is None
    assert ff.is_low_float("NOPE") is None      # not False, and not True


def test_a_cached_empty_profile_is_unknown_not_zero(cache):
    """Finnhub returns {} for delisted tickers. Zero shares is not a float."""
    cache({"DEAD": {"shares_out": None, "ts": time.time()}})
    assert ff.shares_out("DEAD") is None
    assert ff.is_low_float("DEAD") is None


def test_the_cap_is_configurable_and_strict(cache):
    cache({"X": {"shares_out": 10.0, "ts": time.time()}})
    assert ff.is_low_float("X", 10.0) is False
    assert ff.is_low_float("X", 10.1) is True


def test_symbols_are_matched_case_insensitively(cache):
    cache({"PCLA": {"shares_out": 9.61, "ts": time.time()}})
    assert ff.shares_out("pcla") == pytest.approx(9.61)


def test_a_corrupt_cache_is_unknown_rather_than_fatal(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(ff, "CACHE_PATH", p)
    ff._CACHE["mtime"] = None
    assert ff.load_cache() == {}
    assert ff.shares_out("ANY") is None


def test_a_missing_cache_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "CACHE_PATH", tmp_path / "absent.json")
    ff._CACHE["mtime"] = None
    assert ff.shares_out("ANY") is None


def test_the_read_path_never_raises_on_junk(cache):
    cache({})
    for bad in (None, "", 123, object()):
        assert ff.shares_out(bad) is None
        assert ff.is_low_float(bad) is None


def test_refresh_without_symbols_or_key_is_a_no_op(monkeypatch):
    assert ff.refresh([]) == 0
    monkeypatch.setattr(ff, "_api_key", lambda: None)
    assert ff.refresh(["AAPL"]) == 0


def test_refresh_skips_names_already_cached_and_fresh(cache, monkeypatch):
    """A share count is a fundamental; re-fetching it every pass is waste."""
    cache({"AAPL": {"shares_out": 14687.0, "ts": time.time()}})
    monkeypatch.setattr(ff, "_api_key", lambda: "k")
    calls = []
    monkeypatch.setattr(ff, "_fetch_one",
                        lambda s, k, timeout=8.0: calls.append(s) or
                        {"shares_out": 1.0, "ts": time.time()})
    assert ff.refresh(["AAPL"]) == 0
    assert calls == []


def test_refresh_does_fetch_a_stale_reading(cache, monkeypatch):
    cache({"AAPL": {"shares_out": 14687.0, "ts": time.time() - 999999}})
    monkeypatch.setattr(ff, "_api_key", lambda: "k")
    monkeypatch.setattr(ff, "_fetch_one", lambda s, k, timeout=8.0:
                        {"shares_out": 2.0, "ts": time.time()})
    assert ff.refresh(["AAPL"]) == 1


def test_a_rate_limit_stops_the_pass_and_keeps_what_it_got(cache, monkeypatch):
    """Finnhub allows 60/minute. The first backfill burned its quota on
    batch one and then reported 'fetched 0' five times, which reads
    exactly like a cache that is already warm."""
    cache({})
    monkeypatch.setattr(ff, "_api_key", lambda: "k")
    seen = []

    def fake(sym, key, timeout=8.0):
        seen.append(sym)
        if len(seen) > 2:
            return ff.RATE_LIMITED
        return {"shares_out": 5.0, "ts": time.time()}

    monkeypatch.setattr(ff, "_fetch_one", fake)
    assert ff.refresh(["A", "B", "C", "D", "E"], limit=5) == 2
    assert len(seen) == 3            # stopped on the 429, did not burn D/E
    assert ff.shares_out("A") == pytest.approx(5.0)


def test_a_failed_fetch_leaves_the_previous_reading_alone(cache, monkeypatch):
    cache({"AAPL": {"shares_out": 14687.0, "ts": time.time() - 999999}})
    monkeypatch.setattr(ff, "_api_key", lambda: "k")
    monkeypatch.setattr(ff, "_fetch_one", lambda s, k, timeout=8.0: None)
    assert ff.refresh(["AAPL"]) == 0
    assert ff.shares_out("AAPL") == pytest.approx(14687.0)
