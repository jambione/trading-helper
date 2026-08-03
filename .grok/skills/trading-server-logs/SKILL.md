---
name: trading-server-logs
description: >
  Inspect trading-helper logs on the Mac mini (engine, dashboard, Discord OCR,
  tunnel, startup, Claude). Use when the user says check logs, tail logs, why is
  the server down, OCR not working, tunnel issues, engine errors, or
  /trading-server-logs.
---

# Trading server — logs

## Resolve repo root

```bash
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO"
```

## Log map

| File | What it is |
|------|------------|
| `logs/engine.log` | Signal engine (largest; trades/signals/bars) |
| `logs/dashboard.log` | Dashboard / API |
| `logs/discord.log` | Discord OCR source |
| `logs/tunnel.log` | Cloudflare tunnel |
| `logs/trending.log` | Trending screener |
| `logs/claude.log` | Claude/AI-related process log |
| `logs/startup.log` | Written by `scripts/startup.command` |
| `server.log` | Older / launchd-style server log (if present at repo root) |
| `tunnel.log` | Older tunnel log at repo root (if present) |

Also useful:

```bash
./trading logs          # official dual tail when stack is up (interactive)
./trading status
```

## Default investigation flow

When the user is vague (“check logs” / “something’s wrong”):

1. **Status first**

```bash
./trading status 2>/dev/null || true
pgrep -fl 'dashboard.py|signal_engine.py|discord_source.py|cloudflared' || true
curl -s --max-time 3 http://localhost:8888/api/meta 2>/dev/null | head -c 300
curl -s --max-time 3 http://localhost:8888/api/discord/status 2>/dev/null | head -c 300
```

2. **Recent errors across main logs** (last ~200 lines each, filter noise)

```bash
for f in logs/dashboard.log logs/engine.log logs/discord.log logs/tunnel.log logs/startup.log; do
  [ -f "$f" ] || continue
  echo "======== $f (mtime $(stat -f '%Sm' "$f" 2>/dev/null || true)) ========"
  tail -n 200 "$f" | rg -i 'error|exception|traceback|failed|fatal|warn|✗|insufficient|denied' || true
done
```

3. **If a specific area is named**, go deep on that file only:

| Symptom | Focus |
|---------|--------|
| No dashboard / API | `logs/dashboard.log`, `./trading status` |
| No signals / stuck RSI | `logs/engine.log` (tail larger: 500–1000 lines) |
| OCR / tickers not updating | `logs/discord.log`, Discord status API |
| Public URL down | `logs/tunnel.log`, `pgrep -fl cloudflared` |
| Morning start failed | `logs/startup.log` then dashboard/engine |

```bash
# Engine deep dive example
tail -n 400 logs/engine.log
# Live follow only if user wants continuous watch (short timeout / user can Ctrl+C in their own terminal)
# tail -f logs/engine.log
```

4. **Time-bounded** when they say “this morning” / “today”:

```bash
# macOS: lines from today in a log (best-effort)
rg " $(date '+%Y-%m-%d')" logs/engine.log | tail -n 100
# or simply use mtime + recent tail if logs lack dates
ls -la logs/
```

## Trade / Alpaca related

Also check local state files (not only logs):

- `alpaca_trade_log.json`
- `claude_reports/trades.jsonl`
- `claude_positions_state.json`
- `signal_state.json`

Summarize last actions with timestamps; do not claim fills without evidence.

## Response format

1. Host + whether stack processes are up  
2. Top issues (errors/warnings) with **file + snippet**  
3. Likely cause in plain language  
4. Suggested fix (restart path, Discord visible for OCR, etc.) — **ask before** restarting unless user already asked to fix  

Do not dump multi-thousand-line logs into the chat; summarize and quote the smallest useful excerpts.
