"""tools/fact_picker.py: SEC facts by filing date, price facts without look-ahead, combined scores, three ledgers."""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import auto_picker as A
import fact_picker as F
import pick_ledger as P

ET = ZoneInfo("America/New_York")
# a company with a December year end: FY2025, then Q1-Q3 2026 year-to-date figures and their year-ago spans
REV = [["2025-01-01", "2025-12-31", 400.0, "2026-02-15"],
       ["2025-01-01", "2025-09-30", 290.0, "2025-11-01"],
       ["2026-01-01", "2026-09-30", 350.0, "2026-11-01"],
       ["2025-01-01", "2025-06-30", 190.0, "2025-08-01"],
       ["2026-01-01", "2026-06-30", 220.0, "2026-08-01"]]


def test_ttm_and_growth_use_only_what_was_filed_before_the_date():
    now = F.visible(REV, "2026-11-10")
    assert F.ttm(now) == 400 + 350 - 290 and abs(F.yoy(now) - (350 / 290 - 1)) < 1e-9
    aug = F.visible(REV, "2026-10-01")                                   # the Q3 filing (11/01) is not out yet
    assert F.ttm(aug) == 400 + 220 - 190 and abs(F.yoy(aug) - (220 / 190 - 1)) < 1e-9
    assert F.ttm(F.visible(REV, "2026-03-01")) == 400                    # a fresh full year is its own trailing twelve months
    assert F.visible(REV, "2026-11-01")[-1][1] != "2026-09-30" or all(r[3] < "2026-11-01" for r in F.visible(REV, "2026-11-01"))
    assert F.ttm([["2026-01-01", "2026-09-30", 350.0, "2026-11-01"]]) is None   # no full year to build on -> unknown, not guessed


def test_a_restated_period_shows_the_version_filed_at_the_time():
    rows = [["2025-01-01", "2025-12-31", 400.0, "2026-02-15"], ["2025-01-01", "2025-12-31", 380.0, "2026-08-01"]]
    assert F.ttm(F.visible(rows, "2026-05-01")) == 400 and F.ttm(F.visible(rows, "2026-09-01")) == 380


def _facts(ni=40.0, last_end="2025-12-31", filed="2026-02-15"):
    start = (date.fromisoformat(last_end) - timedelta(days=364)).isoformat()
    return {"rev": {"t": [[start, last_end, 400.0, filed]]}, "ni": {"t": [[start, last_end, ni, filed]]},
            "sh": {"t": [[start, last_end, 10.0, filed]]}, "eq": {"t": [["", last_end, 200.0, filed]]},
            "liab": {"t": [["", last_end, 300.0, filed]]}, "ocf": {"t": [[start, last_end, 60.0, filed]]}}


def test_fundamentals_ratios_and_refusals():
    f = F.fundamentals(_facts(), "2026-03-01", 20.0)
    assert f["mcap"] == 200 and f["pe"] == 5 and f["ps"] == 0.5 and f["pb"] == 1 and f["margin"] == 0.1 and f["ocf_ni"] == 1.5 and f["liab_eq"] == 1.5
    loss = F.fundamentals(_facts(ni=-5.0), "2026-03-01", 20.0)
    assert loss["pe"] is None and loss["loss"] and loss["ocf_ni"] is None
    assert F.fundamentals(_facts(), "2026-02-15", 20.0) is None          # filed that day: not known before the run
    assert F.fundamentals(_facts(), "2027-03-01", 20.0) is None          # nothing filed for most of a year: stale
    assert F.fundamentals({**_facts(), "sh": {}}, "2026-03-01", 20.0) is None
    assert F.fundamentals(_facts(), "2026-03-01", 5.0, lambda day: 4.0)["mcap"] == 200   # a 4-for-1 split after the filing: 4x the shares at 1/4 the price
    raw, adj = {"2025-12-30": (80.0, 80.0), "2026-01-05": (20.0, 20.0)}, {"2025-12-30": (20.0, 20.0), "2026-01-05": (20.0, 20.0)}
    assert F.split_factor(raw, adj, "2025-12-31") == 4.0 and F.split_factor(raw, adj, "2026-01-05") == 1.0 and F.split_factor({}, adj, "2026-01-05") == 1.0


def _row(start, end, val, filed, form="10-K"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def test_compact_keeps_usd_only_and_the_tag_is_chosen_as_of_the_date():
    old, new = "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"
    raw = {"facts": {"us-gaap": {
        old: {"units": {"USD": [_row("2024-01-01", "2024-12-31", 100, "2025-02-01"), _row("2025-01-01", "2025-12-31", 120, "2026-02-01")],
                        "CNY": [_row("2025-01-01", "2025-12-31", 900, "2026-02-01")]}},
        new: {"units": {"USD": [_row("2025-01-01", "2025-12-31", 30, "2026-02-01"), _row("2026-01-01", "2026-12-31", 150, "2027-02-01"),
                                _row("2019-01-01", "2019-12-31", 5, "2020-02-01")]}}}}}
    c = F.compact(raw)
    assert set(c["rev"]) == {old, new} and all(r[2] != 900 for r in c["rev"][old]) and len(c["rev"][new]) == 2 and c["ni"] == {}
    assert F.ttm(F.pick_tag(c["rev"], "2025-06-01")) == 100              # only the old tag had been filed
    assert F.ttm(F.pick_tag(c["rev"], "2026-06-01")) == 120              # same period end on both tags: the total, not the part
    assert F.ttm(F.pick_tag(c["rev"], "2027-06-01")) == 150              # the company moved to the new tag
    raw["facts"]["us-gaap"][old]["units"]["USD"].append(_row("2026-01-01", "2026-12-31", 1, "2027-03-01", form="20-F"))
    assert F.compact(raw) == {k: {} for k in F.TAGS}                     # a foreign filer gets no sheet


def _scored(sym, stamp, lift):
    h = {"closed": True, "vs_spy": lift, "vs_random": lift, "rank": 0.9 if lift > 0 else 0.1, "beta_excess": lift, "pick": lift, "spy": 0.0}
    return {"symbol": sym, "recorded_at": stamp, "status": "scored", "counts": True, "entry_day": stamp[:10], "h": {P.EARLY: h}}


def test_verdict_has_a_t_across_weeks_even_inside_three_months():
    mondays = [datetime(2026, 11, 16, 8, 45, tzinfo=ET) + timedelta(days=7 * k) for k in range(10)]     # Nov, Dec, Jan only
    good = [_scored(f"S{k}{j}", m.isoformat(), 0.04 + 0.01 * ((k + j) % 3)) for k, m in enumerate(mondays) for j in range(3)]
    v = F.week_verdict(good)
    assert v["weeks"] == 10 and v["t"] > F.T_MIN and v["verdict"] == "PASS" and len({x["entry_day"][:7] for x in good}) == 3
    assert F.judge(v, 0.01) == "PASS" and F.judge(v, 0.10) == "FAIL" and F.judge(v, None).startswith("BLOCKED")
    noisy = [_scored(f"S{k}{j}", m.isoformat(), 0.30 if k == 0 else -0.01) for k, m in enumerate(mondays) for j in range(3)]
    assert F.week_verdict(noisy)["verdict"] == "FAIL"                    # one lucky week is not a pass
    assert F.week_verdict(good[:9])["verdict"].startswith("NOT YET")


def test_pool_control_is_the_whole_sheet_week_by_week():
    days = [(date(2026, 11, 2) + timedelta(days=i)).isoformat() for i in range(60)]
    bars = {"SPY": {d: (100.0, 100.0) for d in days}, "UP": {d: (100.0 + i, 100.0 + i) for i, d in enumerate(days)},
            "FLAT": {d: (50.0, 50.0) for d in days}}
    stamp = f"{days[4]}T08:45:00-05:00"
    a = P.entry_index(days, stamp)
    want = ((100.0 + a + P.EARLY) / (100.0 + a) - 1) / 2
    assert abs(F.pool_control({days[4]: ["UP", "FLAT"]}, {days[4]: stamp}, days, bars) - want) < 1e-9
    assert F.pool_control({days[4]: ["UP", "FLAT"]}, {days[4]: f"{days[50]}T08:45:00-05:00"}, days, bars) is None    # window still open
    assert F.pool_control({days[4]: ["UP", "GONE", "MISSING"]}, {days[4]: stamp}, days, bars) is None               # too few names priced


def _series(n=300, step=0.001, start=100.0):
    return [start * (1 + step) ** i for i in range(n)]


def test_price_facts_on_a_steady_climb():
    c, spy = _series(), _series(step=0.0005)
    p = F.price_facts(c, spy)
    assert abs(p["r1"] - (1.001 ** 21 - 1)) < 1e-9 and p["dd"] == 0 and p["off_high"] == 0 and p["rsi"] == 100 and p["vol"] < 1e-6
    assert p["v50"] > 0 and p["v200"] > p["v50"] and p["r3_vs_spy"] > 0
    assert F.price_facts(c[:200], spy[:200]) is None                     # under a year of history: no sheet


def _bars(days, closes):
    return {d: (c, c) for d, c in zip(days, closes)}


def test_sheet_ignores_prices_on_and_after_the_run_date():
    d0 = date(2025, 6, 2)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(300)]
    asof = days[280]
    flat = [100.0] * 300
    jump = [100.0] * 280 + [500.0] * 20                                  # the jump happens on the run date itself
    bars = {"SPY": _bars(days, flat), "AAA": _bars(days, jump), "BBB": _bars(days[:270], flat[:270])}
    fx = _facts(last_end="2026-01-31", filed="2026-02-20")
    sheet = F.build_sheet(["AAA,BBB: big news", "AAA: more news", "ZZZ: no data"], asof, bars, lambda s: fx)
    assert list(sheet) == ["AAA"]                                        # BBB stopped trading before the run; ZZZ has no bars
    assert "1m +0%" in sheet["AAA"] and "P/E 25.0" in sheet["AAA"] and "NEWS: big news / more news" in sheet["AAA"]
    assert F.build_sheet(["AAA: news"], asof, bars, lambda s: None) == {}


def test_parse_scores_and_the_half_answered_rule():
    pool = ["AAA", "BBB", "CCC", "DDD"]
    a = F.parse_scores('ok {"scores": {"aaa": 90, "BBB": "70", "CCC": 150, "XXX": 50, "DDD": null}}', set(pool))
    assert a == {"AAA": 90.0, "BBB": 70.0}
    assert F.parse_scores("no", set(pool)) == {} and F.parse_scores('{"scores": [1]}', set(pool)) == {}
    g = {"AAA": 50.0, "BBB": 70.0, "CCC": 10.0}
    assert F.combine({"agy": a, "grok": g}, pool) == [("AAA", 70.0), ("BBB", 70.0)]     # names both scored; tie -> alphabetical
    assert F.combine({"agy": {"AAA": 90.0}, "grok": g}, pool) == []                     # one agent answered under half the sheet
    order = F.choose([("AAA", 70.0), ("BBB", 60.0), ("CCC", 10.0)], pool, "2026-11-16")
    assert order["facts"] == ["AAA", "BBB", "CCC"] and order["facts_avoid"] == ["CCC", "BBB", "AAA"]
    assert sorted(order["facts_random"]) == pool and order == F.choose([("AAA", 70.0), ("BBB", 60.0), ("CCC", 10.0)], pool, "2026-11-16")


UNI = [f"C{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(80)]


def test_record_writes_three_committed_ledgers(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HOME", tmp_path)
    pool = UNI[:10]
    scores = {"agy": {s: float(i * 10) for i, s in enumerate(pool)}, "grok": {s: float(i * 10) for i, s in enumerate(pool)}}
    ranked = F.combine(scores, pool)
    now = datetime(2026, 11, 16, 8, 45, tzinfo=ET)
    done = F.record(F.choose(ranked, pool, "2026-11-16"), ranked, scores, now=now, extra={"pool": 10, "models": {}}, universe=UNI)
    assert done["facts"] == pool[:-4:-1] and done["facts_avoid"] == pool[:3] and len(done["facts_random"]) == 3
    for led in F.LEDGERS:
        rows = P.read_ledger(A.ledger_path(led))
        assert P.chain_problem(rows) is None and all(r["source"] == led and len(r["controls"]) == P.N_CONTROL for r in rows)
        n = subprocess.run(["git", "-C", str(tmp_path / led), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
        assert n == "3"
    again = F.record(F.choose(ranked, pool, "2026-11-23"), ranked, scores, now=now + timedelta(days=7), extra={"pool": 10}, universe=UNI)
    assert again["facts"] == pool[-4:-7:-1]                              # last week's names are skipped for 92 days


def test_replay_builds_three_lines_of_picks_from_fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HOME", tmp_path)
    monkeypatch.setattr(P, "frozen_universe", lambda path=None: UNI)
    pool = UNI[:40]
    mondays = A.replay_mondays()
    d0 = (mondays[0] - timedelta(days=420)).date()
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(520)]
    bars = {s: _bars(days, _series(520, 0.0002 * (k + 1))) for k, s in enumerate(["SPY", *pool])}
    fx = _facts(last_end="2026-03-31", filed="2026-05-01")
    heads = [f"{s}: something happened" for s in pool]
    asked = []

    def ask(agent, prompt):
        asked.append(agent)
        assert "analyst price targets" in prompt and "FACT SHEET" in prompt
        return json.dumps({"scores": {s: i for i, s in enumerate(pool)}}), "m"

    lines = F.replay_collect(ask=ask, heads_for=lambda mon: heads, bars=bars, facts_for=lambda s: fx)
    assert len(asked) == 2 * len(mondays) and all(len(v) == 3 * len(mondays) for v in lines.values())
    assert [r["symbol"] for r in lines["facts"][:3]] == pool[:-4:-1] and [r["symbol"] for r in lines["facts_avoid"][:3]] == pool[:3]
    assert len({r["symbol"] for r in lines["facts"]}) == len(lines["facts"])            # no repeat inside 92 days
    F.replay_collect(ask=lambda a, p: 1 / 0, heads_for=lambda mon: heads, bars=bars, facts_for=lambda s: fx)   # resumable: saved replies are reused
