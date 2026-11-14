"""tools/pick_email.py: the daily email's subject and body are built from the holdings report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pick_email as M

POS = """Fact-sheet picks: PAPER positions as of 2026-11-13 16:44 ET. No real money.

OPEN (3)
symbol bought       days   return     S&P  vs S&P   paper $
GOOG   2026-11-13      0    -2.9%   -0.7%   -2.2%    -19.11
  2 of 3 ahead of the S&P; average vs S&P   +0.1%; paper profit $-12.61

Paper account value: $9,885.18
"""
REP = "tables...\nVERDICT at 21 sessions on the first 30 picks (x): NOT YET (3 of 30 picks recorded)\nfacts         NOT YET\nwhole sheet   not scoreable yet\nmore\n"


def test_subject_and_body():
    s, body = M.build(POS, REP, {"facts": "LLY, REGN, GOOG (recorded 2026-11-12)", "facts_avoid": "IREN", "facts_random": "PEG"}, "2026-11-13")
    assert s == "Paper picks 2026-11-13 | 2 of 3 ahead of S&P (+0.1%) | account $9,885.18"
    assert "GOOG   2026-11-13" in body and "bought on paper): LLY, REGN, GOOG" in body and "NOT YET (3 of 30" in body and "more" not in body
    s2, body2 = M.build("no positions yet\n", "", {"facts": "none yet", "facts_avoid": "none yet", "facts_random": "none yet"}, "2026-11-13")
    assert s2 == "Paper picks 2026-11-13" and "THE TEST" not in body2
