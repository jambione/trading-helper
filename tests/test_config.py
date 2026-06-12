"""
test_config.py — offline tests for config.py merge precedence and
save_config secret-separation logic.

Run:
    venv/bin/python -m pytest tests/test_config.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg_mod


@pytest.fixture()
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE",  tmp_path / "bot_config.json")
    monkeypatch.setattr(cfg_mod, "SECRETS_FILE", tmp_path / "secrets.json")
    return tmp_path


def test_defaults_when_no_files(tmp_cfg):
    cfg = cfg_mod.load_config()
    assert cfg["bar_timeframe"] == "1Min"
    assert cfg["bar_count"] == 300


def test_bot_config_overrides_defaults(tmp_cfg):
    (tmp_cfg / "bot_config.json").write_text(json.dumps({"bar_count": 42}))
    cfg = cfg_mod.load_config()
    assert cfg["bar_count"] == 42
    assert cfg["bar_timeframe"] == "1Min"   # other defaults unchanged


def test_secrets_override_bot_config(tmp_cfg):
    (tmp_cfg / "bot_config.json").write_text(json.dumps({"api_key": "from-config"}))
    (tmp_cfg / "secrets.json").write_text(json.dumps({"api_key": "from-secrets"}))
    cfg = cfg_mod.load_config()
    assert cfg["api_key"] == "from-secrets"


def test_missing_secrets_falls_back_to_bot_config_value(tmp_cfg):
    (tmp_cfg / "bot_config.json").write_text(json.dumps({"api_key": "config-key"}))
    cfg = cfg_mod.load_config()
    assert cfg["api_key"] == "config-key"


def test_save_config_puts_secrets_only_in_secrets_file(tmp_cfg):
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg["bar_count"] = 99
    cfg["api_key"]   = "test-alpaca-key"
    cfg_mod.save_config(cfg)

    secrets = json.loads((tmp_cfg / "secrets.json").read_text())
    assert secrets["api_key"] == "test-alpaca-key"

    regular = json.loads((tmp_cfg / "bot_config.json").read_text())
    assert "api_key" not in regular
    assert regular["bar_count"] == 99


def test_save_config_skips_secrets_file_when_all_keys_blank(tmp_cfg):
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg["api_key"] = cfg["secret_key"] = cfg["finnhub_key"] = ""
    cfg_mod.save_config(cfg)
    assert not (tmp_cfg / "secrets.json").exists()


def test_corrupted_bot_config_falls_back_to_defaults(tmp_cfg):
    (tmp_cfg / "bot_config.json").write_text("NOT VALID JSON {{{")
    cfg = cfg_mod.load_config()
    assert cfg["bar_count"] == cfg_mod.DEFAULT_CONFIG["bar_count"]


def test_corrupted_secrets_does_not_crash_and_loads_regular_config(tmp_cfg):
    (tmp_cfg / "bot_config.json").write_text(json.dumps({"bar_count": 7}))
    (tmp_cfg / "secrets.json").write_text("NOT VALID JSON {{{")
    cfg = cfg_mod.load_config()
    assert cfg["bar_count"] == 7
