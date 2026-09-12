"""Source-pin Lever Desk wiring so a half-shipped strip cannot land silently."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
_APP = (_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
_API = (_ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
_LD = (_ROOT / "static" / "js" / "leverDesk.js").read_text(encoding="utf-8")
_DASH = (_ROOT / "dashboard.py").read_text(encoding="utf-8")


def test_html_has_lever_desk_panel():
    assert 'data-panel="lever-desk"' in _HTML
    assert "data-ld-verdict" in _HTML
    assert "data-ld-toggle" in _HTML
    assert "data-ld-body" in _HTML


def test_app_wires_initLeverDesk():
    assert "initLeverDesk" in _APP
    assert "leverDesk.js" in _APP
    assert '[data-panel="lever-desk"]' in _APP


def test_api_helpers_exist():
    assert "getLeverDesk" in _API
    assert "classifyLeverIdea" in _API
    assert "recordLeverVerdict" in _API
    assert "activateLever" in _API
    assert "saveLever" in _API
    assert "/api/lever-desk" in _API
    assert "/api/lever-desk/lever" in _DASH


def test_ui_has_topic_editor_hooks():
    assert "data-ld-save-topic" in _LD
    assert "data-ld-pick" in _LD
    assert "data-ld-activate" in _LD
    assert "Save & score this topic" in _LD or "save-topic" in _LD


def test_lever_desk_and_app_share_one_api_module_pin():
    """Two ?v= pins for api.js → two module instances → getLeverDesk missing."""
    import re
    app_pins = set(re.findall(r"api\.js\?v=(\d+)", _APP))
    ld_pins = set(re.findall(r"api\.js\?v=(\d+)", _LD))
    assert app_pins, "app.js must pin api.js"
    assert ld_pins, "leverDesk.js must pin api.js"
    assert app_pins == ld_pins, f"api.js pin mismatch app={app_pins} leverDesk={ld_pins}"


def test_css_shows_for_owner_and_not_in_hidden_block():
    assert ".lever-desk" in _CSS
    assert "body.user-jmb .lever-desk" in _CSS
    assert "body.engine-visible .lever-desk" in _CSS
    # Must not be buried with the preference hide that kills .engine-bar
    hidden = _CSS.split("/* ── Hidden elements (user preference)")[-1]
    assert ".lever-desk" not in hidden.split("/* Exhaustion:")[0]


def test_dashboard_has_routes_not_on_snapshot():
    assert '/api/lever-desk' in _DASH
    assert "api_lever_desk" in _DASH
    # Must not pollute the hot path
    snap_idx = _DASH.find("def _snapshot(")
    assert snap_idx > 0
    # Look at a window of the snapshot function body for lever-desk pollution
    window = _DASH[snap_idx:snap_idx + 8000]
    assert "lever-desk" not in window
    assert "lever_desk" not in window


def test_lever_desk_js_exports_init():
    assert "export function init" in _LD
    assert "getLeverDesk" in _LD
