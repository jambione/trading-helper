"""slope_now_run fidelity statistics and slope_now_explore's join, bands and sealed confirmation window."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import slope_now_explore as X  # noqa: E402
import slope_now_run as R  # noqa: E402

T0 = 1_793_000_000.0


def _p(fp, sp, spp):
    return {"pctr_fast_pitch": fp, "pctr_slow_pitch": sp, "pctr_slow_pitch_prev": spp}


def test_fidelity_passes_when_replay_matches_live():
    pairs = [({"ts": T0 + i, "pctr_pitch_ts": T0 + i - 1, **_p(3.0, 1.0 + i % 2, 1.0)}, _p(3.2, 1.0 + i % 2, 1.0))
             for i in range(20)] + [({"ts": T0, "pctr_pitch_ts": T0 - 1, **_p(-1.0, 1.0, 2.0)}, _p(-1.0, 1.0, 2.0))]
    out = R.fidelity_stats(pairs)
    assert out["pass"], out


def test_fidelity_fails_on_presence_gap_and_sign_disagreement():
    pairs = [({"ts": T0, "pctr_pitch_ts": T0, **_p(None, None, None)}, _p(3.0, 1.0, 1.0)) for _ in range(10)]
    assert R.fidelity_stats(pairs)["pass"] is False
    pairs = [({"ts": T0, "pctr_pitch_ts": T0, **_p(3.0, 1.0, 1.0)}, _p(-3.0, -1.0, 1.0)) for _ in range(10)]
    out = R.fidelity_stats(pairs)
    assert out["sign_agree"]["fast_gt0"]["share"] == 0.0 and out["pass"] is False


def test_match_live_uses_nearest_poll_within_5s():
    live = {"AAA": [(T0 - 20, _p(1, 1, 1)), (T0 + 3, _p(2, 2, 2))]}
    pairs = R.match_live([{"symbol": "aaa", "ts": T0}, {"symbol": "AAA", "ts": T0 + 60}], live)
    assert len(pairs) == 1 and pairs[0][1]["pctr_fast_pitch"] == 2


def test_join_takes_last_arm_ok_before_entry_and_drops_stale_pitch(tmp_path):
    p = tmp_path / "arms.jsonl.gz"
    with gzip.open(p, "wt") as fh:
        for r in ({"ts": T0 - 50, "symbol": "AAA", "arm_ok": True, "pctr_pitch_ts": T0 - 51, **_p(1.0, 1.0, 1.0)},
                  {"ts": T0 - 10, "symbol": "AAA", "arm_ok": True, "pctr_pitch_ts": T0 - 11, **_p(5.0, 3.0, 2.0)},
                  {"ts": T0 - 5, "symbol": "AAA", "arm_ok": False, **_p(9.0, 9.0, 9.0)},
                  {"ts": T0 - 10, "symbol": "BBB", "arm_ok": True, "pctr_pitch_ts": T0 - 40, **_p(5.0, 3.0, 2.0)}):
            fh.write(json.dumps(r) + "\n")
    rows = X.join([{"symbol": "AAA", "entry_ts": T0, "net": 1.0}, {"symbol": "BBB", "entry_ts": T0, "net": 1.0},
                   {"symbol": "CCC", "entry_ts": T0, "net": 1.0}], X.arms(p))
    assert rows[0]["pctr_fast_pitch"] == 5.0 and rows[0]["joined"]
    assert rows[1]["joined"] and rows[1]["pctr_slow_pitch"] is None          # stale pitch = missing
    assert not rows[2]["joined"]
    assert X.features(rows[0])["gate_rule"] == "pass" and X.features(rows[1])["gate_rule"] == "missing"


def test_explore_window_never_reads_confirmation_days(tmp_path):
    for d in ("2026-09-26", "2026-09-28", "2026-10-13", "2026-10-14", "2026-10-27"):
        (tmp_path / f"{d}-REF.json").write_text("{}")
    assert X.explore_days(tmp_path) == ["2026-09-28", "2026-10-13"]


def test_tables_group_by_band():
    rows = [{"day": f"d{i % 3}", "net": float(i), **_p(3.0, 3.0 if i % 2 else -1.0, 1.0), "pctr": -30.0}
            for i in range(12)]
    t = X.tables(rows)
    assert t["all"]["n"] == 12 and set(t["slow_pitch"]) == {"[-2,+0)", "[+2,+5)"}


def test_explore_join_adds_the_range_feature(tmp_path):
    import datetime
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    t = datetime.datetime(2026, 10, 1, 11, 26, 10, tzinfo=et).timestamp()
    p = tmp_path / "arms.jsonl.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(json.dumps({"ts": t - 5, "symbol": "FE", "arm_ok": True, "pctr_pitch_ts": t - 6, "pctr": -33.2,
                             **_p(1.6, 0.5, -2.8)}) + "\n")
    bars = {"FE": [(m, 46.8, 46.91 if m == 680 else 46.85, 46.77, 46.8, 100) for m in range(660, 690)]}
    rows = X.join([{"symbol": "FE", "entry_ts": t, "net": -30.0}], X.arms(p), bars)
    f = X.features(rows[0])
    assert rows[0]["rng"] == round((46.91 / 46.77 - 1) * 100, 3)
    assert f["range_pct"] == "[0.25,0.5)" and f["min_pitch"] == "[0,1)"
