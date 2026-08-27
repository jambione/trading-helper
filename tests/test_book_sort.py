"""Sortable watchlist headers, and the header/value alignment they sit in.

Two operator reports on 2026-08-27, one old and one new:

  "the column headers and values dont line up"  (second time for this table)
  "I want to be able to sort the header of watchlist"

The alignment half is the reason the sort caret is styled the way it is. The
book header and the book rows are two separate DOM subtrees sharing one grid
definition (.feed-cols--ai-book), so they line up only while every column's
header and cell agree on text-align AND nothing changes a header's width.
That is why the caret is an absolutely-positioned ::after: an inline glyph
would widen the header, and a widened header in an auto-sized track drags the
column off its values.

Source-pinned rather than rendered — there is no JS test runner here, and
these are exactly the regressions that unit tests on the sort function itself
would not catch.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
_JS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")

# Every column in the book, in DOM order, with the cell class it must align
# with. Qty is display:none and deliberately not sortable.
COLUMNS = [
    ("ticker", "cell-ticker"),
    ("state", "ai-book-status"),
    ("last", "cell-price"),
    ("entry", "cell-entry"),
    ("stop", "cell-trail"),
    ("exh", "cell-exh"),
    ("macd", "cell-macd"),
    ("pl", "cell-pl"),
]


def _book_header_block() -> str:
    i = _HTML.index("ai-book-table-header")
    return _HTML[i:_HTML.index("data-ai-book-rows", i)]


# ── alignment ────────────────────────────────────────────────────────────

def _align_of(cls: str, scope: str) -> str:
    """Last text-align that applies to `cls` inside the desktop book rules.

    Mirrors the cascade crudely but adequately: these are all single-class
    selectors at the same specificity, so the last one wins.
    """
    align = "right" if scope == "header" else None  # .th base is right
    for m in re.finditer(
            r"\.panel-ai-watch\s+\.(?:ai-book-table-header|feed-cols--ai-book)"
            r"([^{]*)\{([^}]*)\}", _CSS):
        sel, body = m.group(1), m.group(2)
        if f".{cls}" not in sel and not (scope == "header" and sel.strip() == ""):
            continue
        found = re.search(r"text-align:\s*([a-z]+)", body)
        if found:
            align = found.group(1)
    return align or "left"      # unset => inherited/initial, i.e. left


def test_every_column_header_aligns_with_its_values():
    """The bug, generalised. cell-exh was restored with the EXH column but
    never added to the right-aligned list, so it rendered left inside a
    right-aligned track — the values sat most of a column away from the word
    EXH. Asserting the whole row prevents the next restored column from
    landing the same way."""
    bad = []
    for col, cell in COLUMNS:
        h = _align_of(f"th-{col}", "header")
        c = _align_of(cell, "cell")
        if h != c:
            bad.append(f"{col}: header={h} cell={c}")
    assert not bad, "header/value alignment mismatch — " + "; ".join(bad)


def test_the_sort_indicator_matches_the_scan_tables():
    """The operator asked for the book's sort to look like Scan's. It does so
    by REUSING _updateSortHeaders rather than by copying its output, so the
    two tables cannot drift into two different arrows."""
    i = _JS.index("function _paintBookSortHeaders")
    body = _JS[i:_JS.index("\n}", i)]
    assert "_updateSortHeaders(map, _bookSort.col, _bookSort.dir)" in body
    # And that shared function is the one Scan uses: inline arrow + .th--sorted.
    j = _JS.index("function _updateSortHeaders")
    shared = _JS[j:_JS.index("\n}", j)]
    assert "' ↑'" in shared and "' ↓'" in shared
    assert "th--sorted" in shared


def test_the_book_tracks_have_pixel_minimums():
    """Why an inline arrow is safe here. If any book track were sized by its
    content, a longer header ("MACD GAP ↑") would widen the column and pull it
    off its values — the alignment bug arriving by a new road."""
    # Anchored at line start: the bare selector defines the tracks. Several
    # descendant rules (.ai-book-table-header .feed-cols--ai-book, the mobile
    # override) match as substrings and carry no track list.
    i = _CSS.index("\n.feed-cols--ai-book {")
    block = _CSS[i:_CSS.index("}", i)]
    tracks = re.findall(r"minmax\(([^,]+),", block)
    assert len(tracks) == 8, f"expected 8 tracks, found {len(tracks)}"
    for t in tracks:
        t = t.strip()
        assert re.fullmatch(r"\d+(\.\d+)?(px|rem)", t), (
            f"track min '{t}' is not a fixed length; header text could resize it")


# ── the headers are wired ────────────────────────────────────────────────

def test_all_eight_columns_are_sortable():
    block = _book_header_block()
    for col, _cell in COLUMNS:
        assert f'data-book-sort-col="{col}"' in block, f"{col} not sortable"


def test_the_hidden_qty_column_is_not_sortable():
    """It is display:none; a sort control the operator cannot see or reach."""
    block = _book_header_block()
    m = re.search(r'<div class="th th-qty[^>]*>', block)
    assert m, "qty header missing"
    assert "data-book-sort-col" not in m.group(0)


def test_the_book_binds_its_own_sort_and_repaints():
    assert "_bindBookSort(" in _JS
    i = _JS.index("_bindBookSort(() =>")
    assert "_paintBookTable(" in _JS[i:i + 400], "a click must repaint"


def test_headers_are_reachable_by_keyboard():
    i = _JS.index("function _bindBookSort")
    body = _JS[i:i + 1800]
    assert "tabindex" in body
    assert "keydown" in body


# ── the ordering rules ───────────────────────────────────────────────────

def test_sorting_reads_raw_fields_not_the_rendered_text():
    """"$9.00" sorts after "$10.00" as a string. The cells render currency,
    percentages and "(1.1x)" suffixes, so the comparator must go to the
    underlying numbers or every numeric column sorts wrong."""
    i = _JS.index("function _bookSortVal")
    body = _JS[i:_JS.index("\n}", i)]
    for field in ("r.price", "r.avg_entry", "r.exhaustion", "r.macd_gap", "r.pl"):
        assert field in body, f"{field} not read"
    assert "_bookStopPx(r)" in body, "stop must use the same shelf the cell shows"
    assert "textContent" not in body and "_fmtExh" not in body


def test_unknown_values_sink_in_both_directions():
    """A name with no reading is not the leader. Floating nulls to the top of
    a descending sort makes an empty column look like the winners."""
    i = _JS.index("function _sortBookRows")
    body = _JS[i:_JS.index("\n/** Active book sort", i)]
    assert "if (av == null) return 1;" in body
    assert "if (bv == null) return -1;" in body


def test_the_default_ordering_is_still_phase_first():
    """Open positions first is what the operator watches. It must survive as
    the no-column-selected default."""
    i = _JS.index("function _sortBookRows")
    body = _JS[i:_JS.index("\n/** Active book sort", i)]
    assert "if (!s || !s.col)" in body
    assert "_PHASE_RANK" in body


def test_a_chosen_column_is_not_pre_sorted_by_phase():
    """Clicking MACD GAP must order the whole book by gap. Grouping by phase
    first would silently defeat the click while looking like it worked."""
    i = _JS.index("function _sortBookRows")
    body = _JS[i:_JS.index("\n/** Active book sort", i)]
    tail = body[body.index("const dir ="):]
    # Only the explicit 'state' column may consult phase after a pick.
    assert tail.count("_PHASE_RANK") == 2, "phase leaked into a value sort"
    assert "s.col === 'state'" in tail


def test_the_sort_can_be_cleared_back_to_default():
    """Three-state cycle. A sort you cannot leave hides open risk behind a
    page reload, and this one persists to localStorage."""
    i = _JS.index("function _bindBookSort")
    body = _JS[i:i + 1800]
    assert "_bookSort.col = null" in body


def test_the_sort_survives_a_reload_but_never_throws():
    """localStorage is unavailable in private windows and can throw on
    access; the book must still paint."""
    assert "_BOOK_SORT_LS" in _JS
    i = _JS.index("function _restoreBookSort")
    assert "catch" in _JS[i:i + 500]
    j = _JS.index("localStorage.setItem(_BOOK_SORT_LS")
    assert "catch" in _JS[j:j + 300]
