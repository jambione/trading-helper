import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import after_square_forward as F  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402


def _ts(h, m):
    return datetime(2026, 10, 15, h, m, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_onsets_keep_one_per_symbol_per_window():
    rows = [(_ts(10, 0), "A"), (_ts(10, 1), "A"), (_ts(10, 14), "A"), (_ts(10, 15), "A"), (_ts(10, 2), "B")]
    assert F.onsets(sorted(rows)) == [("A", 600), ("B", 602), ("A", 615)]
