"""
test_watchdog.py — the supervisor's decision logic, without spawning anything.

Covers the two things that make it safe to leave running unattended: it must
not hot-loop on a process that refuses to start, and it must not leave a
pidfile that `./trading stop` will act on. Process launching itself is a thin
subprocess.Popen wrapper and is not exercised here.

Run:
    venv/bin/python -m pytest tests/test_watchdog.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import watchdog as w  # noqa: E402


# ── Backoff ──────────────────────────────────────────────────────────────────

def test_first_restart_is_immediate():
    """The common case is a one-off death; making the desk wait on it would be
    the very delay the watchdog exists to remove."""
    assert w.restart_delay(0) == 0.0


def test_backoff_grows_then_caps():
    delays = [w.restart_delay(n, base=5.0, cap=120.0) for n in range(1, 8)]
    assert delays[:5] == [5.0, 10.0, 20.0, 40.0, 80.0]
    assert all(d == 120.0 for d in delays[5:])
    assert delays == sorted(delays), "backoff must be monotonic"


def test_backoff_never_gives_up():
    """Capped, not abandoned — an unattended desk is worth more with a process
    that keeps retrying than one that quit at 09:31 and told nobody."""
    assert w.restart_delay(500, base=5.0, cap=120.0) == 120.0


# ── Pidfile hygiene ──────────────────────────────────────────────────────────

def test_prune_drops_dead_pids():
    alive = {10, 30}.__contains__
    assert w.prune_pids([10, 20, 30, 40], alive) == [10, 30]


def test_prune_dedupes_preserving_order():
    """A restarted service appends its pid; the same number must not accumulate."""
    assert w.prune_pids([7, 7, 9, 7], lambda p: True) == [7, 9]


def test_prune_of_all_dead_yields_empty():
    assert w.prune_pids([1, 2, 3], lambda p: False) == []


def test_pidfile_roundtrip(tmp_path):
    p = tmp_path / ".pids"
    w.write_pidfile(p, [101, 202])
    assert w.read_pidfile(p) == [101, 202]


def test_empty_pidfile_roundtrip(tmp_path):
    p = tmp_path / ".pids"
    w.write_pidfile(p, [])
    assert w.read_pidfile(p) == []


def test_unreadable_or_garbage_pidfile_reads_as_empty(tmp_path):
    """A corrupt pidfile must not crash the supervisor — it is the one process
    that has to survive everything else going wrong."""
    missing = tmp_path / "nope"
    assert w.read_pidfile(missing) == []
    junk = tmp_path / "junk"
    junk.write_text("not-a-pid\n")
    assert w.read_pidfile(junk) == []


# ── Learn-loop schedules ─────────────────────────────────────────────────────

def test_instrumentation_before_desk_start_is_skipped():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 12, 8, 30, tzinfo=et)
    run, key = w.should_run_instrumentation(
        now, start_hhmm="09:00", last_key=None)
    assert run is False


def test_instrumentation_once_per_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 12, 10, 15, tzinfo=et)
    run, key = w.should_run_instrumentation(
        now, start_hhmm="09:00", last_key=None)
    assert run is True
    assert key == "2026-08-12T10"
    run2, key2 = w.should_run_instrumentation(
        now, start_hhmm="09:00", last_key=key)
    assert run2 is False
    assert key2 == key


def test_eod_once_per_day_after_cutoff():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    before = datetime(2026, 8, 12, 15, 50, tzinfo=et)
    run, day = w.should_run_eod(before, eod_hhmm="16:05", last_day=None)
    assert run is False
    after = datetime(2026, 8, 12, 16, 10, tzinfo=et)
    run, day = w.should_run_eod(after, eod_hhmm="16:05", last_day=None)
    assert run is True
    assert day == "2026-08-12"
    run2, day2 = w.should_run_eod(after, eod_hhmm="16:05", last_day=day)
    assert run2 is False



def test_pid_alive_true_for_self_false_for_unused():
    assert w.pid_alive(os.getpid()) is True
    # PID 0 is the scheduler on macOS/Linux and is never a normal process;
    # os.kill(0, 0) signals our own process group, so use an implausible one.
    assert w.pid_alive(2 ** 22) is False


# ── Service selection ────────────────────────────────────────────────────────

def test_dashboard_is_health_checked_over_http_not_liveness():
    """A hung uvicorn still has a PID, and to the OCR source a hang and a crash
    are the same outage."""
    svcs = {s.name: s for s in w.default_services(8888)}
    assert svcs["dashboard"].health_url == "http://localhost:8888/api/meta"
    assert svcs["engine"].health_url is None


def test_disabled_discord_source_is_not_supervised():
    """A producer switched off in config must not be restarted into existence
    every interval."""
    names = [s.name for s in w.enabled_services({"discord_ocr_enabled": False}, 8888)]
    assert "discord" not in names
    assert "dashboard" in names

    names_on = [s.name for s in w.enabled_services({"discord_ocr_enabled": True}, 8888)]
    assert "discord" in names_on


def test_health_url_honours_a_non_default_port():
    svcs = {s.name: s for s in w.default_services(9999)}
    assert "9999" in svcs["dashboard"].health_url


# ── Health check ─────────────────────────────────────────────────────────────

def test_unreachable_dashboard_reads_as_unhealthy():
    """Port 9 (discard) refuses connections — the exact failure the OCR source
    was hitting while every process still looked alive."""
    svc = w.Service("dashboard", "dashboard.py", "dashboard.log",
                    health_url="http://localhost:9/api/meta")
    assert svc.healthy(sys.executable) is False


def test_liveness_checked_service_sees_a_running_process(monkeypatch):
    svc = w.Service("engine", "signal_engine.py", "engine.log")
    monkeypatch.setattr(w, "process_running", lambda pattern: pattern == "signal_engine.py")
    assert svc.healthy(sys.executable) is True

    gone = w.Service("engine", "not_running_anywhere.py", "engine.log")
    assert gone.healthy(sys.executable) is False


# ── Tunnel ───────────────────────────────────────────────────────────────────

def test_tunnel_is_matched_by_name_not_by_script_path():
    """cloudflared is not a Python process and exposes no port of ours, so it
    is matched the same way ./trading matches it."""
    svc = w.tunnel_service("/opt/homebrew/bin/cloudflared")
    assert svc.pattern == "cloudflared.*trading-helper"
    assert svc.health_url is None
    assert svc.command("ignored-python")[0] == "/opt/homebrew/bin/cloudflared"
    assert "run" in svc.command("ignored-python")


def test_tunnel_is_skipped_when_cloudflared_is_absent():
    names = [s.name for s in w.enabled_services(
        {"discord_ocr_enabled": True}, 8888, cloudflared=None)]
    assert "tunnel" not in names


def test_tunnel_supervised_only_while_the_want_marker_exists(tmp_path):
    """'./trading start local' and './trading tunnel stop' both mean "no tunnel".
    A supervisor that cannot tell that from a crash would undo both."""
    marker = tmp_path / ".want"
    svc = w.tunnel_service("/opt/homebrew/bin/cloudflared")
    svc.want_file = marker

    assert svc.wanted() is False
    marker.touch()
    assert svc.wanted() is True
    marker.unlink()
    assert svc.wanted() is False


def test_python_services_are_always_wanted():
    """Only the tunnel is optional; the rest have no marker and must never be
    skipped because a file is missing."""
    for svc in w.default_services(8888):
        assert svc.wanted() is True


def test_python_service_command_uses_the_interpreter():
    svc = w.Service("dashboard", "dashboard.py", "dashboard.log")
    assert svc.command("/x/python") == ["/x/python", "-u", "dashboard.py"]
