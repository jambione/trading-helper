import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import trade_sim as ts

T0 = 1_800_000_000.0


def path(up_bp=30, down_to=99.0, step=0.01, entry=100.0):
    """1 print/second: climb +up_bp, then fall to `down_to`."""
    px, out, t = entry, [(T0, entry, 1)], T0
    peak = entry * (1 + up_bp / 1e4)
    while px < peak - 1e-9:
        px = round(px + step, 4); t += 1; out.append((t, px, 1))
    while px > down_to + 1e-9:
        px = round(px - step, 4); t += 1; out.append((t, px, 1))
    return out


def cfg(**o):
    c = ts.base_cfg(); c.update(o); return c


def run(over=None, delay=0.0, prints=None):
    return ts.sim_trade("T", 100.0, T0, prints or path(), cfg(**(over or {})), delay)


def test_baseline_exits_and_reports_fields():
    r = run()
    assert r and r["why"] == "local_trail" and r["hold_s"] > 0 and r["t1"] is False


def test_peak_lock_exits_higher_than_baseline_on_a_round_trip():
    b = run()["gross_bp"]
    v = run(ts.VARIANTS["lock50"])["gross_bp"]
    assert v >= b - 1e-9
    assert v >= 10.0          # half of a +30 bp peak, less a tick or two


def test_tranche_target_books_a_blended_result():
    r = run({"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 60.0})
    assert r["t1"] is True
    assert r["gross_bp"] > run()["gross_bp"]


def test_delay_exits_on_a_later_print():
    now, late = run(), run(delay=5.0)
    assert late["hold_s"] >= now["hold_s"] + 4.0
    assert late["gross_bp"] <= now["gross_bp"] + 1e-9


def test_no_prints_returns_none():
    assert ts.sim_trade("T", 100.0, T0, [(T0, 100.0, 1)], cfg()) is None


def test_compare_and_verdict_on_planted_rows():
    rows = [{"day": f"2026-10-{10 + i // 6:02d}", "sym": f"S{i % 30}", "base": 0.0, "var": 2.0 + (i % 3)} for i in range(120)]
    s = ts.compare(rows, boots=500)
    assert s["h1"] > 0 and s["h2"] > 0 and s["p5"] > 0 and s["changed"] == 120
    assert ts.verdict(s, {"n": 1, "mean_day": 1.0})[0] in ("PASS", "FAIL")


def test_vacuous_when_nothing_changes():
    rows = [{"day": f"2026-10-{10 + i // 6:02d}", "sym": "S", "base": 1.0, "var": 1.0} for i in range(120)]
    s = ts.compare(rows, boots=200)
    assert ts.verdict(s, s)[0] == "VACUOUS"


def test_isolated_bad_tick_is_dropped_but_a_real_gap_is_kept():
    base = [(T0 + i, 100.0, 1) for i in range(20)]
    bad = list(base); bad[10] = (T0 + 10, 90.0, 1)                 # -1000 bp for one print
    cleaned, n = ts.clean_prints(bad)
    assert n == 1 and all(p[1] == 100.0 for p in cleaned)
    gap = [(T0 + i, 100.0 if i < 10 else 98.0, 1) for i in range(20)]   # real gap down, stays down
    cleaned, n = ts.clean_prints(gap)
    assert n == 0 and len(cleaned) == 20


def test_bad_tick_no_longer_stops_the_trade_out():
    pr = [(T0 + i, 100.0, 1) for i in range(30)]
    pr[15] = (T0 + 15, 91.0, 1)
    r = run(prints=pr)
    assert r["why"] == "window_end" and abs(r["gross_bp"]) < 1.0
