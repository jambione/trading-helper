#!/usr/bin/env bash
# deploy_mini.sh — ship local trading-helper changes to the Mac mini and reload.
#
# Typical dashboard loop (from your MacBook, inside the repo):
#   1. edit + commit (optional)
#   2. ./scripts/deploy_mini.sh
#      → push origin, ssh pull on mini, ./trading restart, health check
#
# You usually do NOT need full Desktop shutdown/startup for dashboard/engine code.
# Use --full only when Discord / windows / mac_agent need a full session reset.
#
# Usage:
#   ./scripts/deploy_mini.sh              # push (if needed) + pull + restart stack
#   ./scripts/deploy_mini.sh --no-push    # mini pull + restart only (already on GitHub)
#   ./scripts/deploy_mini.sh --pull-only  # pull on mini, do not restart
#   ./scripts/deploy_mini.sh --full       # pull + scripts/shutdown.command + startup.command
#   ./scripts/deploy_mini.sh --status     # remote status + curl only
#
# Env overrides:
#   MINI_SSH   default: jambimac@Jonathans-Mac-mini.local
#   MINI_REPO  default: /Users/jambimac/repo/trading-helper

set -euo pipefail

MINI_SSH="${MINI_SSH:-jambimac@Jonathans-Mac-mini.local}"
MINI_REPO="${MINI_REPO:-/Users/jambimac/repo/trading-helper}"
BACKEND_LOCAL="http://localhost:8888"
PUBLIC_URL="https://trading.jbrasfield.com"

DO_PUSH=1
DO_RESTART=1
FULL_SESSION=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --no-push)   DO_PUSH=0 ;;
    --pull-only) DO_RESTART=0 ;;
    --full)      FULL_SESSION=1; DO_RESTART=1 ;;
    --status)    STATUS_ONLY=1; DO_PUSH=0; DO_RESTART=0 ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg (try --help)"
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ssh_mini() {
  ssh -o BatchMode=yes -o ConnectTimeout=12 \
    -o IdentitiesOnly=yes -i "${HOME}/.ssh/id_ed25519" \
    "$MINI_SSH" "$@"
}

echo ""
echo "============================================"
echo "  Deploy → Mac mini"
echo "  local : $ROOT"
echo "  remote: $MINI_SSH:$MINI_REPO"
echo "============================================"
echo ""

# ── Preflight ──────────────────────────────────────────────────────────────
if ! ssh_mini "test -d '$MINI_REPO/.git'"; then
  echo "❌ Cannot reach mini or repo missing at $MINI_REPO"
  echo "   Fix SSH (ssh $MINI_SSH) or set MINI_SSH / MINI_REPO."
  exit 1
fi
echo "✓ SSH + remote repo OK"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "✓ Local branch: $BRANCH"

if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ Working tree is dirty. Uncommitted changes will NOT deploy."
  git status -sb | head -n 20
  echo "   Commit first, or continue to deploy last committed revision only."
  echo ""
fi

if [ "$STATUS_ONLY" = 1 ]; then
  echo "[status] Remote git + stack"
  ssh_mini "cd '$MINI_REPO' && git status -sb && git log --oneline -3 && echo && ./trading status 2>/dev/null || true"
  ssh_mini "curl -s --max-time 3 $BACKEND_LOCAL/api/meta | head -c 400; echo"
  exit 0
fi

# ── Push ───────────────────────────────────────────────────────────────────
if [ "$DO_PUSH" = 1 ]; then
  echo "[1/3] git fetch + push (if ahead of origin)"
  git fetch origin 2>/dev/null || git fetch
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    AHEAD="$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
    BEHIND="$(git rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)"
    echo "   ahead=$AHEAD behind=$BEHIND (vs upstream)"
    if [ "${BEHIND:-0}" -gt 0 ]; then
      echo "❌ Local is behind origin by $BEHIND commit(s). Pull/rebase locally first."
      exit 1
    fi
    if [ "${AHEAD:-0}" -gt 0 ]; then
      git push
      echo "   ✓ pushed"
    else
      echo "   • nothing to push"
    fi
  else
    echo "   ⚠ No upstream set — attempting git push -u origin HEAD"
    git push -u origin HEAD
  fi
else
  echo "[1/3] skip push (--no-push)"
fi

# ── Pull on mini ───────────────────────────────────────────────────────────
echo "[2/3] git pull --ff-only on mini ($BRANCH)"
ssh_mini "bash -s" <<REMOTE
set -euo pipefail
cd '$MINI_REPO'
git fetch origin
# Prefer same branch name as local when it exists remotely
if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  git checkout "$BRANCH" 2>/dev/null || git checkout -B "$BRANCH" "origin/$BRANCH"
  git pull --ff-only origin "$BRANCH"
else
  git pull --ff-only
fi
echo "   HEAD: \$(git log -1 --oneline)"
git status -sb
REMOTE
echo "   ✓ pull done"

# ── Reload ─────────────────────────────────────────────────────────────────
if [ "$DO_RESTART" = 0 ]; then
  echo "[3/3] skip restart (--pull-only)"
elif [ "$FULL_SESSION" = 1 ]; then
  echo "[3/3] full session: ./trading desk"
  echo "   (shutdown → close terminals → caffeinate → startup on the Mini)"
  ssh_mini "cd '$MINI_REPO' && ./trading desk"
else
  echo "[3/3] ./trading restart  (reloads dashboard/engine/OCR code — not full desktop)"
  ssh_mini "cd '$MINI_REPO' && ./trading restart && ./trading status"
fi

# ── Health ─────────────────────────────────────────────────────────────────
echo ""
echo "Health (on mini):"
ssh_mini "bash -s" <<'REMOTE'
set +e
curl -s --max-time 3 http://localhost:8888/api/meta >/dev/null && echo "  ✓ dashboard /api/meta" || echo "  ✗ dashboard not answering"
curl -s --max-time 3 http://localhost:8888/api/state 2>/dev/null | python3 -c '
import sys,json
try:
  v=(json.load(sys.stdin).get("version") or {})
  print("  version dashboard=%s engine=%s" % (v.get("dashboard"), v.get("engine")))
except Exception as e:
  print("  (no version yet)", e)
' 2>/dev/null
pgrep -fl 'dashboard.py|signal_engine.py|discord_source.py' | sed 's/^/  proc /' || echo "  (no stack procs)"
REMOTE

echo ""
# A stack restarted over SSH cannot read the mini's login Keychain, so the
# Claude CLI comes back logged out even though the credential is present and
# valid — Anthropic research then silently skips every scheduled run. Grok is
# unaffected (its credential is a file). This is a property of macOS session
# gating, not something the script can fix, so it reports rather than pretends.
if ssh_mini "cd '$MINI_REPO' && grep -qE 'claude_auth=fail|agy_auth=fail' logs/ai_trader.log 2>/dev/null"; then
  echo "⚠  claude_auth=fail — this SSH restart logged the Claude CLI out."
  echo "   The credential is still in the mini's Keychain; \`claude /login\`"
  echo "   is NOT the fix. To restore Anthropic research, restart the stack"
  echo "   from a Terminal window ON THE MINI (scripts/session.command)."
  echo "   Trading and Grok research are unaffected."
  echo ""
fi
echo "Done. Hard-refresh the dashboard (Cmd+Shift+R): $BACKEND_LOCAL"
echo "Public (if tunnel up): $PUBLIC_URL"
echo ""
