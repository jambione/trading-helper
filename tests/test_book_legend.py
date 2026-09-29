"""Book cell pass highlights share one ENTRY criteria helper.

The Criteria button and ENTRY/EXIT legend panel are gone. What remains is
``_bookEntryCriteria`` driving ``crit--pass`` on State / EXH cells, so a
pass mark still has to match the arm gate's provenance-first rules.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_JS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")


def _criteria() -> str:
    i = _JS.index("function _bookEntryCriteria")
    return _JS[i:_JS.index("\n}", i)]


def test_criteria_button_and_legend_are_gone():
    assert "data-ai-book-legend-toggle" not in _HTML
    assert "data-ai-book-legend>" not in _HTML
    assert "Criteria</button>" not in _HTML
    assert "function _paintBookLegend" not in _JS
    assert ".ai-book-legend" not in _CSS


def test_cell_pass_highlight_reuses_criteria_helper():
    assert "function _bookEntryCriteria" in _JS
    assert "crit--pass" in _JS
    assert ".crit--pass" in _CSS
    assert "#5fcf96" in _CSS


# ── provenance gates cell pass marks ───────────────────────────────────────
#
# PATH, 2026-08-28: the State column read "MACD not live" while a legend
# showed EITHER as satisfied. Both were describing the same row. The gate
# refuses a MACD not drawn on the live tape BEFORE it looks at gap size,
# so scoring those rules on an unusable reading ticks a branch the gate
# never reached. Cell highlights use the same helper.


def test_provenance_is_read_before_the_rules():
    body = _criteria()
    i = body.index("const src = String(r.macd_src")
    for later in ("const gap = num(r.macd_gap)",
                  "fresh = live === false"):
        assert body.index(later) > i, f"{later} is evaluated before provenance"


def test_a_non_live_macd_makes_its_rules_unjudgeable():
    """null, not false: the rule is not failed, it is unreachable."""
    body = _criteria()
    assert "if (live !== true) macd = null;" in body


def test_a_non_live_macd_fails_fresh_outright():
    """FRESH is about usability, so there it is a real fail rather than unknown."""
    body = _criteria()
    assert "fresh = live === false ? false" in body


def test_unknown_provenance_is_not_treated_as_live():
    body = _criteria()
    assert "live = src ? src === 'realtime' : null" in body
