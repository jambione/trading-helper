"""The Movers tab, and the four places a Scan tab has to be wired.

The operator went looking for this tab and it did not exist — the movers
work had been built as a book SOURCE, feeding the watchlist, with no surface
of its own. Adding one is not a single edit: Scan's tabs are pure CSS radios,
so a tab needs an input, a label, a pane, a `:checked` rule to reveal that
pane, and a JS feed bound to it. Miss any one and the tab renders as a dead
word in the header — clickable, doing nothing, with no error anywhere.

Source-pinned because there is no JS test runner here, and because every one
of these failures is silent in exactly that way.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
_FEEDS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
_APP = (_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
_STORE = (_ROOT / "static" / "js" / "store.js").read_text(encoding="utf-8")
_DASH = (_ROOT / "dashboard.py").read_text(encoding="utf-8")

# Every Scan source, so a fifth tab cannot be added half-wired either.
SOURCES = ["tickers", "trending", "movers", "claude"]


def test_every_scan_source_has_a_radio_a_label_and_a_pane():
    for src in SOURCES:
        assert f'id="scan-src-{src}"' in _HTML, f"{src}: no radio"
        assert f'for="scan-src-{src}"' in _HTML, f"{src}: no label"
        assert f'data-panel="{src}"' in _HTML, f"{src}: no pane"


def test_every_pane_is_revealed_by_its_own_radio():
    """The rule that actually makes a tab work. Without it the pane stays
    display:none and the tab is a word that does nothing."""
    for src in SOURCES:
        rule = f'#scan-src-{src}:checked ~ .scan-body > [data-panel="{src}"]'
        assert rule in _CSS, f"{src}: no :checked reveal rule"


def test_every_tab_lights_up_when_selected():
    for src in SOURCES:
        rule = (f'#scan-src-{src}:checked ~ .panel-header--scan '
                f'label[for="scan-src-{src}"]')
        assert rule in _CSS, f"{src}: selected tab is not styled"


def test_the_radios_are_one_group_so_the_tabs_are_exclusive():
    ids = re.findall(r'<input class="scan-radio"[^>]*id="scan-src-([a-z]+)"[^>]*>',
                     _HTML)
    assert sorted(ids) == sorted(SOURCES)
    for m in re.finditer(r'<input class="scan-radio"[^>]*>', _HTML):
        assert 'name="scan-src"' in m.group(0)


def test_exactly_one_tab_is_checked_by_default():
    checked = re.findall(r'<input class="scan-radio"[^>]*\bchecked\b[^>]*>', _HTML)
    assert len(checked) == 1, "a Scan panel with no default shows no pane at all"


# ── the movers pane's own wiring ─────────────────────────────────────────

def test_the_movers_pane_carries_the_hooks_feeds_init_looks_for():
    """init() finds its nodes by `data-${kind}-rows` and friends, so the
    attribute names are load-bearing — a pane named for a different kind
    silently renders nothing."""
    i = _HTML.index('data-panel="movers"')
    pane = _HTML[i:_HTML.index("</section>", i)]
    for hook in ("data-movers-rows", "data-movers-count",
                 "data-movers-stamp", "data-movers-error"):
        assert hook in pane, f"movers pane missing {hook}"


def test_the_movers_feed_is_initialised():
    assert "initFeeds(document.querySelector('[data-panel=\"movers\"]'), 'movers')" in _APP


def test_the_movers_feed_subscribes_to_its_own_channel():
    """Bound to 'trending' it would render the Stocktwits list under a
    Movers heading — the worst failure here, because it looks like it works."""
    i = _FEEDS.index("const _channel =")
    body = _FEEDS[i:i + 220]
    assert "'movers' ? 'movers'" in body
    assert "subscribe(_channel," in _FEEDS


def test_the_store_carries_a_movers_key():
    """subscribe() only fires for keys the store knows; an unknown key is a
    subscription that never delivers."""
    assert re.search(r"\bmovers:\s*/\*\*", _STORE)


def test_the_server_publishes_movers_on_api_state():
    assert '"movers":             load_movers(),' in _DASH
    assert "def load_movers()" in _DASH
    assert 'MOVERS_FILE        = Path("movers_stocks.json")' in _DASH


def test_movers_reuses_the_trending_column_grid():
    """The rows carry the same fields, and feeds.js gives every non-research
    kind feed-cols--trending. A pane with different headers would misalign
    against the rows the renderer emits — the alignment bug, again."""
    i = _HTML.index('data-panel="movers"')
    pane = _HTML[i:_HTML.index("</section>", i)]
    assert "feed-cols--trending" in pane
    assert "colsClass = kind === 'claude'" in _FEEDS


def test_the_asset_pins_moved_so_browsers_reload_the_new_code():
    """Every one of these files changed for this tab. A stale pin means the
    browser keeps the old module and the tab is missing for the operator
    while being present in the repo — which is indistinguishable from a bug."""
    for name, pin in (("feeds.js", 165), ("store.js", 134),
                      ("app.js", 162), ("styles.css", 158)):
        found = set(re.findall(re.escape(name) + r"\?v=(\d+)",
                               _HTML + _APP + _FEEDS + _STORE))
        assert found, f"{name} is not pinned anywhere"
        assert found == {str(pin)}, f"{name} pins disagree: {found}"
