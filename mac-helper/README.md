# icloud-mac-helper

Lets the icloud-mcp server use **Reminders**, **Notes**, **iCloud Drive**, **Apple Maps**, your **Messages** history and **Apple Health** on your Mac. Apple only exposes those on its own devices,
so this small helper runs on your Mac and does the work when the server asks.

**How it works.** The helper connects *out* to a private HTTPS port of your server and asks "any work?". It opens no listening port.
The server never sends script text: only an operation name and validated arguments from a fixed list. Reminders operations run a
small EventKit program (`bin/reminders-eventkit`, built from `eventkit/` on your Mac by the installer); Notes operations run a static
script in `ops/`; iCloud Drive and Health run the fixed scripts `ops/drive.py` and `ops/health.py`. All of them receive their arguments as one JSON value in `argv`, so text
from a reminder, a note, a file or an AI can never become code.
The connection uses a self-signed certificate that the helper pins by fingerprint, plus a bearer token.

**Requirements.** macOS 14 or newer with the command line tools (`xcode-select --install`; the installer compiles the Reminders program with
`swiftc`), Reminders and Notes signed in to iCloud, and a
network path to the server's bridge port (your home network or your VPN). It only works while the Mac is on and logged in.

## Install

1. On the server, enable the bridge in `.env` (any of `ENABLE_REMINDERS=true`, `ENABLE_NOTES=true`, `ENABLE_DRIVE=true`, plus
   `BRIDGE_TOKEN=...`) and restart. The server logs, and writes to
   `bridge_fingerprint.txt` in its data folder, the certificate fingerprint.
2. Copy this `mac-helper` folder to the Mac and run, in Terminal:
   ```bash
   ./install.sh
   ```
   It asks for the bridge address, the token (hidden) and the fingerprint, builds the Reminders program, the PDF reader and the Maps program, runs the
   self-test, and installs a LaunchAgent. Do this while you are at the Mac and it is unlocked: macOS cannot show its permission prompts on the lock screen.
3. macOS asks two separate things. Click **Allow** on both:
   * **"iCloud Mac Helper (Reminders)" would like full access to your Reminders.** This is the grant Reminders needs. If you missed it:
     System Settings > Privacy & Security > Reminders, enable "iCloud Mac Helper (Reminders)", and run the self-test again.
   * **python3 wants to control Notes** (the first time Notes is used). If you missed it: System Settings > Privacy & Security > Automation.

   The self-test runs itself through launchd, exactly like the background service, so these permissions land on the service. (Run from
   Terminal directly, macOS would attribute them to Terminal and the service would still have none.)

## iCloud Drive

The helper works on the Drive folder your Mac already keeps in sync (`~/Library/Mobile Documents/com~apple~CloudDocs`), so every change
syncs to your other devices by itself. Paths can't leave that folder, and nothing is ever deleted permanently: trashing and replacing use
Apple's own `trash` command. Files offloaded by "Optimise Mac Storage" are downloaded on demand with Apple's `brctl`. PDFs are read by a
small PDFKit program (`bin/pdf-text`) that the installer builds, and Word, RTF, ODT and HTML files by Apple's `textutil`.
Apple Maps travel times and place search (`ENABLE_MAPS`) run through a small MapKit program (`bin/maps-cli`), also built by the
installer. It needs no permission and never uses this Mac's location: places are only what the agent passes.
Messages (`ENABLE_IMESSAGE`) are read from this Mac user's own `~/Library/Messages/chat.db`, read-only, by `ops/imessage.py`
with the same Python and its Full Disk Access; no other user's Messages are ever opened.
Sending (`IMESSAGE_ALLOW_SEND`) goes through Messages with a static script (`ops/imessage_send.js`), iMessage only. Handles listed
in `~/Library/Application Support/icloud-mac-helper/imessage-never-send.txt` (one per line) are never sent to, whatever the
server asks: put your own assistant's Apple ID there. The first send asks once for permission to control Messages; run
`python3 icloud_mac_helper.py --selftest-imessage-send you@example.com` (your own address) to get that prompt while you are at the Mac.

**It needs Full Disk Access.** In System Settings > Privacy & Security > Full Disk Access, press **+**, press Cmd+Shift+G and add:

```
/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app
```

(the version folder may differ on your Mac). On macOS 27 this grant only applies when the helper runs as that app's own executable, which
is why the installer uses it rather than `/usr/bin/python3`: that is a launcher outside the app, which macOS judges by its path, and with it
every read of iCloud Drive (and even of folders Full Disk Access always covers) is refused. A side benefit is that the helper no longer
depends on Xcode, which can move its own Python on an update. If `pdf-text` asks for access to iCloud Drive, allow that too.

## Apple Health

A Mac cannot read HealthKit, so Health data (`ENABLE_HEALTH`) comes from your iPhone. The Shortcut built by
`health/build_shortcut.py` saves the last two days of Health data to iCloud Drive, in the Shortcuts app's own folder
(`~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health`, which the Drive operations cannot reach).
`ops/health.py` reads new or changed exports into a private store, `health.sqlite` (mode 600) next to the helper, and answers with
daily figures.

```bash
python3 health/build_shortcut.py health.shortcut          # the Shortcut, unsigned
shortcuts sign --mode anyone --input health.shortcut --output "Health Export.shortcut"
```

Open the signed file on your iPhone (AirDrop works), run it once by hand to grant Health access, then add an automation that runs it,
such as **App → Messages → Is Opened**. A locked iPhone cannot read Health, so a run while it is locked saves an empty file, which
is ignored; the next run fills the gap, because each export covers two days. The Shortcut runs at most once an hour
(`Health/last-hour.txt`).

For your whole history, export it from the Health app (profile picture → Export All Health Data), put `export.zip` on the Mac, and
run `python3 ops/health.py import export.zip` in the helper's folder. It is read as a stream, even when it is gigabytes.

`health_refresh_data` asks the iPhone for a new export only if you set up a command for that yourself, in
`~/Library/Application Support/icloud-mac-helper/health-refresh.json` (`{"command": [...], "min_minutes": 10}`); the server can never
supply one.

## Check it

```bash
python3 "$HOME/Library/Application Support/icloud-mac-helper/icloud_mac_helper.py" --selftest
```
prints one JSON report with pass/fail, counts and timings, never any reminder or note content. If Reminders access is missing, `reminders_access`
says which state macOS reports (`notDetermined`, `denied`, `restricted` or `writeOnly`) and names the grant it needs. Notes access is reported but is optional: it only fails the
install if you enable Notes on the server and macOS has not granted access. It also proves that hostile strings
(quotes, backslashes, newlines, script-looking text) reach the scripts as plain data. From any MCP client, the `icloud_get_helper_status` tool says
whether the helper is online.

To also prove the write operations on this Mac, run it with `--selftest-write`. It creates, edits (including through an old-style
`x-apple-reminder://` id), completes and deletes one temporary reminder; creates a temporary list with a repeating reminder
with alerts, lists it as completed, renames the list and deletes it
and creates and deletes one temporary note (both named `icloud-mac-helper selftest ...`), removing them even if a step fails. It touches your data, so
it is never run automatically.

## Uninstall

```bash
./uninstall.sh            # keeps the config
./uninstall.sh --purge    # also deletes the config and logs
```

## Notes

* Log file: `~/Library/Logs/icloud-mac-helper/helper.log` (errors only, no content).
* The helper keeps one HTTPS connection to the server open between polls (one TLS handshake per session). Every new connection
  is checked against the pinned fingerprint; Python's own silent reconnect is switched off so it cannot skip that check. The
  config file is read again only when it changes.
* A Notes listing is kept for 30 seconds (listing notes through Notes' scripting interface is slow); any Notes change clears it.
* Reminders go through EventKit and every read is live: about 20-40 ms per operation, whatever the size of your lists. Reminder ids are
  EventKit's; ids in the older `x-apple-reminder://...` form are still accepted. Completed reminders are listed only when asked for
  (`completed=only` or `all`). Since 0.5.0: repeat rules (daily or coarser), extra alerts, and creating, renaming and deleting lists.
* The Reminders program is rebuilt only when its source changes, and the same source always gives the same program, so reinstalling
  keeps the Reminders permission. The JXA Reminders scripts in `ops/` are no longer run; they stay as reference for things EventKit
  cannot do (subtasks, tags, sections, attachments).
* `SHA256SUMS` in the package covers **source only**, including `eventkit/reminders-eventkit.swift` and `eventkit/Info.plist`. That is
  deliberate, not a gap: the Reminders program is compiled on your Mac at install time, so there is no binary to checksum. Do not "fix" this by
  shipping a prebuilt binary. macOS ties the Reminders permission to the exact build, and building on the Mac from checksummed source is what
  keeps both the trust anchor and the permission intact.
* Scripting Notes is slow on very large libraries, and locked notes cannot be read.
* Most files in a Drive with "Optimise Mac Storage" on live only in iCloud. The first read of such a file downloads it, which can take a
  while for large files; the tool then answers that it is still downloading, and a second request a minute later reads it.
* The token lives in `~/.config/icloud-mac-helper/config.json` (mode 600). Treat it like a password; rotate it by changing
  `BRIDGE_TOKEN` on the server and re-running `install.sh` after deleting that file.
