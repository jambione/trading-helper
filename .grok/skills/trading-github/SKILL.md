---
name: trading-github
description: >
  Git/GitHub workflow for trading-helper on the Mac mini or local clone: status,
  fetch, pull latest, branch hygiene, and optional PR checks. Use when the user
  says pull latest, sync with GitHub, git status, update the codebase, fetch
  origin, open a PR, or /trading-github.
---

# Trading helper — GitHub / git

## Resolve repo root

```bash
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO"
hostname
git remote -v
git branch -vv
git status -sb
```

Typical branch on mini: `master-mac` tracking `origin/master-mac`.

## Safety

- **Default is pull / inspect only.** Never `git push --force`, `reset --hard`, or discard uncommitted work unless the user explicitly asks.
- If the working tree is dirty, **show status and diff summary first**; only pull with a strategy that won’t clobber local changes (`--ff-only` preferred).
- Do not rewrite published history.
- Do not commit secrets (`signal_engine.env`, `config/secrets.json`, keys).

## Common operations

### 1. Health snapshot (always start here)

```bash
git status -sb
git remote update 2>/dev/null || git fetch --all --prune
git status -sb
git log --oneline -5
git log --oneline HEAD..@{u} 2>/dev/null || true   # commits to pull
git log --oneline @{u}..HEAD 2>/dev/null || true   # commits to push
```

### 2. Pull latest (most common ask)

Prefer fast-forward only:

```bash
git pull --ff-only
```

If `--ff-only` fails:

- Show diverging commits.
- Ask whether to merge, rebase, or stash → pull → pop.
- Do **not** rebase shared branches without explicit approval.

If dirty tree blocks pull:

```bash
git status -sb
git stash push -u -m "wip before pull $(date +%Y%m%d-%H%M)"
git pull --ff-only
git stash pop
```

Only stash when the user wants latest code and local WIP is not meant to block.

### 3. After pull — when stack is running

Code changes do **not** auto-reload Python processes. Tell the user:

- Stack-only refresh: `./trading restart`
- Full session: stop then start skills / `scripts/shutdown.command` + `scripts/startup.command`

Do **not** restart automatically unless they asked to deploy/restart after pull.

### 4. Optional GitHub CLI

If `gh` is authenticated:

```bash
gh auth status
gh pr status
gh pr list --limit 10
gh run list --limit 5
```

For PR creation, follow normal repo conventions; confirm title/body before `gh pr create`.

### 5. Push (only if asked)

```bash
git status -sb
git push -u origin HEAD
```

Refuse force-push to `master` / `master-mac` / `main` unless explicitly requested with awareness of the risk.

## Response format

1. Branch + tracking + clean/dirty  
2. What was behind/ahead of origin  
3. Actions taken (fetch/pull/stash)  
4. Whether a **process restart** is needed for the live stack  
5. Next step if conflicts or auth failed  
