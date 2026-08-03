---
name: trading-deploy
description: >
  Deploy trading-helper from this machine to the Mac mini: git push, remote
  pull, reload stack (./trading restart). Use when the user wants to deploy,
  ship dashboard changes to the server, push and pull on mini, avoid full
  shutdown/startup, sync MacBook to mini, or /trading-deploy.
---

# Trading helper — deploy MacBook → Mac mini

## Goal

Replace the slow loop:

> edit → commit → push → SSH → pull → Desktop shutdown → Desktop startup

with a **short path** for code (especially dashboard) changes:

> edit → commit → `./scripts/deploy_mini.sh` (or this skill)

Full Desktop start/stop is **only** for session/GUI recovery (Discord OCR layout, mac_agent, window arrange).

## When you are on which machine

| Where Grok is running | What to do |
|----------------------|------------|
| **MacBook** (this skill’s home) | Commit if needed → run deploy script → verify |
| **Mac mini** | Prefer edit + commit + push from mini, then `./trading restart` locally. No SSH deploy needed. |

If the user is on the mini and only wants reload: run `./trading restart` (see trading-server-start skill for full morning session).

## Default remote

- SSH: `jambimac@Jonathans-Mac-mini.local` (keys required; BatchMode)
- Repo: `/Users/jambimac/repo/trading-helper`
- Overrides: `MINI_SSH`, `MINI_REPO`

## Agent procedure (MacBook)

### 1. Preflight

```bash
cd "$(git rev-parse --show-toplevel)"
git status -sb
git branch -vv
ssh -o BatchMode=yes -o ConnectTimeout=12 -o IdentitiesOnly=yes -i "$HOME/.ssh/id_ed25519" \
  jambimac@Jonathans-Mac-mini.local 'hostname; cd ~/repo/trading-helper && git status -sb'
```

If SSH fails: stop and tell the user keys/network are down. Do not invent another host without checking.

### 2. Uncommitted work

- If dirty and user asked to deploy **their changes**: help them **commit first** (clear message; never commit secrets).
- If dirty and they only want what’s already committed: warn and continue.

### 3. Deploy (preferred one-liner)

```bash
./scripts/deploy_mini.sh
```

Flags:

| Flag | Meaning |
|------|---------|
| (default) | push if ahead → pull on mini → `./trading restart` → health |
| `--no-push` | mini already has commits / push done |
| `--pull-only` | update code on mini, leave processes alone |
| `--full` | pull + `shutdown.command` + `startup.command` (GUI session) |
| `--status` | remote git + stack status only |

If the script is not executable: `chmod +x scripts/deploy_mini.sh`.

### 4. Verify

- Script prints `/api/meta` health and process list.
- Tell user to **hard-refresh** browser: Cmd+Shift+R on `http://localhost:8888` (on mini) or `https://trading.jbrasfield.com`.
- On failure: `ssh … 'cd ~/repo/trading-helper && tail -n 40 logs/dashboard.log'`.

## What NOT to do by default

- Do **not** run full `shutdown.command` / `startup.command` for a normal dashboard CSS/JS/Python edit — `./trading restart` reloads the Python stack.
- Do **not** `git push --force` to `master-mac` / `main`.
- Do **not** `git reset --hard` on the mini if it has unexpected local changes — show status and ask.
- Do **not** restart if user only asked to push to GitHub.

## Faster alternatives (mention when relevant)

1. **Edit on mini Grok** in `~/repo/trading-helper` → commit → `./trading restart` (no MacBook round-trip).
2. **Pull-only** when mini is the runtime and GitHub already has the commit: `./scripts/deploy_mini.sh --no-push`.
3. **Full session** when OCR/Discord/windows are broken: `./scripts/deploy_mini.sh --full` or trading-server-stop + trading-server-start skills on the mini.

## Response format

1. Branch + dirty/clean  
2. Pushed? / remote HEAD  
3. Restart type (stack vs full)  
4. Health result  
5. Reminder: hard-refresh dashboard  
