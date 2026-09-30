"""overnight_book.build_snapshot — what the dashboard's Overnight book strip reads."""
from datetime import datetime
from zoneinfo import ZoneInfo

import overnight_book as ob

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
PLAN = {"day": "2026-09-30", "data_through": "2026-09-29", "n_liquid": 1421,
        "picks": [{"sym": "AAA", "mom": 2.5, "adv20": 9e7, "last_close": 50.0},
                  {"sym": "BBB", "mom": 1.5, "adv20": 8e7, "last_close": 20.0}]}


def test_empty_book_shows_the_plan_and_no_score():
    snap = ob.build_snapshot(PLAN, [], [], [], {}, {}, None, NOW)
    assert snap["book_night"] is None
    assert snap["rows"] == []
    assert snap["totals"] is None
    assert [p["sym"] for p in snap["plan"]["picks"]] == ["AAA", "BBB"]
    assert snap["backtest_bp"] == ob.BACKTEST_BP


def test_book_pairs_buy_and_sell_fills_for_the_latest_night():
    ledger = [
        # an older night must not leak into the book
        {"event": "submit", "night": "2026-09-29", "sym": "OLD", "side": "buy", "qty": 3},
        {"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 20, "ref_price": 50.0},
        {"event": "submit", "night": "2026-09-30", "sym": "BBB", "side": "buy", "qty": 50, "ref_price": 20.0},
        {"event": "fill", "day": "2026-09-30", "leg": "buy", "sym": "AAA", "status": "filled",
         "fill": 50.0, "cross": 50.0, "fill_vs_cross_bp": 0.0, "filled_qty": 20},
        {"event": "fill", "day": "2026-10-01", "leg": "sell", "sym": "AAA", "status": "filled",
         "fill": 50.5, "cross": 50.5, "fill_vs_cross_bp": 0.0},
    ]
    snap = ob.build_snapshot(PLAN, ledger, [], [], {}, {}, None, NOW)
    assert snap["book_night"] == "2026-09-30"
    by = {r["sym"]: r for r in snap["rows"]}
    assert set(by) == {"AAA", "BBB"}
    assert abs(by["AAA"]["night_bp"] - 100.0) < 1e-9  # 50.0 -> 50.5
    assert by["BBB"]["night_bp"] is None           # MOC still pending
    assert by["BBB"]["qty"] == 50


def test_positions_are_attached_and_counted():
    ledger = [{"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 20}]
    pos = [{"symbol": "AAA", "qty": 20.0, "avg_entry_price": 50.0, "market_value": 1004.0}]
    snap = ob.build_snapshot(PLAN, ledger, [], pos, {"equity": 25000.0}, {}, None, NOW)
    assert snap["holding"] == 1
    assert snap["rows"][0]["held_qty"] == 20.0
    assert snap["account"]["equity"] == 25000.0


def test_totals_average_the_scored_nights_and_list_newest_first():
    nights = [
        {"night_end": "2026-10-01", "names": 20, "mean_bp_fills": 30.0, "pnl_usd": 55.0},
        {"night_end": "2026-10-02", "names": 20, "mean_bp_fills": -10.0, "pnl_usd": -20.0},
        {"night_end": "2026-10-03", "names": 0, "mean_bp_fills": None, "pnl_usd": 0.0},
    ]
    snap = ob.build_snapshot(PLAN, [], nights, [], {}, {}, None, NOW)
    t = snap["totals"]
    assert t["nights"] == 2
    assert t["mean_bp"] == 10.0
    assert t["pnl_usd"] == 35.0
    assert t["green"] == 1
    assert [n["night_end"] for n in snap["nights"]] == ["2026-10-02", "2026-10-01"]


def test_only_error_steps_are_surfaced():
    state = {"plan": "ok 06:30", "sell": "error boom", "buy": "skipped late at 15:55"}
    snap = ob.build_snapshot(PLAN, [], [], [], {}, state, {"name": "buy", "at": 1.0}, NOW)
    assert snap["errors"] == {"sell": "error boom"}
    assert snap["next_step"]["name"] == "buy"


# ── scoring on the plan, not on paper fills ──────────────────────────────────

def test_score_plan_uses_every_planned_name_at_the_crosses():
    close = {"AAA": (50.0, 900), "BBB": (20.0, 500), "CCC": (10.0, 100)}
    opn = {"AAA": (50.5, 800), "BBB": (19.8, 400)}          # CCC: no opening print
    s = ob.score_plan(["AAA", "BBB", "CCC"], close, opn)
    assert s["n_plan"] == 3 and s["n_scored"] == 2
    assert s["missing"] == ["CCC"]
    assert abs(s["mean_bp_plan"] - 0.0) < 1e-9             # +100 and -100 bp
    assert s["pnl_plan_usd"] == 0.0
    assert s["per_name_bp"] == {"AAA": 100.0, "BBB": -100.0}


def test_score_plan_with_no_crosses_is_unscored_not_zero():
    s = ob.score_plan(["AAA"], {}, {})
    assert s["mean_bp_plan"] is None and s["pnl_plan_usd"] is None


def _isolate_out(monkeypatch, tmp_path):
    monkeypatch.setattr(ob, "OUT", tmp_path)
    monkeypatch.setattr(ob, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ob, "NIGHTS", tmp_path / "nights.jsonl")
    monkeypatch.setattr(ob, "LOG", tmp_path / "run.log")


def test_night_is_scored_on_the_plan_even_when_paper_filled_one_name(monkeypatch, tmp_path):
    import json
    from datetime import date
    _isolate_out(monkeypatch, tmp_path)
    (tmp_path / "plan_2026-09-30.json").write_text(json.dumps(PLAN))
    # paper filled only AAA, and at the quote rather than the cross
    (tmp_path / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"event": "fill", "day": "2026-09-30", "leg": "buy", "sym": "AAA", "fill": 50.02,
         "cross": 50.0, "filled_qty": 20},
        {"event": "fill", "day": "2026-10-01", "leg": "sell", "sym": "AAA", "fill": 50.48,
         "cross": 50.5, "filled_qty": 20},
    ]))
    cx = {("close", "2026-09-30"): {"AAA": (50.0, 1), "BBB": (20.0, 1)},
          ("open", "2026-10-01"): {"AAA": (50.5, 1), "BBB": (20.4, 1)}}
    fetch = lambda syms, day, leg: cx[(leg, day.isoformat())]
    ob.night_summary(date(2026, 10, 1), fetch=fetch)
    ob.night_summary(date(2026, 10, 1), fetch=fetch)       # a re-score replaces, not appends
    rows = [json.loads(x) for x in (tmp_path / "nights.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    n = rows[0]
    assert n["plan_day"] == "2026-09-30" and n["n_scored"] == 2
    assert abs(n["mean_bp_plan"] - 150.0) < 1e-9           # +100 and +200 bp
    assert n["pnl_plan_usd"] == 30.0                       # $10 + $20 at $1,000 each
    assert n["names"] == 1 and n["pnl_usd"] == 9.2         # paper: AAA only, 20 x 0.46


def test_totals_headline_the_plan_and_keep_paper_beside_it():
    nights = [
        {"night_end": "2026-10-01", "mean_bp_plan": 20.0, "pnl_plan_usd": 40.0,
         "mean_bp_fills": -5.0, "pnl_usd": -3.0},
        {"night_end": "2026-10-02", "mean_bp_plan": None, "mean_bp_fills": None, "pnl_usd": 0.0},
    ]
    t = ob.build_snapshot(PLAN, [], nights, [], {}, {}, None, NOW)["totals"]
    assert t["nights"] == 1 and t["mean_bp"] == 20.0
    assert t["pnl_usd"] == 40.0 and t["paper_pnl_usd"] == -3.0


def test_account_carries_what_the_pl_line_needs():
    acct = {"equity": 25014.08, "cash": 17646.02, "last_equity": 25000.0}
    a = ob.build_snapshot(PLAN, [], [], [], acct, {}, None, NOW)["account"]
    assert a["last_equity"] == 25000.0
    assert a["start_equity"] == ob.START_EQUITY


# ── market mode: every name filled on paper ──────────────────────────────────

import types  # noqa: E402

OP = datetime(2026, 10, 1, 9, 30, tzinfo=ET)
CL = datetime(2026, 10, 1, 16, 0, tzinfo=ET)


def _o(sym, status, qty, filled=0, px=None, cid=None):
    return types.SimpleNamespace(symbol=sym, status=status, qty=qty, filled_qty=filled,
                                 filled_avg_price=px, filled_at=None, client_order_id=cid or f"on-x-{sym}-buy")


def test_market_schedule_buys_two_minutes_before_the_close_with_a_topup():
    s = dict(ob.schedule(OP, CL, "market"))
    assert s["buy"].strftime("%H:%M:%S") == "15:58:00"
    assert s["buy_topup"].strftime("%H:%M:%S") == "15:59:30"
    assert s["sell"].strftime("%H:%M") == "09:31" and s["sell_topup"].strftime("%H:%M") == "09:33"
    a = dict(ob.schedule(OP, CL, "auction"))
    assert a["buy"].strftime("%H:%M") == "15:40" and "buy_topup" not in a


def test_topup_resends_only_finished_short_names():
    targets = {"AAA": 10, "BBB": 5, "CCC": 7, "DDD": 3}
    orders = [
        _o("AAA", "expired", 10, filled=4, px=50.0),              # short, done -> +6
        _o("BBB", "filled", 5, filled=5, px=20.0),                # full
        _o("CCC", "partially_filled", 7, filled=2, px=9.0),       # still working -> wait
    ]                                                             # DDD: never placed -> +3
    need = ob.topup_needs(targets, orders)
    assert need == {"AAA": (6, 1), "DDD": (3, 0)}


def test_aggregate_joins_first_order_and_topup_at_vwap():
    orders = [_o("AAA", "expired", 10, filled=4, px=50.0, cid="on-d-AAA-buy"),
              _o("AAA", "filled", 6, filled=6, px=51.0, cid="on-d-AAA-buy-r1")]
    a = ob.aggregate_orders(orders)["AAA"]
    assert a["filled_qty"] == 10 and a["orders"] == 2 and a["status"] == "filled"
    assert abs(a["fill"] - 50.6) < 1e-9


class _FakeTC:
    def __init__(self, positions):
        self.positions, self.submitted, self.cancelled = positions, [], 0

    def cancel_orders(self):
        self.cancelled += 1

    def get_all_positions(self):
        return self.positions

    def submit_order(self, req):
        self.submitted.append(req)
        return types.SimpleNamespace(id="x")


def _isolate_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(ob, "OUT", tmp_path)
    monkeypatch.setattr(ob, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ob, "LOG", tmp_path / "run.log")


def test_sell_topup_sizes_off_qty_available_and_never_cancels(monkeypatch, tmp_path):
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "ORDER_MODE", "market")
    pos = [types.SimpleNamespace(symbol="AAA", qty="10", qty_available="4", side="long"),
           types.SimpleNamespace(symbol="BBB", qty="5", qty_available="0", side="long")]  # all held by a working sell
    tc = _FakeTC(pos)
    ob.sell(tc, date(2026, 10, 1), attempt=1)
    assert tc.cancelled == 0
    assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 4)]
    assert tc.submitted[0].client_order_id.endswith("-sell-r1")
    assert str(tc.submitted[0].time_in_force.value).lower() == "day"


def test_buy_uses_day_market_orders_in_market_mode_and_cls_in_auction(monkeypatch, tmp_path):
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "load_plan", lambda d: PLAN)
    monkeypatch.setattr(ob, "latest_prices", lambda syms: {"AAA": 50.0, "BBB": 20.0})
    for mode, tif in (("market", "day"), ("auction", "cls")):
        monkeypatch.setattr(ob, "ORDER_MODE", mode)
        tc = _FakeTC([])
        ob.buy(tc, date(2026, 10, 1))
        assert {str(r.time_in_force.value).lower() for r in tc.submitted} == {tif}
        assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 20), ("BBB", 50)]
