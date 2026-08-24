"""Which names get a catalyst reading, when there are more than the cap.

`sorted(syms)[:N]` is the bug that cut a 314-name universe at KTOS on
2026-08-22 and turned every screen that week into a report on A-K. In a
screen it eventually showed up as a missing symbol. Here it would mean
names late in the alphabet never get news at all, forever, with nothing
downstream looking wrong — a silent blind spot rather than a wrong number.

So the selection is pinned: most recently seen first.
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

wd = pytest.importorskip("watchdog")


def _write_shadow(tmp_path, rows):
    d = tmp_path / "ai_reports"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "shadow.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return tmp_path


def _run(tmp_path, monkeypatch, rows, cap=3):
    _write_shadow(tmp_path, rows)
    monkeypatch.setattr(wd, "ROOT", tmp_path)
    monkeypatch.setattr(wd, "NEWS_MAX_SYMBOLS", cap)
    captured = {}

    class FakeFeed:
        @staticmethod
        def refresh(symbols, *a, **k):
            captured["symbols"] = list(symbols)
            return len(symbols)

    monkeypatch.setitem(sys.modules, "news_feed", FakeFeed)
    wd.refresh_news_cache()
    return captured.get("symbols", [])


def test_selection_is_by_recency_not_by_alphabet(tmp_path, monkeypatch):
    now = time.time()
    # ZZZZ is the most recent; alphabetical truncation would drop it first.
    rows = [
        {"symbol": "AAAA", "ts": now - 3000},
        {"symbol": "BBBB", "ts": now - 2000},
        {"symbol": "CCCC", "ts": now - 1000},
        {"symbol": "ZZZZ", "ts": now - 10},
    ]
    picked = _run(tmp_path, monkeypatch, rows, cap=2)
    assert picked == ["ZZZZ", "CCCC"]
    assert "AAAA" not in picked


def test_a_symbol_is_ranked_by_its_most_recent_sighting(tmp_path, monkeypatch):
    now = time.time()
    rows = [
        {"symbol": "OLD", "ts": now - 5},
        {"symbol": "DUP", "ts": now - 4000},
        {"symbol": "DUP", "ts": now - 1},      # seen again, just now
    ]
    picked = _run(tmp_path, monkeypatch, rows, cap=1)
    assert picked == ["DUP"]


def test_stale_names_are_not_refreshed(tmp_path, monkeypatch):
    """Six hours back. Friday's watchlist is not what we hold on Monday."""
    now = time.time()
    rows = [
        {"symbol": "FRESH", "ts": now - 60},
        {"symbol": "STALE", "ts": now - 9 * 3600},
    ]
    picked = _run(tmp_path, monkeypatch, rows, cap=10)
    assert picked == ["FRESH"]


def test_no_recent_rows_refreshes_nothing(tmp_path, monkeypatch):
    now = time.time()
    rows = [{"symbol": "OLD", "ts": now - 48 * 3600}]
    assert _run(tmp_path, monkeypatch, rows, cap=10) == []


def test_a_missing_shadow_log_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "ROOT", tmp_path)
    assert wd.refresh_news_cache() == 0


def test_a_corrupt_line_does_not_stop_the_refresh(tmp_path, monkeypatch):
    now = time.time()
    d = tmp_path / "ai_reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / "shadow.jsonl").write_text(
        "{not json\n" + json.dumps({"symbol": "GOOD", "ts": now - 5}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(wd, "ROOT", tmp_path)
    monkeypatch.setattr(wd, "NEWS_MAX_SYMBOLS", 10)

    class FakeFeed:
        @staticmethod
        def refresh(symbols, *a, **k):
            return len(symbols)

    monkeypatch.setitem(sys.modules, "news_feed", FakeFeed)
    assert wd.refresh_news_cache() == 1


def test_a_broken_news_feed_never_raises_into_the_supervisor(tmp_path,
                                                             monkeypatch):
    """The watchdog restarts the desk. It may not die of a news outage."""
    now = time.time()
    _write_shadow(tmp_path, [{"symbol": "AAA", "ts": now - 5}])
    monkeypatch.setattr(wd, "ROOT", tmp_path)

    class Boom:
        @staticmethod
        def refresh(*a, **k):
            raise RuntimeError("alpaca down")

    monkeypatch.setitem(sys.modules, "news_feed", Boom)
    assert wd.refresh_news_cache() == 0


# ------------------------------------------------------- learn job isolation

def test_learn_jobs_do_not_inherit_the_launching_shells_stdin():
    """The watchdog outlives the Terminal that started it.

    nohup redirects stdout and stderr but leaves fd 0 inherited. When the
    launching shell exits, fd 0 goes bad and every child dies before
    running a line of Python -- "init_sys_streams ... [Errno 9] Bad file
    descriptor" -- which run_learn_job reports as a nonzero rc. On
    2026-08-24 that produced a CRITICAL alarm from setup_audit that had
    nothing to do with the desk: instrumentation_check passed at 06:04:21
    and setup_audit failed at 06:05:52, the shell having closed between
    them. A safety net that dies with its Terminal is worse than none.
    """
    import inspect
    src = inspect.getsource(wd.run_learn_job)
    assert "stdin=subprocess.DEVNULL" in src


def test_a_learn_job_actually_runs_without_a_usable_stdin(tmp_path,
                                                          monkeypatch):
    """Behavioural version: close stdin, then run a real child."""
    import os
    import subprocess
    import sys
    monkeypatch.setattr(wd, "LOGDIR", tmp_path)
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wd, "ROOT", tmp_path)
    (tmp_path / "tools" / "probe.py").write_text(
        "import sys; sys.exit(0)\n", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    saved = os.dup(0)
    try:
        os.dup2(devnull, 0)
        os.close(devnull)
        rc = wd.run_learn_job(sys.executable, "probe.py", timeout=30.0)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    assert rc == 0
