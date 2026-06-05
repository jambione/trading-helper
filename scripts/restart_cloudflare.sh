#!/usr/bin/env bash
# restart_cloudflare.sh — Kill and restart the Cloudflare tunnel

# Get the directory of this script
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"   # repo root (this script now lives in scripts/)
CONFIG_FILE="$REPO/config/cloudflared-config.yml"

echo "Stopping any running cloudflared processes..."
pkill cloudflared 2>/dev/null || true

# Give it a second to release the port/resources
sleep 1

echo "Starting cloudflared tunnel with config: $CONFIG_FILE"
# Using 'tunnel run [ID]' can sometimes be more robust than just 'tunnel run'
# ID is read from the config file.
TUNNEL_ID=$(grep '^tunnel:' "$CONFIG_FILE" | awk '{print $2}')

echo "Tunnel ID: $TUNNEL_ID"
cloudflared --config "$CONFIG_FILE" tunnel run "$TUNNEL_ID"
