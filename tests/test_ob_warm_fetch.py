"""Order-block bar warm fetch (approved 2026-10-06 09:43 ET).

The ob store was fed only by _fetch_symbol_lows, which book names never reach,
so ai_watch_ob_resist_skip had no reading and was inert. _ob_warm_request (poll
thread, enqueue only) + _ob_warm_job (one background worker, alpaca_api.fetch_bars)
feed ob_observe ONLY. Pinned here: the poll never waits; <=1 request per name per
120 s; backs off when the desk was recently 429'd; first fetch covers prior day +
today; nothing but the ob store is written (%R inputs untouched); failure / stale
store = no reading = the skip fails open; counters + log line.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import alpaca_api as aa  # noqa: E402
import ai_entry_watch as ew  # noqa: E402
import ob_observe  # noqa: E402

pd = pytest.importorskip("pandas")
ET = ZoneInfo("America/New_York")
SKIP_ON = {"ai_watch_ob_resist_skip": True}


class _FakeExec:
    def __init__(self, run=False):
        self.run, self.calls = run, []

    def submit(self, fn, *a):
        self.calls.append(a)
        if self.run:
            fn(*a)


def _frame(days=((2026, 10, 5), (2026, 10, 6)), start=(9, 30), n=90, base=10.0):
    idx, rows = [], []
    px = base
    for d in days:
        t0 = datetime(*d, *start, tzinfo=ET).timestamp()
        for i in range(n):
            # a saw-tooth so swings (and blocks) exist
            px = base + (0.5 if (i // 12) % 2 else -0.5) * ((i % 12) / 12.0)
            o, c = px, px + (0.03 if i % 2 else -0.03)
            idx.append(pd.Timestamp(t0 + 60 * i, unit="s", tz="UTC"))
            rows.append((o, max(o, c) + 0.02, min(o, c) - 0.02, c, 1000))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx),
                        columns=["open", "high", "low", "close", "volume"])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TH_OB_WARM_OFF", raising=False)
    ob_observe.reset()
    ew._ob_warm_last.clear()
    ew._ob_warm_pending.clear()
    for k in ew._OB_WARM_STATS:
        ew._OB_WARM_STATS[k] = 0
    monkeypatch.setattr(aa, "recently_rate_limited", lambda *a, **k: False)
    fx = _FakeExec()
    monkeypatch.setattr(ew, "_ob_warm_executor", lambda: fx)
    yield fx
    ob_observe.reset()
    ew._ob_warm_last.clear()
    ew._ob_warm_pending.clear()


def test_enqueue_only_and_one_per_name_per_120s(_clean):
    fx = _clean
    assert ew._ob_warm_request("abc", now_wall=1000.0) is True
    assert fx.calls == [("ABC",)]          # enqueued, not run: poll never waits
    assert ew._ob_warm_request("ABC", now_wall=1050.0) is False   # pending
    ew._ob_warm_pending.clear()
    assert ew._ob_warm_request("ABC", now_wall=1119.0) is False   # < 120 s
    assert ew._ob_warm_request("ABC", now_wall=1120.0) is True
    assert ew._ob_warm_request("XYZ", now_wall=1120.0) is True    # per name
    assert len(fx.calls) == 3 and ew.ob_warm_stats()["requested"] == 3


def test_backs_off_when_desk_recently_rate_limited(_clean, monkeypatch):
    monkeypatch.setattr(aa, "recently_rate_limited", lambda *a, **k: True)
    assert ew._ob_warm_request("ABC", now_wall=1000.0) is False
    assert _clean.calls == [] and ew.ob_warm_stats()["skipped_429"] == 1
    # and it waits a full window before trying again
    monkeypatch.setattr(aa, "recently_rate_limited", lambda *a, **k: False)
    assert ew._ob_warm_request("ABC", now_wall=1060.0) is False
    assert ew._ob_warm_request("ABC", now_wall=1120.0) is True


def test_kill_switch_env(monkeypatch, _clean):
    monkeypatch.setenv("TH_OB_WARM_OFF", "1")
    assert ew._ob_warm_request("ABC", now_wall=1000.0) is False
    assert _clean.calls == []


def test_job_feeds_ob_store_only(monkeypatch):
    seen = []
    df = _frame()
    monkeypatch.setattr(ew, "_ob_warm_data_client", lambda: object())

    def fake_fetch(client, sym, cfg):
        seen.append((sym, cfg["bar_timeframe"], cfg["bar_count"]))
        return df
    monkeypatch.setattr(aa, "fetch_bars", fake_fetch)
    monkeypatch.setattr(ew, "_seed_stream_from_iex",
                        lambda *a, **k: pytest.fail("must not seed the stream"))
    ohlc_before = dict(ew._ohlc_cache)
    bar_before = dict(ew._bar_cache)
    ew._ob_warm_pending.add("ABC")
    assert ew._ob_warm_job("ABC") == len(df)
    assert seen == [("ABC", "1Min", ew.OB_WARM_FIRST_LIMIT)]
    assert ob_observe.store_size("ABC") == len(df)
    assert ob_observe.has_prior_day("ABC") is True
    assert dict(ew._ohlc_cache) == ohlc_before and dict(ew._bar_cache) == bar_before
    assert "ABC" not in ew._ob_warm_pending
    st = ew.ob_warm_stats()
    assert st["ok"] == 1 and st["bars"] == len(df) and st["fail"] == 0
    # store has prior day now -> later refreshes are small (still one request)
    ew._ob_warm_job("ABC")
    assert seen[-1] == ("ABC", "1Min", ew.OB_WARM_REFRESH_LIMIT)


def test_first_limit_covers_prior_day_and_today():
    # 04:00-20:00 ext-hours prior day (960) + 04:00-16:00 today (720)
    assert ew.OB_WARM_FIRST_LIMIT >= 960 + 720
    assert ew.OB_WARM_REFRESH_SEC >= 120.0


@pytest.mark.parametrize("mode", ["raise", "none", "nocreds"])
def test_failure_counts_and_fails_open(monkeypatch, mode):
    if mode == "nocreds":
        monkeypatch.setattr(ew, "_ob_warm_data_client", lambda: None)
    else:
        monkeypatch.setattr(ew, "_ob_warm_data_client", lambda: object())

    def fake_fetch(*a):
        if mode == "raise":
            raise RuntimeError("boom")
        return None
    monkeypatch.setattr(aa, "fetch_bars", fake_fetch)
    ew._ob_warm_pending.add("ABC")
    assert ew._ob_warm_job("ABC") == 0          # never raises
    assert "ABC" not in ew._ob_warm_pending
    st = ew.ob_warm_stats()
    assert st["no_data" if mode == "none" else "fail"] == 1
    assert ob_observe.store_size("ABC") == 0
    rec = {"indicator": {"pctr": -20}}
    f = ew._ob_observe_stamp(rec, "ABC", 10.0, SKIP_ON, time.time())
    assert f == {} and ew._ob_resist_refusal(f, SKIP_ON) is False


def test_stale_store_withholds_reading_so_skip_fails_open(monkeypatch):
    df = _frame()
    ob_observe.absorb_df("ABC", df)
    now = df.index[-1].timestamp() + 120
    assert ob_observe.fields("ABC", 10.0, now)            # a reading exists
    ob_observe._LAST_ABSORB["ABC"] = time.time() - ew.OB_WARM_STALE_SEC - 5
    rec = {"indicator": {"pctr": -20}}
    f = ew._ob_observe_stamp(rec, "ABC", 10.0, SKIP_ON, now)
    assert f == {} and ew._ob_resist_refusal(f, SKIP_ON) is False
    assert "ob_resist_0.3" not in rec["indicator"]
    monkeypatch.setattr(ew, "_push_cfg", lambda: dict(SKIP_ON))
    assert ew._ob_no_reading_note("ABC") == {"ob_stale": True}
    # fresh again -> reading back
    ob_observe._LAST_ABSORB["ABC"] = time.time()
    assert ew._ob_observe_stamp(rec, "ABC", 10.0, SKIP_ON, now)


def test_stamp_enqueues_without_waiting(_clean):
    t0 = time.perf_counter()
    f = ew._ob_observe_stamp({"indicator": {"pctr": -20}}, "ABC", 10.0, SKIP_ON, time.time())
    assert time.perf_counter() - t0 < 0.5
    assert f == {} and _clean.calls == [("ABC",)]


def test_stamp_does_nothing_when_knobs_off(_clean):
    assert ew._ob_observe_stamp({"indicator": {"x": 1}}, "ABC", 10.0, {}, time.time()) == {}
    assert _clean.calls == []


def test_real_worker_thread_runs_job(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ew, "_ob_warm_executor", lambda: pool)
    monkeypatch.setattr(ew, "_ob_warm_data_client", lambda: object())
    monkeypatch.setattr(aa, "fetch_bars", lambda *a: _frame())
    assert ew._ob_warm_request("ABC") is True
    pool.shutdown(wait=True)
    assert ob_observe.store_size("ABC") > 0 and "ABC" not in ew._ob_warm_pending


def test_client_gets_http_timeout(monkeypatch):
    got = {}

    class Sess:
        def request(self, method, url, **kw):
            got.update(kw)
            return "ok"

    class Client:
        _session = Sess()

    import config
    monkeypatch.setattr(config, "load_config", lambda: {"api_key": "k", "secret_key": "s"})
    monkeypatch.setattr(aa, "connect_data_client", lambda creds: Client())
    monkeypatch.setattr(ew, "_ob_warm_client", None)
    c = ew._ob_warm_data_client()
    assert c._session.request("GET", "u") == "ok"
    assert got["timeout"] == ew.OB_WARM_HTTP_TIMEOUT_SEC
    monkeypatch.setattr(ew, "_ob_warm_client", None)


def test_counter_log_line(capsys):
    ew._ob_warm_maybe_log(force=True)
    out = capsys.readouterr().out
    assert out.startswith("[ob] warm fetch: requested=0 ok=0 no_data=0 fail=0")


def test_ob_stale_on_wire_keys_and_js():
    assert "ob_stale" in ew._OB_WIRE_KEYS
    body = (ROOT / "static/js/feeds.js").read_text(encoding="utf-8")
    assert "ob_stale: !!w.ob_stale" in body and "'stale'" in body
