"""Stage attribution in the historical-universe trace. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import hist_universe_trace as T  # noqa: E402


def _t():
    return {"recorded_ts": 1.0, "status": "missing", "recorded": {"ts": 1.0, "pct_change": 3.0, "price": 50.0}}


def test_stages():
    targets = {s: _t() for s in ("NONE", "LIST", "DISC", "SEED", "DOOR", "OK")}
    ev = {
        "LIST": [(10, "actives", None)],
        "DISC": [(10, "actives", None), (10, "measure:discard", {"pct": 0.8})],
        "SEED": [(10, "scan", None), (11, "measure:row", {}), (11, "seed:dropped", None)],
        "DOOR": [(10, "gainers", None), (11, "measure:row", {}), (11, "seed:kept", None), (11, "door:spread_wide", None)],
        "OK": [(10, "gainers", None), (11, "measure:row", {}), (11, "seed:kept", None), (11, "door:pass", None)],
    }
    s = T.summarize("2026-10-09", targets, ev)
    w = {k: v["where"].split(" (")[0] for k, v in s["names"].items()}
    assert w["NONE"].startswith("never a candidate")
    assert w["LIST"] == "listed but never measured"
    assert w["DISC"] == "measured, never a row"
    assert w["SEED"] == "row, never seeded"
    assert w["DOOR"] == "seeded, refused at the door"
    assert w["OK"] == "admitted"
    assert sum(s["stages"].values()) == 6


def test_tracer_only_notes_targets():
    tr = T.Tracer({"AAA"})
    tr.T = 5
    tr.note("AAA", "gainers")
    tr.note("BBB", "gainers")
    assert dict(tr.ev) == {"AAA": [(5, "gainers", None)]}
