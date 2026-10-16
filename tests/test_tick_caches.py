"""Per-tick caches added for the 10/16 live profile (book loop over its 3 s tick).

One test group per cache: TTL expiry, invalidation on order writes / fills,
and re-parse on file change. Each cache must be invisible except for speed.
"""
import json
import os
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import alpaca_trader as tr
import config as cfg_mod
import desk_core

ET = ZoneInfo("America/New_York")


# ── market_is_open: cached broker clock ──────────────────────────────────────

class _Clock:
    def __init__(self, is_open=True, boundary_in=3600.0, fail=False):
        self.calls = 0
        self.is_open = is_open
        self.boundary_in = boundary_in
        self.fail = fail

    def get_clock(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429")
        b = datetime.fromtimestamp(time.time() + self.boundary_in, ET)
        return SimpleNamespace(is_open=self.is_open, next_close=b, next_open=b)


@pytest.fixture()
def clock_client(monkeypatch):
    def _arm(**kw):
        c = _Clock(**kw)
        monkeypatch.setattr(tr, "_mode", "paper")
        monkeypatch.setattr(tr, "_client", c)
        tr.invalidate_clock_cache()
        return c
    yield _arm
    tr.invalidate_clock_cache()


def _advance(monkeypatch, secs):
    real = time.time()
    monkeypatch.setattr(tr.time, "time", lambda: real + secs)


def test_clock_is_fetched_once_within_ttl(clock_client):
    c = clock_client(is_open=True)
    assert all(tr.market_is_open() for _ in range(50))
    assert c.calls == 1


def test_clock_refetches_after_ttl(clock_client, monkeypatch):
    c = clock_client(is_open=True)
    tr.market_is_open()
    _advance(monkeypatch, tr._RAW_CLOCK_TTL_SEC + 1)
    tr.market_is_open()
    assert c.calls == 2


def test_clock_never_held_past_session_boundary(clock_client, monkeypatch):
    c = clock_client(is_open=True, boundary_in=2.0)   # closes in 2 s
    assert tr.market_is_open() is True
    _advance(monkeypatch, 3.0)                         # inside the 30 s TTL
    c.is_open = False
    assert tr.market_is_open() is False
    assert c.calls == 2


def test_clock_error_is_held_briefly_then_retried(clock_client, monkeypatch):
    c = clock_client(fail=True)
    assert tr.market_is_open() is False
    assert tr.market_is_open() is False
    assert c.calls == 1
    _advance(monkeypatch, tr._RAW_CLOCK_ERR_TTL_SEC + 0.5)
    c.fail = False
    assert tr.market_is_open() is True
    assert c.calls == 2


# ── load_config_ro: shared read-only config ──────────────────────────────────

@pytest.fixture()
def real_cfg(tmp_path, monkeypatch):
    """The real loader against a tmp config (conftest patches load_config)."""
    monkeypatch.setattr(cfg_mod, "load_config", cfg_mod._load_config_impl)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", tmp_path / "bot_config.json")
    monkeypatch.setattr(cfg_mod, "SECRETS_FILE", tmp_path / "secrets.json")
    cfg_mod.invalidate_cache()
    yield tmp_path / "bot_config.json"
    cfg_mod.invalidate_cache()


def test_load_config_ro_is_shared_and_read_only(real_cfg):
    real_cfg.write_text(json.dumps({"ai_max_positions": 7}))
    a, b = cfg_mod.load_config_ro(), cfg_mod.load_config_ro()
    assert a is b and isinstance(a, dict)
    assert a["ai_max_positions"] == 7
    with pytest.raises(TypeError):
        a["ai_max_positions"] = 1
    with pytest.raises(TypeError):
        a.update({"x": 1})
    c = a.copy()
    c["ai_max_positions"] = 1                      # a copy is writable
    assert cfg_mod.load_config_ro()["ai_max_positions"] == 7
    assert cfg_mod.load_config()["ai_max_positions"] == 7


def test_load_config_ro_sees_a_file_edit(real_cfg):
    real_cfg.write_text(json.dumps({"ai_max_positions": 7}))
    first = cfg_mod.load_config_ro()
    real_cfg.write_text(json.dumps({"ai_max_positions": 12, "pad": "x"}))
    os.utime(real_cfg, ns=(time.time_ns() + 10**9,) * 2)
    second = cfg_mod.load_config_ro()
    assert second is not first
    assert second["ai_max_positions"] == 12


def test_load_config_ro_defers_to_a_patched_loader(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"patched": True})
    assert cfg_mod.load_config_ro() == {"patched": True}


# ── broker reads: per-tick sharing, invalidated on every write ───────────────

class _Broker:
    def __init__(self):
        self.pos_calls = 0
        self.order_calls = 0
        self.positions = [SimpleNamespace(
            symbol="ABC", qty="10", avg_entry_price="5", current_price="5.1",
            unrealized_pl="1", unrealized_plpc="0.02", market_value="51")]
        self.fail = False

    def get_all_positions(self):
        self.pos_calls += 1
        if self.fail:
            raise RuntimeError("429")
        return list(self.positions)

    def get_orders(self, filter=None):
        self.order_calls += 1
        return [SimpleNamespace(id="o1", symbol="ABC", side="sell", qty="10",
                                filled_qty="0", type="limit", status="new",
                                limit_price="6", stop_price=None)]

    def submit_order(self, req):
        return SimpleNamespace(id="o2")

    def cancel_order_by_id(self, oid):
        return None

    def close_position(self, sym):
        return SimpleNamespace(id="o3")

    def get_order_by_id(self, oid):
        return SimpleNamespace(status="filled", filled_avg_price="5", filled_qty="10")


@pytest.fixture()
def broker(monkeypatch):
    b = _Broker()
    monkeypatch.setattr(tr, "_mode", "paper")
    monkeypatch.setattr(tr, "_client", b)
    tr.invalidate_broker_reads()
    yield b
    tr.invalidate_broker_reads()


def test_positions_shared_across_detail_and_plain_within_ttl(broker):
    for _ in range(5):
        assert tr.get_positions_detail()["ABC"]["qty"] == 10.0
        assert tr.get_open_positions()["ABC"]["qty"] == 10.0
    assert broker.pos_calls == 1


def test_open_orders_shared_within_ttl(broker):
    for _ in range(5):
        assert tr.get_open_orders()[0]["id"] == "o1"
    assert broker.order_calls == 1


def test_broker_read_expires_after_ttl(broker, monkeypatch):
    tr.get_positions_detail()
    real = time.monotonic()
    monkeypatch.setattr(tr.time, "monotonic",
                        lambda: real + tr._BROKER_READ_TTL_SEC + 0.1)
    tr.get_positions_detail()
    assert broker.pos_calls == 2


@pytest.mark.parametrize("write", [
    lambda: tr._submit_order(object()),
    lambda: tr._cancel_order_by_id("o1"),
    lambda: tr._close_position("ABC"),
    lambda: tr.get_order("o1"),                 # observed fill
])
def test_any_order_write_or_fill_invalidates(broker, write):
    tr.get_positions_detail()
    tr.get_open_orders()
    write()
    tr.get_positions_detail()
    tr.get_open_orders()
    assert broker.pos_calls == 2
    assert broker.order_calls == 2


def test_fresh_bypasses_the_cache(broker):
    tr.get_positions_detail()
    tr.get_positions_detail(fresh=True)
    tr.get_open_positions(fresh=True)
    assert broker.pos_calls == 3


def test_failed_read_is_not_cached(broker):
    broker.fail = True
    assert tr.get_positions_detail() is None
    broker.fail = False
    assert tr.get_positions_detail()["ABC"]["qty"] == 10.0
    assert broker.pos_calls == 2


def test_read_in_flight_across_a_write_is_not_stored(broker):
    orig = broker.get_all_positions

    def racing():
        rows = orig()
        tr.invalidate_broker_reads()      # an order lands mid-read
        return rows
    broker.get_all_positions = racing
    tr.get_positions_detail()
    broker.get_all_positions = orig
    tr.get_positions_detail()
    assert broker.pos_calls == 2


def test_cached_rows_cannot_be_mutated_through_a_caller(broker):
    tr.get_open_orders()[0]["id"] = "mutated"
    tr.get_positions_detail()["ABC"]["qty"] = -1
    assert tr.get_open_orders()[0]["id"] == "o1"
    assert tr.get_positions_detail()["ABC"]["qty"] == 10.0


# ── parse_file_cached: unchanged files parse once ────────────────────────────

def _bump(path):
    os.utime(path, ns=(time.time_ns() + 10**9,) * 2)


def test_parse_file_cached_parses_once_until_the_file_changes(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": 1}')
    calls = []

    def parse(text):
        calls.append(text)
        return json.loads(text)
    assert desk_core.parse_file_cached(p, parse) == {"a": 1}
    assert desk_core.parse_file_cached(p, parse) == {"a": 1}
    assert len(calls) == 1
    p.write_text('{"a": 2}')
    _bump(p)
    assert desk_core.parse_file_cached(p, parse) == {"a": 2}
    assert len(calls) == 2


def test_parse_file_cached_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        desk_core.parse_file_cached(tmp_path / "nope.json", json.loads)


def test_realized_r_today_reparses_on_append(tmp_path, monkeypatch):
    import ai_positions as ap
    p = tmp_path / "outcomes.jsonl"
    monkeypatch.setattr(ap, "OUTCOMES_PATH", p)
    now = time.time()
    p.write_text(json.dumps({"realized_r_multiple": 1.5, "exit_time": now}) + "\n")
    assert ap.realized_r_today(now) == pytest.approx(1.5)
    with open(p, "a") as f:
        f.write(json.dumps({"realized_r_multiple": -0.5, "exit_time": now}) + "\n")
    _bump(p)
    assert ap.realized_r_today(now) == pytest.approx(1.0)


def test_load_token_metrics_reparses_and_hands_out_copies(tmp_path):
    import ai_suggest as su
    p = tmp_path / "token_metrics.jsonl"
    p.write_text(json.dumps({"n": 1}) + "\n")
    rows = su.load_token_metrics(p)
    rows[0]["n"] = 99
    assert su.load_token_metrics(p) == [{"n": 1}]
    with open(p, "a") as f:
        f.write(json.dumps({"n": 2}) + "\n")
    _bump(p)
    assert su.load_token_metrics(p, limit=1) == [{"n": 2}]


def test_overnight_snapshot_reparses_on_change(tmp_path, monkeypatch):
    import ai_trader
    seen = []
    real = desk_core.parse_file_cached

    def spy(path, parse, **kw):
        seen.append(str(path))
        return real(path, parse, **kw)
    monkeypatch.setattr(desk_core, "parse_file_cached", spy)
    # The helper resolves its path from the module file; just prove it routes
    # through the cache and survives a missing file.
    assert ai_trader._overnight_snapshot("no_such_book_dir") is None
    assert seen and seen[0].endswith("no_such_book_dir/snapshot.json")


# ── premarket_bursts: final for the day after the open ───────────────────────

def test_premarket_bursts_freeze_after_open(tmp_path, monkeypatch):
    import strength_signal as ss
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    ss._BURSTS.clear()
    day = "2026-10-16"
    pm = datetime(2026, 10, 16, 8, 0, tzinfo=ET).timestamp()
    rth = datetime(2026, 10, 16, 9, 40, tzinfo=ET).timestamp()
    log = tmp_path / "signal_shadow.jsonl"
    log.write_text(
        json.dumps({"ts": pm, "signal": "mention_burst", "ticker": "abc"}) + "\n"
        + json.dumps({"ts": pm, "signal": "other", "ticker": "zzz"}) + "\n")
    assert ss.premarket_bursts(day, rth) == {"ABC"}
    log.unlink()                                  # a later read would see nothing
    assert ss.premarket_bursts(day, rth + 10 * ss.BURST_REFRESH_SEC) == {"ABC"}
    ss._BURSTS.clear()


def test_premarket_bursts_still_refresh_before_open(tmp_path, monkeypatch):
    import strength_signal as ss
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    ss._BURSTS.clear()
    day = "2026-10-16"
    t = datetime(2026, 10, 16, 8, 0, tzinfo=ET).timestamp()
    log = tmp_path / "signal_shadow.jsonl"
    log.write_text("")
    assert ss.premarket_bursts(day, t) == set()
    log.write_text(json.dumps({"ts": t, "signal": "mention_burst", "ticker": "x"}) + "\n")
    assert ss.premarket_bursts(day, t + ss.BURST_REFRESH_SEC + 1) == {"X"}
    ss._BURSTS.clear()


# ── env reload + data client reuse on the quote path ─────────────────────────

def test_load_desk_env_cached_skips_unchanged_files(tmp_path, monkeypatch):
    env = tmp_path / "signal_engine.env"
    env.write_text("ALPACA_API_KEY=k\nALPACA_SECRET_KEY=s\n")
    monkeypatch.setattr(desk_core, "ENV_FILE", env)
    monkeypatch.setattr(desk_core, "SECRETS_FILE", tmp_path / "secrets.json")
    monkeypatch.setattr(desk_core, "_env_stamp", None)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []
    real = desk_core.load_desk_env
    monkeypatch.setattr(desk_core, "load_desk_env",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(5):
        desk_core.load_desk_env_cached()
    assert len(calls) == 1
    env.write_text("ALPACA_API_KEY=k\nALPACA_SECRET_KEY=s\nX_NEW=1\n")
    _bump(env)
    desk_core.load_desk_env_cached()
    assert len(calls) == 2
    monkeypatch.delenv("ALPACA_API_KEY")          # a removed key forces a reload
    desk_core.load_desk_env_cached()
    assert len(calls) == 3


def test_quote_data_client_is_reused(monkeypatch):
    import ai_trading as t
    built = []

    class _C:
        def __init__(self, a, s):
            built.append((a, s))
    monkeypatch.setattr("alpaca.data.historical.StockHistoricalDataClient", _C)
    monkeypatch.setattr(t, "_data_clients", {})
    a = t._data_client("k", "s")
    assert t._data_client("k", "s") is a
    assert t._data_client("k2", "s") is not a     # rotated keys: new client
    assert len(built) == 2
