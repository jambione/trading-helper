#!/usr/bin/env bash
# check_agy_cli.sh — preflight for Antigravity CLI used by ai_trader research.
#
# Exit 0  = binary found and authenticated (agy models succeeds)
# Exit 1  = missing binary or not logged in
# Exit 2  = probe ran but unexpected failure
#
# Usage (on the Mac mini trading user, from a local Terminal):
#   bash scripts/check_agy_cli.sh
#   bash scripts/check_agy_cli.sh --smoke

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

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
echo "  Antigravity CLI preflight"
echo "  host: $(hostname -s 2>/dev/null || hostname)"
echo "  user: $(whoami)"
echo "========================================"

AGY_BIN=""
for candidate in \
  "${AGY_CLI_BIN:-}" \
  "${HOME}/.local/bin/agy" \
  "/opt/homebrew/bin/agy" \
  "/usr/local/bin/agy"
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    AGY_BIN="$candidate"
    break
  fi
done
if [ -z "$AGY_BIN" ] && command -v agy >/dev/null 2>&1; then
  AGY_BIN="$(command -v agy)"
fi

if [ -z "$AGY_BIN" ]; then
  echo "✗ agy not found"
  echo "  Install from https://antigravity.google/docs/cli/install"
  echo "  Expected: \$HOME/.local/bin/agy"
  exit 1
fi
echo "✓ binary: $AGY_BIN"
"$AGY_BIN" --version 2>/dev/null | head -1 | sed 's/^/  /' || true

echo "→ agy models ..."
MODELS_OUT="$("$AGY_BIN" models 2>&1 || true)"
echo "$MODELS_OUT" | sed 's/^/  /' | head -20

if echo "$MODELS_OUT" | grep -qiE "authentication required|please sign in|not logged into"; then
  echo "✗ not logged in"
  echo "  Run: agy    (from a Terminal on this machine), then restart the stack there."
  echo "  SSH cannot read the Keychain login — same rule as Claude Code."
  exit 1
fi
if ! echo "$MODELS_OUT" | grep -q "gemini-"; then
  echo "✗ unexpected models output"
  exit 2
fi
echo "✓ auth: session (agy models ok)"

if [ "$SMOKE" = true ]; then
  echo "→ smoke: agy -p PONG --output-format json"
  SMOKE_OUT="$("$AGY_BIN" -p "Reply with exactly the word PONG and nothing else." \
    --output-format json --print-timeout 90s --disable-slash-commands 2>&1 || true)"
  echo "$SMOKE_OUT" | sed 's/^/  /' | head -8
  if echo "$SMOKE_OUT" | grep -q '"status":"SUCCESS"'; then
    echo "✓ smoke ok"
  else
    echo "✗ smoke did not return SUCCESS"
    exit 2
  fi
fi

echo "========================================"
echo "  Antigravity CLI OK"
echo "========================================"
exit 0
