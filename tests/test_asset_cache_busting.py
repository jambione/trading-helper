"""A changed asset with an unchanged ?v= is invisible to every warm browser.

The dashboard serves HTML, CSS and JS straight off disk, and each module is
pinned with a query string: `app.js` imports `./feeds.js?v=151`, and
`dashboard.html` loads `app.js?v=148` and `styles.css?v=148`. Deploying a
changed file without bumping its pin means the browser keeps the cached
copy indefinitely — no error, no warning, just old behaviour.

2026-08-26 it bit twice at once. The MACD redesign rewrote feeds.js,
tickers.js and notifications.js, and a Trader Bro badge rewrote feeds.js
and styles.css; none of the five bumps happened. `dashboard.html` is not
itself versioned, so the browser rendered the NEW "MACD Gap" header over
OLD rows that still emitted EXH and RSI cells — one cell more than the new
8-track grid, so the P&L wrapped to a second line. The MACD column looked
broken when the real problem was that its code had never been fetched.

This compares each pin against git history: if the asset has a newer commit
than the file that pins it, the pin is stale. Skips rather than fails when
git history is unavailable, since a shallow checkout cannot answer it.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _committed_at(rel: str):
    """Unix time of the last commit touching *rel*, or None if unknowable."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", rel],
            cwd=_ROOT, capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    got = (out.stdout or "").strip()
    return int(got) if got.isdigit() else None


def _require_git():
    if _committed_at("static/js/app.js") is None:
        pytest.skip("no usable git history in this checkout")


def _stale(pinned_in: str, pairs):
    """(asset, pinning-file) pairs where the asset is newer than its pin."""
    owner_ts = _committed_at(pinned_in)
    bad = []
    for asset, version in pairs:
        ts = _committed_at(asset)
        if ts is None or owner_ts is None:
            continue
        if ts > owner_ts:
            bad.append(f"{asset} (pinned v={version}) changed after {pinned_in}")
    return bad


def test_every_js_module_pin_is_current():
    """app.js pins its imports; changing a module without touching app.js
    leaves every warm browser on the old file."""
    _require_git()
    src = (_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    pairs = [(f"static/js/{m}", v)
             for m, v in re.findall(r"\./([A-Za-z0-9_]+\.js)\?v=(\d+)", src)]
    assert pairs, "app.js pins no modules — the convention has been dropped"
    bad = _stale("static/js/app.js", pairs)
    assert not bad, (
        "stale cache-buster(s); bump the ?v= in static/js/app.js:\n  "
        + "\n  ".join(bad))


def test_dashboard_pins_are_current():
    """dashboard.html pins app.js and styles.css. app.js matters most: if it
    is cached, the bumped module pins inside it are never even read."""
    _require_git()
    html = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
    pairs = []
    for path, version in re.findall(
            r"/static/(?:js|css)/([A-Za-z0-9_]+\.(?:js|css))\?v=(\d+)", html):
        sub = "js" if path.endswith(".js") else "css"
        pairs.append((f"static/{sub}/{path}", version))
    assert pairs, "dashboard.html pins nothing — the convention has been dropped"
    bad = _stale("dashboard.html", pairs)
    assert not bad, (
        "stale cache-buster(s); bump the ?v= in dashboard.html:\n  "
        + "\n  ".join(bad))


def test_the_entry_point_itself_is_versioned():
    """dashboard.html is fetched fresh every load, so it is the only place a
    bump can reach a browser that has everything else cached. If app.js were
    unversioned here, no module bump could ever take effect."""
    html = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
    assert re.search(r"/static/js/app\.js\?v=\d+", html), (
        "app.js must carry a ?v= in dashboard.html or module pins are unreachable")
    assert re.search(r"/static/css/styles\.css\?v=\d+", html), (
        "styles.css must carry a ?v= in dashboard.html")
