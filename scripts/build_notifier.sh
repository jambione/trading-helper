#!/usr/bin/env bash
# build_notifier.sh — compile mac_notifier/notifier.swift into BrasfieldNotifier.app.
#
# Produces a proper .app bundle (UNUserNotificationCenter needs bundle identity)
# signed with your Apple Development cert so macOS keeps the notification
# permission across rebuilds. Output: <repo-root>/BrasfieldNotifier.app
#
# Usage:  scripts/build_notifier.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/mac_notifier/notifier.swift"
APP="$ROOT/BrasfieldNotifier.app"
MACOS_DIR="$APP/Contents/MacOS"
BIN="$MACOS_DIR/BrasfieldNotifier"
BUNDLE_ID="com.brasfield.notifier"

if ! command -v swiftc &>/dev/null; then
    echo "ERROR: swiftc not found. Install Xcode or the Command Line Tools." >&2
    exit 1
fi
if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found." >&2
    exit 1
fi

echo "Building BrasfieldNotifier.app …"
rm -rf "$APP"
mkdir -p "$MACOS_DIR" "$APP/Contents/Resources"

swiftc "$SRC" -o "$BIN" \
    -framework AppKit -framework Foundation \
    -framework Network -framework UserNotifications

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>BrasfieldNotifier</string>
    <key>CFBundleDisplayName</key>     <string>Brasfield Trading</string>
    <key>CFBundleExecutable</key>      <string>BrasfieldNotifier</string>
    <key>CFBundleIdentifier</key>      <string>${BUNDLE_ID}</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>LSUIElement</key>            <true/>
    <key>LSMinimumSystemVersion</key>  <string>13.0</string>
</dict>
</plist>
PLIST

# Sign with the Apple Development identity if present (stable signature → the
# notification permission sticks). Fall back to ad-hoc otherwise.
IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
            | awk -F'"' '/Apple Development/{print $2; exit}')"
if [ -n "$IDENTITY" ]; then
    echo "Signing with: $IDENTITY"
    codesign --force --options runtime --sign "$IDENTITY" "$APP"
else
    echo "No Apple Development identity — ad-hoc signing (permission may re-prompt on rebuild)."
    codesign --force --sign - "$APP"
fi

codesign --verify --verbose "$APP" 2>&1 | sed 's/^/  /' || true
echo "✓ Built $APP"
echo "  First run will prompt for notification permission — click Allow."
