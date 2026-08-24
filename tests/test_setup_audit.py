"""The audit's job is to fail loudly; a silent audit is worse than none.

Every wrong conclusion in 2026-08 came from plumbing that produced a
plausible number instead of an error. So the checks that carry a CRITICAL
are pinned here.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

sa = pytest.importorskip("setup_audit")


def test_exit_knobs_are_fingerprinted():
    """A min-hold session must not stamp the same regime as a scalp one.

    ai_exit_min_hold_sec moves max loss per position from ~0.10R to 1R. If
    it is not in the fingerprint, the forward test's rows are pooled with
    the rows it is meant to be compared against.
    """
    import learn_stamps
    fp = set(learn_stamps._FINGERPRINT_KEYS)
    for k in ("ai_exit_min_hold_sec",
              "ai_local_trail_give_spread_k",
              "ai_local_trail_give_spread_max_r",
              "ai_local_trail_be_at_spread_k",
              "ai_watch_open_seed_min_pct"):
        assert k in fp, f"{k} changes what the desk banks and must be stamped"


def test_regime_patterns_catch_the_exit_knobs():
    import re
    for k in ("ai_exit_min_hold_sec", "ai_local_trail_give_r",
              "ai_watch_open_seed_min_pct", "desk_product"):
        assert any(re.search(p, k) for p in sa.REGIME_PATTERNS), k


def test_a_regime_knob_outside_the_fingerprint_is_critical(monkeypatch):
    """The check must actually raise, not just print."""
    monkeypatch.setattr(sa, "CRITICAL", [])
    monkeypatch.setattr(sa, "WARN", [])
    import learn_stamps
    monkeypatch.setattr(learn_stamps, "_FINGERPRINT_KEYS", ())
    sa.audit_fingerprint({"ai_exit_min_hold_sec": 900})
    assert sa.CRITICAL, "an unfingerprinted regime knob must be CRITICAL"


def test_clean_fingerprint_raises_nothing(monkeypatch):
    monkeypatch.setattr(sa, "CRITICAL", [])
    monkeypatch.setattr(sa, "WARN", [])
    import learn_stamps
    sa.audit_fingerprint({})
    assert not sa.CRITICAL, sa.CRITICAL


def test_exempt_list_is_narrow():
    """Exemptions are how an audit quietly stops auditing."""
    assert len(sa.FINGERPRINT_EXEMPT) <= 8
    for k in sa.FINGERPRINT_EXEMPT:
        assert "give" not in k and "min_hold" not in k, (
            f"{k} affects the shelf and must not be exempt")


def test_log_floors_are_set_above_a_coin_flip():
    """A field present half the time cannot support a verdict."""
    for _f, field, floor in sa.LOG_FIELDS:
        assert floor >= 80, f"{field} floor {floor}% is too permissive"


def test_process_start_parses_the_field_that_exists():
    """macOS ps has no etimes. The first version of this check used it,
    got an error string, fell back to 0, and reported every process fresh."""
    import time
    now = time.time()
    out = sa._proc_start_epoch(str(os.getpid()))
    assert out is not None, "could not read this very process's start time"
    assert 0 <= now - out < 86400


def test_unreadable_start_time_is_none_not_a_guess():
    assert sa._proc_start_epoch("0") is None
    assert sa._proc_start_epoch("notapid") is None


def test_unmeasurable_freshness_is_critical(monkeypatch):
    """Cannot-measure must raise, never silently pass."""
    monkeypatch.setattr(sa, "CRITICAL", [])
    monkeypatch.setattr(sa, "WARN", [])
    monkeypatch.setattr(sa, "_proc_start_epoch", lambda pid: None)
    # `ps -A -o pid=,command=` format: the process must be FOUND, so that
    # the unmeasurable-age branch is the one under test. (Was bare pgrep
    # output before process lookup moved off pgrep — see
    # test_process_lookup_does_not_use_pgrep.)
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "12345 /usr/bin/python -u ai_trader.py\n"
                            "12346 /usr/bin/python dashboard.py\n"
                            "12347 /usr/bin/python tools/watchdog.py\n"})())
    sa.audit_freshness()
    assert sa.CRITICAL, "unmeasurable process age must be CRITICAL"


def test_quick_mode_skips_the_expensive_scan(monkeypatch, capsys):
    """The frequent path must not read a 70MB shadow log every 30 minutes."""
    called = {"logs": False}
    monkeypatch.setattr(sa, "audit_logs", lambda d: called.__setitem__("logs", True))
    monkeypatch.setattr(sa, "audit_knobs", lambda live: None)
    monkeypatch.setattr(sa, "audit_fingerprint", lambda live: None)
    monkeypatch.setattr(sa, "audit_freshness", lambda: None)
    monkeypatch.setattr(sys, "argv", ["setup_audit.py", "--quick"])
    sa.main()
    assert called["logs"] is False
    monkeypatch.setattr(sys, "argv", ["setup_audit.py"])
    sa.main()
    assert called["logs"] is True


def test_critical_sets_a_nonzero_exit(monkeypatch):
    """The watchdog gates on the return code, so it has to be real."""
    monkeypatch.setattr(sa, "audit_knobs", lambda live: None)
    monkeypatch.setattr(sa, "audit_fingerprint", lambda live: None)
    monkeypatch.setattr(sa, "audit_freshness",
                        lambda: sa.CRITICAL.append("boom"))
    monkeypatch.setattr(sa, "CRITICAL", [])
    monkeypatch.setattr(sa, "WARN", [])
    monkeypatch.setattr(sys, "argv", ["setup_audit.py", "--quick"])
    assert sa.main() == 1


def test_watchdog_never_blocks_trading_on_the_audit():
    """A false CRITICAL halting the desk is worse than the staleness.

    The audit's own freshness check shipped reporting a false OK; the
    opposite bug must not be able to stop the book from trading.
    """
    src = open(os.path.join(ROOT, "tools", "watchdog.py"), encoding="utf-8").read()
    i = src.index("setup_audit CRITICAL")
    window = src[i - 900:i + 400]
    for stopper in ("sys.exit", "raise SystemExit", "stopping = True", "return 1"):
        assert stopper not in window, (
            f"watchdog must not {stopper} on a failed audit")


def test_watchdog_runs_quick_periodically_and_full_at_eod():
    src = open(os.path.join(ROOT, "tools", "watchdog.py"), encoding="utf-8").read()
    assert '"setup_audit.py", "--quick"' in src
    assert '"setup_audit.py", "--days", "5"' in src
    assert "AUDIT_SETTLE_SEC" in src and "AUDIT_INTERVAL_SEC" in src


# ------------------------------------------------ the watchdog's blind spot

def test_process_lookup_does_not_use_pgrep():
    """BSD pgrep excludes its own ancestors.

    So when the watchdog spawned this audit, `pgrep -f tools/watchdog.py`
    could not see the watchdog — it was the caller's parent. Every
    scheduled run since 2026-08-22 reported "DOWN tools/watchdog.py"
    while a human running the same command by hand saw it healthy, which
    means the watchdog's own code freshness was never actually checked.
    A watchdog running stale code is the exact failure this audit exists
    to catch, and it was the one process structurally invisible to it.
    """
    import inspect
    body = inspect.getsource(sa._pids_matching)
    code = body.split('"""')[-1]          # skip the docstring, which names it
    assert "pgrep" not in code, (
        "pgrep cannot see the caller's ancestors; use ps -A")
    assert '"ps"' in code and '"-A"' in code


def test_a_process_is_found_by_its_command_line(monkeypatch):
    fake = "  101 /usr/bin/python -u tools/watchdog.py\n" \
           "  102 /usr/bin/python ai_trader.py\n"

    class R:
        returncode = 0
        stdout = fake

    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: R())
    assert sa._pids_matching("tools/watchdog.py") == ["101"]
    assert sa._pids_matching("ai_trader.py") == ["102"]
    assert sa._pids_matching("nothing.py") == []


def test_an_unlistable_process_table_is_unknown_not_empty(monkeypatch):
    """None and [] mean different things: cannot check vs checked and absent."""
    def boom(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(sa.subprocess, "run", boom)
    assert sa._pids_matching("anything") is None
