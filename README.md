<div align="center">

# iCloud MCP

**Give Claude your iCloud: Mail, Calendar, Contacts, Reminders, Notes and iCloud Drive.**
**Self-hosted, single-owner, and built around your approval, not the model's good behaviour.**

[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg?logo=python&logoColor=white)](pyproject.toml)
[![Model Context Protocol](https://img.shields.io/badge/MCP-server-6e56cf.svg)](https://modelcontextprotocol.io)
[![Docker](https://img.shields.io/badge/docker-compose-2496ed.svg?logo=docker&logoColor=white)](docker-compose.yml)
[![Self-hosted](https://img.shields.io/badge/self--hosted-your%20server-555.svg)](#quick-start)
[![Tools](https://img.shields.io/badge/tools-46-f28b30.svg)](#tools)

[Why](#why-icloud-mcp) · [What you can ask](#what-you-can-ask-claude) · [How it works](#how-it-works) · [Security](#security-first) · [Quick start](#quick-start) · [Tools](#tools) · [Mac helper](#reminders-notes-and-icloud-drive-through-your-mac) · [Configuration](#configuration)

</div>

---

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that connects **Claude** (or any MCP client) to your **Apple iCloud** account through one custom connector. Mail, Calendar and Contacts speak the standard IMAP, SMTP, CalDAV and CardDAV protocols. Reminders, Notes and iCloud Drive, which Apple only exposes on its own devices, go through an optional helper on your Mac.

> [!NOTE]
> **Not affiliated with Apple.** iCloud is a trademark of Apple Inc. This is an independent open-source project.

## Why iCloud MCP

| | |
|---|---|
| 🍎 **All of iCloud in one connector** | 46 tools across Mail, Calendar, Contacts, Reminders, Notes and iCloud Drive, instead of a separate integration for each. |
| 🔐 **Your credentials never leave your server** | Apple offers no OAuth for these protocols, so an app-specific password lives only in your server's environment. Claude signs in to *your* server through its own single-owner OAuth and never sees it. |
| ✋ **You approve what leaves** | Outgoing mail is queued for your approval in a browser by default. Invitations to other people are blocked unless you allow them. Deletes go to the Trash. |
| 🛡️ **Built for prompt injection** | Every email, event, note and file is marked as untrusted data, and the dangerous actions are gated by configuration rather than by asking the model nicely. |
| 🧪 **Tested against the real iCloud** | 250 offline tests, plus integration tests against local mail, calendar and contacts servers, plus manual runs against a live account for the quirks only Apple's servers show. |

## What you can ask Claude

- *"Find the email from my landlord about the heating and draft a polite reply."*
- *"What's on my calendar next week? Move Thursday's dentist appointment to Friday at the same time."*
- *"Add Anna's new work address to her contact card."*
- *"Remind me to renew my passport on the first of next month."*
- *"Move all my recipe notes into a Recipes folder, and delete the three empty notes."*
- *"Find my tax return PDF in iCloud Drive and tell me what I paid last year."*

## How it works

```mermaid
flowchart LR
    C["🤖 Claude<br/>(or any MCP client)"] -- "OAuth + MCP over HTTPS" --> S["🖥️ icloud-mcp<br/>your server"]
    S -- "IMAP · SMTP · CalDAV · CardDAV" --> I["☁️ iCloud<br/>Mail · Calendar · Contacts"]
    M["💻 Mac helper<br/>(optional)"] -- "polls out over pinned TLS" --> S
    M -- "EventKit · AppleScript · files" --> A["Reminders · Notes · iCloud Drive"]
```

The server runs anywhere Docker runs (a home server, a Raspberry Pi, a small VPS) behind a public HTTPS address such as a Cloudflare Tunnel. The Mac helper is only needed for Reminders, Notes and iCloud Drive. It connects *out* to the server, so your Mac never opens a port.

## Security first

A connector that can read your mail and act for you is a prompt-injection target: a hostile email or calendar invite can contain text that tries to steer the agent. The server labels all such content as untrusted and tells agents to treat it as data, but **that is a request to a language model, not a guarantee**. What actually protects you is configuration:

| Risk | Default | Setting |
|---|---|---|
| Agent sends mail on injected instructions | Sending only **queues** the message. You approve it in a browser with your owner password at `/outbox` | `SEND_REQUIRES_APPROVAL=true` |
| Agent emails invitations to strangers | Attendee changes are **blocked** | `ALLOW_CALENDAR_INVITES=false` |
| Agent mails arbitrary addresses | Any address, at most 25 per message | `SEND_ALLOWLIST`, `MAX_RECIPIENTS` |
| Agent destroys mail | Delete moves to Trash; permanent delete is off | `ALLOW_PERMANENT_DELETE=false` |
| Agent destroys notes or files | Notes go to Recently Deleted, Drive files to the Trash. Nothing through the Mac helper is ever deleted permanently | always on |
| Agent changes anything at all | Everything writable | `READ_ONLY=true` for a read-only connector |

In Claude you can also set the send, reply, forward and delete tools to "ask before use". Anyone who obtains the app-specific password has **full access to mail, calendar and contacts** (Apple offers no narrower scope), so protect the server and its `.env` accordingly.

<details>
<summary><b>The full security model</b></summary>

- iCloud credentials exist only in the server environment. Clients hold short-lived bearer tokens for *this* server.
- Clients may register dynamically, but nothing is authorised without the owner password. Redirect hosts are restricted. Tokens are stored as SHA-256 hashes (file mode 600). The approval and outbox pages lock after 10 wrong passwords in 15 minutes (server-wide; existing tokens keep working).
- Every refused sign-in renewal is logged with its reason (already rotated, expired, wrong client, not recognised), never with token values, and unauthenticated callers cannot flood the log: `docker compose logs icloud-mcp | grep 'oauth:'`.
- The MCP endpoint validates `Host` and `Origin`. Tool results carry an untrusted-content notice. HTTP-client request logging is disabled so account identifiers do not reach the logs.
- The Mac bridge runs on its own private TLS port with a self-signed certificate the helper pins by fingerprint, plus a bearer token. It is never served on the public address or through the tunnel. The server sends only an operation name and validated arguments from a fixed list, never script text.
- Single-owner by design: one deployment serves one iCloud account. It is not multi-tenant, and storing other people's app-specific passwords is deliberately out of scope.

</details>

## Quick start

**You need:**
- Docker with the compose plugin (or Python 3.11+)
- An Apple Account with two-factor authentication and an **app-specific password** (account.apple.com → Sign-In and Security → App-Specific Passwords)
- A public **HTTPS** address for the server. Claude connects from Anthropic's cloud, so a LAN or VPN address won't work. A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) is the simplest option: outbound only, no port forwarding.
- A Claude plan that supports custom connectors

> [!WARNING]
> Don't put Cloudflare Access or any other login wall in front of the server. Claude's servers can't pass an interactive login, and the server has its own OAuth.

**1. Configure**

```bash
git clone https://github.com/epinethrone/icloud-mcp.git && cd icloud-mcp
cp .env.example .env     # fill in ICLOUD_USERNAME, ICLOUD_APP_PASSWORD, ICLOUD_DISPLAY_NAME,
                         # MCP_PUBLIC_URL (your https address, no trailing slash), MCP_OWNER_PASSWORD
chmod 600 .env
```

`MCP_OWNER_PASSWORD` is a new random password (12+ characters) that **you** type when approving a client and on the outbox page. It is not your Apple password. `./configure.sh` asks for the secrets with silent prompts if you prefer.

**2. Check your credentials against the real iCloud before exposing anything**

```bash
docker build -t icloud-mcp:local .
docker run --rm --env-file .env icloud-mcp:local python -m icloud_mcp.selftest                              # logs in to IMAP, SMTP, CalDAV, CardDAV; sends nothing
docker run --rm --env-file .env icloud-mcp:local python -m icloud_mcp.selftest --probe-sent you@example.com   # sends ONE test mail
```

If a login fails, the login name is the usual cause: set `IMAP_USERNAME`, `SMTP_USERNAME`, `CALDAV_USERNAME` or `CARDDAV_USERNAME` separately.

**3. Run it**

```bash
docker compose up -d --build     # listens on 127.0.0.1:8000 only
```

For the bundled Cloudflare Tunnel: create a tunnel, point its public hostname (the host in `MCP_PUBLIC_URL`) at `http://icloud-mcp:8000`, put the token in `.tunnel.env` as `TUNNEL_TOKEN=...`, and start with `docker compose --profile tunnel up -d --build`. Any TLS front (Caddy, nginx) works too, as long as it keeps the `Host` header. `MCP_PUBLIC_URL` must match the public address exactly.

**4. Connect Claude**

Settings → Connectors → Add custom connector → `https://<your-host>/mcp`. Your server shows an approval page: enter the owner password. Reconnect the connector whenever you change tools or settings, because Claude caches tool definitions. To revoke every connected client, delete `oauth_state.json` in the data volume and restart.

### Approving outgoing mail

With the default `SEND_REQUIRES_APPROVAL=true`, `mail_send`, `mail_reply` and `mail_forward` return `queued_for_owner_approval` and nothing leaves. Open `https://<your-host>/outbox` (bookmark it, and only type the password there, never on a link an agent gives you), enter the owner password, review the exact recipients and text, then approve or discard. Queued messages expire after `OUTBOX_TTL_SECONDS` (24 hours by default) and are released at most once.

## Tools

**24 tools** for Mail, Calendar and Contacts, plus **22** more with the optional Mac helper.

<details>
<summary><b>📧 Mail</b> (13)</summary>

| | |
|---|---|
| Read | `mail_list_folders`, `mail_search`, `mail_find_correspondent`, `mail_get_message`, `mail_get_thread`, `mail_get_attachment` |
| Write | `mail_send`, `mail_reply` (including reply-all), `mail_forward`, `mail_mark`, `mail_move`, `mail_delete` (to Trash), `mail_create_folder` |

- Replies keep the `Re:` subject, `In-Reply-To` and `References`, the right recipients and the quoted original in plain text and HTML. Sent mail is copied to Sent and the original is flagged Answered (forwards get `$Forwarded`). `draft=true` saves to Drafts instead of sending.
- Reading a message does not mark it read. Bcc recipients receive the mail, but the header is stripped on the wire.
- Recipients accept `a@b.com`, `Name <a@b.com>` or `mailto:a@b.com`. Anything else is rejected with a clear error and never silently dropped.

</details>

<details>
<summary><b>📅 Calendar</b> (6)</summary>

`calendar_list_calendars`, `calendar_list_events`, `calendar_get_event`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`

- Multiple calendars, recurring events expanded when listing, all-day events, alerts, links, notes and attendees. Editing a recurring event changes the whole series.
- **Apple travel time and map locations.** Events can carry Apple's travel time (by bike, on foot, by car or public transport) and a structured destination, which is what makes Apple draw the map card.
- **Adding a guest leaves everyone else alone.** Updating the guest list merges instead of replacing, so existing guests keep their RSVP and aren't sent the invitation again. Invitations are emailed by iCloud itself and are off unless you allow them.

</details>

<details>
<summary><b>👥 Contacts</b> (5)</summary>

`contacts_search`, `contacts_get`, `contacts_create`, `contacts_update`, `contacts_delete`

- Contacts are fetched whole, cached and searched locally by name, nickname, company, email or phone, ignoring accents. A contact with no email comes back with `has_email: false`, so an agent asks instead of guessing.
- **Misspelled names are handled.** `contacts_search` suggests similar-sounding names when nothing matches exactly, and `mail_find_correspondent` finds people you've emailed by approximate name, address or company, reading only message headers. Approximate matches are labelled, and agents must ask you to confirm before sending, inviting or editing on one.
- Updates keep every field outside the changed ones and use ETags to refuse stale overwrites. Contact photos and notes are never returned.

</details>

<details>
<summary><b>✅ Reminders</b> (6, Mac helper)</summary>

`reminders_lists`, `reminders_list`, `reminders_create`, `reminders_update`, `reminders_complete`, `reminders_delete`

- Runs through Apple's EventKit: every read is live and takes about 20 to 40 ms, however long your lists are. Only active reminders are returned.
- List names can repeat across accounts, so tools accept a `list_id` and refuse an ambiguous name. Due dates are validated as real dates (a bare date means 09:00 local time).

</details>

<details>
<summary><b>📝 Notes</b> (7, Mac helper)</summary>

`notes_folders`, `notes_list`, `notes_read`, `notes_create`, `notes_create_folder`, `notes_move`, `notes_delete`

- Read, create, and **organise**: create folders and subfolders, and move notes between them. The text of an existing note is never edited.
- Move and delete act on one note at a time and need its **current title** as well as its id, so a stale or wrong id changes nothing.
- Delete moves a note to **Recently Deleted**, where you can recover it for about 30 days. It refuses locked notes, and notes already in Recently Deleted, because removing them from there would be permanent. Nothing is ever moved into Recently Deleted.

</details>

<details>
<summary><b>🗂️ iCloud Drive</b> (8, Mac helper)</summary>

`drive_list`, `drive_search`, `drive_info`, `drive_read`, `drive_write`, `drive_create_folder`, `drive_move`, `drive_trash`

- Works on your **whole iCloud Drive** as your Mac keeps it in sync, so every change syncs to your other devices by itself.
- `drive_read` returns text from plain text files, **PDFs** and **Word, RTF, ODT and HTML** documents. Files offloaded by "Optimise Mac Storage" are downloaded first. If that takes too long, the answer says the file is still downloading, instead of timing out.
- `drive_write` creates plain text files. Replacing a file needs `overwrite`, and the old version goes to the Trash. `drive_move` never overwrites.
- **Nothing is ever deleted permanently.** `drive_trash` moves items to the Trash, where you can recover them.
- Paths are relative to the Drive and can't leave it, not through `..` and not through a symbolic link (links that lead outside are not even listed). The Drive's trash folder is off limits.

</details>

<details>
<summary><b>🔌 Status</b> (1)</summary>

`mac_helper_status` says whether the Mac helper is online, when it was last seen and which version it runs.

</details>

## Reminders, Notes and iCloud Drive through your Mac

Apple only exposes Reminders, Notes and iCloud Drive on its own devices, so a small helper ([`mac-helper/`](mac-helper/README.md)) runs on your Mac and does the work when the server asks.

- **Nothing listens on your Mac.** The helper connects *out* to a private HTTPS port of the server (never the public address, never the tunnel) and long-polls for jobs.
- **No code is ever sent.** The server sends an operation name and validated arguments from a fixed list. Reminders run a small EventKit program the installer builds on your Mac. Notes run static scripts. iCloud Drive runs one fixed Python script under Apple's own Python. In every case the arguments arrive as one JSON value, never as code.
- **Pinned and authenticated.** TLS with a self-signed certificate the helper pins by fingerprint, plus a bearer token.
- **Honest when it's off.** It works while your Mac is on and reachable (home network or VPN). When it isn't, the tools say so.

Enable it in `.env` with any of `ENABLE_REMINDERS=true`, `ENABLE_NOTES=true` and `ENABLE_DRIVE=true`, a `BRIDGE_TOKEN` of at least 32 random characters, and `BRIDGE_BIND` set to the address the Mac reaches the server on. The server logs the certificate fingerprint for the installer, and also writes it to `bridge_fingerprint.txt` in its data folder. Then follow the [Mac helper guide](mac-helper/README.md).

> [!IMPORTANT]
> **iCloud Drive needs Full Disk Access** for the helper's Python. On macOS 27 the grant only takes effect when the helper runs as the Command Line Tools `Python.app` executable, which is what the installer sets up. Details in the [Mac helper guide](mac-helper/README.md#icloud-drive).

The Notes scripts are adapted from [MrGo2/icloud-mcp](https://github.com/MrGo2/icloud-mcp) (MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Configuration

Everything is an environment variable. [`.env.example`](.env.example) has a comment for each one.

<details>
<summary><b>All settings</b></summary>

| Variable | Default | Meaning |
|---|---|---|
| `ICLOUD_USERNAME` | required | The Apple Account you sign in with |
| `ICLOUD_APP_PASSWORD` | required | App-specific password |
| `ICLOUD_EMAIL_ADDRESS` | username | From address (your iCloud address or alias) |
| `ICLOUD_DISPLAY_NAME`, `EMAIL_SIGNATURE` | empty | Sender name; plain-text signature added to sent mail (`\n` = new line) |
| `IMAP_HOST/PORT/SECURITY/USERNAME` | `imap.mail.me.com`, 993, ssl, username | IMAP |
| `SMTP_HOST/PORT/SECURITY/USERNAME` | `smtp.mail.me.com`, 587, starttls, username | SMTP |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_REQUIRE_TLS` | `https://caldav.icloud.com`, username, true | CalDAV |
| `CARDDAV_URL`, `CARDDAV_USERNAME` | `https://contacts.icloud.com`, username | CardDAV |
| `DEFAULT_TIMEZONE`, `DEFAULT_CALENDAR` | `UTC`, auto | Timezone for times without an offset; calendar for new events (else "Calendar" or "Home", else the first) |
| `ENABLE_MAIL`, `ENABLE_CALENDAR`, `ENABLE_CONTACTS` | true | Switch whole areas off |
| `ENABLE_REMINDERS`, `ENABLE_NOTES`, `ENABLE_DRIVE` | false | Areas that go through the Mac helper (need `BRIDGE_TOKEN`) |
| `BRIDGE_TOKEN`, `BRIDGE_BIND` | empty, 127.0.0.1 | Mac helper secret (32+ characters) and the address its private port is published on |
| `BRIDGE_JOB_TIMEOUT_SECONDS` | 60 | How long a tool call waits for the Mac |
| `READ_ONLY` | false | No sending, moving, deleting, or calendar, contact, reminder, note or file changes |
| `ALLOW_SEND` | true | false = agents can only save drafts |
| `SEND_REQUIRES_APPROVAL` | true | Queue outgoing mail for browser approval |
| `OUTBOX_TTL_SECONDS`, `OUTBOX_MAX` | 86400, 20 | Queue lifetime and size |
| `ALLOW_CALENDAR_INVITES` | false | Allow attendees (iCloud then emails invitations, updates and cancellations) |
| `SEND_ALLOWLIST` | empty | Only these addresses or domains may receive mail (`@example.org,friend@example.com`) |
| `MAX_RECIPIENTS` | 25 | Per message |
| `ALLOW_PERMANENT_DELETE` | false | Allow deleting mail from Trash |
| `SAVE_SENT_COPY` | true | Copy sent mail to Sent (iCloud doesn't do it itself) |
| `MAX_BODY_CHARS`, `MAX_ATTACHMENT_BYTES` | 30000, 5 MiB | Result size caps |
| `MCP_PUBLIC_URL`, `MCP_OWNER_PASSWORD` | required | Public https address; owner password (12+ characters) |
| `MCP_HOST`, `MCP_PORT`, `MCP_EXTRA_ALLOWED_HOSTS` | 0.0.0.0, 8000, empty | Bind address and extra allowed `Host` headers |
| `MCP_STATELESS` | true | No server-side MCP sessions, so a restart never breaks a connected client ("Missing session ID") |
| `TOOL_TIMEOUT_SECONDS` | 90 | A tool call running longer is abandoned with an error instead of hanging |
| `DATA_DIR` | `./data` (`/data` in Docker) | OAuth state and the outbox |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | `claude.ai,claude.com,localhost,127.0.0.1` | Clients that may register |
| `ACCESS_TOKEN_TTL`, `REFRESH_TOKEN_TTL` | 3600, 30 days | Token lifetimes (refresh tokens rotate) |
| `LOG_LEVEL` | INFO | |

</details>

## iCloud quirks this project works around

These only show up against Apple's real servers, never against local test servers:

- **CalDAV** rejects UID-filtered queries (`412`), so events are fetched by resource name with a scan fallback. An attendee who is the account owner is rewritten to an internal path with the address in the `EMAIL` parameter.
- **IMAP** has no `MOVE`. Moving and deleting use COPY, flag `\Deleted`, then `UID EXPUNGE` of exactly those messages (never a plain `EXPUNGE`). iCloud doesn't file sent mail by itself.
- **CardDAV** discovery ends on a different host than it starts on (follow the returned links), returns the whole address book in one request, and stores about half of all emails in grouped `itemN.EMAIL` properties with labels in `itemN.X-ABLabel`.
- **Travel time** is a number Apple stores, not a live estimate. It is never recomputed, so an origin or travel mode without a duration is refused instead of silently doing nothing.

## Limits

- Reminders, Notes and iCloud Drive need the Mac helper and a Mac that is on. Contact photos and notes are deliberately not exposed to agents, and deleting a contact is permanent.
- One identity: aliases can't be used as the From address. Attachments that aren't text come back as base64 and are size-capped.
- Each tool call opens a fresh connection, about 1.5 to 5 seconds per call against iCloud.
- Claude doesn't show custom icons for custom connectors yet ([open request](https://github.com/anthropics/claude-ai-mcp/issues/152)). Icon files placed in `src/icloud_mcp/static/` are served and advertised anyway; none ship with the source.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest tests --ignore=tests/integration      # offline tests, no network
sudo apt install dovecot-imapd && dev/start_local_stack.sh
pytest tests                                 # adds integration tests against local Dovecot, an SMTP sink and Radicale
```

`dev/e2e_http.py` drives a running server over HTTP (OAuth plus tool calls). Local test servers accept things iCloud doesn't (see the quirks above), so treat `selftest` and a manual run against a real account as part of testing any change.

## License

[MIT](LICENSE). Contributions and issue reports are welcome.

<div align="center">
<sub>Built for people who want an AI assistant for their Apple life without handing their Apple ID to anyone.</sub>
</div>
