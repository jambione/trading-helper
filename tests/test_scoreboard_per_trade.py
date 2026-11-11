"""Per-trade numbers beside day means (restructure plan WP1; running list rows 116-117)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import daily_scoreboard as SB  # noqa: E402
import trade_sim as TS  # noqa: E402


def _day(name, n, bp):
    return (name, [{"bp": bp, "ng": False, "usd": 0.0} for _ in range(n)])


def test_trade_mean_counts_every_trade_once():
    chunk = [_day("a", 2, 40.0), _day("b", 2, 40.0), _day("c", 60, -5.0)]
    assert sum(SB._day_means(chunk)) / 3 == 25.0                    # the day mean says +25
    assert round(SB._trade_mean(chunk), 2) == -2.19                 # the account got -2.2


def test_scoreboard_flags_when_per_trade_disagrees_with_day_means(monkeypatch, tmp_path):
    monkeypatch.setattr(SB, "ROOT", str(tmp_path))
    old = [_day(f"2026-10-{d:02d}", 20, 0.0 + (d % 3 - 1)) for d in range(1, 11)]
    new = [_day(f"2026-11-{d:02d}", 2, 30.0 + d) for d in range(1, 10)] + [_day("2026-11-10", 300, -6.0)]
    text = "\n".join(SB.trend_and_ships(old + new))
    assert "GROWING" in text and "points the OTHER way" in text


def test_trade_sim_needs_a_positive_per_trade_delta():
    s = {"n": 100, "changed": 50, "h1": 1.0, "h2": 1.0, "t": 3.0, "win_h1": 1.0, "win_h2": 1.0, "p5": 0.5, "base_mean": -5.0, "var_mean": -6.0}
    verdict, bad = TS.verdict(s, {"n": 100, "mean_day": 1.0})
    assert verdict == "NO" and bad == ["per-trade delta > 0"]
    assert TS.verdict({**s, "var_mean": -4.0}, {"n": 100, "mean_day": 1.0})[0] == "IMPROVES"
