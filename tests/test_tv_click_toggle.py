import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
_TICKERS = (_ROOT / "static" / "js" / "tickers.js").read_text(encoding="utf-8")
_FEEDS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")


def test_tv_toggle_btn_in_dashboard_scan_header():
    """Toggle button exists in dashboard.html within the scan panel header."""
    assert 'data-tv-toggle-btn' in _HTML
    assert 'tv-click-toggle' in _HTML
    scan_header_idx = _HTML.index('panel-header--scan')
    scan_body_idx = _HTML.index('scan-body')
    assert scan_header_idx < _HTML.index('data-tv-toggle-btn') < scan_body_idx


def test_tv_toggle_btn_hidden_on_mobile_in_css():
    """Toggle button is hidden on mobile via body.mobile and max-width media query."""
    assert 'body.mobile [data-tv-toggle-btn]' in _CSS
    assert 'body.mobile .tv-click-toggle' in _CSS
    rule_match = re.search(r'body\.mobile\s+\.tv-click-toggle,\s*body\.mobile\s+\[data-tv-toggle-btn\]\s*\{([^}]+)\}', _CSS)
    assert rule_match, "body.mobile rule for tv toggle not found"
    assert "display: none !important" in rule_match.group(1)

    mq_match = re.search(r'@media\s*\(\s*max-width:\s*768px\s*\)\s*\{([^}]+\[data-tv-toggle-btn\][^}]*)\}', _CSS, re.DOTALL)
    assert mq_match, "max-width: 768px rule for tv toggle not found"
    assert "display: none !important" in mq_match.group(1)


def test_tickers_js_defines_toggle_and_url_opener():
    """tickers.js exposes isTvClickOpenEnabled, openTradingViewChart, and initTvToggle."""
    assert 'export function isTvClickOpenEnabled' in _TICKERS
    assert 'export function openTradingViewChart' in _TICKERS
    assert 'export function initTvToggle' in _TICKERS
    assert 'export function updateTickerTitles' in _TICKERS


def test_tickers_js_guards_against_mobile():
    """isTvClickOpenEnabled in tickers.js checks for mobile body class and width."""
    assert "document.body.classList.contains('mobile')" in _TICKERS


def test_tickers_js_opens_tradingview_url():
    """One tab, reused — not a new one per click.

    A NAMED target is the whole feature. With '_blank' every click spawns
    another TradingView tab, so clicking five tickers leaves five of them and
    the operator is back to hunting for the right window, which is the problem
    this was built to remove. A name reuses the same tab and reloads it with
    the new symbol.

    noopener has to go for that to work: it forces a fresh browsing context
    and defeats the name. The opener reference is nulled instead.
    """
    assert "window.open(url, String(cfg.tv_chart_window || 'tvchart'))" in _TICKERS
    assert "window.open(url, '_blank'" not in _TICKERS, (
        "_blank opens a new tab per click — the named target is the feature")
    assert "win.opener = null" in _TICKERS


def test_a_blocked_popup_is_reported_not_swallowed():
    """The bug that made this feature look dead for two commits was an empty
    catch. A blocked tab must say so somewhere the operator can find it."""
    assert "console.warn" in _TICKERS
    assert "pop-ups" in _TICKERS


def test_the_browser_path_does_not_depend_on_the_desk_agent():
    """It must work on a machine with no agent running. The agent notify is
    best-effort and comes AFTER the window is opened, never before."""
    i = _TICKERS.index("export function openTradingViewChart")
    body = _TICKERS[i:_TICKERS.index("\nexport function updateTickerTitles", i)]
    assert body.index("window.open(") < body.index("api.addToTV("), (
        "the agent call must not gate the browser path")


def test_copy_ticker_invokes_open_when_enabled():
    """copyTicker checks isTvClickOpenEnabled and calls openTradingViewChart."""
    assert "if (isTvClickOpenEnabled())" in _TICKERS
    assert "openTradingViewChart(ticker)" in _TICKERS


def test_tickers_pin_agreement_across_modules():
    """Every file that imports tickers.js must use the exact same ?v= pin."""
    pins = set(re.findall(r"tickers\.js\?v=(\d+)", (_ROOT / "static" / "js" / "app.js").read_text()))
    feeds_pins = set(re.findall(r"tickers\.js\?v=(\d+)", _FEEDS))
    controls_pins = set(re.findall(r"tickers\.js\?v=(\d+)", (_ROOT / "static" / "js" / "controls.js").read_text()))

    assert len(pins) == 1
    assert pins == feeds_pins == controls_pins, f"Pin mismatch: app={pins}, feeds={feeds_pins}, controls={controls_pins}"


def test_workflows_delegates_to_mac_agent_on_macos():
    """workflows.py delegates to mac_agent on macOS."""
    workflows_code = (_ROOT / "transcription" / "workflows.py").read_text(encoding="utf-8")
    assert "if _IS_MACOS:" in workflows_code
    assert "mac_agent.workflow_add_tv(ticker" in workflows_code


def test_mac_agent_ensures_browser_launched():
    """mac_agent ensures Brave Browser or Chrome is launched if not already open."""
    agent_code = (_ROOT / "mac_agent.py").read_text(encoding="utf-8")
    assert "_ensure_app_open(candidate)" in agent_code
