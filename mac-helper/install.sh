#!/bin/bash
# Installs icloud-mac-helper for the current user: a config file (mode 600), the helper, and a LaunchAgent that keeps it running.
# Run it in Terminal on the Mac. It asks for the server address, the bridge token (hidden) and the certificate fingerprint.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/icloud-mac-helper"
CFG_DIR="$HOME/.config/icloud-mac-helper"
CFG="$CFG_DIR/config.json"
LABEL="local.icloud-mac-helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/icloud-mac-helper"

[ "$(uname)" = "Darwin" ] || { echo "This helper only runs on macOS." >&2; exit 1; }
# Prefer Apple's own python3. macOS lets Apple-signed programs reach the local network, but blocks a Homebrew or python.org python
# ("No route to host") until it is granted Local Network access, which a background service cannot easily be given.
# Best of all is the Command Line Tools Python started as its app program: macOS applies a Full Disk Access grant for "Python" (needed
# for iCloud Drive) only to that executable, never to /usr/bin/python3, which is a launcher outside the app and is judged by its path.
# It also does not depend on Xcode, which can move its own Python on an update.
PY=""
for c in /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python; do
  [ -x "$c" ] && PY="$c"
done
if [ -z "$PY" ]; then
  if [ -x /usr/bin/python3 ] && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; else PY="$(command -v python3 || true)"; fi
fi
[ -n "$PY" ] || { echo "python3 is required. Install the command line tools with: xcode-select --install" >&2; exit 1; }
echo "Using $PY ($("$PY" --version 2>&1))"
case "$PY" in /usr/bin/python3|/Library/Developer/CommandLineTools/*) ;; *) echo "Note: this is not Apple's python3. If the connection fails with 'No route to host', run: xcode-select --install, then re-run this installer." ;; esac
command -v osascript >/dev/null || { echo "osascript was not found." >&2; exit 1; }
# Always through xcrun: the bare toolchain swiftc (what `xcrun --find` prints) does not know the SDK and cannot find the standard library.
xcrun --sdk macosx --find swiftc >/dev/null 2>&1 || { echo "swiftc was not found (Reminders needs it to build the helper's EventKit program). Install the command line tools with: xcode-select --install" >&2; exit 1; }

if [ -f "$CFG" ]; then
  echo "Keeping the existing config: $CFG"
else
  read -rp "Server bridge address (for example https://server.local:8001): " SERVER
  read -rsp "Bridge token (input is hidden): " TOKEN; echo
  read -rp "Server certificate fingerprint (64 hex characters, printed by the server): " FP
  mkdir -p "$CFG_DIR"; chmod 700 "$CFG_DIR"
  (umask 077; SERVER="$SERVER" TOKEN="$TOKEN" FP="$FP" CFG="$CFG" "$PY" - <<'PYEOF'
import json, os
with open(os.environ["CFG"], "w") as f:
    json.dump({"server": os.environ["SERVER"].strip(), "token": os.environ["TOKEN"].strip(), "fingerprint": os.environ["FP"].strip()}, f)
PYEOF
  )
  chmod 600 "$CFG"
  unset TOKEN
fi

mkdir -p "$DEST/ops" "$DEST/bin" "$LOGDIR"
cp "$HERE/icloud_mac_helper.py" "$DEST/"
cp "$HERE/ops/"*.js "$HERE/ops/"*.py "$DEST/ops/"
chmod 700 "$DEST/icloud_mac_helper.py"

# Reminders goes through EventKit. The program is built HERE, on this Mac, from eventkit/: the package ships source only, so SHA256SUMS
# covers exactly what gets compiled. Info.plist is embedded into the binary and the ad-hoc signature carries the same bundle id; without
# that embedded usage string macOS 27 never shows the Reminders prompt at all. An ad-hoc grant is tied to the exact build, so the program is
# rebuilt only when its source, its Info.plist or the compiler changes, and a reinstall keeps the permission already given.
EK_ID="local.icloud-mac-helper.reminders-eventkit"
EK_SRC="$HERE/eventkit/reminders-eventkit.swift"
EK_PLIST="$HERE/eventkit/Info.plist"
EK_BIN="$DEST/bin/reminders-eventkit"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$EK_PLIST")" = "$EK_ID" ] || { echo "eventkit/Info.plist must carry the bundle id $EK_ID" >&2; exit 1; }
STAMP="$( { shasum -a 256 "$EK_SRC" "$EK_PLIST" | cut -d' ' -f1; xcrun --sdk macosx swiftc --version 2>&1 | head -1; } | shasum -a 256 | cut -d' ' -f1)"
if [ -x "$EK_BIN" ] && [ "$(cat "$DEST/bin/.build-stamp" 2>/dev/null)" = "$STAMP" ] && codesign --verify "$EK_BIN" 2>/dev/null; then
  echo "Keeping the Reminders program already built from this exact source (so it keeps its Reminders permission)."
else
  echo "Building the Reminders program (EventKit)..."
  # Built under its final file name (in a scratch directory): the linker derives the binary's signature from the output name, so the same
  # source then always gives the same binary, and even a rebuild keeps the permission.
  TMP_DIR="$(mktemp -d "$DEST/bin/.build.XXXXXX")"
  if ! xcrun --sdk macosx swiftc -O "$EK_SRC" -o "$TMP_DIR/reminders-eventkit" -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$EK_PLIST" \
       || ! codesign --force --sign - --identifier "$EK_ID" "$TMP_DIR/reminders-eventkit"; then
    rm -rf "$TMP_DIR"; echo "Building the Reminders program failed (see above)." >&2; exit 1
  fi
  chmod 755 "$TMP_DIR/reminders-eventkit"
  mv -f "$TMP_DIR/reminders-eventkit" "$EK_BIN"
  rm -rf "$TMP_DIR"
  echo "$STAMP" > "$DEST/bin/.build-stamp"
fi

# iCloud Drive reads PDFs through a tiny PDFKit program, built here from pdftext/ the same way. It needs no permission of its own.
PDF_SRC="$HERE/pdftext/pdf-text.swift"
PDF_BIN="$DEST/bin/pdf-text"
PDF_STAMP="$( { shasum -a 256 "$PDF_SRC" | cut -d' ' -f1; xcrun --sdk macosx swiftc --version 2>&1 | head -1; } | shasum -a 256 | cut -d' ' -f1)"
if [ -x "$PDF_BIN" ] && [ "$(cat "$DEST/bin/.pdf-build-stamp" 2>/dev/null)" = "$PDF_STAMP" ]; then
  echo "Keeping the PDF reader already built from this exact source."
else
  echo "Building the PDF reader (PDFKit)..."
  TMP_DIR="$(mktemp -d "$DEST/bin/.build.XXXXXX")"
  if ! xcrun --sdk macosx swiftc -O "$PDF_SRC" -o "$TMP_DIR/pdf-text" || ! codesign --force --sign - "$TMP_DIR/pdf-text"; then
    rm -rf "$TMP_DIR"; echo "Building the PDF reader failed (see above)." >&2; exit 1
  fi
  chmod 755 "$TMP_DIR/pdf-text"
  mv -f "$TMP_DIR/pdf-text" "$PDF_BIN"
  rm -rf "$TMP_DIR"
  echo "$PDF_STAMP" > "$DEST/bin/.pdf-build-stamp"
fi

echo
echo "Running the self-test. It runs through launchd exactly like the background service, so the permissions you grant now land on the"
echo "service and not on Terminal. macOS may ask:"
echo "  - \"iCloud Mac Helper (Reminders)\" would like full access to your Reminders: click Allow (the self-test waits 30 seconds for it)."
echo "  - the first time Notes is used, whether python3 may control Notes: click Allow if you use Notes."
if ! ICLOUD_MAC_HELPER_PYTHON="$PY" "$PY" "$DEST/icloud_mac_helper.py" --selftest; then
  echo
  echo "The self-test reported a problem (see the JSON above)."
  read -rp "Install the background service anyway? [y/N] " GO
  [[ "$GO" =~ ^[Yy]$ ]] || { echo "Stopped. Fix the problem and run install.sh again."; exit 1; }
fi

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/icloud_mac_helper.py</string>
    <string>--run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/helper.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/helper.log</string>
</dict>
</plist>
PLISTEOF
chmod 644 "$PLIST"

UIDN="$(id -u)"
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UIDN" "$PLIST"
launchctl kickstart -k "gui/$UIDN/$LABEL"

echo
echo "Installed. The helper now runs in the background and starts at login."
echo "  Log:        $LOGDIR/helper.log"
echo "  Status:     launchctl print gui/$UIDN/$LABEL | head -20"
echo "  Uninstall:  $HERE/uninstall.sh"
echo "If Reminders requests are refused: System Settings > Privacy & Security > Reminders, enable \"iCloud Mac Helper (Reminders)\","
echo "then check again with: $PY \"$DEST/icloud_mac_helper.py\" --selftest"
echo "If Notes requests are refused: System Settings > Privacy & Security > Automation, enable Notes for python3."
