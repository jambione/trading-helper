"""1-share scouts (ai_first_trade_fixed_qty) neither take a slot nor count as committed cash (operator 11/19)."""
from desk_risk import open_book_notional, scout_qty_limit, size_long_from_free_equity
from ai_trading import count_slot_positions


def test_scout_qty_limit():
    assert scout_qty_limit({"ai_first_trade_fixed_qty": 1}) == 1.0
    assert scout_qty_limit({"ai_first_trade_fixed_qty": 0}) == 0.0
    assert scout_qty_limit({}) == 0.0 and scout_qty_limit(None) == 0.0


def test_open_book_skips_scouts():
    rows = {"A": {"qty": 1, "avg_entry": 40.0, "current": 40.0, "mkt_val": 40.0},
            "B": {"qty": 80, "avg_entry": 40.0, "current": 40.0, "mkt_val": 3200.0}}
    n_all, c_all = open_book_notional(rows)
    n, c = open_book_notional(rows, scout_max_qty=1.0)
    assert c_all == 2 and c == 1 and n < n_all


def test_slot_count_ignores_scouts():
    detail = {"A": {"qty": 1.0}, "B": {"qty": 1.0}, "C": {"qty": 1.0}, "D": {"qty": 80.0}}
    assert count_slot_positions(detail, 0.0) == 4
    assert count_slot_positions(detail, 1.0) == 1          # three scouts never fill the book
    assert count_slot_positions({"X": "garbage"}, 1.0) == 1  # unknown rows stay counted (conservative)


def test_full_trade_after_scouts_is_one_slot():
    eq, px = 9800.0, 40.0
    base = size_long_from_free_equity(account_equity=eq, price=px, stop=38.0, risk_pct=1.65, open_notional=0.0, open_count=0,
                                      max_positions=3, slot_equity=eq / 3, max_position_pct=33.0, max_open_risk_pct=5.0,
                                      buying_power=4 * eq)
    rows = {"S1": {"qty": 1, "mkt_val": 40.0, "avg_entry": 40.0, "current": 40.0},
            "S2": {"qty": 1, "mkt_val": 40.0, "avg_entry": 40.0, "current": 40.0}}
    on, oc = open_book_notional(rows, scout_max_qty=1.0)
    after = size_long_from_free_equity(account_equity=eq, price=px, stop=38.0, risk_pct=1.65, open_notional=on, open_count=oc,
                                       max_positions=3, slot_equity=eq / 3, max_position_pct=33.0, max_open_risk_pct=5.0,
                                       buying_power=4 * eq)
    assert after.qty == base.qty                            # two scouts open: still one normal slot (~$3,240), not $9,720
