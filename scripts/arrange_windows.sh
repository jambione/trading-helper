#!/usr/bin/env bash
# arrange_windows.sh — size and position trading windows after startup.
# Called from startup.command in the background; delay allows mac_agent to start first.

DELAY="${1:-6}"
sleep "$DELAY"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$REPO/.venv/bin/python" ]; then
    MONITOR_PY="$REPO/.venv/bin/python"
else
    MONITOR_PY="python3"
fi

osascript << APPLESCRIPT
-- Get usable screen size from Finder desktop bounds
tell application "Finder"
    set screenBounds to bounds of window of desktop
    set screenW to item 3 of screenBounds
    set screenH to item 4 of screenBounds
end tell

-- Discord takes 60% on the left; the momentum monitor fills the remaining 40% on the right.
set leftW  to (screenW * 0.60) as integer
set rightX to leftW
set rightW to screenW - leftW

-- ── Momentum monitor ─────────────────────────────────────────────────────────
-- momentum_signal.py needs its own TTY for single-key hotkeys, so it gets a
-- fresh Terminal window rather than reusing the one running mac_agent.
tell application "Terminal"
    activate
    set agentWindow to front window
    do script "cd $REPO && $MONITOR_PY momentum-monitor/momentum_signal.py"
    delay 1
    set monitorWindow to front window
    set bounds of monitorWindow to {rightX, 0, screenW, screenH}
end tell

-- ── Discord ───────────────────────────────────────────────────────────────────
-- Dock to the left so the OCR source can read the -daytrading-alerts channel.
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
                    set position of window 1 to {0, 0}
                    set size of window 1 to {leftW, screenH}
                end if
            end tell
        end tell
    end try
end if

-- Minimize the original startup Terminal window (mac_agent stays running, just hidden)
tell application "Terminal"
    set miniaturized of agentWindow to true
end tell
APPLESCRIPT
