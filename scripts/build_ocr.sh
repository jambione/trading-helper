#!/usr/bin/env bash
# Build the discord_ocr Swift binary used by discord_source.py.
# Run from the repo root or the scripts/ directory — the output lands at
# <repo-root>/discord_ocr regardless of where you invoke the script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$ROOT/discord_ocr.swift"
BIN="$ROOT/discord_ocr"

if ! command -v swiftc &>/dev/null; then
    echo "ERROR: swiftc not found. Install Xcode or the Command Line Tools:"
    echo "  xcode-select --install"
    exit 1
fi

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: $SRC not found."
    exit 1
fi

# Skip compilation when the binary is already up-to-date.
if [[ -f "$BIN" && "$BIN" -nt "$SRC" ]]; then
    echo "discord_ocr is up-to-date — nothing to do."
    exit 0
fi

echo "Compiling $SRC → $BIN …"
swiftc "$SRC" -o "$BIN"
echo "Done. Run 'python discord_source.py --check' to verify."
