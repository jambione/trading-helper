import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord_source as ds  # noqa: E402


def _w(p, **kw):
    p.write_text(json.dumps(kw))


def test_fresh_capture_returns_lines(tmp_path):
    p = tmp_path / "live.json"
    _w(p, ts=1000.0, ok=True, lines=["$AAPL alert", "  ", "second"])
    assert ds._read_ocr_live(p, 1005.0, 15.0) == (["$AAPL alert", "second"], True, "ok", "")


def test_stale_missing_and_failed(tmp_path):
    p = tmp_path / "live.json"
    assert ds._read_ocr_live(p, 0.0, 15.0)[1:3] == (False, "daemon_missing")
    _w(p, ts=1000.0, ok=True, lines=["x"])
    assert ds._read_ocr_live(p, 1020.0, 15.0)[1:3] == (False, "daemon_stale")
    _w(p, ts=1000.0, ok=False, lines=[], error="Screen Recording not granted")
    r = ds._read_ocr_live(p, 1001.0, 15.0)
    assert r[1] is False and r[2] == ds._ocr_failure_kind("Screen Recording not granted")
    p.write_text("{not json")
    assert ds._read_ocr_live(p, 1001.0, 15.0)[2] == "daemon_unreadable"


def test_run_ocr_daemon_never_spawns(tmp_path, monkeypatch):
    p = tmp_path / "live.json"
    monkeypatch.setattr(ds, "OCR_LIVE_PATH", p)
    monkeypatch.setattr(ds, "_run_ocr_binary", lambda cmd: (_ for _ in ()).throw(AssertionError("spawned")))
    import time
    _w(p, ts=time.time(), ok=True, lines=["hi"])
    assert ds._run_ocr(["/bin/echo"], {"discord_ocr_daemon": True, "discord_ocr_poll_sec": 2.5})[:2] == (["hi"], True)
