import os
import sys
import json
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tools.studies.entry_geometry_audit import run_audit

def test_entry_geometry_audit_runs_on_synthetic_data():
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        records = [
            {"symbol": "AAPL", "features": {"pctr": -85.0}, "net_return_bp": -15.0, "peak_gain_bp": 2.0},
            {"symbol": "MSFT", "features": {"pctr": -60.0}, "net_return_bp": -30.0, "peak_gain_bp": 3.0},
            {"symbol": "NVDA", "features": {"pctr": -40.0}, "net_return_bp": 10.0, "peak_gain_bp": 25.0},
            {"symbol": "AMZN", "features": {"pctr": -20.0}, "net_return_bp": 5.0, "peak_gain_bp": 12.0},
            {"symbol": "GOOG", "features": {"pctr": -5.0}, "net_return_bp": -25.0, "peak_gain_bp": 1.0},
        ]
        for r in records:
            tf.write(json.dumps(r) + "\n")
        tf_name = tf.name

    try:
        res = run_audit(tf_name)
        assert res is not None
        assert "Deep Oversold" in res
        assert "Terminal Apex" in res
    finally:
        if os.path.exists(tf_name):
            os.remove(tf_name)
