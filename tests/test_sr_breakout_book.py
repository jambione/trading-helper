"""tools/studies/sr_breakout_book.py — rules from docs/studies/sr_breakout_book_prereg.json, on a fake market."""
import collections
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "studies"))
sys.path.insert(0, str(ROOT / "tools"))
import sr_breakout_book as S  # noqa: E402
import bro_sr_wr as B  # noqa: E402

DAY = "2026-10-08"


def T(h, m, s=0):
    return B.et_ts(DAY, h, m) + s


def row(sym, ts, brk=None, *, src="tight", price=100.0, bars=400, ob=True, psrc="quote", arm="exh_falling",
        resist=False, ver="PIN"):
    r = {"ts": ts, "symbol": sym, "source": src, "price": price, "price_src": psrc, "arm_why": arm, "git_version": ver}
    if ob:
        r.update({"ob_brk_dist_pct": brk, "ob_bars": bars, "ob_resist_0.3": resist, "ob_room_pct": None})
    return r


class FakeMkt:
    """Mid = path(sym, t); spread fixed. Quotes are always fresh unless sym/t is listed as stale."""

    def __init__(self, path, spread=0.02, stale=()):
        self.path, self.spread, self.stale, self.requests, self.calls = path, spread, set(stale), 0, []

    def quote_ts(self, sym, t):
        self.calls.append((sym, t))
        if sym in self.stale:
            return None
        m = self.path(sym, t)
        return [t, m - self.spread / 2, m + self.spread / 2]

    def first_quote_after(self, sym, t, within=1800.0):
        return None

    def minute_bars(self, *a, **k):
        return []

    def flush(self):
        pass


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(S, "build_ok", lambda v: v == "PIN")


def flat(sym, t):
    return 100.2


def test_new_clean_reading_needs_a_prior_reading_and_no_backfill():
    c = collections.Counter()
    rs = {"AAA": [row("AAA", T(10, 0), None, bars=400), row("AAA", T(10, 0, 30), 0.15, bars=401)],
          "BBB": [row("BBB", T(10, 0, 30), 0.15)],                                     # no prior check
          "CCC": [row("CCC", T(10, 0), None, ob=False), row("CCC", T(10, 0, 30), 0.15)],  # prior had no reading
          "DDD": [row("DDD", T(10, 0), None, bars=100), row("DDD", T(10, 0, 30), 0.15, bars=400)],  # backfill jump
          "EEE": [row("EEE", T(10, 0), 0.12), row("EEE", T(10, 0, 30), 0.15)],          # continuation
          "FFF": [row("FFF", T(9, 50), None), row("FFF", T(10, 0, 30), 0.15)]}           # prior > 120 s back
    got = S.readings(rs, c)
    assert [s for s in got] == ["AAA"]
    assert c["CLEAN_no_prev_check"] == 3  # BBB, FFF, and EEE's own first row
    assert c["CLEAN_prev_without_ob"] == 1 and c["CLEAN_bars_jump"] == 1


def test_null_means_present_key_not_missing():
    r = row("AAA", 1.0, None)
    assert S.has_ob(r) and "ob_brk_dist_pct" in r
    assert not S.has_ob(row("AAA", 1.0, ob=False))


def _book(extra=None):
    rows = [row("AAA", T(10, 0), None), row("AAA", T(10, 0, 30), 0.15, price=100.15),
            row("CTL", T(10, 0, 20), None, price=50.0)]
    return rows + (extra or [])


def test_score_pairs_an_event_with_a_same_minute_control_and_stays_blind(capsys):
    out, c = S.score_day(DAY, mkt=FakeMkt(flat), rows=_book())
    pairs = [r for r in out if r["status"] == "pair"]
    assert len(pairs) == 1 and pairs[0]["ctrlA"] == "CTL" and pairs[0]["group"] == "TIGHT"
    assert capsys.readouterr().out == ""                       # score_day prints nothing


def test_control_that_breaks_out_after_c_is_still_eligible():
    """Skeptic #1: excluding controls that break out AFTER c removes the risers (look-ahead)."""
    later = [row("CTL", T(10, 5), None), row("CTL", T(10, 5, 30), 0.2)]
    out, _ = S.score_day(DAY, mkt=FakeMkt(flat), rows=_book(later))
    assert [r for r in out if r["status"] == "pair"][0]["ctrlA"] == "CTL"


def test_control_with_a_clean_reading_in_the_prior_15_min_is_excluded():
    before = [row("CTL", T(9, 50), None), row("CTL", T(9, 50, 30), 0.2)]
    out, _ = S.score_day(DAY, mkt=FakeMkt(flat), rows=_book(before))
    ev = [r for r in out if r["symbol"] == "AAA" and r["band"] == "CLEAN"][0]
    assert ev["status"] == "drop_no_control"


def test_control_check_after_c_is_not_read():
    """Skeptic #2: a control check at c+25 s carries post-entry information."""
    rows = [row("AAA", T(10, 0), None), row("AAA", T(10, 0, 30), 0.15, price=100.15),
            row("CTL", T(10, 0, 55), None, price=50.0)]
    out, _ = S.score_day(DAY, mkt=FakeMkt(flat), rows=rows)
    assert [r for r in out if r["symbol"] == "AAA"][0]["status"] == "drop_no_control"


def test_spread_only_breakout_is_not_primary_and_blocks_later_events():
    # block top rebuilt from the ask: 100.15 / 1.0015 = 100.0; mid 100.05 is only +0.05% -> spread_only
    rows = _book([row("AAA", T(11, 0), None), row("AAA", T(11, 0, 30), 0.15, price=100.15)])
    out, c = S.score_day(DAY, mkt=FakeMkt(lambda s, t: 100.05), rows=rows)
    aaa = [r for r in out if r["symbol"] == "AAA" and r["band"] == "CLEAN"]
    assert aaa[0]["status"] == "spread_only"
    assert aaa[1]["status"] == "non_first"                     # skeptic r2 #2: first decided before drops


def test_nonprimary_rows_and_other_builds_never_make_events():
    rows = [row("AAA", T(10, 0), None), row("AAA", T(10, 0, 30), 0.15, psrc="tape"),
            row("BBB", T(10, 0), None, ver="other"), row("BBB", T(10, 0, 30), 0.15, ver="other")]
    out, c = S.score_day(DAY, mkt=FakeMkt(flat), rows=rows)
    assert not [r for r in out if r["band"] == "CLEAN" and r["status"] == "pair"]
    assert c["CLEAN_nonprimary_row"] == 1 and c["rows_other_build"] == 2


def test_cost_is_half_spread_each_end_plus_a_cent_under_5():
    c = collections.Counter()
    m = FakeMkt(lambda s, t: 4.0 if t < T(10, 10) else 4.04, spread=0.02)
    x = S.simulate(m, "LOW", DAY, T(10, 0), 900, c, "event")
    assert abs(x["gross"] - 100.0) < 1e-6
    assert abs(x["cost"] - (0.01 / 4.0 * 1e4 + 0.01 / 4.04 * 1e4 + 0.01 / 4.0 * 1e4)) < 1e-6


def test_entry_is_c_plus_5s():
    m = FakeMkt(flat)
    S.simulate(m, "AAA", DAY, T(10, 0), 900, collections.Counter(), "event")
    assert m.calls[0] == ("AAA", T(10, 0) + 5.0)


def test_stale_quote_drops_never_costs_zero():
    out, c = S.score_day(DAY, mkt=FakeMkt(flat, stale={"AAA"}), rows=_book())
    assert [r for r in out if r["symbol"] == "AAA"][0]["status"] == "drop_mid_stale"


def _sealed(n_days, per_day, diff, grp="TIGHT"):
    rows = []
    days = [f"2026-10-{d:02d}" for d in range(8, 8 + n_days)]
    for i, d in enumerate(days):
        for k in range(per_day):
            rows.append({"day": d, "symbol": f"S{k}", "group": grp, "band": "CLEAN", "status": "pair",
                         "diff": diff + (((i * 7 + k * 3) % 5) - 2) * 0.8 + ((i % 3) - 1) * 0.6, "ev": {"net": diff}})
    halves = {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(days)}
    return rows, halves


def test_power_reports_no_means():
    rows, halves = _sealed(4, 3, 10.0)
    pw = S.power_table(rows, halves)
    blob = json.dumps(pw)
    assert "mean" not in blob and pw["TIGHT"]["ready"] is False


def test_underpowered_then_closed_unproven_at_the_deadline():
    rows, halves = _sealed(4, 3, 10.0)
    assert S.verdict("TIGHT", rows, halves, today="2026-11-01")["verdict"] == "UNDERPOWERED"
    assert S.verdict("TIGHT", rows, halves, today="2026-12-15")["verdict"] == "CLOSED UNPROVEN"


def test_powered_null_is_a_fail_and_a_clear_win_passes():
    rows, halves = _sealed(32, 10, 0.0)
    v0 = S.verdict("TIGHT", rows, halves, today="2026-11-30")
    assert v0["power"]["ready"] and v0["verdict"] == "FAIL"
    rows, halves = _sealed(32, 10, 8.0)
    v = S.verdict("TIGHT", rows, halves, today="2026-11-30")
    assert v["power"]["ready"] and v["verdict"] == "PASS"
    # the same win carried by only 5 symbols fails the drop-top-5-symbols rule
    rows, halves = _sealed(32, 5, 8.0)
    assert S.verdict("TIGHT", rows, halves, today="2026-11-30")["verdict"] == "FAIL"


def test_strongly_negative_fails_even_underpowered():
    rows, halves = _sealed(4, 3, -50.0)
    assert S.verdict("TIGHT", rows, halves, today="2026-10-20")["verdict"] == "FAIL"


def _wire(tmp_path, monkeypatch, rows, halves):
    monkeypatch.setattr(S, "READS", str(tmp_path / "reads.json"))
    monkeypatch.setattr(S, "READY_FIRST", str(tmp_path / "ready.json"))
    monkeypatch.setattr(S, "SEALED", str(tmp_path / "sealed.jsonl"))
    open(S.SEALED, "w").close()
    S.jl_append(S.SEALED, rows)
    monkeypatch.setattr(S, "halves_map", lambda *a, **k: halves)


def test_read_is_refused_before_power_and_only_once(tmp_path, monkeypatch):
    rows, halves = _sealed(4, 3, 10.0)
    _wire(tmp_path, monkeypatch, rows, halves)
    with pytest.raises(SystemExit):
        S.cmd_read("TIGHT")
    rows, halves = _sealed(32, 10, 8.0)
    _wire(tmp_path, monkeypatch, rows, halves)
    with pytest.raises(SystemExit):
        S.cmd_read("TIGHT")                 # ready, but `power` has not frozen the cutoff yet
    S.cmd_power()
    S.cmd_read("TIGHT")
    with pytest.raises(SystemExit):
        S.cmd_read("TIGHT")


def test_read_uses_only_sessions_up_to_the_first_ready_night(tmp_path, monkeypatch, capsys):
    """Skeptic code review #2: the read cannot be timed by hand."""
    rows, halves = _sealed(32, 10, 8.0)
    _wire(tmp_path, monkeypatch, rows, halves)
    S.cmd_power()
    cutoff = json.load(open(S.READY_FIRST))["TIGHT"]
    late = [{"day": "2026-12-01", "symbol": f"Z{k}", "group": "TIGHT", "band": "CLEAN", "status": "pair",
             "diff": -500.0, "ev": {"net": -500.0}} for k in range(50)]
    S.jl_append(S.SEALED, late)
    halves["2026-12-01"] = "A"
    capsys.readouterr()
    S.cmd_read("TIGHT")
    res = json.loads(capsys.readouterr().out)
    assert res["cutoff_day"] == cutoff and res["verdict"] == "PASS"   # the later sessions are not read


def test_read_due_reads_at_the_deadline_as_closed_unproven(tmp_path, monkeypatch, capsys):
    rows, halves = _sealed(4, 3, 10.0)
    _wire(tmp_path, monkeypatch, rows, halves)

    class D(S.datetime):
        @classmethod
        def now(cls, tz=None):
            return S.datetime(2026, 12, 15, 17, 0, tzinfo=tz)
    monkeypatch.setattr(S, "datetime", D)
    S.cmd_read_due()
    reads = json.load(open(S.READS))
    assert reads["TIGHT"]["verdict"] == "CLOSED UNPROVEN" and reads["MOVERS"]["verdict"] == "CLOSED UNPROVEN"


def test_window_ends_at_15_30_00():
    assert S.in_window(DAY, T(15, 30)) and not S.in_window(DAY, T(15, 30, 50)) and not S.in_window(DAY, T(9, 44, 59))


def test_tape_row_clean_reading_does_not_exclude_a_control():
    """Review #7: the exclusion is on primary-row CLEAN readings, as the prereg lists."""
    extra = [row("CTL", T(9, 55), None, price=50.0), row("CTL", T(9, 55, 30), 0.2, psrc="tape", price=50.0)]
    out, _ = S.score_day(DAY, mkt=FakeMkt(flat), rows=_book(extra))
    assert [r for r in out if r["symbol"] == "AAA" and r["band"] == "CLEAN"][0]["ctrlA"] == "CTL"


def test_control_in_resistance_or_other_group_is_not_eligible():
    for extra in ([row("CTL", T(10, 0, 20), None, price=50.0, resist=True)],
                  [row("CTL", T(10, 0, 20), None, price=50.0, src="movers")]):
        rows = [row("AAA", T(10, 0), None), row("AAA", T(10, 0, 30), 0.15, price=100.15)] + extra
        out, _ = S.score_day(DAY, mkt=FakeMkt(flat), rows=rows)
        assert [r for r in out if r["symbol"] == "AAA"][0]["status"] == "drop_no_control"


def test_control_b_is_drawn_only_after_the_event_exit():
    later = [row("AAA", T(10, 10), None), row("AAA", T(10, 20), None)]
    m = FakeMkt(flat)
    out, _ = S.score_day(DAY, mkt=m, rows=_book(later))
    ev = [r for r in out if r["status"] == "pair"][0]
    assert ev.get("cb") is not None
    b_entries = [t for s_, t in m.calls if s_ == "AAA" and t > T(10, 0, 30) + 5 + 1800]
    assert all(t >= T(10, 20) for t in [t for s_, t in m.calls if s_ == "AAA" and abs(t - (T(10, 20) + 5)) < 1])
    assert not any(abs(t - (T(10, 10) + 5)) < 1 for s_, t in m.calls if s_ == "AAA")


def test_halt_exit_takes_the_first_quote_after_resume(monkeypatch):
    m = FakeMkt(flat)
    gone = T(10, 15) + 5
    real = m.quote_ts
    m.quote_ts = lambda sym, t: None if abs(t - gone) < 1 else real(sym, t)
    m.first_quote_after = lambda sym, t, within=1800.0: [t + 600, 101.0, 101.02]
    monkeypatch.setattr(S, "_halted", lambda *a: True)
    c = collections.Counter()
    x = S.simulate(m, "AAA", DAY, T(10, 0), 900, c, "event")
    assert c["event_halt_exit"] == 1 and x["exit_mid"] == pytest.approx(101.01)
    monkeypatch.setattr(S, "_halted", lambda *a: False)
    c = collections.Counter()
    assert S.simulate(m, "AAA", DAY, T(10, 0), 900, c, "event") is None and c["event_exit_stale"] == 1


def test_drop_top_removes_the_biggest_contributors():
    pairs = [{"day": d, "symbol": f"S{k}", "d": v} for d, v in (("D1", 90.0), ("D2", 80.0), ("D3", 70.0), ("D4", -1.0))
             for k in range(2)]
    rest = S._drop_top(pairs, "day", 3)
    assert {p["day"] for p in rest} == {"D4"} and sum(p["d"] for p in rest) < 0


def test_knife_edge_flag():
    rows, halves = _sealed(32, 10, 8.0)
    for b in ("POKE", "CHASE"):
        rows.append({"day": "2026-10-08", "symbol": "K", "group": "TIGHT", "band": b, "status": "pair",
                     "diff": -3.0, "ev": {"net": -3.0}})
    v = S.verdict("TIGHT", rows, halves, today="2026-11-30")
    assert v["verdict"] == "PASS" and v["knife_edge"] is True


def test_unparseable_and_symbol_less_lines_are_counted(tmp_path):
    p = tmp_path / "shadow.jsonl"
    good = json.dumps(row("AAA", T(10, 0), None))
    p.write_text(good + "\n" + '{"ts": %f, "symbol": ' % T(10, 1) + "\n" + "garbage\n"
                 + json.dumps({"ts": T(10, 2), "price": 1.0, "git_version": "PIN"}) + "\n")
    c = collections.Counter()
    rows = S.load_day_rows(DAY, path=str(p), counts=c)
    assert len(rows) == 2 and c["lines_unparseable"] == 1 and c["lines_without_ts"] == 1
    out, c2 = S.score_day(DAY, mkt=FakeMkt(flat), rows=rows)
    assert c2["rows_without_symbol"] == 1


def test_missing_pin_refuses_to_score(monkeypatch):
    monkeypatch.setattr(S, "_PIN_BLOBS", {})
    monkeypatch.setattr(S, "_git", lambda *a: "")
    with pytest.raises(SystemExit):
        S.pin_blobs()


def test_real_pins_accept_head_and_reject_dirty(monkeypatch):
    import subprocess
    monkeypatch.undo()                      # the autouse fixture stubs build_ok; use the real one here
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    S._BUILD_OK.clear()
    assert S.build_ok(head) is True
    assert S.build_ok(head + "+") is False and S.build_ok("") is False


def test_day_review_breakout_section_is_blind_from_10_8(tmp_path):
    import subprocess
    rep = tmp_path / "ai_reports"
    rep.mkdir()
    rows = [dict(row("AAA", T(10, 0) + i * 10, 0.15 if i == 3 else None), price=100 + i * 0.01) for i in range(120)]
    (rep / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "studies" / "day_review.py"), DAY], cwd=tmp_path,
                         capture_output=True, text=True, env={"REPO": str(tmp_path), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert "Blinded while the breakout-on-book study runs" in out.stdout and "Avg gross 15m bp" not in out.stdout


def test_sessions_before_10_8_are_refused():
    with pytest.raises(SystemExit):
        S.cmd_score("2026-10-07")
