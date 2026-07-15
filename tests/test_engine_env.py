"""
test_engine_env.py — dashboard-driven signal_engine.env editing.

engine_env.py is the only writer of signal_engine.env besides the user's
editor, and it feeds a system that trades money — so the rules are strict:
whitelist only, validated values, TRADER_MODE=live always rejected, comments
and unrelated lines preserved.

Run:
    venv/bin/python -m pytest tests/test_engine_env.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine_env  # noqa: E402


SAMPLE = """\
# signal_engine.env — test sample
DASHBOARD_URL=https://example.com

# Trader mode
TRADER_MODE=paper

# Risk management settings (%)
STOP_LOSS=1.0
TAKE_PROFIT=2.0
# MAX_HOLD_MINUTES=15
"""


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / "signal_engine.env"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


# ── Reading ───────────────────────────────────────────────────────────────────

def test_read_active_values(env_file):
    vals = engine_env.read_engine_env(env_file)
    assert vals["TRADER_MODE"] == "paper"
    assert vals["STOP_LOSS"] == "1.0"
    assert vals["MAX_HOLD_MINUTES"] is None        # commented = unset
    assert "DASHBOARD_URL" not in vals             # not whitelisted — never exposed


def test_read_strips_inline_comments(tmp_path):
    p = tmp_path / "e.env"
    p.write_text("TAKE_PROFIT=2.5  # scalp target\n")
    assert engine_env.read_engine_env(p)["TAKE_PROFIT"] == "2.5"


# ── Validation ────────────────────────────────────────────────────────────────

def test_live_mode_rejected(env_file):
    with pytest.raises(ValueError, match="live"):
        engine_env.update_engine_env({"TRADER_MODE": "live"}, env_file)
    # nothing written
    assert engine_env.read_engine_env(env_file)["TRADER_MODE"] == "paper"


def test_non_whitelisted_key_rejected(env_file):
    with pytest.raises(ValueError, match="not an editable"):
        engine_env.update_engine_env({"ALPACA_SECRET_KEY": "x"}, env_file)


def test_invalid_number_rejected(env_file):
    with pytest.raises(ValueError, match="STOP_LOSS"):
        engine_env.update_engine_env({"STOP_LOSS": "abc"}, env_file)
    with pytest.raises(ValueError, match="TRADE_AMOUNT"):
        engine_env.update_engine_env({"TRADE_AMOUNT": "-5"}, env_file)


def test_ticker_list_normalised(env_file):
    engine_env.update_engine_env({"COMPARE_TICKERS": " aapl, spy "}, env_file)
    assert engine_env.read_engine_env(env_file)["COMPARE_TICKERS"] == "AAPL,SPY"
    with pytest.raises(ValueError, match="invalid ticker"):
        engine_env.update_engine_env({"COMPARE_TICKERS": "TOOLONG1"}, env_file)


def test_enum_validation(env_file):
    with pytest.raises(ValueError):
        engine_env.update_engine_env({"PDT_PROTECT": "maybe"}, env_file)
    with pytest.raises(ValueError):
        engine_env.update_engine_env({"STRATEGY_MODE": "yolo"}, env_file)
    engine_env.update_engine_env({"PDT_PROTECT": "BLOCK"}, env_file)   # case-folded
    assert engine_env.read_engine_env(env_file)["PDT_PROTECT"] == "block"


# ── Writing ───────────────────────────────────────────────────────────────────

def test_update_existing_key_in_place(env_file):
    engine_env.update_engine_env({"TAKE_PROFIT": "1.5"}, env_file)
    # utf-8 explicitly: engine_env reads and writes utf-8, but read_text()
    # follows the locale, which is cp1252 on Windows -- so the em-dash below
    # came back mojibake and this passed only on the mac side of the branch.
    text = env_file.read_text(encoding="utf-8")
    assert "TAKE_PROFIT=1.5" in text
    assert text.count("TAKE_PROFIT=") == 1
    # comments and unrelated lines preserved
    assert "# signal_engine.env — test sample" in text
    assert "DASHBOARD_URL=https://example.com" in text


def test_new_key_appended_to_managed_section(env_file):
    engine_env.update_engine_env({"DAILY_LOSS_LIMIT": "75"}, env_file)
    text = env_file.read_text(encoding="utf-8")
    assert engine_env._MANAGED_HEADER in text
    assert "DAILY_LOSS_LIMIT=75" in text
    # commented placeholder for a DIFFERENT key untouched
    assert "# MAX_HOLD_MINUTES=15" in text


def test_engine_would_load_updated_value(env_file, monkeypatch):
    """End-to-end: the engine's env loader sees what the dashboard wrote."""
    # Import FIRST — module import loads the real env into os.environ; the
    # delenv below must come after so our tmp file values actually land.
    import signal_engine as se

    engine_env.update_engine_env({"TAKE_PROFIT": "1.8", "TRADER_MODE": "off"}, env_file)
    monkeypatch.delenv("TAKE_PROFIT", raising=False)
    monkeypatch.delenv("TRADER_MODE", raising=False)

    se._load_env_file(env_file)
    assert os.environ["TAKE_PROFIT"] == "1.8"
    assert os.environ["TRADER_MODE"] == "off"
    monkeypatch.delenv("TAKE_PROFIT", raising=False)
    monkeypatch.delenv("TRADER_MODE", raising=False)
