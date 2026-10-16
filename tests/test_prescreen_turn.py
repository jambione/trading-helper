"""prescreen_turn: warehouse day-files feed early_entry_honest.score unchanged. Synthetic data only."""
import os
import random
import sys
from datetime import datetime
from types import SimpleNamespace as N

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402
import warehouse_fetch as W  # noqa: E402


def _day(path, day, seed):
    r = random.Random(seed)
    y, m, d = map(int, day.split("-"))
    rows = {}
    for s in ("AAA", "BBB", "CCC"):
        p, bars = 20.0, []
        for mm in range(570, 960):
            o = p
            p *= 1 + r.gauss(0, 0.002)
            bars.append(N(timestamp=datetime(y, m, d, mm // 60, mm % 60, tzinfo=W.ET), open=o, high=max(o, p) * 1.001,
                          low=min(o, p) * 0.999, close=p, volume=r.randint(100, 900)))
        rows[s] = bars
    np.savez_compressed(path, **W.pack(day, rows))


def test_lazy_days_score_end_to_end(tmp_path, monkeypatch):
    for f in ("iex", "sip"):
        os.makedirs(tmp_path / f)
    for k, day in enumerate(("2026-10-05", "2026-10-06", "2026-10-07")):
        for f in ("iex", "sip"):
            _day(tmp_path / f / f"{day}.npz", day, k)
    monkeypatch.setattr(P, "WH", str(tmp_path))
    monkeypatch.setattr(E, "RULES", P.RULES)
    monkeypatch.setattr(E, "GRADED", P.GRADED)
    monkeypatch.setattr(E, "WARM_LINES", True)
    days = P.usable_days()
    assert [d["day"] for d in days] == ["2026-10-06", "2026-10-07"]          # first session: no prior, excluded
    assert set(days[0]["board"]) == {"AAA", "BBB", "CCC"} and len(days[0]["iex_prior"]["AAA"]) == E.CON_PRIOR_BARS
    _ = days[1]["iex"]
    assert "iex" not in dict.keys(days[0])                                   # one day in memory at a time
    res = E.score(days)
    assert set(res["rules"]) == set(P.RULES)


def test_sample_is_seeded_and_capped():
    names = [f"S{i:03d}" for i in range(1000)]
    a = P.sample_names("2026-10-06", names)
    assert a == P.sample_names("2026-10-06", list(reversed(names))) and len(a) == P.SAMPLE
    assert a != P.sample_names("2026-10-07", names)


def test_main_score_applies_cross_gate(tmp_path, monkeypatch, capsys):
    wh = tmp_path / "ai_reports" / "warehouse"
    for f in ("iex", "sip"):
        os.makedirs(wh / f)
    for k, day in enumerate(("2026-10-05", "2026-10-06", "2026-10-07")):
        for f in ("iex", "sip"):
            _day(wh / f / f"{day}.npz", day, k)
    monkeypatch.setattr(P, "WH", str(wh))
    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["prescreen_turn.py", "--score"])
    P.main()
    out = capsys.readouterr().out
    assert "Pre-screen gates" in out and "cross-name lift" in out and "days excluded for missing warm-up: 0" in out
    assert (wh / "prescreen_turn_result.md").exists()
