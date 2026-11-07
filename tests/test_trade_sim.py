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


def run(over=None, delay=0.0, prints=None, t1_delay=0.0):
    return ts.sim_trade("T", 100.0, T0, prints or path(), cfg(**(over or {})), delay, t1_delay)


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
    assert ts.verdict(s, {"n": 1, "mean_day": 1.0})[0] in ("IMPROVES", "NO")


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


def test_resting_limit_needs_the_through_distance_and_pays_no_spread():
    tight = run({"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 60.0, "sim_t1_limit": True, "sim_t1_through_bp": 0.0})
    far = run({"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 60.0, "sim_t1_limit": True, "sim_t1_through_bp": 15.0})
    mkt = run({"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 60.0})
    assert tight["t1"] is True and far["t1"] is False        # +30 bp path never trades 15 bp through a +20 bp target... but 35 bp is not reached
    assert tight["gross_bp"] > mkt["gross_bp"]               # no extra sale cost


def test_delay_moves_the_market_tranche_sale_but_not_a_resting_limit():
    pr = [(T0 + i, 100.0 + 0.01 * i, 1) for i in range(60)]          # steady climb, never stops out
    mkt = {"ai_watch_synth_rr": 0.003, "ai_watch_synth_scale_out_pct": 50.0}
    lim = {**mkt, "sim_t1_limit": True}
    a, b = run(mkt, 0.0, pr), run(mkt, 0.0, pr, t1_delay=5.0)
    assert a["t1"] and b["t1"]
    c, d = run(lim, 0.0, pr), run(lim, 0.0, pr, t1_delay=5.0)
    # the market sale happens 5 s later at a HIGHER price on a rising path; the limit sale is the same price either way
    assert b["gross_bp"] > a["gross_bp"] - 1e-9
    t1_gain = lambda r: r["gross_bp"]  # noqa: E731
    assert abs(t1_gain(c) - t1_gain(d)) < 30.0


def test_tick_series_keeps_last_print_per_bucket():
    pr = [(T0 + 0.01, 100.0, 1), (T0 + 0.10, 99.0, 1), (T0 + 0.30, 101.0, 1)]
    assert [x[1] for x in ts.tick_series(pr, 0.25)] == [99.0, 101.0]


def test_causal_filter_drops_an_isolated_bad_tick_but_accepts_a_real_gap():
    bad = [(T0 + i, 100.0, 1) for i in range(20)]
    bad[10] = (T0 + 10, 91.0, 1)
    cleaned, n = ts.clean_prints(bad)
    assert n == 1
    gap = [(T0 + i, 100.0, 1) for i in range(10)] + [(T0 + 10 + i, 95.0, 1) for i in range(10)]
    cleaned, n = ts.clean_prints(gap)
    assert n <= 3 and sum(1 for x in cleaned if x[1] == 95.0) >= 7     # first 1-3 prints of a real 5% gap dropped, then accepted


def test_filter_is_causal():
    base = [(T0 + i, 100.0, 1) for i in range(10)]
    a, _ = ts.clean_prints(base + [(T0 + 10, 91.0, 1)])
    b, _ = ts.clean_prints(base + [(T0 + 10, 91.0, 1)] + [(T0 + 11 + i, 91.0, 1) for i in range(10)])
    assert (a[-1][1] == 91.0) == any(x[0] == T0 + 10 for x in b)       # the decision on print 10 ignores what comes after
