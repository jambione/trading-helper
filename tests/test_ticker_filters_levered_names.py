"""PTIR 2026-10-08: a 2x fund passed the ticker-only levered check on the movers path."""
import json

import ticker_filters as tf


def test_ptir_is_on_the_denylist():
    assert tf.is_levered_etp("PTIR") and tf.is_levered_etp("PLTU") and not tf.is_levered_etp("PLTR")


def test_cached_issuer_name_catches_a_levered_fund_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(tf, "ASSET_NAMES_FILE", tmp_path / "asset_names.json")
    monkeypatch.setattr(tf, "_NAMES", {"mtime": None, "names": {}})
    assert tf.is_levered_etp("ZZZX") is False                       # no file: ticker check only
    tf.save_asset_names({"ZZZX": "Acme ETF Trust Acme 2x Long FOO Daily ETF", "BULL": "Webull Corp"})
    assert tf.is_levered_etp("ZZZX") is True
    assert tf.is_levered_etp("BULL") is False                      # bare BULL is a stock
    assert json.loads((tmp_path / "asset_names.json").read_text())["names"]["BULL"] == "Webull Corp"


def test_a_broken_names_file_never_raises(tmp_path, monkeypatch):
    p = tmp_path / "asset_names.json"
    p.write_text("{not json")
    monkeypatch.setattr(tf, "ASSET_NAMES_FILE", p)
    monkeypatch.setattr(tf, "_NAMES", {"mtime": None, "names": {}})
    assert tf.is_levered_etp("ZZZX") is False
