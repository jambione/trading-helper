"""Credential precedence: shell > config/secrets.json > signal_engine.env.

secrets.json is the desk's single source of truth for credentials. Six modules
read them through cfg["api_key"], fourteen through os.getenv("ALPACA_API_KEY").
load_desk_env() is what keeps those two halves agreeing — before it existed the
two stores could drift silently, and a stale key in one file changed behaviour
for half the desk with no error anywhere.
"""
import json

import pytest

import desk_core


@pytest.fixture()
def files(tmp_path):
    env = tmp_path / "signal_engine.env"
    sec = tmp_path / "secrets.json"
    env.write_text(
        "ALPACA_API_KEY=env_api\n"
        "ALPACA_SECRET_KEY=env_secret\n"
        "FINNHUB_API_KEY=env_finnhub\n"
        "TRADER_MODE=paper\n"
    )
    sec.write_text(json.dumps({
        "api_key": "secrets_api",
        "secret_key": "secrets_secret",
        "finnhub_key": "secrets_finnhub",
    }))
    return env, sec


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FINNHUB_API_KEY",
              "TRADER_MODE"):
        monkeypatch.delenv(k, raising=False)


def test_secrets_beats_env_file(files, monkeypatch):
    """The whole point: signal_engine.env must not shadow secrets.json."""
    env, sec = files
    desk_core.load_desk_env(env, sec)
    assert monkeypatch  # fixture ordering
    import os
    assert os.environ["ALPACA_API_KEY"] == "secrets_api"
    assert os.environ["ALPACA_SECRET_KEY"] == "secrets_secret"
    assert os.environ["FINNHUB_API_KEY"] == "secrets_finnhub"


def test_non_credential_env_values_untouched(files):
    """Only the three credentials are overlaid; engine settings still load."""
    import os
    env, sec = files
    desk_core.load_desk_env(env, sec)
    assert os.environ["TRADER_MODE"] == "paper"


def test_real_shell_value_wins_over_secrets(files, monkeypatch):
    """`ALPACA_API_KEY=... ./trading start` must still override the files."""
    import os
    env, sec = files
    monkeypatch.setenv("ALPACA_API_KEY", "shell_api")
    desk_core.load_desk_env(env, sec)
    assert os.environ["ALPACA_API_KEY"] == "shell_api"
    # A key the shell did NOT set still comes from secrets.
    assert os.environ["ALPACA_SECRET_KEY"] == "secrets_secret"


def test_returns_keys_for_execv_restart(files):
    """signal_engine / ai_trader pop these before re-exec, so overlaid
    credentials must be in the list or a restart inherits stale values."""
    env, sec = files
    keys = desk_core.load_desk_env(env, sec)
    assert "TRADER_MODE" in keys
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FINNHUB_API_KEY"):
        assert k in keys
    assert len(keys) == len(set(keys)), "no duplicates"


def test_missing_secrets_falls_back_to_env_file(files):
    """A desk with no secrets.json still boots on the env file."""
    import os
    env, _ = files
    desk_core.load_desk_env(env, "/nonexistent/secrets.json")
    assert os.environ["ALPACA_API_KEY"] == "env_api"


def test_malformed_secrets_does_not_raise(files, tmp_path):
    """Never take the desk down over an unparseable secrets file."""
    import os
    env, _ = files
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    desk_core.load_desk_env(env, bad)
    assert os.environ["ALPACA_API_KEY"] == "env_api"


def test_blank_secret_does_not_clobber_env_file(files, tmp_path):
    """An empty string in secrets.json is 'unset', not 'use empty creds'."""
    import os
    env, _ = files
    sec = tmp_path / "blank.json"
    sec.write_text(json.dumps({"api_key": "", "secret_key": "   "}))
    desk_core.load_desk_env(env, sec)
    assert os.environ["ALPACA_API_KEY"] == "env_api"
    assert os.environ["ALPACA_SECRET_KEY"] == "env_secret"


def test_live_secrets_file_feeds_both_read_paths():
    """The two halves of the desk resolve the same credentials.

    Guards the drift this change exists to prevent: config readers go through
    load_config(), env readers through os.getenv, and they must agree.
    """
    import os
    import config
    desk_core.load_desk_env()
    cfg = config.load_config()
    for cfg_key, env_key in desk_core.CREDENTIAL_ENV.items():
        if cfg.get(cfg_key):
            assert os.getenv(env_key) == cfg[cfg_key], (
                f"{env_key} disagrees with cfg[{cfg_key!r}]")
