#!/usr/bin/env bash
# run_trading_server.sh
# Keeps the Mac awake (AC power), runs the trading server, and opens
# a Cloudflare Quick Tunnel so the dashboard is publicly reachable.
# Started by launchd at 4:00 AM on weekdays.

REPO="/Users/jonathanbrasfield/repo/trading-helper/trading-helper"
LOG="$REPO/server.log"
TUNNEL_LOG="$REPO/tunnel.log"
PYTHON="$(which python3)"
CF="$(which cloudflared)"

echo "" >> "$LOG"
echo "======================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading server" >> "$LOG"
echo "======================================" >> "$LOG"

# ── Start Cloudflare named tunnel in background ───────────────────────────────
# Named tunnel gives a permanent URL: https://trading.jbrasfield.com
echo "" > "$TUNNEL_LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Cloudflare tunnel (trading.jbrasfield.com)..." >> "$TUNNEL_LOG"
"$CF" tunnel --config ~/.cloudflared/config.yml run trading-helper >> "$TUNNEL_LOG" 2>&1 &
CF_PID=$!

sleep 5
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Public URL: https://trading.jbrasfield.com" >> "$LOG"

# ── Start server (caffeinate keeps Mac awake while on AC power) ───────────────
caffeinate -si "$PYTHON" "$REPO/start_all.py" >> "$LOG" 2>&1

# ── Server exited — clean up tunnel ──────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Trading server exited — stopping tunnel" >> "$LOG"
kill "$CF_PID" 2>/dev/null
wait "$CF_PID" 2>/dev/null
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done." >> "$LOG"
