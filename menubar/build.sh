#!/bin/zsh
# Builds "iCloud MCP Control.app" into menubar/build/ and signs it ad hoc (for your own Mac; see README for notarised builds).
set -euo pipefail
HERE="${0:A:h}"
SCRATCH="${SCRATCH:-$HERE/.build}"
APP="$HERE/build/iCloud MCP Control.app"
swift build -c release --package-path "$HERE" --scratch-path "$SCRATCH"
BIN="$(swift build -c release --package-path "$HERE" --scratch-path "$SCRATCH" --show-bin-path)/ICloudMCPControl"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ICloudMCPControl"
cp "$HERE/Resources/Info.plist" "$APP/Contents/Info.plist"
[ -f "$HERE/Resources/AppIcon.icns" ] && cp "$HERE/Resources/AppIcon.icns" "$APP/Contents/Resources/"
codesign --force --sign - --options runtime --identifier io.github.epinethrone.icloud-mcp-control "$APP"
codesign --verify --strict "$APP"
echo "Built $APP"
