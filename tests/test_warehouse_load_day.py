"""load_day merges SPY/QQQ from the sip_hedge warehouse when a main day file lacks them (11/11 on)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import relative_strength_scan as R  # noqa: E402


def _write(path, syms, base):
    rows = [(i, m) for i in range(len(syms)) for m in range(3)]
    np.savez(path, syms=np.array(syms), sym_idx=np.array([r[0] for r in rows]), minute=np.array([R.M0 + r[1] for r in rows]),
             **{k: np.array([base + i + m for i, m in rows], dtype=float) for k in ("o", "h", "l", "c", "v")})


def test_spy_is_merged_from_sip_hedge(tmp_path):
    (tmp_path / "sip").mkdir(); (tmp_path / "sip_hedge").mkdir()
    _write(tmp_path / "sip" / "2026-11-12.npz", ["AAA", "BBB"], 10.0)
    _write(tmp_path / "sip_hedge" / "2026-11-12.npz", ["QQQ", "SPY"], 500.0)
    d = R.load_day(tmp_path / "sip" / "2026-11-12.npz")
    assert d["syms"] == ["AAA", "BBB", "SPY", "QQQ"]
    assert d["c"][d["idx"]["SPY"], 0] == 501.0 and d["c"][d["idx"]["AAA"], 2] == 12.0


def test_a_file_that_has_spy_is_untouched(tmp_path):
    (tmp_path / "sip").mkdir(); (tmp_path / "sip_hedge").mkdir()
    _write(tmp_path / "sip" / "2026-11-10.npz", ["SPY", "QQQ", "AAA"], 10.0)
    _write(tmp_path / "sip_hedge" / "2026-11-10.npz", ["QQQ", "SPY"], 500.0)
    d = R.load_day(tmp_path / "sip" / "2026-11-10.npz")
    assert d["syms"] == ["SPY", "QQQ", "AAA"] and d["c"][0, 0] == 10.0
