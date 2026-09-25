"""session_recorder — sources / config streams and flush behavior."""
import gzip
import json

import pytest

import session_recorder as rec


@pytest.fixture()
def recdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(rec, "_buffers", {})
    monkeypatch.setattr(rec, "_buf_bytes", {})
    monkeypatch.setattr(rec, "_source_state", {"day": None, "keys": {}})
    monkeypatch.setattr(rec, "_config_state", {"digest": None})
    monkeypatch.setattr(rec, "_last_flush_mono", 0.0)
    return tmp_path


def _read(path):
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def _at(hh, mm):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 9, 25, hh, mm, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_source_set_logs_enter_and_leave_only(recdir):
    t = _at(9, 40)
    rows = [{"symbol": "PFE", "source": "movers", "pct_change": 3.1, "rvol": 2.0,
             "price": 28.6},
            {"symbol": "kr", "source": "trending", "pct": 1.2}]
    rec.record_source_set(rows, ts=t)
    rec.record_source_set(rows, ts=t + 2)          # unchanged: nothing written
    rec.record_source_set(rows[:1], ts=t + 4)      # KR leaves
    rec.flush(force=True)
    got = _read(recdir / "sessions" / "2026-09-25" / "sources.jsonl.gz")
    assert [(r["symbol"], r["source"], r["event"]) for r in got] == [
        ("PFE", "movers", "enter"), ("KR", "trending", "enter"),
        ("KR", "trending", "leave")]
    assert got[0]["pct"] == 3.1 and got[0]["price"] == 28.6
    assert got[1]["pct"] == 1.2


def test_config_snap_only_on_change_and_strips_secrets(recdir):
    # Fake values: the test proves these keys are stripped from the snapshot.
    cfg = {"ai_watch_min_price": 20.0, "tv_webhook_secret": "s3cret",  # pragma: allowlist secret
           "finnhub_key": "k", "discord_webhook_url": "https://x"}  # pragma: allowlist secret
    rec.record_config_snap(cfg, git_sha="abc", fingerprint="fp1")
    rec.record_config_snap(dict(cfg), git_sha="abc", fingerprint="fp1")
    rec.record_config_snap({**cfg, "ai_watch_min_price": 25.0}, git_sha="abc")
    rec.flush(force=True)
    days = list((recdir / "sessions").iterdir())
    got = _read(days[0] / "config.jsonl.gz")
    assert [r["config"]["ai_watch_min_price"] for r in got] == [20.0, 25.0]
    blob = json.dumps(got)
    assert "s3cret" not in blob and "finnhub_key" not in blob
    assert "https://x" not in blob


def test_day_filtered_flush_keeps_other_days(recdir):
    rec.record_print("AAA", 30.0, src="finnhub", ts=_at(10, 0))
    rec._append("prints", {"ts": _at(10, 0) + 86400, "symbol": "BBB"})
    rec.flush(day="2026-09-25")
    assert any(k[0] == "2026-09-26" for k in rec._buffers)
    rec.flush(force=True)
    assert _read(recdir / "sessions" / "2026-09-26" / "prints.jsonl.gz")[0]["symbol"] == "BBB"


def test_appended_members_stay_readable(recdir):
    for i in range(3):
        rec.record_print("AAA", 30.0 + i, src="finnhub", ts=_at(10, i))
        rec.flush(force=True)
    got = _read(recdir / "sessions" / "2026-09-25" / "prints.jsonl.gz")
    assert [r["price"] for r in got] == [30.0, 31.0, 32.0]


def test_input_values_are_recorded_when_computed(recdir):
    import ai_entry_watch as ew
    ew._SIP_SPREAD_CACHE.pop("PFE", None)
    t = _at(10, 30)
    got = ew.sip_spread_pct("PFE", now=t, fetch=lambda s, when: [(28.60, 28.62)] * 3)
    assert got is not None
    ew.sip_spread_pct("PFE", now=t + 5, fetch=lambda s, when: [])  # cache hit: not re-recorded
    rec.flush(force=True)
    rows = _read(recdir / "sessions" / "2026-09-25" / "inputs.jsonl.gz")
    assert [(r["kind"], r["symbol"]) for r in rows] == [("sip_spread", "PFE")]
    assert abs(rows[0]["value"] - got) < 1e-12 and rows[0]["ts"] == t


def test_discord_alerts_are_recorded_with_price(recdir):
    t = _at(7, 41)
    rec.record_discord("jagx", "JAGX  Squeeze Potential Alert", ts=t, price=3.12, age_sec=1.4,
                       alert={"ticker": "JAGX", "burst": True, "card_brand": "find_it_first"})
    rec.flush(force=True)
    got = _read(recdir / "sessions" / "2026-09-25" / "discord.jsonl.gz")
    assert got == [{"ts": t, "et": "07:41:00", "symbol": "JAGX",
                    "line": "JAGX  Squeeze Potential Alert", "burst": True,
                    "card_brand": "find_it_first", "price": 3.12, "age_sec": 1.4}]
