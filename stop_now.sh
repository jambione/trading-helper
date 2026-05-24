#!/usr/bin/env bash
cd /Users/jonathanbrasfield/repo/trading-helper/trading-helper
bash stop_trading_server.sh
pkill -f "cloudflared" 2>/dev/null
echo "✅ Trading server + Cloudflare tunnel stopped."
