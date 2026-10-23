import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import retail_imbalance as R  # noqa: E402


def test_classify_subpenny():
    assert R.classify(10.0075) == 1          # frac 0.75: retail buy (price improvement below the ask)
    assert R.classify(10.0025) == -1         # frac 0.25: retail sell
    assert R.classify(10.005) == 0           # midpoint: unclassified
    assert R.classify(10.01) == 0            # whole cent
    assert R.classify(25.0062) == 1


def test_sample_is_deterministic():
    names = [f"S{i}" for i in range(400)]
    assert R.sample("2026-07-01", names) == R.sample("2026-07-01", list(reversed(names)))
    assert len(R.sample("2026-07-01", names)) == 150
