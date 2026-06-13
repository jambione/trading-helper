#!/usr/bin/env bash
# arrange_windows.sh — size and position trading windows after startup.
# Called from startup.command in the background; delay allows mac_agent to start first.

DELAY="${1:-6}"
sleep "$DELAY"

osascript << 'APPLESCRIPT'
-- Get usable screen size from Finder desktop bounds
tell application "Finder"
    set screenBounds to bounds of window of desktop
    set screenW to item 3 of screenBounds
    set screenH to item 4 of screenBounds
end tell

-- Brave takes ~45%; Discord fills the remaining ~55% on the right.
-- Ratio derived from screenshot: Brave≈661px, Discord≈809px on 1470px screen.
set leftW  to (screenW * 0.45) as integer
set rightX to leftW
set rightW to screenW - leftW

-- ── Brave Browser ────────────────────────────────────────────────────────────
-- Goal: TradingView pinned as tab 1, Brave sized to the left 45% of screen.
-- Strategy:
--   • If TV is already tab 1 → skip (already pinned from a prior session).
--   • If TV is open elsewhere → activate that tab, then pin via Tab menu once.
--   • If TV is not open at all → open it, then pin once.
-- We avoid the "set active tab to tab 1" call on pinned tabs (errors in Brave).

tell application "Brave Browser"
    activate
    delay 0.5
    set tvURL to "https://www.tradingview.com/chart/?symbol=TSLA"
    set tvIsFirst to false

    if (count of tabs of window 1) > 0 then
        if (URL of tab 1 of window 1) contains "tradingview.com" then
            set tvIsFirst to true
        end if
    end if

    if not tvIsFirst then
        -- Search all tabs for an existing TradingView tab
        set tvFound to false
        repeat with t in tabs of window 1
            if URL of t contains "tradingview.com" then
                set active tab of window 1 to t
                set tvFound to true
                exit repeat
            end if
        end repeat
        -- Not open → open it (becomes the active tab automatically)
        if not tvFound then
            open location tvURL
            delay 2
        end if
    end if
end tell

-- Pin the active TV tab via the Tab menu (only when it wasn't already at pos 1).
-- One click pins it and moves it to position 1. Skipping on the fast path prevents
-- accidentally toggling it OFF on repeat runs.
if not tvIsFirst then
    delay 0.3
    tell application "System Events"
        tell process "Brave Browser"
            try
                click menu item "Pin Tab" of menu "Tab" of menu bar 1
                delay 0.3
            end try
        end tell
    end tell
end if

-- Switch to tab 1 (TV is there after pinning, or was already there)
tell application "System Events"
    tell process "Brave Browser"
        keystroke "1" using command down
        delay 0.2
    end tell
end tell

-- Size Brave to the left 45% of the screen
tell application "System Events"
    tell process "Brave Browser"
        set position of front window to {0, 0}
        set size of front window to {leftW, screenH}
    end tell
end tell

-- ── Discord ───────────────────────────────────────────────────────────────────
-- Dock to the right so the OCR source can read the -daytrading-alerts channel.
tell application "System Events"
    set discordOpen to (exists process "Discord")
end tell
if discordOpen then
    tell application "Discord" to activate
    delay 0.3
    try
        tell application "System Events"
            tell process "Discord"
                if (count of windows) > 0 then
                    set position of window 1 to {rightX, 0}
                    set size of window 1 to {rightW, screenH}
                end if
            end tell
        end tell
    end try
end if

-- Minimize the Terminal window (mac_agent stays running, just hidden)
tell application "Terminal"
    set miniaturized of front window to true
end tell
APPLESCRIPT
