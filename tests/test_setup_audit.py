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
    monkeypatch.setattr(sa.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "12345\n"})())
    sa.audit_freshness()
    assert sa.CRITICAL, "unmeasurable process age must be CRITICAL"
