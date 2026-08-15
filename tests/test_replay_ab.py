"""Replay tuner: declared overlays, honesty rails, pinned 08-11 tape."""
from __future__ import annotations

import gzip
import shutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import replay_ab as rab  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "sim_2026-08-11"
_DAY = "2026-08-11"


def _unpack(dest: Path) -> None:
    n = 0
    for gz in sorted(_FIXTURE.glob("*.jsonl.gz")):
        with gzip.open(gz, "rb") as fi, open(dest / gz.name[:-3], "wb") as fo:
            shutil.copyfileobj(fi, fo)
        n += 1
    assert n, f"no fixture in {_FIXTURE}"


@pytest.fixture
def tape_dir(tmp_path, monkeypatch):
    _unpack(tmp_path)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    # sim._report binds off resolve_report_dir at call time, but some module
    # paths were imported already — replay_ab always goes through sim._report
    # / resolve_report_dir, both of which read the env.
    return tmp_path


def test_hybrid_matches_pinned_swing(tape_dir):
    """The 08-11 write-up number is the tuner’s load-bearing check."""
    payload = rab.run_days([_DAY], write=False, only=["hybrid-exit"])
    assert payload["ok"] is True
    hy = next(e for e in payload["experiments"] if e["name"] == "hybrid-exit")
    assert hy["n"] == 14
    assert hy["n_scored"] == 12  # 2 unknown exits have no $
    assert hy["delta_usd"] == pytest.approx(226.94, abs=0.01)
    assert hy["live_usd"] == pytest.approx(-117.60, abs=0.01)
    assert hy["variant_usd"] == pytest.approx(109.34, abs=0.01)


def test_hybrid_is_hypothesis_not_candidate_on_one_day(tape_dir):
    """n=14 cannot clear min_n=30. A +$227 day is still not a promote."""
    payload = rab.run_days([_DAY], write=False, only=["hybrid-exit"])
    hy = payload["experiments"][0]
    assert hy["verdict"] == rab.VERDICT_HYPOTHESIS
    assert hy["underpowered"] is True
    assert payload["best_candidate"] is None
    assert payload["best_hypothesis"]["name"] == "hybrid-exit"
    assert "do not change config" in payload["action"]


def test_heat_floor_is_not_a_dollar_candidate(tape_dir):
    payload = rab.run_days([_DAY], write=False, only=["heat-floor"])
    heat = payload["experiments"][0]
    assert heat["metric"] == "fwd_pct"
    assert heat["delta_usd"] is None
    assert heat["verdict"] == rab.VERDICT_NO_SCORE
    assert payload["ranking"] == []
    assert heat["core"]["n"] == 148


def test_flatten_hold_scores_clock_exits(tape_dir):
    payload = rab.run_days([_DAY], write=False, only=["flatten-vs-hold"])
    flat = payload["experiments"][0]
    assert flat["n_flatten"] == 4
    assert flat["metric"] == "session_usd"
    assert flat["delta_usd"] is not None
    # Missing later prints stay live, not a silent 0R.
    assert flat["n_skipped"] + flat["n_scored"] == flat["n_flatten"]


def test_ranking_puts_best_hypothesis_first_when_no_candidate(tape_dir):
    payload = rab.run_days([_DAY], write=False)
    names = [r["name"] for r in payload["ranking"]]
    assert "hybrid-exit" in names
    assert "continuation" in names
    assert "flatten-vs-hold" in names
    assert "heat-floor" not in names
    assert payload["best_candidate"] is None
    assert payload["best_hypothesis"]["name"] in names


def test_does_not_write_bot_config(tape_dir):
    cfg = _ROOT / "config" / "bot_config.json"
    before = cfg.read_bytes()
    rab.run_days([_DAY], write=True)
    assert cfg.read_bytes() == before
    assert (tape_dir / "replay_ab" / f"{_DAY}.md").exists()
    assert (tape_dir / "replay_ab" / f"{_DAY}.json").exists()


def test_missing_tape_is_a_skip_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    payload = rab.run_days(["2026-01-01"], write=False)
    assert payload["ok"] is True
    assert payload["skipped"] == "no_tape"
    assert payload["experiments"] == []


def test_decide_half_flip_is_never_a_candidate():
    exp = {
        "metric": "session_usd",
        "delta_usd": 50.0,
        "n_scored": 40,
        "holds_both_halves": False,
    }
    assert rab.decide(exp, min_n=30) == rab.VERDICT_DO_NOT


def test_decide_surviving_halves_at_min_n_is_a_candidate():
    exp = {
        "metric": "session_usd",
        "delta_usd": 50.0,
        "n_scored": 40,
        "holds_both_halves": True,
    }
    assert rab.decide(exp, min_n=30) == rab.VERDICT_CANDIDATE


def test_required_n_still_makes_live_ab_hopeless():
    assert 700 <= rab.osl.required_n(0.10) <= 850


def test_pack_round_trip_preserves_outcomes(tape_dir):
    packed = rab.desk_tape.pack([_DAY], dest=tape_dir / "tapes" / _DAY,
                                report_dir=tape_dir)
    tape = rab.desk_tape.load(packed["path"])
    assert tape["days"] == [_DAY]
    assert len(tape["outcomes"]) == 14
    assert len(tape["shadow"]) >= 1000
    live = rab.load_tape(_DAY, report_dir=tape_dir)
    assert len(tape["outcomes"]) == len(live["outcomes"])


def test_search_hybrid_settings_match_pinned_swing(tape_dir):
    """The 08-11 hybrid overlay must still be the money number under --search."""
    tape = rab.load_tape(_DAY, report_dir=tape_dir)
    index = rab.prepare_index(tape, [20.0])
    cell = rab.score_settings(index, {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_heat_min_pct": 50,
        "flatten": "clock",
        "dead_min": 20,
    }, min_n=30)
    assert cell["delta_usd"] == pytest.approx(226.94, abs=0.01)
    assert cell["verdict"] == rab.VERDICT_HYPOTHESIS


def test_search_does_not_write_bot_config(tape_dir):
    cfg = _ROOT / "config" / "bot_config.json"
    before = cfg.read_bytes()
    tape = rab.load_tape(_DAY, report_dir=tape_dir)
    # Tiny grid so the test stays a unit test, not an overnight run.
    reg = {
        "min_n": 30,
        "delta_r_for_power": 0.1,
        "search": {
            "ai_edge_mode": ["exhaustion_scalp"],
            "ai_exit_left_overbought": [False],
            "flatten": ["clock"],
            "dead_min": [20],
        },
    }
    payload = rab.run_search(tape, registry=reg, write=True)
    assert cfg.read_bytes() == before
    assert payload["n_cells"] == 1
    assert payload["best_candidate"] is None
    assert payload["best_hypothesis"]["delta_usd"] == pytest.approx(226.94, abs=0.01)
    assert (tape_dir / "replay_ab" / f"search_{_DAY}.md").exists()


def test_iter_grid_is_the_declared_product():
    cells = rab.iter_grid({
        "ai_edge_mode": ["exhaustion_scalp", "continuation"],
        "flatten": ["clock", "hold"],
    })
    assert len(cells) == 4
    assert {"ai_edge_mode": "continuation", "flatten": "hold"} in cells
