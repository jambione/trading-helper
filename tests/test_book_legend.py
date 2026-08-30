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
    i = _JS.index("_paintBookLegend(get('config'),")
    assert "try {" in _JS[i - 80:i]
    assert "catch" in _JS[i:i + 300]


# ── provenance gates the whole panel ───────────────────────────────────────
#
# PATH, 2026-08-28: the State column read "MACD not live" while the legend
# showed EITHER as satisfied. Both were describing the same row. The gate
# refuses a MACD not drawn on the live tape BEFORE it looks at gap size,
# separation or the override, so scoring those rules on an unusable reading
# ticks a branch the gate never reached.
#
# A reading that cannot be used is not a rule that passes. It is a rule that
# could not be judged — the same distinction the desk draws everywhere else.

def test_provenance_is_read_before_the_rules():
    body = _legend()
    i = body.index("const src = String(r.macd_src")
    # The assignments, not the `let` line that declares them all as null.
    for later in ("const gap = num(r.macd_gap)", "_macdLeg",
                  "fresh = live === false"):
        assert body.index(later) > i, f"{later} is evaluated before provenance"


def test_a_non_live_macd_makes_its_rules_unjudgeable():
    """null, not false: the rule is not failed, it is unreachable."""
    body = _legend()
    assert "macd = live !== true ? null" in body
    assert "both = live !== true ? null" in body


def test_a_non_live_macd_fails_fresh_outright():
    """FRESH is the row that is ABOUT usability, so there it is a real fail
    rather than an unknown — otherwise nothing on the panel would say why."""
    body = _legend()
    assert "fresh = live === false ? false" in body


def test_fresh_says_it_covers_the_tape():
    """The row has to name what it now checks, or the panel is accurate and
    still unreadable."""
    assert "MACD on the live tape" in _JS


def test_unknown_provenance_is_not_treated_as_live():
    body = _legend()
    assert "live = src ? src === 'realtime' : null" in body


# ── RSI leg ──────────────────────────────────────────────────────────────

def test_the_legend_reads_the_same_rsi_knobs_the_gate_does():
    """A legend that hardcodes a rule drifts from the gate the first time a
    knob moves, and then it is worse than no legend — it asserts something
    false about why a row did or did not arm."""
    for knob in ("ai_watch_arm_require_cm_rsi", "ai_watch_arm_cm_rsi_max",
                 "ai_watch_arm_cm_rsi_min",
                 "ai_watch_arm_cm_rsi_allow_falling_below"):
        assert knob in _JS, f"legend never reads {knob}"


def test_a_missing_rsi_reading_shows_as_a_refusal_not_a_pass():
    """cm_rsi_allows_buy fails CLOSED: `if rsi is None: return False,
    "no_rsi_data"`. The legend has to agree — 35% of armed polls in the
    historical record carried no reading, and a legend that showed those as
    passing would explain an arm that never happened."""
    i = _JS.index("const rv = num(r.cm_rsi);")
    body = _JS[i:i + 900]
    assert "rsi = false;" in body
    assert "no_rsi_data" in body or "the gate refuses this" in body


def test_the_rsi_leg_is_off_when_the_gate_is_off():
    """Switched off it is not a rule in force, so it must read as
    unjudgeable rather than as a pass — the same discipline the MACD
    provenance check already follows."""
    i = _JS.index("if (!n('ai_watch_arm_require_cm_rsi', 0))")
    assert "rsi = null;" in _JS[i:i + 160]


def test_every_knob_the_legend_prints_is_on_the_wire():
    """The legend renders from /api/state's config, which is filtered by
    SAFE_CONFIG_KEYS. A knob missing there reads undefined and the legend
    quietly prints a default instead of the live value."""
    from config import SAFE_CONFIG_KEYS
    for knob in ("ai_watch_arm_require_cm_rsi", "ai_watch_arm_cm_rsi_max",
                 "ai_watch_arm_cm_rsi_min",
                 "ai_watch_arm_cm_rsi_allow_falling_below"):
        assert knob in SAFE_CONFIG_KEYS, f"{knob} never reaches the browser"
