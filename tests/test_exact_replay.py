"""Record -> exact replay must reproduce the desk's own decisions.

The regression guard for the replay foundation. A fresh process runs the
real desk code (sync, paint, poll) with desk_io recording live, against a
scripted dashboard; a second fresh process replays that recording with
tools/replay_session.py --exact. The replay must serve every read (0 misses),
run without errors and agree with the recorded decisions on every check.

Both run in a throwaway copy of the working tree: exact mode rewrites
config/bot_config.json in its tree, which must never be the real checkout.
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

RECORD = r'''
import json, os, sys, time, io
sys.path.insert(0, os.getcwd())
import desk_io
desk_io.install_live()
import ai_entry_watch as ew, session_recorder, config

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

n = {"i": 0}
def fake_urlopen(url, *, data=None, method=None):
    n["i"] += 1
    px = 25.0 + 0.01 * n["i"]
    payload = {"tickers": [
        {"ticker": "AAAA", "price": px, "price_age_sec": 1.0, "pct_change": 4.2, "src": "book"},
        {"ticker": "BBBB", "price": 41.0, "price_age_sec": 30.0, "pct_change": -1.0, "src": "book"}],
        "engine": {"updated": "x"}, "ai_positions": {"account": {"equity": 50000.0}},
        "claude_positions": {"big": "echo"}}
    return Resp(json.dumps(payload).encode())

ew._dash_urlopen = fake_urlopen
cfg = config.load_config()
session_recorder.record_config_snap(cfg, git_sha="test")
for _ in range(6):
    ew._DASH_CACHE = (0.0, {})
    t = time.time()
    ew.sync_watch_from_source_panels(cfg, now=t)
    ew.book_table_rows(positions={}, watch_rows=ew.public_snapshot())
    ew.poll_once(cfg=cfg, now=time.time())
    time.sleep(0.05)
session_recorder.flush(force=True)
print("RECORDED", json.dumps(desk_io.report()))
'''


def _copy_tree(dst):
    files = subprocess.check_output(["git", "-C", ROOT, "ls-files", "-z"]).decode().split("\0")
    for rel in files:
        if not rel:
            continue
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src):
            continue
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)
    for rel in ("desk_io.py", "tests/test_exact_replay.py"):  # may be untracked yet
        shutil.copy2(os.path.join(ROOT, rel), os.path.join(dst, rel))
    shutil.copy2(os.path.join(ROOT, "tools", "replay_session.py"),
                 os.path.join(dst, "tools", "replay_session.py"))


@pytest.mark.timeout(300) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_record_then_exact_replay_agrees(tmp_path):
    tree = tmp_path / "tree"
    rep = tmp_path / "reports"
    rep.mkdir()
    _copy_tree(str(tree))
    env = dict(os.environ, AI_REPORT_DIR=str(rep), PYTHONDONTWRITEBYTECODE="1")
    env.pop("REPLAY_EXPORTED", None)
    t0 = datetime.now(ET)
    r = subprocess.run([sys.executable, "-c", RECORD], cwd=tree, env=env,
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "RECORDED" in r.stdout, r.stdout[-2000:]
    day = t0.strftime("%Y-%m-%d")
    sess = rep / "sessions" / day
    wire_rows = [json.loads(l) for l in gzip.open(sess / "wire.jsonl.gz", "rt")]
    assert any(w.get("ch") == "boot" for w in wire_rows)
    assert any(w.get("ch") == "dash" for w in wire_rows)
    polls = [json.loads(l) for l in gzip.open(sess / "decisions.jsonl.gz", "rt")]
    assert sum(1 for p in polls if p.get("ev") == "poll") == 6

    t1 = datetime.now(ET)
    start = t0.replace(second=0, microsecond=0).strftime("%H:%M")
    end_m = t1.replace(second=0, microsecond=0)
    end = (end_m.replace(minute=end_m.minute) if end_m.minute == 59
           else end_m.replace(minute=end_m.minute + 1)).strftime("%H:%M")
    out = tmp_path / "exact.json"
    env2 = dict(env, REPLAY_EXPORTED="1", REPLAY_REPO=str(tree), REPLAY_SHA="worktree",
                REPLAY_WORK=str(tmp_path), AI_REPORT_DIR=str(tmp_path / "replay_reports"))
    (tmp_path / "replay_reports").mkdir()
    r2 = subprocess.run([sys.executable, "-u", "tools/replay_session.py", "--exact",
                         "--day", day, "--start", start, "--end", end,
                         "--sessions", str(sess), "--out", str(out)],
                        cwd=tree, env=env2, capture_output=True, text=True, timeout=240)
    assert out.exists(), r2.stdout[-3000:] + r2.stderr[-3000:]
    res = json.loads(out.read_text())
    print("exact:", {k: res.get(k) for k in ("polls", "checks", "agree", "arm_pass_mismatch",
                                               "live_buys", "replay_entries")},
          "served:", (res.get("io") or {}).get("served"), "wall:", res.get("wall_sec"))
    missed = (res.get("io") or {}).get("missed") or {}
    assert not missed, (missed, (res.get("io") or {}).get("miss_callers"), r2.stdout[-2000:])
    assert not res.get("errors"), res.get("errors")
    assert res["polls"] == 6 and res["arm_pass_mismatch"] == 0, res
    assert res["checks"] == 0 or res["decision_agreement"] == 1.0, res["first_divergence"]
    assert (res.get("io") or {}).get("served", {}).get("dash /api/state", 0) > 0


def test_exact_score_counts_agreement_divergence_and_buys(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import replay_session as rs
    wire = tmp_path / "wire.jsonl.gz"
    with gzip.open(wire, "wt") as f:
        for ts, sym in ((101.0, "AAA"), (201.0, "BBB")):
            f.write(json.dumps({"ts": ts, "ch": "alpaca", "m": "POST",
                                "p": "https://paper-api.alpaca.markets/v2/orders",
                                "q": {"symbol": sym, "side": "buy"}}) + "\n")
    events = [(100.0, "sync"), (100.0, "poll"), (200.0, "poll"), (300.0, "poll")]
    live = {100.0: [{"s": "AAA", "st": "watching", "b": None},
                    {"s": "BBB", "st": "watching", "b": "wait_mid_rise"}],
            200.0: [{"s": "BBB", "st": "watching", "b": "stale_quote"}]}
    rep = {100.0: [{"s": "AAA", "st": "watching", "b": None},
                   {"s": "BBB", "st": "watching", "b": "wait_mid_rise"}],
           200.0: [{"s": "BBB", "st": "watching", "b": "wait_mid_rise"},
                   {"s": "CCC", "st": "watching", "b": None}],
           300.0: [{"s": "AAA", "st": "watching", "b": None}]}
    o = rs.exact_score(events, live, rep, [(101.5, "AAA"), (230.0, "BBB")], wire, 0.0, 1e9)
    assert o["polls"] == 3
    assert o["arm_pass_mismatch"] == 1          # poll 300: only the replay reached it
    assert o["agree"] == 2                      # AAA and BBB at 100
    assert o["replay_only_names"] == 2          # CCC at 200, AAA at 300
    assert o["divergence_kinds"] == {"stale_quote -> wait_mid_rise": 1}
    assert o["first_divergence"]["BBB"]["live"]["b"] == "stale_quote"
    assert o["live_buys"] == 2 and o["buys_matched_5s"] == 1   # BBB entered 29 s late
    assert o["decision_agreement"] < 1.0
