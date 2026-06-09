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

-- Brave takes the left 66% (extends well to the right); Webull fills the rest.
set leftW  to (screenW * 0.66) as integer
set rightX to leftW
set rightW to screenW - leftW

-- Brave Browser: left half, TradingView tab active (Cmd+1)
tell application "Brave Browser" to activate
delay 0.5
try
    tell application "System Events"
        tell process "Brave Browser"
            keystroke "1" using command down
            delay 0.3
            set position of front window to {0, 0}
            set size of front window to {leftW, screenH}
        end tell
    end tell
end try

-- Webull Desktop: right half
-- Electron apps often block System Events window access; try direct AS dict first.
set webullOpen to false
tell application "System Events"
    set webullOpen to (exists process "Webull Desktop")
end tell

if webullOpen then
    tell application "Webull Desktop" to activate
    delay 0.5
    -- Attempt 1: direct AppleScript dictionary (works if Webull exposes it)
    set wbMoved to false
    try
        tell application "Webull Desktop"
            set bounds of window 1 to {rightX, 0, screenW, screenH}
        end tell
        set wbMoved to true
    end try
    -- Attempt 2: System Events (works for non-Electron windows)
    if not wbMoved then
        try
            tell application "System Events"
                tell process "Webull Desktop"
                    if (count of windows) > 0 then
                        set position of window 1 to {rightX, 0}
                        set size of window 1 to {rightW, screenH}
                    end if
                end tell
            end tell
        end try
    end if
end if

-- Discord: dock to the right and bring to front so the OCR source can read the
-- -daytrading-alerts channel. Placed LAST so it sits on top of the right side.
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
        end try
    end try
end if

-- Minimize the Terminal window (mac_agent stays running, just hidden)
tell application "Terminal"
    set miniaturized of front window to true
end tell
APPLESCRIPT
