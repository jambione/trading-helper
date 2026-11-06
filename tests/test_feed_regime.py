"""feed_regime: IEX and SIP samples are never pooled; the read happens once on the first sample to reach the minimum."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import feed_regime as fr  # noqa: E402


def _rows(n, t0=1_000_000.0):
    return [{"t": t0 + i} for i in range(n)]


def test_no_flip_means_everything_is_iex():
    s = fr.split(_rows(10), sip_from=None)
    assert len(s["IEX"]) == 10 and s["SIP"] == []


def test_flip_splits_by_entry_time_and_never_pools():
    rows = _rows(100)
    s = fr.split(rows, sip_from=1_000_040.0)
    assert len(s["IEX"]) == 40 and len(s["SIP"]) == 60


def test_pick_reads_the_first_sample_to_reach_the_minimum():
    rows = _rows(200)
    lab, sample, info = fr.pick(rows, 150, sip_from=1_000_040.0)      # IEX 40, SIP 160
    assert lab == "SIP" and len(sample) == 160 and "never pooled" in info[0]
    lab, sample, _ = fr.pick(_rows(200), 150, sip_from=None)         # no flip: IEX reaches 150
    assert lab == "IEX" and len(sample) == 200
    lab, sample, _ = fr.pick(_rows(100), 150, sip_from=1_000_040.0)  # nobody there yet
    assert lab is None and sample is None


def test_flip_time_file_parsing(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"sip_from_et": None}))
    assert fr.sip_from_ts(str(p)) is None
    p.write_text(json.dumps({"sip_from_et": "2026-11-10T09:30:00"}))
    t = fr.sip_from_ts(str(p))
    assert t and abs(t - 1_794_321_000.0) < 86400 * 30
    p.write_text("not json")
    assert fr.sip_from_ts(str(p)) is None
