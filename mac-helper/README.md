# icloud-mac-helper

Lets the icloud-mcp server use **Reminders** and **Notes** on your Mac. Apple only exposes those through their own apps, which are
scriptable on a Mac, so this small helper runs there and does the work when the server asks.

**How it works.** The helper connects *out* to a private HTTPS port of your server and asks "any work?". It opens no listening port.
The server never sends script text: only an operation name and validated arguments from a fixed list. Reminders operations run a
small EventKit program (`bin/reminders-eventkit`, built from `eventkit/` on your Mac by the installer); Notes operations run a static
script in `ops/`. Both receive their arguments as one JSON value in `argv`, so text from a reminder, a note or an AI can never become code.
The connection uses a self-signed certificate that the helper pins by fingerprint, plus a bearer token.

**Requirements.** macOS 14 or newer with the command line tools (`xcode-select --install`; the installer compiles the Reminders program with
`swiftc`), Reminders and Notes signed in to iCloud, and a
network path to the server's bridge port (your home network or your VPN). It only works while the Mac is on and logged in.

## Install

1. On the server, enable the bridge in `.env` (`ENABLE_REMINDERS=true`, `BRIDGE_TOKEN=...`) and restart. The server logs, and writes to
   `bridge_fingerprint.txt` in its data folder, the certificate fingerprint.
2. Copy this `mac-helper` folder to the Mac and run, in Terminal:
   ```bash
   ./install.sh
   ```
   It asks for the bridge address, the token (hidden) and the fingerprint, builds the Reminders program, runs the self-test, and installs
   a LaunchAgent. Do this while you are at the Mac and it is unlocked: macOS cannot show its permission prompts on the lock screen.
3. macOS asks two separate things. Click **Allow** on both:
   * **"iCloud Mac Helper (Reminders)" would like full access to your Reminders.** This is the grant Reminders needs. If you missed it:
     System Settings > Privacy & Security > Reminders, enable "iCloud Mac Helper (Reminders)", and run the self-test again.
   * **python3 wants to control Notes** (the first time Notes is used). If you missed it: System Settings > Privacy & Security > Automation.

   The self-test runs itself through launchd, exactly like the background service, so these permissions land on the service. (Run from
   Terminal directly, macOS would attribute them to Terminal and the service would still have none.)

## Check it

```bash
python3 "$HOME/Library/Application Support/icloud-mac-helper/icloud_mac_helper.py" --selftest
```
prints one JSON report with pass/fail, counts and timings, never any reminder or note content. If Reminders access is missing, `reminders_access`
says which state macOS reports (`notDetermined`, `denied`, `restricted` or `writeOnly`) and names the grant it needs. Notes access is reported but is optional: it only fails the
install if you enable Notes on the server and macOS has not granted access. It also proves that hostile strings
(quotes, backslashes, newlines, script-looking text) reach the scripts as plain data. In Claude, the `mac_helper_status` tool says
whether the helper is online.

To also prove the write operations on this Mac, run it with `--selftest-write`. It creates, edits (including through an old-style
`x-apple-reminder://` id), completes and deletes one temporary reminder
and creates and deletes one temporary note (both named `icloud-mac-helper selftest ...`), removing them even if a step fails. It touches your data, so
it is never run automatically.

## Uninstall

```bash
./uninstall.sh            # keeps the config
./uninstall.sh --purge    # also deletes the config and logs
```

## Notes

* Log file: `~/Library/Logs/icloud-mac-helper/helper.log` (errors only, no content).
* Reminders go through EventKit and every read is live: about 20-40 ms per operation, whatever the size of your lists. Reminder ids are
  EventKit's; ids in the older `x-apple-reminder://...` form are still accepted. Completed reminders are never returned.
* The Reminders program is rebuilt only when its source changes, and the same source always gives the same program, so reinstalling
  keeps the Reminders permission. The JXA Reminders scripts in `ops/` are no longer run; they stay as reference for things EventKit
  cannot do (subtasks, tags, sections, attachments).
* `SHA256SUMS` in the package covers **source only**, including `eventkit/reminders-eventkit.swift` and `eventkit/Info.plist`. That is
  deliberate, not a gap: the Reminders program is compiled on your Mac at install time, so there is no binary to checksum. Do not "fix" this by
  shipping a prebuilt binary. macOS ties the Reminders permission to the exact build, and building on the Mac from checksummed source is what
  keeps both the trust anchor and the permission intact.
* Scripting Notes is slow on very large libraries, and locked notes cannot be read.
* The token lives in `~/.config/icloud-mac-helper/config.json` (mode 600). Treat it like a password; rotate it by changing
  `BRIDGE_TOKEN` on the server and re-running `install.sh` after deleting that file.
