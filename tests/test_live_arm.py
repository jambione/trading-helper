"""Live arming — two factors, separate keys, and the account assertion.

This is the gate between paper and real money. The rail it replaces was
`mode="paper"` hard-coded in ai_trading.init_for_ai under "HARD RULE: AI desk
path never places live orders through this module", so the replacement has to
be at least as hard to get past by accident.

Every test here is about a way live could engage when it should not, or a way
the desk could end up trading an account nobody named.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import live_arm


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """Everything satisfied; each test then removes exactly one factor."""
    arm = tmp_path / "live_armed.json"
    arm.write_text(json.dumps({"account_number": "LIVE123",
                               "armed_by": "jmb", "armed_at": "2026-09-18"}))
    monkeypatch.setenv(live_arm.LIVE_KEY_ENV, "k")
    monkeypatch.setenv(live_arm.LIVE_SECRET_ENV, "s")
    monkeypatch.setattr(live_arm, "host_allows", lambda cfg=None: (True, ""))
    return {"cfg": {"ai_live_trading_enabled": True}, "path": arm}


def _state(armed, **cfg_over):
    cfg = {**armed["cfg"], **cfg_over}
    return live_arm.arm_state(cfg, arm_path=armed["path"])


# ── Both factors are required ─────────────────────────────────────────────────

def test_both_factors_present_arms(armed):
    st = _state(armed)
    assert st["armed"] is True
    assert st["account_number"] == "LIVE123"
    assert st["reasons"] == []


def test_config_flag_alone_does_not_arm(armed, tmp_path):
    """A config push onto a machine nobody armed by hand must do nothing."""
    missing = tmp_path / "absent.json"
    st = live_arm.arm_state(armed["cfg"], arm_path=missing)
    assert st["armed"] is False
    assert st["factors"]["config"] is True
    assert st["factors"]["arm_file"] is False


def test_arm_file_alone_does_not_arm(armed):
    """A stray file cannot arm a desk whose tracked config says paper."""
    st = _state(armed, ai_live_trading_enabled=False)
    assert st["armed"] is False
    assert st["factors"]["arm_file"] is True
    assert st["factors"]["config"] is False


def test_default_config_is_disarmed():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_live_trading_enabled"] is False


def test_the_flag_cannot_be_set_over_http():
    """Refused loudly rather than ignored silently — the attempt is logged."""
    from config import PROTECTED_CONFIG_KEYS, SAFE_CONFIG_KEYS
    assert "ai_live_trading_enabled" in PROTECTED_CONFIG_KEYS
    assert "ai_live_trading_enabled" in SAFE_CONFIG_KEYS


# ── The arming file has to actually name an account ───────────────────────────

def test_a_file_without_an_account_number_does_not_arm(armed):
    armed["path"].write_text(json.dumps({"armed_by": "jmb"}))
    st = _state(armed)
    assert st["armed"] is False
    assert any("account_number" in r for r in st["reasons"])


def test_malformed_arming_file_does_not_arm(armed):
    armed["path"].write_text("{not json")
    assert _state(armed)["armed"] is False


def test_empty_arming_file_does_not_arm(armed):
    armed["path"].write_text("{}")
    assert _state(armed)["armed"] is False


# ── Live keys are separate and never fall back ────────────────────────────────

def test_paper_keys_cannot_arm_live(armed, monkeypatch):
    """A missing live key must disarm, not quietly reuse the paper pair.

    One env file that can address either account is how you trade the wrong
    one by accident.
    """
    monkeypatch.delenv(live_arm.LIVE_KEY_ENV, raising=False)
    monkeypatch.delenv(live_arm.LIVE_SECRET_ENV, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")

    st = _state(armed)
    assert st["armed"] is False
    assert st["factors"]["live_keys"] is False
    assert live_arm.live_keys() == ("", "")


def test_half_a_key_pair_does_not_arm(armed, monkeypatch):
    monkeypatch.delenv(live_arm.LIVE_SECRET_ENV, raising=False)
    assert _state(armed)["armed"] is False


# ── Host lock ─────────────────────────────────────────────────────────────────

def test_wrong_host_cannot_arm(armed, monkeypatch):
    """Risk caps are per-instance and liquidate_all closes the whole account."""
    monkeypatch.setattr(live_arm, "host_allows",
                        lambda cfg=None: (False, "host 'laptop' is not …"))
    st = _state(armed)
    assert st["armed"] is False
    assert st["factors"]["host"] is False


def test_empty_trading_host_means_no_restriction(monkeypatch):
    monkeypatch.setattr(live_arm.socket, "gethostname", lambda: "anything")
    assert live_arm.host_allows({"ai_trading_host": ""}) == (True, "")


def test_host_matches_on_the_short_name(monkeypatch):
    """ssh and GUI sessions disagree about the .local suffix."""
    monkeypatch.setattr(live_arm.socket, "gethostname", lambda: "Jonathans-Mac-mini")
    ok, _ = live_arm.host_allows({"ai_trading_host": "Jonathans-Mac-mini.local"})
    assert ok is True


def test_a_different_host_is_refused(monkeypatch):
    monkeypatch.setattr(live_arm.socket, "gethostname", lambda: "Jonathans-MacBook-2.local")
    ok, why = live_arm.host_allows({"ai_trading_host": "Jonathans-Mac-mini.local"})
    assert ok is False and "not ai_trading_host" in why


# ── The account assertion ─────────────────────────────────────────────────────

def test_matching_account_passes():
    assert live_arm.verify_account("LIVE123", "LIVE123") == (True, "")


def test_case_and_whitespace_do_not_break_the_match():
    ok, _ = live_arm.verify_account("  live123 ", "LIVE123")
    assert ok is True


def test_a_different_account_is_refused():
    """The wrong-key-pair case: connects fine, wrong account entirely."""
    ok, why = live_arm.verify_account("PAPER999", "LIVE123")
    assert ok is False
    assert "PAPER999" in why and "LIVE123" in why


def test_an_unidentifiable_account_is_refused():
    """An account we cannot name is not the account we armed."""
    ok, why = live_arm.verify_account("", "LIVE123")
    assert ok is False and "did not report" in why


def test_no_expected_account_is_refused():
    ok, why = live_arm.verify_account("LIVE123", "")
    assert ok is False and "no armed account_number" in why


# ── The desk wiring ───────────────────────────────────────────────────────────

def test_is_ready_accepts_live_but_never_off():
    import ai_trading as gt

    for mode, ready, want in (("paper", True, True), ("live", True, True),
                              ("off", True, False), ("live", False, False),
                              ("paper", False, False)):
        gt._mode, gt._ready = mode, ready
        assert gt.is_ready() is want, f"mode={mode} ready={ready}"
    gt._mode, gt._ready = "off", False


def test_the_old_hard_coded_paper_rail_is_gone_and_replaced():
    """Guards the replacement, not the removal.

    If someone deletes the live_arm call, this goes red rather than quietly
    reverting the desk to a rail that no longer exists.
    """
    src = (_ROOT / "ai_trading.py").read_text(encoding="utf-8")
    assert 'mode="paper",' not in src, "paper is hard-coded again"
    assert "import live_arm" in src
    assert "verify_account" in src
    assert "alpaca_trader.shutdown()" in src, "a failed assertion must disconnect"


def test_a_failed_assertion_leaves_no_live_client():
    """Disarming a flag is not enough; the connection has to be dropped."""
    import alpaca_trader

    alpaca_trader._mode = "live"
    alpaca_trader._client = object()
    alpaca_trader._account_number = "WRONG"
    alpaca_trader.shutdown()
    assert alpaca_trader.is_active() is False
    assert alpaca_trader._client is None
    assert alpaca_trader.account_number() == ""


def test_describe_never_leaks_a_key(armed, monkeypatch):
    monkeypatch.setenv(live_arm.LIVE_KEY_ENV, "SUPERSECRETKEY")
    monkeypatch.setenv(live_arm.LIVE_SECRET_ENV, "SUPERSECRETSECRET")
    text = live_arm.describe(armed["cfg"])
    assert "SUPERSECRET" not in text
