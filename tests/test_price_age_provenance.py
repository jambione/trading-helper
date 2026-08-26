"""price_age_sec is the field every staleness guard on the desk keys off.

On 2026-08-26 it was None on all nine live names and absent on 53% of RTH
shadow rows, and the cause was one dropped field: Finnhub's REST /quote
returns `t` (the quote's own unix time in seconds), fetch_realtime_quote
discarded it, and the dashboard poll then called update_price with no
timestamp. With no trade time the merge publishes trade_ts=None, so
price_age_sec is None, so:

  ai_positions._fresh_tape_px      returns the price unconditionally
  ai_entry_watch._row_tape_stale   returns "not stale"
  the 15s blind-book flatten       cannot evaluate at all

`stale_quote` had blocked 0 of 17,585 RTH rows while tape_age_sec exceeded
its own 8s threshold on 69% of the rows that recorded one.

These pin the provenance end of that chain: a real stamp must survive, and
a missing or dishonest one must stay None rather than become "now".
"""
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import finnhub_stream as fs  # noqa: E402


# ── the REST quote must carry its own time ───────────────────────────────

def test_rest_quote_returns_the_quote_timestamp(monkeypatch):
    """`t` is what makes the price's age provable. It was being dropped."""
    payload = {"c": 12.5, "h": 13.0, "l": 12.0, "o": 12.1, "pc": 12.2,
               "t": 1787688000}

    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    got = fs.fetch_realtime_quote("key", "AAA")
    assert got["ok"] is True
    assert got["t"] == 1787688000
    assert got["c"] == 12.5


def test_rest_quote_reports_a_missing_timestamp_as_zero(monkeypatch):
    """Absent `t` must be 0 — update_price reads that as unknown, not epoch."""
    class _Resp:
        def read(self):
            return json.dumps({"c": 12.5}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    assert fs.fetch_realtime_quote("key", "AAA")["t"] == 0


# ── update_price turns that stamp into a provable age ────────────────────

def _state():
    return fs.FinnhubState() if hasattr(fs, "FinnhubState") else fs.FINNHUB_STATE


def test_a_real_stamp_becomes_trade_ts():
    st = _state()
    now_ms = int((time.time() - 3.0) * 1000)
    st.update_price("AAA", 10.0, timestamp=now_ms)
    with st.lock:
        rec = st.prices["AAA"]
    assert rec["trade_ts"] is not None
    assert abs(rec["trade_ts"] - now_ms / 1000.0) < 0.01


def test_no_stamp_stays_unknown_rather_than_now():
    """The whole bug in one assertion: absent age must not read as fresh."""
    st = _state()
    st.update_price("BBB", 10.0)
    with st.lock:
        rec = st.prices["BBB"]
    assert rec["trade_ts"] is None
    assert rec["ts_unix"] > 0        # we still know when WE learned it


def test_a_future_stamp_is_refused():
    """A skewed clock would claim a print from the future and win every
    merge, which is worse than having no age at all."""
    st = _state()
    st.update_price("CCC", 10.0, timestamp=int((time.time() + 600) * 1000))
    with st.lock:
        assert st.prices["CCC"]["trade_ts"] is None


def test_an_old_stamp_is_kept_because_old_is_the_point():
    """A 14-hour-old premarket quote must report 14 hours, not None. The
    guards exist to refuse exactly this, and can only do so if it survives.
    """
    st = _state()
    old = int((time.time() - 50_000) * 1000)
    st.update_price("DDD", 10.0, timestamp=old)
    with st.lock:
        rec = st.prices["DDD"]
    assert rec["trade_ts"] is not None
    assert time.time() - rec["trade_ts"] > 49_000


# ── the seconds/milliseconds boundary ────────────────────────────────────

def test_finnhub_seconds_convert_to_update_price_milliseconds():
    """Finnhub sends `t` in seconds; update_price takes ms.

    The failure mode of getting this wrong is NOT a rejected stamp — that
    was this test's first assumption and it was wrong. Seconds passed as
    milliseconds divide down to a 1970 timestamp, which satisfies
    `0 < trade_ts <= now + 5` and is stored happily. Every price would then
    report an age of ~56 years, and once the guards fail closed that stops
    the desk dead rather than quietly. Pinned so the conversion cannot drift
    without a red test.
    """
    st = _state()
    t_sec = time.time() - 5.0
    st.update_price("EEE", 10.0, timestamp=int(t_sec * 1000.0))
    with st.lock:
        rec = st.prices["EEE"]
    assert rec["trade_ts"] is not None
    assert abs(rec["trade_ts"] - t_sec) < 0.01

    st.update_price("FFF", 10.0, timestamp=int(t_sec))   # seconds-as-ms
    with st.lock:
        bad = st.prices["FFF"]["trade_ts"]
    assert bad is not None                      # accepted, not rejected
    assert time.time() - bad > 1_000_000_000    # and absurdly stale


def test_the_dashboard_poll_passes_the_stamp_through():
    """Pinned as source text: the poll is threaded and network-bound, and a
    regression here is invisible until price_age_sec quietly goes None again.
    """
    src = (_ROOT / "dashboard.py").read_text(encoding="utf-8")
    i = src.index("def _finnhub_rest_poll_worker")
    body = src[i:i + 3000]
    assert 'q.get("t")' in body, "the quote's own time must be read"
    assert "timestamp=q_ms" in body, "and handed to update_price"
    assert "1000" in body, "seconds -> milliseconds"
