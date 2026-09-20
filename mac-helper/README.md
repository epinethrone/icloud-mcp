# icloud-mac-helper

Lets the icloud-mcp server use **Reminders** and **Notes** on your Mac. Apple only exposes those through their own apps, which are
scriptable on a Mac, so this small helper runs there and does the work when the server asks.

**How it works.** The helper connects *out* to a private HTTPS port of your server and asks "any work?". It opens no listening port.
The server never sends script text: only an operation name and validated arguments from a fixed list. Each operation is a static
script in `ops/` that receives its arguments as JSON in `argv`, so text from a reminder, a note or an AI can never become code.
The connection uses a self-signed certificate that the helper pins by fingerprint, plus a bearer token.

**Requirements.** macOS with the command line tools (`xcode-select --install`), Reminders and Notes signed in to iCloud, and a
network path to the server's bridge port (your home network or your VPN). It only works while the Mac is on and logged in.

## Install

1. On the server, enable the bridge in `.env` (`ENABLE_REMINDERS=true`, `BRIDGE_TOKEN=...`) and restart. The server logs, and writes to
   `bridge_fingerprint.txt` in its data folder, the certificate fingerprint.
2. Copy this `mac-helper` folder to the Mac and run, in Terminal:
   ```bash
   ./install.sh
   ```
   It asks for the bridge address, the token (hidden) and the fingerprint, runs the self-test, and installs a LaunchAgent.
3. macOS asks whether to allow control of Reminders (and, the first time Notes is used, Notes). Click **Allow**. If you missed it:
   System Settings > Privacy & Security > Automation, and enable Reminders and Notes for python3.

## Check it

```bash
python3 "$HOME/Library/Application Support/icloud-mac-helper/icloud_mac_helper.py" --selftest
```
prints one JSON report with pass/fail, counts and timings, never any reminder or note content. It times one read of each Reminders list, which takes
about 15 ms per reminder (a list with a thousand completed reminders takes ~16 s), so the self-test can run for a minute on a Mac with large lists. Notes access is reported but is optional: it only fails the
install if you enable Notes on the server and macOS has not granted access. It also proves that hostile strings
(quotes, backslashes, newlines, script-looking text) reach the scripts as plain data. In Claude, the `mac_helper_status` tool says
whether the helper is online.

To also prove the write operations on this Mac, run it with `--selftest-write`. It creates, edits, completes and deletes one temporary reminder
and creates and deletes one temporary note (both named `icloud-mac-helper selftest ...`), removing them even if a step fails. It touches your data, so
it is never run automatically.

## Uninstall

```bash
./uninstall.sh            # keeps the config
./uninstall.sh --purge    # also deletes the config and logs
```

## Notes

* Log file: `~/Library/Logs/icloud-mac-helper/helper.log` (errors only, no content).
* Scripting Notes and Reminders is slow on very large libraries, and locked notes cannot be read. Reminders scans a whole list for almost every request, so
  the helper keeps a cache of your *active* reminders (refreshed in the background, big lists at most every 5 minutes) and reaches reminders by position
  instead of by id. Completed reminders are never read.
* The token lives in `~/.config/icloud-mac-helper/config.json` (mode 600). Treat it like a password; rotate it by changing
  `BRIDGE_TOKEN` on the server and re-running `install.sh` after deleting that file.
