import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
_TICKERS = (_ROOT / "static" / "js" / "tickers.js").read_text(encoding="utf-8")
_FEEDS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")


def test_the_tv_toggle_is_in_the_scan_header():
    """Restored 2026-08-29 the same day it was withdrawn.

    It was pulled after the operator reported it doing nothing, and that
    report was true of the version their browser had cached — but not of the
    named-target build, which was already deployed and which they then used to
    open four charts. The withdrawal was one commit early.
    """
    assert 'data-tv-toggle-btn' in _HTML
    assert 'tv-click-toggle' in _HTML
    scan_header_idx = _HTML.index('panel-header--scan')
    scan_body_idx = _HTML.index('scan-body')
    assert scan_header_idx < _HTML.index('data-tv-toggle-btn') < scan_body_idx


def test_the_feature_is_enabled_but_keeps_its_kill_switch():
    """One line to flip if it regresses, so nobody has to work out which of
    the four attempted shapes the code is currently in."""
    assert "const _TV_CLICK_ENABLED = true;" in _TICKERS
    i = _TICKERS.index("export function isTvClickOpenEnabled")
    assert "if (!_TV_CLICK_ENABLED) return false;" in _TICKERS[i:i + 260]


def test_the_url_machinery_is_kept_for_a_future_attempt():
    """The failure was never in building the URL — it was in getting a browser
    to reuse a tab. Leave the parts for whoever tries again."""
    assert "export function openTradingViewChart" in _TICKERS


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

    It is also what gets past the pop-up blocker. With '_blank' every click
    asks for a NEW window, so the browser allows the first and refuses the
    rest — observed as "it opened once and then nothing". Named, only the
    first click creates a window; the rest navigate a tab that already exists,
    which is not a pop-up.

    noopener has to go for that to work: it forces a fresh browsing context
    and defeats the name. The opener must NOT be nulled afterwards either —
    that can detach the tab from this browsing-context group, and the name
    lookup is the entire mechanism. Nulling it reintroduces a tab per click.
    """
    assert "window.open(url, String(cfg.tv_chart_window || 'tvchart'))" in _TICKERS
    assert "window.open(url, '_blank'" not in _TICKERS, (
        "_blank opens a new tab per click — the named target is the feature")
    assert "win.opener = null" not in _TICKERS, (
        "nulling opener detaches the tab and defeats the name lookup")


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


# ── osascript must never block the caller ────────────────────────────────

def test_every_applescript_call_is_bounded():
    """A GUI app that stops answering does not fail — it never replies.

    2026-08-30: read_tv_symbol against Brave left the desk at 0.1% CPU for
    hours, main thread parked in select.poll() on a child osascript that
    never returned, showing a blank screen. The process was healthy, the
    data was fine, Brave was running, and nothing timed out because nothing
    was watching the clock.

    Every AppleScript in mac_agent goes through _osascript, so bounding it
    there covers all six call sites at once.
    """
    src = (_ROOT / "mac_agent.py").read_text(encoding="utf-8")
    i = src.index("def _osascript(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "timeout=" in body, "_osascript must bound the subprocess"
    assert "TimeoutExpired" in body, "a timeout must be caught, not raised"


def test_a_timed_out_applescript_looks_like_a_failed_one():
    """Callers already handle a nonzero return code. Returning that shape on
    timeout means a hang degrades into the failure they were written for,
    rather than a new one they have never seen."""
    src = (_ROOT / "mac_agent.py").read_text(encoding="utf-8")
    i = src.index("def _osascript(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "return -1, \"\"" in body


def test_the_timeout_is_declared_not_inline():
    """One number, findable, with the incident written next to it."""
    src = (_ROOT / "mac_agent.py").read_text(encoding="utf-8")
    assert "OSASCRIPT_TIMEOUT_SEC" in src
