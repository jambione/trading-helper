---
name: trading-server-start
description: >
  Start or recover the Brasfield trading stack on the Mac mini (dashboard,
  signal engine, Discord OCR, Cloudflare tunnel, Discord alerts, notifier,
  window layout). Use when the user says start the server, morning startup,
  bring up the stack, run startup.command, desktop startup, or /trading-server-start.
---

# Trading server — start / morning session

Run this skill when working **on the Mac mini** (or any host that owns this repo’s live stack). Prefer the repo scripts; do not reinvent process lists.

## Resolve repo root

```bash
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO"
hostname
whoami
pwd
```

Canonical mini path: `/Users/jambimac/repo/trading-helper`.  
Backend: `http://localhost:8888`.

Ensure `PATH` includes `$HOME/.local/bin` if needed for tools.

## Safety

- Prefer **status → start/restart only if needed**. Do not kill a healthy stack.
- Do **not** place trades, change Alpaca keys, or force-push.
- Desktop scripts may open GUI apps (Discord). That is expected on the mini.
- Report what you ran and the health outcome; paste short error tails if something fails.

## Preferred entry points (pick one)

### A. Full desktop morning session (default when user wants “startup” / “morning”)

This matches the Desktop double-click script:

```bash
cd "$REPO"
bash scripts/startup.command
```

`startup.command` already:

1. Starts or recovers the stack via `./trading start|restart`
2. Opens Discord → daytrading alerts
3. Verifies Discord OCR
4. Launches notifier (if built)
5. Arranges windows
6. May launch `mac_agent` (foreground in the script’s design)

If the script blocks waiting for a keypress on failure, stop waiting after showing logs; fix the underlying issue instead of re-running blindly.

### B. Stack only (no Discord / windows) — “just the server”

```bash
cd "$REPO"
./trading status
./trading start          # or: ./trading restart  after code pull / half-dead stack
./trading status
```

### C. Lighter morning helper (legacy)

```bash
bash scripts/morning_start.sh
```

Note: some older scripts hardcode different `REPO` paths — if they fail with “could not cd”, run the **repo-relative** `scripts/startup.command` or `./trading` from the real git root instead.

## Health checks (always run after start)

```bash
./trading status
curl -s --max-time 3 http://localhost:8888/api/meta | head -c 500
curl -s --max-time 3 http://localhost:8888/api/discord/status | head -c 500
curl -s --max-time 3 http://localhost:8888/api/state | head -c 800
pgrep -fl 'dashboard.py|signal_engine.py|discord_source.py|cloudflared|momentum_signal|mac_agent' || true
```

Public URL (if tunnel up): `https://trading.jbrasfield.com`

## If backend never comes up

1. `tail -n 40 logs/dashboard.log`
2. `tail -n 40 logs/engine.log`
3. `./trading restart` once
4. If still down: stop, summarize errors, do not loop restarts

## Response format

Tell the user:

1. Host + repo path  
2. What command path you used (A/B/C)  
3. Stack health (dashboard / engine / discord OCR / tunnel)  
4. Any warnings and next action  
