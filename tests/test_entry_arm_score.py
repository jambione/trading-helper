"""entry_arm_score prices every arm at the decision, not at a submit the wait arm delayed.

Review 2026-10-03: the wait arm's first buy goes out up to ai_entry_test_cross_sec after the
decision, so benchmarking at the submit left the move during the wait out of its cost.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import entry_arm_score as eas  # noqa: E402

T0 = 1_791_040_000.0   # 2026-10-02 RTH-ish; only the day bucket matters


def _setup(tmp_path, monkeypatch, entry_test):
    rep = tmp_path / "ai_reports"
    (rep / "fills").mkdir(parents=True)
    day = eas.bars.day_of(T0)
    (rep / "outcomes.jsonl").write_text(json.dumps({
        "symbol": "XYZ", "entry_time": T0 + 9, "entry_price": 10.02, "exit_price": 10.10,
        "entry_test": entry_test}) + "\n")
    # The wait arm's only buy submit lands 8 s after the decision.
    (rep / "fills" / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event": "submit", "action": "BUY", "order_id": "M", "symbol": "XYZ", "ts": T0 + 8},
        {"event": "fill", "order_id": "M", "type": "market", "filled_qty": 100},
    ]) + "\n")
    monkeypatch.setattr(eas, "ROOT", str(tmp_path))
    monkeypatch.setattr(eas, "CACHE", str(rep / "cache.json"))
    monkeypatch.setattr(eas.bars, "client", lambda: object())
    monkeypatch.setattr(eas.time, "sleep", lambda s: None)
    asked = []
    monkeypatch.setattr(eas.xr, "nbbo_at", lambda cl, sym, dt: asked.append(dt.timestamp()) or (10.0, 10.02))
    monkeypatch.setattr(sys, "argv", ["entry_arm_score", "--from", day, "--to", day])
    return asked


def test_wait_arm_is_priced_at_t_decide(tmp_path, monkeypatch):
    asked = _setup(tmp_path, monkeypatch, {"arm": "wait", "t_decide": T0, "waited_sec": 8.0})
    eas.main()
    assert asked == [T0]


def test_old_wait_outcome_backs_the_wait_out_of_the_submit(tmp_path, monkeypatch):
    asked = _setup(tmp_path, monkeypatch, {"arm": "wait", "waited_sec": 8.0})
    eas.main()
    assert asked == [T0]


def test_passive_arm_without_t_decide_keeps_its_first_submit():
    # A passive arm's first submit is its limit, sent at the decision; waited_sec is its rest.
    assert eas.ref_time({"arm": "bid", "waited_sec": 10.0}, T0 + 8) == T0 + 8
