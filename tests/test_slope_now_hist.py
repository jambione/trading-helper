"""slope_now_hist.py: pitch from completed lines, empty-square events with controls, bands, both-halves shortlist."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import slope_now_hist as Hh  # noqa: E402


def test_pitches_use_completed_minutes():
    fast = {600: -40.0, 601: -30.0, 602: -26.0}
    slow = {600: -34.0, 601: -32.0, 602: -31.5}
    assert Hh.pitches(fast, slow, 602) == (4.0, 0.5, 2.0)
    assert Hh.pitches(fast, slow, 600) == (None, None, None)


def test_events_fire_on_an_empty_square_with_controls():
    fast = {m: -60.0 for m in range(580, 700)}
    slow = {m: -40.0 for m in range(580, 700)}
    for m in range(610, 700):
        fast[m], slow[m] = -30.0 + (m - 610) * 0.0, -28.0
    fast[610], fast[609], slow[609], slow[608] = -30.0, -45.0, -36.0, -38.0
    sip = {m: (100.0 + 0.01 * m, 100.0 + 0.01 * m) for m in range(580, 760)}
    sips = {"AAA": sip, "BBB": {m: (50.0, 50.0) for m in range(580, 760)}}
    rows = Hh.events("2026-06-01", "AAA", fast, slow, sip, sips, ["AAA", "BBB"], {})
    assert [r["t"] for r in rows] == [610]
    r = rows[0]
    assert r["fp"] == 15.0 and r["sp"] == 8.0 and r["spp"] == 2.0
    assert r["gross"] > 0 and abs(r["lift_x"] - r["gross"]) < 1e-9


def test_features_and_gate_rule():
    f = Hh.features({"fp": 3.0, "sp": 2.5, "spp": 1.0, "fast": -30.0})
    assert f["gate_rule"] == "pass" and f["slow_pitch"] == "[+2,+5)" and f["fast_level"] == "[-35,-25)"
    assert Hh.features({"fp": 3.0, "sp": 1.0, "spp": 2.0, "fast": -30.0})["gate_rule"] == "refuse"
    assert Hh.features({"fp": None, "sp": 1.0, "spp": 2.0, "fast": -30.0})["gate_rule"] == "missing"


def _rows(day, n, sp, lift):
    return [{"day": day, "sym": f"S{i}", "t": 600, "fp": 2.0, "sp": sp, "spp": 0.0, "fast": -30.0, "slow": -30.0,
             "gross": lift, "lift_d": lift, "lift_x": lift} for i in range(n)]


def test_shortlist_needs_the_same_sign_in_both_halves():
    rows = []
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        rows += _rows(d, 12, 3.0, -10.0) + _rows(d, 12, 1.0, 5.0)
    for d in ("2026-08-03", "2026-08-04", "2026-08-05"):
        rows += _rows(d, 12, 3.0, -8.0) + _rows(d, 12, 1.0, -4.0)
    short = Hh.consistent(Hh.tables(rows))
    bands = {(s["feature"], s["band"]): s["sign"] for s in short}
    assert bands.get(("slow_pitch", "[+2,+5)")) == "-"
    assert ("slow_pitch", "[+0,+2)") not in bands
