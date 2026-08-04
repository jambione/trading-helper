#!/usr/bin/env bash
# Build DiscordOCR.app + repo-root discord_ocr symlink used by discord_source.py.
#
# Packaging as a real .app gives macOS a stable TCC identity so Screen Recording
# "Allow" sticks (bare CLI binaries + Python parents re-prompt endlessly on
# macOS 15+/26).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$ROOT/discord_ocr.swift"
APP="$ROOT/DiscordOCR.app"
MACOS_DIR="$APP/Contents/MacOS"
BIN="$MACOS_DIR/discord_ocr"
LINK="$ROOT/discord_ocr"
IDENT="com.jbrasfield.trading-helper.discord-ocr"
DISPLAY_NAME="Discord OCR"

if ! command -v swiftc &>/dev/null; then
    echo "ERROR: swiftc not found. Install Xcode or the Command Line Tools:"
    echo "  xcode-select --install"
    exit 1
fi

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: $SRC not found."
    exit 1
fi

# Rebuild if missing, source newer, or not an app bundle yet.
need_build=0
if [[ ! -x "$BIN" ]]; then
    need_build=1
elif [[ "$SRC" -nt "$BIN" ]]; then
    need_build=1
fi

if [[ "$need_build" -eq 0 ]]; then
    echo "DiscordOCR.app is up-to-date — nothing to do."
    # Keep flat path in sync for discord_source.py
    ln -sfn "DiscordOCR.app/Contents/MacOS/discord_ocr" "$LINK"
    exit 0
fi

echo "Compiling $SRC → $BIN …"
mkdir -p "$MACOS_DIR" "$APP/Contents/Resources"
swiftc -O "$SRC" -o "$BIN"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>discord_ocr</string>
    <key>CFBundleIdentifier</key>
    <string>${IDENT}</string>
    <key>CFBundleName</key>
    <string>${DISPLAY_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${DISPLAY_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo "Codesigning ${DISPLAY_NAME}.app ($IDENT) …"
codesign --force --deep --sign - --identifier "$IDENT" "$APP"

# Flat path expected by discord_source.py / docs
ln -sfn "DiscordOCR.app/Contents/MacOS/discord_ocr" "$LINK"

echo "Done → $APP"
echo
echo "ONE-TIME PERMISSION (stops the repeating dialog):"
echo "  1. System Settings → Privacy & Security → Screen & System Audio Recording"
echo "  2. Click + and add:  $APP"
echo "     (or enable **${DISPLAY_NAME}** if it already appears)"
echo "  3. Optionally turn OFF Screen Recording for bare 'Python' — that was the"
echo "     spammy dialog; OCR does not need Python to capture."
echo "  4. Verify:  python3 discord_source.py --check"
echo
# Open the privacy pane to make the click path obvious.
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
