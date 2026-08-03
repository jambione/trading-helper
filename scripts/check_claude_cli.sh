#!/usr/bin/env bash
# check_claude_cli.sh — preflight for Claude Code CLI used by ai_trader research.
#
# Exit 0  = binary found and authenticated (subscription login or API key)
# Exit 1  = missing binary or not logged in
# Exit 2  = probe ran but unexpected failure
#
# Usage (on the Mac mini trading user):
#   bash scripts/check_claude_cli.sh
#   bash scripts/check_claude_cli.sh --smoke   # also send a tiny -p prompt
#
# Put this in morning startup so empty research from "Not logged in" is caught
# before the open.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Headless / SSH PATH is often bare — include user + Homebrew bins.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

SMOKE=false
for arg in "$@"; do
  case "$arg" in
    --smoke|-s) SMOKE=true ;;
    -h|--help)
      echo "Usage: $0 [--smoke]"
      exit 0
      ;;
  esac
done

echo "========================================"
echo "  Claude CLI preflight"
echo "  host: $(hostname -s 2>/dev/null || hostname)"
echo "  user: $(whoami)"
echo "========================================"

CLAUDE_BIN=""
for candidate in \
  "${CLAUDE_CLI_BIN:-}" \
  "${HOME}/.local/bin/claude" \
  "/opt/homebrew/bin/claude" \
  "/usr/local/bin/claude"
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    CLAUDE_BIN="$candidate"
    break
  fi
done
if [ -z "$CLAUDE_BIN" ] && command -v claude >/dev/null 2>&1; then
  CLAUDE_BIN="$(command -v claude)"
fi

if [ -z "$CLAUDE_BIN" ]; then
  echo "✗ Claude CLI not found"
  echo "  Install Claude Code, or set CLAUDE_CLI_BIN to the binary path."
  echo "  Expected: \$HOME/.local/bin/claude"
  exit 1
fi
echo "✓ binary: $CLAUDE_BIN"
"$CLAUDE_BIN" --version 2>/dev/null | head -1 | sed 's/^/  /' || true

# API key short-circuit (server-style auth)
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${CLAUDE_API_KEY:-}" ]; then
  echo "✓ auth: API key present in environment (ANTHROPIC_API_KEY/CLAUDE_API_KEY)"
  if [ "$SMOKE" = true ]; then
    echo "  smoke: skipped (API key path — use research run to verify)"
  fi
  echo "========================================"
  echo "  Claude CLI OK"
  echo "========================================"
  exit 0
fi

# Load signal_engine.env if present (optional API key there)
if [ -f "$REPO/signal_engine.env" ]; then
  # shellcheck disable=SC1091
  set +u
  # Export KEY=value lines without sourcing secrets into the shell log.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    case "$line" in
      ANTHROPIC_API_KEY=*|CLAUDE_API_KEY=*)
        key="${line%%=*}"
        val="${line#*=}"
        val="${val%%#*}"
        val="$(echo "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^["'\'']//;s/["'\'']$//')"
        if [ -n "$val" ]; then
          export "$key=$val"
          echo "✓ auth: $key found in signal_engine.env"
          echo "========================================"
          echo "  Claude CLI OK"
          echo "========================================"
          exit 0
        fi
        ;;
    esac
  done < "$REPO/signal_engine.env"
  set -u
fi

echo "→ claude auth status ..."
STATUS_OUT="$("$CLAUDE_BIN" auth status 2>&1 || true)"
echo "$STATUS_OUT" | sed 's/^/  /' | head -20

LOGGED_IN=false
if echo "$STATUS_OUT" | grep -qiE '"loggedIn"[[:space:]]*:[[:space:]]*true'; then
  LOGGED_IN=true
elif echo "$STATUS_OUT" | grep -qiE 'not logged in|please run /login|"loggedIn"[[:space:]]*:[[:space:]]*false'; then
  LOGGED_IN=false
elif echo "$STATUS_OUT" | grep -qiE '"loggedIn"[[:space:]]*:[[:space:]]*true|logged in'; then
  LOGGED_IN=true
fi

# Prefer Python probe (same logic as ai_trader) when available.
PY=""
for candidate in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
  "$REPO/venv/bin/python3" "$REPO/.venv/bin/python3" python3; do
  if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -n "$PY" ]; then
  if "$PY" - <<'PY' 2>/dev/null
import sys
sys.path.insert(0, ".")
from ai_suggest import claude_auth_status, resolve_claude_cli
import os
st = claude_auth_status(os.environ.get("CLAUDE_CLI_BIN"))
print("python_probe logged_in=", st.get("logged_in"), "error=", (st.get("error") or "")[:120])
sys.exit(0 if st.get("logged_in") else 1)
PY
  then
    LOGGED_IN=true
    echo "✓ python probe: logged_in=true"
  else
    LOGGED_IN=false
    echo "✗ python probe: not logged in"
  fi
fi

if [ "$LOGGED_IN" != true ]; then
  echo ""
  echo "✗ Claude CLI is NOT logged in on this machine/user."
  echo ""
  echo "  Fix (on this Mac, as user $(whoami) — needs a browser):"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo "    claude /login"
  echo "    claude auth status"
  echo ""
  echo "  Or set ANTHROPIC_API_KEY in signal_engine.env (never commit it)."
  echo "========================================"
  exit 1
fi

echo "✓ auth: logged in"

if [ "$SMOKE" = true ]; then
  echo "→ smoke prompt (claude -p) ..."
  SMOKE_OUT="$("$CLAUDE_BIN" -p "Reply with exactly: OK" --output-format text 2>&1 || true)"
  echo "$SMOKE_OUT" | sed 's/^/  /' | head -10
  if echo "$SMOKE_OUT" | grep -qiE 'not logged in|please run /login'; then
    echo "✗ smoke failed: still not logged in"
    exit 1
  fi
  if echo "$SMOKE_OUT" | grep -q 'OK'; then
    echo "✓ smoke: OK"
  else
    echo "⚠ smoke: unexpected output (auth may still be OK)"
    # Don't hard-fail: models can be flaky; auth status already passed.
  fi
fi

echo "========================================"
echo "  Claude CLI OK"
echo "========================================"
exit 0
