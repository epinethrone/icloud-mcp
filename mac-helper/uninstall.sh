#!/bin/bash
# Stops and removes icloud-mac-helper. Keeps your config (address, token, fingerprint) unless you pass --purge.
set -uo pipefail
LABEL="local.icloud-mac-helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UIDN="$(id -u)"
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$HOME/Library/Application Support/icloud-mac-helper"
if [ "${1:-}" = "--purge" ]; then rm -rf "$HOME/.config/icloud-mac-helper" "$HOME/Library/Logs/icloud-mac-helper"; echo "Removed, including the config and logs."; else echo "Removed. The config in ~/.config/icloud-mac-helper was kept (use --purge to delete it)."; fi
