# Security policy

## Threat model

iCloud MCP is a **single-owner** server. One deployment serves one iCloud account and holds that account's app-specific password in its environment. It is reached from the internet over HTTPS by MCP clients such as Claude, and every client has to be approved by the owner with a separate owner password. An optional helper on the owner's Mac reaches Reminders, Notes and iCloud Drive over a private, certificate-pinned channel.

The contents it handles (email, calendar events, contacts, notes, files) can be written by anyone who can send the owner an email or an invitation. That makes it a prompt-injection target. The project's position is that **a model following injected text is expected, and the configured gates are what must hold.**

- **In scope:**
  - Bypassing OAuth or the owner password, or getting a client authorised without the owner's approval.
  - Getting mail out without approval while `SEND_REQUIRES_APPROVAL=true`.
  - Getting invitations out while `ALLOW_CALENDAR_INVITES=false`.
  - Getting any write through while `READ_ONLY=true`.
  - Deleting mail or reminders permanently while `ALLOW_PERMANENT_DELETE=false`.
  - Leaking the app-specific password, the owner password, OAuth tokens or the bridge token.
  - Anything that turns data into code, for example in the AppleScript or EventKit operations or `drive.py`.
  - Reading or writing outside iCloud Drive through the Drive tools, or deleting a note or file permanently through the Mac helper.
  - Reaching the Mac bridge from the public address or through the tunnel, or defeating its certificate pinning.
  - Server-side request forgery, path traversal or remote code execution in the server.
- **Out of scope:**
  - An agent doing something the owner's configuration allows because injected text asked it to. Report it anyway if you think a default is unsafe; that is a design discussion rather than a vulnerability.
  - Attacks that need the owner's app-specific password, owner password or `.env` file.
  - Exposing the server without HTTPS, or putting a login wall in front of it against the documented setup.
  - Weaknesses in iCloud itself, or in Claude or other MCP clients.
  - Denial of service by exhausting the owner's own iCloud quota or disk.

If you are not sure whether a finding is in scope, please report it. A report that is declined costs far less than one that was never made.

## Supported versions

The project is on a rolling release. Only the `main` branch receives security fixes. Please check that your finding reproduces on the latest commit before reporting.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

Use one of these private channels:

1. **Preferred:** a [GitHub private security advisory](https://github.com/epinethrone/icloud-mcp/security/advisories/new). It is private and threaded, and a CVE can be requested through it if appropriate.
2. **Fallback:** open an issue titled "Security report, please contact me" with no details, and the maintainer will set up a private channel.

Please include:

- A description of the vulnerability and its impact.
- The commit SHA you reproduced against, and the relevant settings (`SEND_REQUIRES_APPROVAL`, `READ_ONLY`, `ENABLE_*` and so on).
- Step-by-step reproduction. A short script, or the exact tool calls and arguments, is ideal.
- Any logs, with account names, addresses and tokens removed.
- Your assessment of severity and any suggested fix.

Never include a real app-specific password, owner password or token in a report.

## What to expect

- **Acknowledgement** within 5 business days.
- **Triage and a proposed timeline** within 14 days.
- **A fix and coordinated disclosure** usually within 90 days, sooner for severe issues.
- **Credit** in the advisory and release notes, unless you prefer to stay anonymous.

## Hardening notes for operators

By default the server:

- Queues outgoing mail for approval in a browser (`/outbox`), blocks calendar invitations, moves deleted mail to Trash, and caps recipients per message.
- Stores OAuth tokens only as SHA-256 hashes (file mode 600), rotates refresh tokens, caps open client registrations, and locks the approval and outbox pages after 10 wrong passwords in 15 minutes. The bridge port locks out an address after 20 wrong tokens, never the correct token.
- Masks passwords and tokens in every tool error and cuts URLs to their host; refuses IMAP search strings that contain control characters; refuses repeat rules finer than hourly and never expands one it finds in a stranger's invitation; binds to loopback unless told otherwise (the Docker image binds all interfaces inside the container).
- Validates `Host` and `Origin` on the MCP endpoint, labels every piece of iCloud content as untrusted, and keeps account identifiers out of HTTP client logs.
- Serves the Mac bridge only on its own private port, pinned by certificate fingerprint and protected by a bearer token, never on the public address. The server sends the Mac only an operation name and validated arguments from a fixed list, never script text.
- Confines iCloud Drive access to the Drive folder, and never deletes notes or files permanently through the Mac helper: notes go to Recently Deleted and files to the Trash. Reminders have no trash, so `reminders_delete` exists only when `ALLOW_PERMANENT_DELETE=true`.

To tighten a deployment further:

- Set `READ_ONLY=true` if agents only need to read, or `ALLOW_SEND=false` to allow drafts only.
- Use `SEND_ALLOWLIST` to restrict who can receive mail, and keep `ALLOW_CALENDAR_INVITES=false` unless you need it.
- Only enable the Mac areas you use (`ENABLE_REMINDERS`, `ENABLE_NOTES`, `ENABLE_DRIVE`), and keep the bridge port on your LAN or VPN.
- In Claude, set the send, reply, forward and delete tools to "ask before use".
- Keep `.env` at mode 600. Revoke all clients by deleting `oauth_state.json` and restarting. Rotate the app-specific password at account.apple.com if the server or its backups may have been exposed.
