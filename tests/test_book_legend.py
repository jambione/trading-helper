"""Entry rules on the book, rendered from the live config.

The operator asked to see what values have to cross before a position opens.
The one thing that must not happen is a legend written down by hand: it would
drift from the thresholds it claims to describe the first time a knob moved,
and this desk has been bitten three times in one session by a number that said
one thing while the code did another —

  ai_watch_decision_max_age_sec read 30 and resolved to a hardcoded 8
  ai_watch_min_pct_change read 50 and gated a path admissions do not use
  the research seed refused red names with no knob at all

A legend is the same failure with a friendlier face, so every value comes off
the config the server actually loaded.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_JS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")

KEYS = [
    "macd_min_gap", "macd_sep_mult",
    "ai_watch_exhaustion_heat_min_pct", "ai_watch_ob_flat_min_pct",
    "ai_watch_macd_exh_override_min_pct",
    "ai_watch_decision_max_age_sec", "ai_watch_macd_max_age_sec",
    "ai_watch_arm_confirm_ticks", "ai_exit_min_hold_sec",
    "ai_exit_macd_hard_sell_sep",
    "ai_local_trail_give_max_pct", "ai_local_trail_be_at_pct",
]


def _legend() -> str:
    i = _JS.index("function _paintBookLegend")
    return _JS[i:_JS.index("\n}", i)]


def test_every_threshold_reaches_the_client():
    """A key the server does not publish renders as its fallback, which is a
    legend quietly showing a number nobody set."""
    from config import SAFE_CONFIG_KEYS
    missing = [k for k in KEYS if k not in set(SAFE_CONFIG_KEYS)]
    assert not missing, f"not published to the client: {missing}"


def test_every_threshold_is_read_from_config():
    body = _legend()
    missing = [k for k in KEYS if k not in body]
    assert not missing, f"legend does not read: {missing}"


def test_no_threshold_is_written_by_hand():
    """The regression that matters. Each rule line must interpolate n(...),
    never a literal the config could move away from."""
    body = _legend()
    lines = [l for l in body.split("\n") if "['" in l or "['" in l]
    rules = [l for l in body.split("\n") if re.search(r"^\s*\['[A-Z]+',", l)]
    assert rules, "no rule rows found"
    for l in rules:
        assert "n('" in l, f"rule row has no config lookup: {l.strip()[:70]}"


def test_the_container_exists_under_the_rows():
    i = _HTML.index("data-ai-book-rows")
    j = _HTML.index("data-ai-book-legend")
    assert j > i, "the legend belongs below the book, not above it"


def test_it_hides_itself_when_empty():
    """Before the first config arrives there is nothing true to say."""
    assert ".ai-book-legend:empty" in _CSS
    assert "return;" in _legend()


def test_it_can_never_break_the_book():
    """A legend is a reference, not a mechanism. If it throws, the rows still
    have to paint."""
    i = _JS.index("_paintBookLegend(get('config'))")
    assert "try {" in _JS[i - 40:i]
    assert "catch" in _JS[i:i + 120]
