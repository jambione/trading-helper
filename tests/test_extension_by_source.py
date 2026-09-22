"""Ship rule for the RTH source allow-list. Synthetic rows only."""
from __future__ import annotations

from tools.extension_by_source import is_extended, ship_verdict


def _row(source, r, mfe, *, ext=None, day_hour=10):
    # 2026-09-22 14:00 UTC-ish via a fixed epoch plus hours. Day stamp only
    # needs to parse; the rule pools every row it is given.
    row = {
        "source": source,
        "symbol": source[:4].upper(),
        "entry_time": 1_790_000_000 + day_hour * 3600,
        "realized_r_multiple": r,
        "mfe_r": mfe,
        "extension_class": ext or ("extended" if mfe >= 0.15 else "dead_follow_through"),
        "features": {"pct_change": 10.0, "rvol": 2.0},
    }
    return row


def _book(*, blocked_n=25, blocked_r=-0.05, blocked_ext=0, allowed_ext=8, allowed_dead=8):
    rows = []
    for i in range(allowed_ext):
        rows.append(_row("momentum", 0.30, 0.40, day_hour=i))
    for i in range(allowed_dead):
        rows.append(_row("movers", -0.05, 0.02, day_hour=i))
    for i in range(blocked_n - blocked_ext):
        rows.append(_row("trending", blocked_r, 0.01, day_hour=i))
    for i in range(blocked_ext):
        rows.append(_row("xai", 0.40, 0.50, day_hour=i))
    return rows


def test_extended_follows_class_or_mfe():
    assert is_extended(_row("momentum", 0.1, 0.2))
    assert is_extended(_row("momentum", -0.1, 0.0, ext="extended"))
    assert not is_extended(_row("trending", -0.05, 0.10))


def test_thin_when_blocked_sample_is_small():
    verdict = ship_verdict(_book(blocked_n=15))
    assert verdict["verdict"] == "THIN"
    assert any("blocked n" in r for r in verdict["reasons"])


def test_pass_when_blocked_book_is_dead_and_large():
    verdict = ship_verdict(_book())
    assert verdict["verdict"] == "PASS"
    assert verdict["reasons"] == []
    assert verdict["blocked"]["n"] == 25
    assert verdict["blocked"]["extended_n"] == 0


def test_fail_when_blocked_source_extends():
    verdict = ship_verdict(_book(blocked_ext=2))
    assert verdict["verdict"] == "FAIL"
    assert any("blocked extensions" in r for r in verdict["reasons"])


def test_fail_when_blocked_sum_r_is_positive():
    verdict = ship_verdict(_book(blocked_r=0.20))
    assert verdict["verdict"] == "FAIL"
    assert any("sum R" in r for r in verdict["reasons"])
