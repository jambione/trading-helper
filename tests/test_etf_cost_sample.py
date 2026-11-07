import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import etf_cost_sample as ec


def test_plan_is_seeded_and_in_window():
    days = [f"2026-06-{d:02d}" for d in range(1, 29)]
    a, b = ec.sample_plan("SPY", days), ec.sample_plan("SPY", days)
    assert a == b and len(a) == 200
    assert all(9 * 60 + 45 <= m <= 15 * 60 + 30 for _, m in a)
    assert ec.sample_plan("QQQ", days) != a


def test_spread_bp():
    assert abs(ec.spread_bp(99.99, 100.01) - 2.0) < 1e-9
    assert ec.spread_bp(0, 1) is None and ec.spread_bp(2, 1) is None
