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
PY=""
if [ -x /usr/bin/python3 ] && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; else PY="$(command -v python3 || true)"; fi
[ -n "$PY" ] || { echo "python3 is required. Install the command line tools with: xcode-select --install" >&2; exit 1; }
echo "Using $PY ($("$PY" --version 2>&1))"
case "$PY" in /usr/bin/python3) ;; *) echo "Note: this is not Apple's python3. If the connection fails with 'No route to host', run: xcode-select --install, then re-run this installer." ;; esac
command -v osascript >/dev/null || { echo "osascript was not found." >&2; exit 1; }

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

mkdir -p "$DEST/ops" "$LOGDIR"
cp "$HERE/icloud_mac_helper.py" "$DEST/"
cp "$HERE/ops/"*.js "$DEST/ops/"
chmod 700 "$DEST/icloud_mac_helper.py"

echo
echo "Running the self-test (on a Mac with large Reminders lists this can take a minute). macOS may now ask whether to allow control of Reminders: click Allow."
if ! "$PY" "$DEST/icloud_mac_helper.py" --selftest; then
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
echo "If the first Reminders request is blocked, open System Settings > Privacy & Security > Automation and enable Reminders for python3."
