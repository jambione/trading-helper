---
name: trading-server-stop
description: >
  Shut down the Brasfield trading session on the Mac mini (Discord, local stack,
  tunnel, mac_agent, momentum monitor). Use when the user says stop the server,
  evening shutdown, shut down, kill the stack, run shutdown.command, desktop
  shutdown, or /trading-server-stop.
---

# Trading server — stop / evening session

## Resolve repo root

```bash
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO"
hostname; whoami; pwd
```

## Safety

- Confirm intent if the user only said something ambiguous (“stop”) while debugging — if they clearly want shutdown, proceed.
- Prefer the **official scripts** over ad-hoc `kill -9` of unrelated processes.
- Do **not** delete data, clear watchlists via UI automation, or force-push.
- Optional watchlist cleanup in `shutdown.command` stays **commented out** — leave it disabled unless the user explicitly asks and provides steps.

## Preferred entry points

### A. Full desktop shutdown (default)

```bash
cd "$REPO"
bash scripts/shutdown.command
```

This:

1. Quits Discord  
2. Runs `./trading stop` (dashboard + engine + OCR + tunnel)  
3. Stops `mac_agent`  
4. Stops `momentum_signal.py`  

### B. Stack only

```bash
cd "$REPO"
./trading stop
./trading status
```

### C. Legacy evening script

```bash
bash scripts/evening_stop.sh
```

## Verify stopped

```bash
./trading status 2>/dev/null || true
pgrep -fl 'dashboard.py|signal_engine.py|discord_source.py|start_all.py|cloudflared.*trading|mac_agent|momentum_signal' || echo "No matching processes"
curl -s --max-time 2 http://localhost:8888/api/meta >/dev/null && echo "WARNING: backend still answering" || echo "Backend down (expected)"
```

If stragglers remain after the script, re-run `./trading stop`, then only target leftover PIDs that still match the patterns above. Do not broad-kill Python.

## Response format

1. What you ran  
2. Process / backend check  
3. Anything still running and whether a second pass is needed  
