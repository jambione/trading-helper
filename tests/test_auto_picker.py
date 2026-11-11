"""tools/auto_picker.py: parse a model's reply, record at most three picks a week in the agent's own git ledger."""
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import auto_picker as A
import pick_ledger as P

ET = ZoneInfo("America/New_York")
UNI = [f"C{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(80)] + ["GOOD", "FINE", "OKAY", "MORE", "LAST"]
REPLY = '''Sure. {"picks": [
 {"sym": "GOOD", "thesis": "backlog doubled on the new contract", "months": 6, "sell_if": "the contract is cancelled"},
 {"sym": "TQQQ", "thesis": "leveraged index fund goes up a lot", "months": 6, "sell_if": "the market falls"},
 {"sym": "good", "thesis": "the same name again in lower case", "months": 6, "sell_if": "anything at all"},
 {"sym": "FINE", "thesis": "short", "months": 6, "sell_if": "x"},
 {"sym": "OKAY", "thesis": "margins recover as input costs fall", "months": 40, "sell_if": "costs rise again"},
 {"sym": "MORE", "thesis": "new product ramps through next year", "months": "soon", "sell_if": "the launch slips"},
 {"sym": "LAST", "thesis": "share buyback at a low valuation", "months": 9, "sell_if": "buyback is halted"}]}'''


def test_parse_keeps_only_listed_symbols_with_a_real_thesis():
    p = A.parse(REPLY, set(UNI))
    assert [x["sym"] for x in p] == ["GOOD", "OKAY", "MORE", "LAST"]          # off-list fund, duplicate and thin thesis dropped
    assert p[1]["months"] == 12.0 and p[2]["months"] == 6.0                    # clamped; unreadable -> 6
    assert A.parse("I cannot help with that", set(UNI)) == [] and A.parse('{"picks": "none"}', set(UNI)) == []


def test_record_takes_three_commits_each_and_skips_a_recent_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HOME", tmp_path)
    A.ensure_repo("agy")
    now = datetime(2026, 11, 16, 8, 30, tzinfo=ET)
    done = A.record("agy", A.parse(REPLY, set(UNI)), model="m1", prompt_sha="abc", n_heads=100, now=now, universe=UNI)
    assert [r["symbol"] for r in done] == ["GOOD", "OKAY", "MORE"] and done[0]["source"] == "agy" and done[0]["model"] == "m1"
    led = A.ledger_path("agy")
    assert P.chain_problem(P.read_ledger(led)) is None
    n = subprocess.run(["git", "-C", str(led.parent), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert n == "3"
    assert P.first_commit_ts(done[0]["id"], led) is not None
    assert A.this_week_done("agy", now) and not A.this_week_done("grok", now)
    nxt = A.record("agy", A.parse(REPLY, set(UNI)), model="m1", prompt_sha="abc", n_heads=100, now=datetime(2026, 11, 23, 8, 30, tzinfo=ET), universe=UNI)
    assert [r["symbol"] for r in nxt] == ["LAST"]                              # the three recent names are skipped, not re-recorded


def test_prompt_forbids_tools_and_does_not_paste_the_allowed_list():
    a = A.build_prompt("DATE", ["GOOD: backlog doubles"], UNI, ["OKAY"])
    assert "Do NOT run commands" in a and "No money is traded" in a and "Do not repeat these recent picks: OKAY" in a
    assert "GOOD: backlog doubles" in a and " ".join(UNI[:5]) not in a          # the 1,931-symbol list made AGY reach for a tool
    assert A.build_prompt("DATE", [], UNI, []) == A.build_prompt("DATE", [], [], [])


def test_replay_mondays_are_twelve_past_mondays_in_four_months():
    m = A.replay_mondays()
    assert len(m) == 12 and all(x.weekday() == 0 for x in m) and m[0].strftime("%Y-%m-%d") == "2026-07-27" and m[-1].strftime("%Y-%m-%d") == "2026-10-12"
    assert len({x.strftime("%Y-%m") for x in m}) == 4


def test_replay_collects_three_a_week_skips_repeats_and_adds_a_random_control(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HOME", tmp_path)
    monkeypatch.setattr(P, "frozen_universe", lambda path=None: UNI)
    asked = []

    def ask(agent, prompt):
        asked.append((agent, prompt[:20]))
        return REPLY, "m"
    heads = [f"{s}: something happened at {s}" for s in UNI[:40]]
    lines = A.replay_collect(ask=ask, heads_for=lambda mon: heads)
    assert [r["symbol"] for r in lines["agy"][:4]] == ["GOOD", "OKAY", "MORE", "LAST"]     # week 1 takes three, week 2 the one left
    assert len(lines["agy"]) == 4 and len(lines["random"]) == 36                             # only 4 distinct ideas; the control fills every week
    assert all(len(r["controls"]) == 30 and r["source"].startswith("replay_") for r in lines["grok"])
    assert lines["agy"][0]["recorded_at"].startswith("2026-07-27T08:30")
    n = len(asked)
    A.replay_collect(ask=ask, heads_for=lambda mon: heads)                                    # resumable: saved replies are reused
    assert len(asked) == n
