"""Every declared grid knob has to reach a live config key.

A search JSON names knobs in the sweep's own vocabulary; OVERLAY_KEYS is
what turns those into the ai_watch_* keys should_arm_buy actually reads. A
name with no entry there is not an error — the cell simply runs the
baseline config, four times, and reports four identical rows. That is
indistinguishable from "the knob makes no difference", which is how
2026-09-05 spent a morning reading an inert MACD A/B as a result.

Cheap guard against the whole class: the keys have to exist, and the values
have to be things a config can hold.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import optimize_rstop as opt  # noqa: E402

GRIDS = sorted((_ROOT / "tools").glob("rstop_search*.json"))


def test_there_are_grids_to_check():
    assert GRIDS, "no tools/rstop_search*.json found"


@pytest.mark.parametrize("path", GRIDS, ids=lambda p: p.name)
def test_every_grid_knob_maps_to_a_config_key(path):
    grid = json.loads(path.read_text())
    search = grid.get("search") or {}
    assert isinstance(search, dict), f"{path.name}: search must be an object"
    unmapped = sorted(k for k in search if k not in opt.OVERLAY_KEYS)
    assert not unmapped, (
        f"{path.name}: {unmapped} are not in optimize_rstop.OVERLAY_KEYS, so "
        f"those cells would silently run the baseline config")


@pytest.mark.parametrize("path", GRIDS, ids=lambda p: p.name)
def test_every_grid_knob_offers_values(path):
    grid = json.loads(path.read_text())
    for knob, values in (grid.get("search") or {}).items():
        assert isinstance(values, list) and values, (
            f"{path.name}: {knob} needs a non-empty list of values")
        assert len(values) == len(set(map(repr, values))), (
            f"{path.name}: {knob} repeats a value, which duplicates cells")


@pytest.mark.parametrize("path", GRIDS, ids=lambda p: p.name)
def test_every_grid_declares_min_n(path):
    grid = json.loads(path.read_text())
    n = grid.get("min_n")
    assert isinstance(n, int) and n > 0, (
        f"{path.name}: min_n gates the candidate verdict; without one an "
        f"underpowered cell can be promoted")


def test_the_macd_direction_knobs_are_mapped():
    """The three that today's work turns on, named explicitly.

    Parametrised coverage above would pass if a grid stopped mentioning
    them; these are the ones the arm-gate question rests on.
    """
    for knob, key in (("require_macd", "ai_watch_arm_require_macd"),
                      ("macd_block_bearish", "ai_watch_macd_block_bearish"),
                      ("macd_block_narrowing",
                       "ai_watch_macd_block_narrowing")):
        assert opt.OVERLAY_KEYS.get(knob) == key
