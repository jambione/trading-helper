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


def test_errors_missed_windows_and_alerts_are_surfaced_but_not_disarmed_skips():
    # 2026-10-03 skeptic review #1: a step skipped late is a problem (a missed
    # sell holds the book through the day), so the dashboard shows it; a
    # deliberate disarmed skip is not.
    state = {"plan": "ok 06:30", "sell": "error boom", "sell_topup": "skipped late at 09:50",
             "buy": "skipped: live not armed at 15:40",
             "sell_catchup": "ALERT 3 names held past the open, sold 09:50"}
    snap = ob.build_snapshot(PLAN, [], [], [], {}, state, {"name": "buy", "at": 1.0}, NOW)
    assert snap["errors"] == {"sell": "error boom", "sell_topup": "skipped late at 09:50",
                              "sell_catchup": "ALERT 3 names held past the open, sold 09:50"}
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
    assert a["buy_check"].strftime("%H:%M") == "15:45"         # before the 15:50 MOC cutoff
    assert a["buy_fallback"].strftime("%H:%M") == "15:58"
    assert a["sell_check"].strftime("%H:%M") == "09:25"        # before the 09:28 MOO cutoff
    assert a["sell_fallback"].strftime("%H:%M") == "09:31"


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

    def get_account(self):
        return types.SimpleNamespace(cash="25000", equity="25000")

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
    monkeypatch.setattr(ob, "INTRADAY_MIN", None)
    monkeypatch.setattr(ob, "session_opens", lambda syms, day: {})
    for mode, tif in (("market", "day"), ("auction", "cls")):
        monkeypatch.setattr(ob, "ORDER_MODE", mode)
        tc = _FakeTC([])
        ob.buy(tc, date(2026, 10, 1))
        assert {str(r.time_in_force.value).lower() for r in tc.submitted} == {tif}
        # 98% of the $25,000 account split over 2 names
        assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 245), ("BBB", 612)]



# ── intraday filter: drop picks down > 1% open -> now ────────────────────────

def test_intraday_filter_drops_losers_and_keeps_unknowns():
    opens = {"AAA": 50.0, "BBB": 20.0, "CCC": 10.0}
    prices = {"AAA": 50.4, "BBB": 19.7, "CCC": 9.95}            # +0.8%, -1.5%, -0.5%
    kept, rows = ob.intraday_filter(["AAA", "BBB", "CCC", "DDD"], opens, prices, -0.01)
    assert kept == ["AAA", "CCC", "DDD"]                       # DDD: no open -> kept, marked
    by = {r["sym"]: r for r in rows}
    assert by["BBB"]["kept"] is False and abs(by["BBB"]["intraday"] + 0.015) < 1e-9
    assert by["DDD"]["unknown"] is True and by["DDD"]["kept"] is True
    assert ob.intraday_filter(["BBB"], opens, prices, None)[0] == ["BBB"]   # off


def test_buy_submits_only_the_names_that_pass(monkeypatch, tmp_path):
    import json
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "ORDER_MODE", "market")
    monkeypatch.setattr(ob, "INTRADAY_MIN", -0.01)
    monkeypatch.setattr(ob, "load_plan", lambda d: PLAN)
    monkeypatch.setattr(ob, "latest_prices", lambda syms: {"AAA": 50.0, "BBB": 19.0})
    monkeypatch.setattr(ob, "session_opens", lambda syms, day: {"AAA": 49.5, "BBB": 20.0})
    tc = _FakeTC([])
    ob.buy(tc, date(2026, 10, 1))
    assert [r.symbol for r in tc.submitted] == ["AAA"]
    flt = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()
           if json.loads(x).get("event") == "filter"]
    assert flt and flt[0]["kept"] == ["AAA"] and flt[0]["night"] == "2026-10-01"


def test_filter_off_buys_all_and_logs_the_shadow_drops(monkeypatch, tmp_path):
    import json
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "ORDER_MODE", "market")
    monkeypatch.setattr(ob, "INTRADAY_MIN", None)
    monkeypatch.setattr(ob, "load_plan", lambda d: PLAN)
    monkeypatch.setattr(ob, "latest_prices", lambda syms: {"AAA": 50.0, "BBB": 19.0})
    monkeypatch.setattr(ob, "session_opens", lambda syms, day: {"AAA": 49.5, "BBB": 20.0})
    tc = _FakeTC([])
    ob.buy(tc, date(2026, 10, 1))
    assert sorted(r.symbol for r in tc.submitted) == ["AAA", "BBB"]          # nothing dropped
    rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert not [r for r in rows if r.get("event") == "filter"]               # the book is the plan
    sh = [r for r in rows if r.get("event") == "filter_shadow"]
    assert sh and sh[0]["would_keep"] == ["AAA"] and sh[0]["floor"] == ob.SHADOW_INTRADAY_MIN


def test_night_headline_is_the_book_held_with_all_20_beside_it(monkeypatch, tmp_path):
    import json
    from datetime import date
    _isolate_out(monkeypatch, tmp_path)
    (tmp_path / "plan_2026-09-30.json").write_text(json.dumps(PLAN))
    (tmp_path / "ledger.jsonl").write_text(json.dumps(
        {"event": "filter", "night": "2026-09-30", "floor": -0.01, "kept": ["AAA"]}) + "\n")
    cx = {("close", "2026-09-30"): {"AAA": (50.0, 1), "BBB": (20.0, 1)},
          ("open", "2026-10-01"): {"AAA": (50.5, 1), "BBB": (19.6, 1)}}
    ob.night_summary(date(2026, 10, 1), fetch=lambda syms, day, leg: cx[(leg, day.isoformat())])
    n = json.loads((tmp_path / "nights.jsonl").read_text().splitlines()[0])
    assert n["n_book"] == 1 and abs(n["mean_bp_book"] - 100.0) < 1e-9
    assert abs(n["mean_bp_plan"] - (100.0 - 200.0) / 2) < 1e-9   # all 20 (here 2) beside it
    assert ob.night_bp(n) == n["mean_bp_book"] and ob.night_pnl(n) == n["pnl_book_usd"]



# ── auction-mode safety checks ───────────────────────────────────────────────

class _OrdersTC(_FakeTC):
    def __init__(self, orders, positions=()):
        super().__init__(list(positions))
        self.orders = orders

    def get_orders(self, req):
        return self.orders


def test_buy_check_resends_rejected_moc_as_moc_and_leaves_accepted_alone(monkeypatch, tmp_path):
    import json
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    (tmp_path / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"event": "submit", "night": "2026-10-01", "sym": "AAA", "side": "buy", "qty": 20,
         "client_order_id": "on-2026-10-01-AAA-buy"},
        {"event": "submit", "night": "2026-10-01", "sym": "BBB", "side": "buy", "qty": 50,
         "client_order_id": "on-2026-10-01-BBB-buy"},
        {"event": "submit", "night": "2026-10-01", "sym": "CCC", "side": "buy", "qty": 9,
         "client_order_id": "on-2026-10-01-CCC-buy", "error": "rate limited"},    # never reached Alpaca
    ]))
    tc = _OrdersTC([_o("AAA", "new", 20, cid="on-2026-10-01-AAA-buy"),           # accepted MOC, waiting
                    _o("BBB", "rejected", 50, cid="on-2026-10-01-BBB-buy")])
    ob.buy_topup(tc, date(2026, 10, 1), auction=True, tag="buy_check")
    sent = {r.symbol: (r.qty, str(r.time_in_force.value).lower()) for r in tc.submitted}
    assert sent == {"BBB": (50, "cls"), "CCC": (9, "cls")}


def test_sell_check_skips_positions_an_accepted_moo_already_covers(monkeypatch, tmp_path):
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    pos = [types.SimpleNamespace(symbol="AAA", qty="10", qty_available="0", side="long"),    # MOO accepted
           types.SimpleNamespace(symbol="BBB", qty="5", qty_available="5", side="long")]     # MOO rejected
    tc = _FakeTC(pos)
    ob.sell(tc, date(2026, 10, 1), attempt=1, tif="opg", tag="sell_check")
    assert tc.cancelled == 0
    assert [(r.symbol, r.qty, str(r.time_in_force.value).lower()) for r in tc.submitted] == [("BBB", 5, "opg")]


# ── paper P&L: leftover shares keep their own buy price ──────────────────────

def test_fifo_prices_leftover_shares_at_their_own_buy():
    ledger = [
        {"event": "fill", "day": "2026-10-01", "leg": "buy", "sym": "AAA", "fill": 50.0, "filled_qty": 10},
        {"event": "fill", "day": "2026-10-02", "leg": "sell", "sym": "AAA", "fill": 51.0, "filled_qty": 6},  # 4 left
        {"event": "fill", "day": "2026-10-02", "leg": "buy", "sym": "AAA", "fill": 60.0, "filled_qty": 10},
        {"event": "fill", "day": "2026-10-03", "leg": "sell", "sym": "AAA", "fill": 61.0, "filled_qty": 14},
        {"event": "fill", "day": "2026-10-03", "leg": "sell", "sym": "AAA", "fill": 61.0, "filled_qty": 14},  # re-run dup
    ]
    m = ob.fifo_matches(ledger, "2026-10-03")
    assert [(x["qty"], x["buy"], x["buy_day"]) for x in m] == [(4.0, 50.0, "2026-10-01"), (10.0, 60.0, "2026-10-02")]
    assert sum(x["qty"] * (x["sell"] - x["buy"]) for x in m) == 4 * 11 + 10 * 1     # not 14 x $1
    first = ob.fifo_matches(ledger, "2026-10-02")
    assert [(x["qty"], x["buy"]) for x in first] == [(6.0, 50.0)]


# ── live mode: off by default, hard caps, settlement ─────────────────────────

def test_live_is_off_by_default():
    assert ob.LIVE is False and ob.OUT.name == "overnight"


def test_live_client_refuses_without_the_enable_switch(monkeypatch):
    import pytest
    monkeypatch.delenv("OVERNIGHT_LIVE_ENABLE", raising=False)
    with pytest.raises(SystemExit, match="OVERNIGHT_LIVE_ENABLE"):
        ob.live_client()


def test_live_client_refuses_desk_or_paper_keys(monkeypatch):
    import pytest
    monkeypatch.setenv("OVERNIGHT_LIVE_ENABLE", "yes")
    monkeypatch.setattr(ob, "desk_keys", lambda: ("DESK", "x"))
    monkeypatch.setattr(ob, "overnight_keys", lambda: ("PAPER", "y"))
    monkeypatch.setattr(ob, "live_keys", lambda: ("PAPER", "y"))
    with pytest.raises(SystemExit, match="REFUSED"):
        ob.live_client()
    monkeypatch.setattr(ob, "live_keys", lambda: ("", ""))
    with pytest.raises(SystemExit, match="no live keys"):
        ob.live_client()


def test_size_live_whole_shares_under_every_cap():
    syms = ["SNDK", "IOVA", "SLS", "MU", "RLAY", "ERAS", "TWST"]
    px = {"SNDK": 1742.0, "IOVA": 14.8, "SLS": 12.4, "MU": 1065.0, "RLAY": 18.1, "ERAS": 14.7, "TWST": 193.0}
    picks = ob.size_live(syms, px, held={"ERAS"}, cash=1000.0, max_book=40.0, max_order=25.0, max_shares=1)
    assert picks == [("IOVA", 1, 14.8), ("SLS", 1, 12.4)]          # RLAY would pass $40 -> skipped
    assert sum(q * p for _s, q, p in picks) <= 40.0
    assert ob.size_live(syms, px, set(), cash=10.0, max_book=100.0, max_order=25.0, max_shares=1) == []  # cash binds


def test_live_skips_buying_on_a_day_it_sold():
    from datetime import date
    led = [{"event": "submit", "side": "sell", "night_end": "2026-10-05", "sym": "IOVA"}]
    assert ob.sold_today(led, date(2026, 10, 5)) is True
    assert ob.sold_today(led, date(2026, 10, 6)) is False
    assert ob.sold_today([dict(led[0], error="rejected")], date(2026, 10, 5)) is False


def test_live_cash_uses_buying_power_not_the_zero_non_marginable_field():
    acct = types.SimpleNamespace(buying_power="100", cash="100", non_marginable_buying_power="0")
    assert ob.live_cash(acct) == 100.0                      # the 2026-10-01 live account
    assert ob.live_cash(types.SimpleNamespace(buying_power="60", cash="100")) == 60.0
    assert ob.live_cash(types.SimpleNamespace(buying_power="0", cash="0")) == 0.0


def test_compare_lines_live_auction_up_with_paper_and_the_cross():
    paper = [{"event": "fill", "day": "2026-10-05", "leg": "buy", "sym": "IOVA", "fill": 14.62, "cross": 14.60,
              "qty": 68, "filled_qty": 68}]
    live = [{"event": "fill", "day": "2026-10-05", "leg": "buy", "sym": "IOVA", "fill": 14.60, "cross": 14.60,
             "qty": 1, "filled_qty": 1, "status": "filled"},
            {"event": "fill", "day": "2026-10-06", "leg": "sell", "sym": "IOVA", "fill": 14.70, "cross": 14.72,
             "qty": 1, "filled_qty": 0, "status": "expired"}]
    out = ob.compare_rows(paper, live)
    buy, sell = out["rows"]
    assert buy["full"] and abs(buy["live_vs_cross_bp"]) < 1e-9
    assert abs(buy["paper_vs_cross_bp"] - (14.62 / 14.60 - 1) * 1e4) < 1e-9     # paid more = positive
    assert sell["full"] is False                                              # expired, 0/1
    assert abs(sell["live_vs_cross_bp"] - (14.72 / 14.70 - 1) * 1e4) < 1e-9   # got less = positive
    assert out["totals"]["orders"] == 2 and out["totals"]["full"] == 1


def test_live_needs_the_arm_file_paper_never_does(monkeypatch, tmp_path):
    monkeypatch.setattr(ob, "ARMED_FILE", tmp_path / "overnight_live.armed")
    monkeypatch.setattr(ob, "LIVE", False)
    assert ob.live_armed() is True                       # paper is always armed
    monkeypatch.setattr(ob, "LIVE", True)
    assert ob.live_armed() is False                      # installed, disarmed
    (tmp_path / "overnight_live.armed").touch()
    assert ob.live_armed() is True                       # armed


def test_arm_file_with_a_start_date_arms_from_that_day(monkeypatch, tmp_path):
    from datetime import date
    f = tmp_path / "overnight_live.armed"
    monkeypatch.setattr(ob, "ARMED_FILE", f)
    monkeypatch.setattr(ob, "LIVE", True)
    f.write_text("2026-10-05\n")
    assert ob.live_armed(date(2026, 10, 2)) is False      # Friday: not yet
    assert ob.live_armed(date(2026, 10, 5)) is True       # Monday: armed
    f.write_text("10/5")                                  # unreadable: stay disarmed
    assert ob.live_armed(date(2026, 10, 6)) is False
    f.unlink()
    assert ob.live_armed(date(2026, 10, 6)) is False      # deleted: cancelled


def test_a_sold_night_gives_way_to_todays_plan():
    ledger = [
        {"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 20},
        {"event": "fill", "day": "2026-09-30", "leg": "buy", "sym": "AAA", "status": "filled",
         "fill": 50.0, "filled_qty": 20},
        {"event": "fill", "day": "2026-10-01", "leg": "sell", "sym": "AAA", "status": "filled", "fill": 50.5},
    ]
    today = dict(PLAN, day="2026-10-01")
    snap = ob.build_snapshot(today, ledger, [], [], {}, {}, None, NOW)
    assert snap["book_night"] is None and snap["rows"] == []
    assert snap["plan"]["day"] == "2026-10-01"
    # still holding (the sell has not gone in): last night stays on screen
    pos = [{"symbol": "AAA", "qty": 20.0}]
    snap = ob.build_snapshot(today, ledger, [], pos, {}, {}, None, NOW)
    assert snap["book_night"] == "2026-09-30"
    # same-day plan (before the next morning's 06:30): the bought book shows
    snap = ob.build_snapshot(PLAN, ledger, [], [], {}, {}, None, NOW)
    assert snap["book_night"] == "2026-09-30"


def test_paper_sizing_splits_the_budget_equally_in_whole_shares():
    px = {"AAA": 50.0, "BBB": 20.0, "CCC": 1500.0, "DDD": 2500.0}
    picks = ob.size_paper(["AAA", "BBB", "CCC", "DDD"], px, set(), 24500.0)
    got = {s: q for s, q, _ in picks}
    # DDD is over the one-share cap; 3 names share $24,500 -> ~$8,167 each.
    # CCC's 5 shares leave it $667 short; the leftover goes to AAA and BBB.
    assert got == {"AAA": 170, "BBB": 425, "CCC": 5}
    assert sum(q * p for _, q, p in picks) <= 24500.0


def test_paper_sizing_never_passes_the_budget_and_skips_held():
    px = {"BIG": 1800.0, "A": 10.0, "B": 10.0}
    picks = ob.size_paper(["BIG", "A", "B"], px, {"B"}, 2000.0)
    # BIG gets its one share even though it is over its $1,000 share; A fills what is left
    assert [(s, q) for s, q, _ in picks] == [("BIG", 1), ("A", 20)]
    assert sum(q * p for _, q, p in picks) <= 2000.0
    assert ob.size_paper(["A"], px, set(), 0.0) == []


class _BoomClient:
    def get_orders(self, *_a, **_k):
        raise RuntimeError("broker down")

    def get_account(self):
        raise RuntimeError("broker down")


def test_audits_never_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "OUT", tmp_path)
    monkeypatch.setattr(ob, "LOG", tmp_path / "run.log")
    monkeypatch.setattr(ob, "LEDGER", tmp_path / "ledger.jsonl")
    ob.order_audit(_BoomClient(), NOW.date(), "buy")
    ob.account_audit(_BoomClient(), NOW.date(), "reconcile_buy")
    assert "broker down" in (tmp_path / "run.log").read_text()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_order_audit_records_status_and_fills(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace as NS
    monkeypatch.setattr(ob, "OUT", tmp_path)
    monkeypatch.setattr(ob, "LEDGER", tmp_path / "ledger.jsonl")
    o = NS(id="x1", client_order_id="on-2026-10-01-AAA-buy", symbol="AAA", side=NS(value="buy"), type="market",
           time_in_force=NS(value="cls"), status=NS(value="filled"), qty="1", filled_qty="1",
           filled_avg_price="50.01", submitted_at=NOW, filled_at=NOW, canceled_at=None, expired_at=None,
           failed_at=None)
    monkeypatch.setattr(ob, "our_orders", lambda _tc, prefix: [o] if prefix == "on-2026-10-01-" else [])
    ob.order_audit(object(), NOW.date(), "reconcile_buy")
    row = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert row["event"] == "orders" and row["step"] == "reconcile_buy"
    assert row["orders"][0]["time_in_force"] == "cls" and row["orders"][0]["status"] == "filled"
    assert row["orders"][0]["filled_avg_price"] == "50.01"


def test_live_buys_the_same_day_it_sold_unless_the_settle_wait_is_on(monkeypatch, tmp_path):
    """Alpaca has no cash accounts: a limited-margin account (< $2,000) may buy
    with the morning's unsettled proceeds, so live trades every night unless
    OVERNIGHT_LIVE_SETTLE_WAIT=yes."""
    import json
    from datetime import date as _d
    from types import SimpleNamespace as NS
    monkeypatch.setattr(ob, "OUT", tmp_path)
    monkeypatch.setattr(ob, "LOG", tmp_path / "run.log")
    monkeypatch.setattr(ob, "LEDGER", tmp_path / "ledger.jsonl")
    (tmp_path / "ledger.jsonl").write_text(json.dumps(
        {"event": "submit", "side": "sell", "night_end": "2026-10-05", "dry_run": False}) + "\n")
    sized = []
    monkeypatch.setattr(ob, "live_cash", lambda a: 100.0)
    monkeypatch.setattr(ob, "size_live", lambda *a, **k: sized.append(1) or [])
    tc = NS(get_account=lambda: NS())
    monkeypatch.setattr(ob, "LIVE_SETTLE_WAIT", False)
    ob._buy_live(tc, _d(2026, 10, 5), ["AAA"], {"AAA": 20.0}, {}, True)
    assert sized == [1]                        # went on to size tonight's buy
    monkeypatch.setattr(ob, "LIVE_SETTLE_WAIT", True)
    ob._buy_live(tc, _d(2026, 10, 5), ["AAA"], {"AAA": 20.0}, {}, True)
    assert sized == [1]                        # the wait skipped it


# ── 2026-10-03 skeptic review #2: disarming must never strand a position ─────

def test_disarm_gates_only_buy_steps_sells_always_run():
    from datetime import timedelta
    at = OP + timedelta(minutes=1)
    for name in ("sell", "sell_check", "sell_fallback", "sell_topup"):
        assert ob.step_action(name, at, at, {}, armed=False) == "run", name
    for name in ("buy", "buy_check", "buy_fallback", "buy_topup"):
        assert ob.step_action(name, at, at, {}, armed=False) == "disarmed", name
        assert ob.step_action(name, at, at, {}, armed=True) == "run", name
    assert ob.step_action("sell", at, at, {"sell": "ok 09:31"}, armed=False) is None       # done
    assert ob.step_action("sell", at, at - timedelta(seconds=1), {}, armed=False) is None  # not yet
    assert ob.step_action("sell", at, at + timedelta(minutes=11), {}, armed=True) == "late"
    assert ob.step_action("reconcile_sell", at, at + timedelta(hours=3), {}, armed=True) == "run"


def test_book_positions_are_net_fills_plus_the_latest_buy_night():
    ledger = [
        {"event": "fill", "day": "2026-09-29", "leg": "buy", "sym": "OLD", "fill": 10.0, "filled_qty": 3},
        {"event": "fill", "day": "2026-09-30", "leg": "sell", "sym": "OLD", "fill": 10.1, "filled_qty": 3},
        {"event": "fill", "day": "2026-09-30", "leg": "buy", "sym": "AAA", "fill": 20.0, "filled_qty": 1},
        {"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 1},
        # the newest night, not reconciled yet: its submits count
        {"event": "submit", "night": "2026-10-01", "sym": "BBB", "side": "buy", "qty": 2},
        {"event": "submit", "night": "2026-10-01", "sym": "ERR", "side": "buy", "qty": 1, "error": "x"},
        {"event": "submit", "night": "2026-10-01", "sym": "DRY", "side": "buy", "qty": 1, "dry_run": True},
    ]
    assert ob.book_positions(ledger) == {"AAA": 1.0, "BBB": 2.0}


def test_disarmed_sell_touches_only_the_books_names_and_never_cancels_all(monkeypatch, tmp_path):
    from datetime import date
    _isolate_ledger(monkeypatch, tmp_path)
    pos = [types.SimpleNamespace(symbol="AAA", qty="3", qty_available="3", side="long"),   # 1 is ours
           types.SimpleNamespace(symbol="MIR", qty="5", qty_available="5", side="long")]   # someone else's
    tc = _FakeTC(pos)
    ob.sell(tc, date(2026, 10, 2), only={"AAA": 1.0})
    assert tc.cancelled == 0
    assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 1)]


# ── 2026-10-03 skeptic review #1: a missed sell is caught up, loudly ─────────

def test_catchup_window_runs_after_the_last_sell_step_until_the_first_buy():
    lo, hi = ob.catchup_window(OP, CL, "market")
    assert lo.strftime("%H:%M") == "09:35" and hi.strftime("%H:%M") == "15:57"   # topup 09:33, buy 15:58
    lo, hi = ob.catchup_window(OP, CL, "auction")
    assert lo.strftime("%H:%M") == "09:33" and hi.strftime("%H:%M") == "15:39"   # fallback 09:31, buy 15:40
    half = CL.replace(hour=13)
    assert ob.catchup_window(OP, half, "auction")[1].strftime("%H:%M") == "12:39"


def test_sell_catchup_sells_held_names_at_market_unless_a_sell_is_working(monkeypatch, tmp_path):
    from datetime import date, timedelta
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "ORDER_MODE", "auction")
    pos = [types.SimpleNamespace(symbol="AAA", qty="2", qty_available="2", side="long"),
           types.SimpleNamespace(symbol="BBB", qty="1", qty_available="1", side="long"),
           types.SimpleNamespace(symbol="CCC", qty="4", qty_available="0", side="long")]   # held by an order
    tc = _OrdersTC([_o("BBB", "new", 1, cid="on-2026-10-02-BBB-sell-r2")], pos)           # still working
    n = ob.sell_catchup(tc, date(2026, 10, 2), OP + timedelta(minutes=20), OP)
    assert n == 1
    assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 2)]
    r = tc.submitted[0]
    assert str(r.time_in_force.value).lower() == "day" and str(r.side.value).lower() == "sell"
    assert r.client_order_id == "on-2026-10-02-AAA-sell-r120"                  # unique per minute
    assert tc.cancelled == 0
    empty = _OrdersTC([], [])
    assert ob.sell_catchup(empty, date(2026, 10, 2), OP + timedelta(minutes=20), OP) == 0
    assert empty.submitted == []


def test_sell_catchup_respects_the_disarmed_scope(monkeypatch, tmp_path):
    from datetime import date, timedelta
    _isolate_ledger(monkeypatch, tmp_path)
    pos = [types.SimpleNamespace(symbol="AAA", qty="2", qty_available="2", side="long"),
           types.SimpleNamespace(symbol="MIR", qty="9", qty_available="9", side="long")]
    tc = _OrdersTC([], pos)
    assert ob.sell_catchup(tc, date(2026, 10, 2), OP + timedelta(minutes=20), OP, only={"AAA": 1.0}) == 1
    assert [(r.symbol, r.qty) for r in tc.submitted] == [("AAA", 1)]


def test_alert_logs_runs_the_hook_once_and_never_raises(monkeypatch, tmp_path):
    _isolate_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(ob, "_ALERTED", set())
    out = tmp_path / "hook.txt"
    monkeypatch.setattr(ob, "ALERT_CMD", f"sh -c 'echo \"$0\" >> {out}'")
    ob.alert("sell missed", key="k1")
    ob.alert("sell missed", key="k1")                     # same key: once
    assert out.read_text().strip() == "sell missed"
    monkeypatch.setattr(ob, "ALERT_CMD", "/nonexistent/notify")
    ob.alert("still fine", key="k2")                       # a broken hook never breaks the runner
    assert (tmp_path / "run.log").read_text().count("ALERT") == 2
