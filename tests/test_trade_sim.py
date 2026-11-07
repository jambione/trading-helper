import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import trade_sim as ts

T0 = 1_800_000_000.0


def path(up_bp=30, down_to=99.0, step=0.01, entry=100.0, dt=1.0):
    """One print per dt seconds: climb +up_bp, then fall to `down_to`."""
    px, out, t = entry, [(T0, entry, 1)], T0
    peak = entry * (1 + up_bp / 1e4)
    while px < peak - 1e-9:
        px = round(px + step, 4); t += dt; out.append((t, px, 1))
    while px > down_to + 1e-9:
        px = round(px - step, 4); t += dt; out.append((t, px, 1))
    return out


def cfg(**o):
    c = ts.base_cfg(); c.update(o); return c


def run(over=None, lat=0.0, prints=None):
    return ts.sim_trade("T", 100.0, T0, prints or path(), cfg(**(over or {})), lat)


def test_baseline_exits_on_the_trail():
    r = run()
    assert r and r["why"] == "local_trail" and r["hold_s"] > 0 and r["t1"] is False


def test_no_prints_returns_none():
    assert ts.sim_trade("T", 100.0, T0, [(T0, 100.0, 1)], cfg()) is None


def test_tick_series_forward_fills_and_stops_after_15s_silence():
    pr = [(T0, 100.0, 1), (T0 + 1.0, 101.0, 1), (T0 + 40.0, 102.0, 1)]
    t = ts.tick_series(pr, 0.25, 15.0)
    assert any(abs(x[0] - (T0 + 0.75)) < 1e-9 and x[1] == 100.0 for x in t)   # re-fed between prints
    assert not any(T0 + 16.5 < x[0] < T0 + 39.9 for x in t)                   # nothing after 15 s of silence
    assert t[-1][1] == 102.0


def test_causal_filter_drops_isolated_bad_tick_and_accepts_a_real_gap():
    bad = [(T0 + i, 100.0, 1) for i in range(20)]
    bad[10] = (T0 + 10, 91.0, 1)
    assert ts.clean_prints(bad)[1] == 1
    gap = [(T0 + i, 100.0, 1) for i in range(10)] + [(T0 + 10 + i, 95.0, 1) for i in range(10)]
    cleaned, n = ts.clean_prints(gap)
    assert n <= 3 and sum(1 for x in cleaned if x[1] == 95.0) >= 7


def test_filter_is_causal():
    base = [(T0 + i, 100.0, 1) for i in range(10)]
    a, _ = ts.clean_prints(base + [(T0 + 10, 91.0, 1)])
    b, _ = ts.clean_prints(base + [(T0 + 10, 91.0, 1)] + [(T0 + 11 + i, 91.0, 1) for i in range(10)])
    assert (a[-1][1] == 91.0) == any(x[0] == T0 + 10 for x in b)


# ---------------- canaries (code review 11/07) ----------------
def test_latency_canary_fill_is_the_first_print_lat_seconds_after_the_stop_decision():
    """Expected value computed straight from the prints, not through the simulator's own bookkeeping."""
    pr = [(T0 + i * 0.5, 100.0, 1) for i in range(8)] + [(T0 + 4 + i * 0.5, round(99.0 - 0.05 * i, 2), 1) for i in range(20)]
    r0, r1 = run(lat=0.0, prints=pr), run(lat=1.0, prints=pr)
    assert r0["why"] == r1["why"] == "local_trail"
    stop_t = T0 + r0["hold_s"]                     # lat 0: filled on the decision print
    exp = next(px for t, px, _ in pr if t >= stop_t + 1.0 - 1e-9)
    assert abs(r1["gross_bp"] - (exp / 100.0 - 1) * 1e4) < 1e-6


def test_planted_target_canary_matches_a_direct_computation():
    """Sell 100% with a resting limit at +15 bp, through 0: expected = +15 bp if a print reaches the target after the
    3 s attach and before the baseline's exit fill, else the baseline result."""
    over = {"ai_watch_synth_rr": 0.03, "ai_watch_synth_scale_out_pct": 100.0, "sim_t1_limit": True, "sim_t1_through_bp": 0.0}
    for up in (10, 20, 40):
        pr = path(up_bp=up)
        b, v = run(lat=0.5, prints=pr), run(over, lat=0.5, prints=pr)
        tgt = 100.0 * 1.0015
        hit = next((t for t, px, _ in pr if t >= T0 + 3.0 and px >= tgt - 1e-9), None)
        if hit is not None and hit < T0 + b["hold_s"]:
            assert abs(v["gross_bp"] - (tgt / 100.0 - 1) * 1e4) < 1e-6
        else:
            assert abs(v["gross_bp"] - b["gross_bp"]) < 1e-6


def test_null_canary_a_knob_that_changes_nothing_is_vacuous():
    rows = []
    for k in range(40):
        pr = path(up_bp=10 + k % 25)
        b, v = run(lat=0.5, prints=pr), run({"sim_unused_knob": 1}, lat=0.5, prints=pr)
        rows.append({"day": f"2026-10-{10 + k // 4:02d}", "sym": f"S{k}", "base": b["gross_bp"], "var": v["gross_bp"]})
    s = ts.compare(rows, boots=50)
    assert ts.verdict(s, s)[0] == "VACUOUS"


def test_pending_market_tranche_is_not_lost_when_the_stop_hits_first():
    over = {"ai_watch_synth_rr": 0.03, "ai_watch_synth_scale_out_pct": 50.0, "sim_t1_limit": True, "sim_t1_through_bp": 0.0}
    # already through the +15 bp target at the 3 s book tick -> market scale goes out; then a crash
    pr = [(T0, 100.0, 1), (T0 + 1, 100.2, 1), (T0 + 2.9, 100.2, 1), (T0 + 3.1, 100.2, 1), (T0 + 3.3, 99.0, 1), (T0 + 4.0, 98.9, 1)]
    r = run(over, lat=0.5, prints=pr)
    assert r["t1"] is True


def test_compare_reports_winsorized_halves_and_verdict_labels():
    rows = [{"day": f"2026-10-{10 + i // 6:02d}", "sym": f"S{i % 30}", "base": 0.0, "var": 2.0 + (i % 7) * 0.5} for i in range(120)]
    s = ts.compare(rows, boots=200)
    assert s["win_h1"] > 0 and s["win_h2"] > 0 and s["p5"] > 0
    assert ts.verdict(s, s)[0] == "IMPROVES"
    s_bad = dict(s, h2=-1.0)
    st, bad = ts.verdict(s_bad, s)
    assert st == "NO" and "both halves > 0" in bad


# ---------------- never-green bleed variants (11/07) ----------------
def _bleed_then_back(back_at=400.0):
    """Never green: drifts to -30 bp by t=200, flat, then back to +5 bp from `back_at`."""
    pr, t = [], T0
    while t <= T0 + 200:
        pr.append((t, round(100.0 - 0.30 * (t - T0) / 200, 4), 1)); t += 1
    while t < T0 + back_at:
        pr.append((t, 99.70, 1)); t += 1
    while t <= T0 + back_at + 120:
        pr.append((t, 100.05, 1)); t += 1
    return pr


def test_scratch_canary_exits_flat_once_price_returns_to_entry():
    pr = _bleed_then_back()
    r = run({"sim_scratch_after_sec": 300.0, "sim_scratch_through_bp": 2.0}, lat=0.5, prints=pr)
    assert r["why"] == "scratch" and r["gross_bp"] == 0.0
    exp = next(t for t, px, _ in pr if t >= T0 + 300 and px >= 100.0 * 1.0002) - T0     # computed straight from the prints
    assert abs(r["hold_s"] - exp) < 1e-6


def test_scratch_not_attached_on_a_trade_that_went_green():
    pr = [(T0 + i, 100.0 + (0.10 if 10 <= i <= 20 else -0.20), 1) for i in range(600)]   # +10 bp early, then -20 bp
    r = run({"sim_scratch_after_sec": 300.0, "sim_scratch_through_bp": 2.0}, lat=0.5, prints=pr)
    assert r["why"] != "scratch"


def test_underwater_leash_exits_a_flat_red_trade_earlier():
    # sits around -20 bp with 1-2 cent wiggles (the leash parks 1c under the price, so a flat tape never trips it), never near -60
    pr = [(T0, 100.0, 1)] + [(T0 + i, 99.78 if i % 7 == 0 else 99.80, 1) for i in range(1, 900)]
    b = run(lat=0.5, prints=pr)
    v = run({"ai_local_trail_decay_red_step_r": 0.01}, lat=0.5, prints=pr)
    assert b["why"] == "window_end"
    assert v["why"] == "local_trail" and v["hold_s"] < b["hold_s"]
