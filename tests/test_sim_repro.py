"""The 2026-08-11 edge-mode numbers must reproduce from the frozen fixture.

The write-up that recommended the hybrid edge mode is only as good as its
inputs, and those inputs (ai_reports/*.jsonl) are gitignored append-only logs
on the trading host — rotate them and the analysis becomes unfalsifiable.
tools/sim_repro.py pins the load-bearing numbers against a committed slice;
this drags that check into the normal test run so drift surfaces here.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "sim_2026-08-11"


@pytest.mark.skipif(
    not (_FIXTURE / "expected.json").exists(),
    reason="sim fixture not present",
)
def test_sim_numbers_reproduce_from_fixture():
    p = subprocess.run(
        [sys.executable, str(_ROOT / "tools" / "sim_repro.py")],
        capture_output=True, text=True, cwd=_ROOT, check=False,
    )
    assert p.returncode == 0, f"sim numbers drifted:\n{p.stdout}\n{p.stderr}"


def test_pinned_values_match_the_writeup():
    """Guard the specific figures quoted in the edge-mode write-up.

    Pinned separately from sim_repro's own compare so that re-pinning with
    --update cannot silently rewrite a number the write-up still claims.
    """
    want = json.loads((_FIXTURE / "expected.json").read_text())

    # Re-pinned 2026-08-21 after the replay RSI fix. Until then the simulator
    # could not write cm_rsi_rising, so should_arm_buy refused every bar in a
    # replay and these numbers came from a sim that never armed on direction.
    # The hybrid arm gate is still exactly exhaustion_scalp — that identity
    # held — but the count moved once the gate could actually see a turn.
    assert want["hybrid_arms"] == 194
    assert want["hybrid_scalp_mismatch"] == 0
    assert want["in_zone_n"] == 1076

    # Only half the left_overbought trades were overbought at entry.
    assert want["exit_n_scored"] == 8
    assert want["book_hybrid_n"] == 4
    # Strict and loose admission NO LONGER agree: -0.72R vs -0.12R on the same
    # four trades. That agreement was quoted as the error bar for the hybrid
    # recommendation, so its collapse is the point — on four trades the
    # admission rule alone moves the answer by 0.6R. Pinned apart deliberately
    # so a future run that makes them agree again is loud, not invisible.
    assert want["book_hybrid_r"] == pytest.approx(-0.724, abs=5e-3)
    assert want["book_hybrid_loose_r"] == pytest.approx(-0.123, abs=5e-3)

    # Books over those trades. The hybrid was recommended at +$127.49 here;
    # with the simulator fixed the same tape gives -$56.76, i.e. slightly
    # worse than live rather than three times better. See BENCHMARKS.md.
    assert want["book_live_usd"] == pytest.approx(-56.15, abs=0.01)
    assert want["book_cont_usd"] == pytest.approx(70.73, abs=0.01)
    assert want["book_hybrid_usd"] == pytest.approx(-56.76, abs=0.01)

    # Whole session, day-filtered. The unfiltered read mixed in other
    # sessions and invented a cohort of blind entries; 08-11 has none.
    assert want["day_n_outcomes"] == 14
    assert "none" not in want["day_states"]
    assert want["day_live_usd"] == pytest.approx(-117.60, abs=0.01)
    # Adopted orphans keep left_overbought under the hybrid, so they are not
    # re-priced by the continuation exit — see _adopt_unmanaged and the
    # adopted override in evaluate_positions.
    assert want["day_hybrid_usd"] == pytest.approx(109.34, abs=0.01)
    assert want["day_swing_usd"] == pytest.approx(226.94, abs=0.01)

    # The overbought core the heating tail is measured against.
    assert want["core_n"] == 148
    assert want["core_mean"] == pytest.approx(1.693, abs=5e-3)
