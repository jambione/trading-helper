"""exit_arm_score grades the exit-cost A/B: exit fill vs SIP mid at the trail hit, mid vs market."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import exit_arm_score as xas  # noqa: E402

T0 = 1_791_040_000.0


def _setup(tmp_path, monkeypatch, rows, sells=()):
    rep = tmp_path / "ai_reports"
    (rep / "fills").mkdir(parents=True)
    day = xas.bars.day_of(T0)
    (rep / "outcomes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (rep / "fills" / f"{day}.jsonl").write_text("".join(json.dumps(
        {"event": "submit", "action": "SELL_LIMIT", "order_id": f"o{i}", "symbol": s, "ts": t}) + "\n"
        for i, (s, t) in enumerate(sells)))
    monkeypatch.setattr(xas, "ROOT", str(tmp_path))
    monkeypatch.setattr(xas, "CACHE", str(rep / "cache.json"))
    monkeypatch.setattr(xas.bars, "client", lambda: object())
    monkeypatch.setattr(xas.time, "sleep", lambda s: None)
    asked = []
    monkeypatch.setattr(xas.xr, "nbbo_at", lambda cl, sym, dt: asked.append((sym, dt.timestamp())) or (9.98, 10.02))
    monkeypatch.setattr(sys, "argv", ["exit_arm_score", "--from", day, "--to", day])
    return asked, day


def _row(sym, arm, exit_price, **xt):
    return {"symbol": sym, "exit_time": T0 + 12, "exit_price": exit_price,
            "exit_test": {"arm": arm, **xt}}


def test_prices_both_arms_at_t_decide_and_reports_the_difference(tmp_path, monkeypatch, capsys):
    asked, day = _setup(tmp_path, monkeypatch, [
        _row("AAA", "market", 9.98, t_decide=T0),                                   # sold at the bid
        _row("BBB", "mid", 10.00, t_decide=T0 + 1, fill=10.00, crossed_qty=0.0),    # rested at the mid
    ])
    xas.main()
    assert asked == [("AAA", T0), ("BBB", T0 + 1)]
    out = capsys.readouterr().out
    assert "2 trades, 1 day(s)" in out
    rows = xas.score(xas.outcomes(day, day), lambda s, t: (9.98, 10.02))
    by = {x["arm"]: x for x in rows}
    assert abs(by["market"]["cost"] - 20.0) < 1e-6 and abs(by["mid"]["cost"]) < 1e-6
    assert by["mid"]["passive"] is True and by["market"]["passive"] is False


def test_old_outcome_falls_back_to_the_first_sell_submit(tmp_path, monkeypatch):
    asked, _day = _setup(tmp_path, monkeypatch, [_row("AAA", "mid", 10.0)],
                         sells=[("AAA", T0 + 2), ("AAA", T0 + 11)])
    xas.main()
    assert asked == [("AAA", T0 + 2)]
