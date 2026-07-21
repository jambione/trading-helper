#!/usr/bin/env bash
# startup.command — double-click from the Desktop to start the full trading session.
#
#   1. Bring up the local stack (dashboard + signal engine + Discord OCR + tunnel)
#   2. Open Discord → -daytrading-alerts for OCR
#   3. Launch the toast notifier (if built)
#   4. Arrange windows
#   5. Launch the macOS TradingView/Webull agent (foreground — keep this window open)
#
# Source of truth: scripts/startup.command in the repo.
# Desktop copy:   ~/Desktop/startup.command  (re-copy after edits)
#
# Recovery: if the stack is half-dead (engine up, dashboard crashed), this
# script detects it and runs `./trading restart` instead of hanging forever.

set -uo pipefail

REPO="/Users/jambimac/repo/trading-helper"
BACKEND="http://localhost:8888"
LOGFILE="${REPO}/logs/startup.log"
WAIT_SECS=90          # how long to wait for the backend after start/restart
RESTART_ON_FAIL=1     # one automatic restart if backend never answers

mkdir -p "${REPO}/logs"
# Log everything; still show output in the Terminal window when double-clicked.
exec > >(tee -a "$LOGFILE") 2>&1

cd "$REPO" || { echo "❌ Could not cd to $REPO"; echo "Press any key to close."; read -r -n1; exit 1; }

echo ""
echo "============================================"
echo "  Brasfield Trading — Startup  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
echo ""

backend_up() {
    curl -s --max-time 3 "$BACKEND/api/meta" >/dev/null 2>&1
}

# True if any of the three Python services is running.
any_stack_proc() {
    pgrep -f "dashboard.py" >/dev/null 2>&1 \
        || pgrep -f "signal_engine.py" >/dev/null 2>&1 \
        || pgrep -f "discord_source.py" >/dev/null 2>&1
}

# True if all three Python services are running.
all_stack_procs() {
    pgrep -f "dashboard.py" >/dev/null 2>&1 \
        && pgrep -f "signal_engine.py" >/dev/null 2>&1 \
        && pgrep -f "discord_source.py" >/dev/null 2>&1
}

wait_for_backend() {
    local limit="${1:-$WAIT_SECS}"
    echo "      Waiting for backend at $BACKEND (up to ${limit}s)..."
    local i
    for i in $(seq 1 "$limit"); do
        if backend_up; then
            echo "      ✓ Backend up after ${i}s"
            return 0
        fi
        # Bail early if dashboard process died during the wait.
        if ! pgrep -f "dashboard.py" >/dev/null 2>&1; then
            echo "      ✗ Dashboard process died — see logs/dashboard.log"
            tail -n 15 "$REPO/logs/dashboard.log" 2>/dev/null | sed 's/^/        /'
            return 1
        fi
        sleep 1
    done
    echo "      ⚠ Backend still not responding after ${limit}s — check logs/dashboard.log"
    return 1
}

# ── 1. Start / recover the local stack ─────────────────────────────────────
echo "[1/5] Local stack (dashboard + signal engine + Discord OCR + tunnel)"

if backend_up && all_stack_procs; then
    echo "      ✓ Stack already healthy — leaving processes alone"
    ./trading status 2>/dev/null || true
elif any_stack_proc && ! backend_up; then
    # Classic failure mode: engine/discord still up, dashboard crashed.
    echo "      Partial / unresponsive stack detected — running ./trading restart"
    ./trading restart
elif any_stack_proc && ! all_stack_procs; then
    # Some pieces missing but backend may still answer — start fills gaps.
    echo "      Incomplete stack — running ./trading start (fills missing pieces)"
    ./trading start
else
    echo "      Starting fresh stack..."
    ./trading start
fi

if ! wait_for_backend "$WAIT_SECS"; then
    if [ "$RESTART_ON_FAIL" = 1 ]; then
        echo "      Backend failed first attempt — one full restart..."
        ./trading restart
        if ! wait_for_backend "$WAIT_SECS"; then
            echo ""
            echo "❌ Backend did not come up. Aborting remaining steps."
            echo "   Tail of logs/dashboard.log:"
            tail -n 30 "$REPO/logs/dashboard.log" 2>/dev/null | sed 's/^/   /'
            echo ""
            echo "Press any key to close."
            read -r -n1
            exit 1
        fi
    else
        echo "❌ Backend not up — aborting."
        echo "Press any key to close."
        read -r -n1
        exit 1
    fi
fi

echo "      Verifying signal engine..."
sleep 2
ENG_RESP=$(curl -s --max-time 3 "$BACKEND/api/state" 2>/dev/null || true)
ENG_INFO=$(echo "$ENG_RESP" | python3 -c '
import sys, json
try:
    v = (json.load(sys.stdin).get("version") or {})
    eng = v.get("engine") or ""
    strat = v.get("engine_strategy") or ""
    if eng:
        print(f"{eng}|{strat}")
except Exception:
    pass
' 2>/dev/null || true)

if [ -n "$ENG_INFO" ]; then
    ENG_VER="${ENG_INFO%%|*}"
    ENG_MODE="${ENG_INFO#*|}"
    echo "      ✓ Signal engine running (build $ENG_VER · $ENG_MODE)"
else
    echo "      ⚠ Signal engine not reporting yet — check logs/engine.log"
fi

# ── 2. Discord ─────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Opening Discord → -daytrading-alerts..."
if [ -x "$REPO/venv/bin/python" ]; then
    "$REPO/venv/bin/python" "$REPO/open_discord_alerts.py" \
        && echo "      ✓ Discord opened & docked" \
        || echo "      ⚠ Could not auto-open Discord — do it manually"
else
    echo "      ⚠ venv/python missing — open Discord manually"
fi

echo "      Verifying Discord OCR source..."
sleep 3
RESPONSE=$(curl -s --max-time 3 "$BACKEND/api/discord/status" 2>/dev/null || true)
if echo "$RESPONSE" | grep -qE '"running"[[:space:]]*:[[:space:]]*true'; then
    echo "      ✓ Discord OCR is live"
elif [ -n "$RESPONSE" ]; then
    echo "      ⚠ OCR not live yet (it starts with the stack). Response: $RESPONSE"
else
    echo "      ⚠ Could not reach /api/discord/status"
fi

# ── 3. Toast Notifier ──────────────────────────────────────────────────────
echo ""
echo "[3/5] Launching toast notifier..."
if [ -d "$REPO/BrasfieldNotifier.app" ]; then
    open "$REPO/BrasfieldNotifier.app" && echo "      ✓ Notifier launched" \
        || echo "      ⚠ Failed to launch notifier"
else
    echo "      ⚠ BrasfieldNotifier.app not found → run: scripts/build_notifier.sh"
fi

# ── 4. Window arrangement ──────────────────────────────────────────────────
echo ""
echo "[4/5] Scheduling window arrangement in 10s..."
if [ -f "$REPO/scripts/arrange_windows.sh" ]; then
    bash "$REPO/scripts/arrange_windows.sh" 10 &
else
    echo "      ⚠ scripts/arrange_windows.sh missing — skip"
fi

# ── 5. macOS Agent (foreground) ────────────────────────────────────────────
echo ""
echo "[5/5] Launching macOS TradingView/Webull agent..."
echo "      Keep this window open. Close it (Ctrl+C) to stop the agent."
echo ""

cd "$REPO"
if [ ! -f mac_agent.sh ]; then
    echo "❌ mac_agent.sh not found in $REPO"
    echo "Press any key to close."
    read -r -n1
    exit 1
fi
exec bash mac_agent.sh
