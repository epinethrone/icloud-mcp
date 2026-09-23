# icloud-mcp

A self-hosted [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) access to your **iCloud Mail**, **Calendar** and **Contacts** through one connector.

```
Claude  --OAuth + MCP over HTTPS-->  icloud-mcp (your server)  --IMAP / SMTP / CalDAV / CardDAV-->  iCloud
```

Apple offers no OAuth for these protocols, so the server logs in with an **app-specific password** that lives only in the server's environment. Claude never sees it. Claude connects to *your* server through the server's own single-owner OAuth login.

> **Not affiliated with Apple.** iCloud is a trademark of Apple Inc. This is an independent project that speaks the standard IMAP, SMTP, CalDAV and CardDAV protocols.

## Read this first

An MCP connector that can read mail and act on your behalf is a prompt-injection target: a hostile email or calendar invite can contain text that tries to steer the agent. The server marks all mail, calendar and contact content as untrusted and its instructions tell agents to treat it as data, but **that is a request to a language model, not a guarantee**. What actually protects you is configuration:

| Risk | Default | Setting |
|---|---|---|
| Agent sends mail on injected instructions | Sending only **queues** a message; you approve it in a browser with your owner password (`/outbox`) | `SEND_REQUIRES_APPROVAL=true` (turn off only if you accept the risk) |
| Agent emails invitations to strangers | Attendee changes are **blocked** | `ALLOW_CALENDAR_INVITES=false` |
| Agent mails arbitrary addresses | Any address, max 25 per message | `SEND_ALLOWLIST`, `MAX_RECIPIENTS` |
| Agent destroys mail | Delete moves to Trash; permanent delete is off | `ALLOW_PERMANENT_DELETE=false` |
| Agent changes anything | Everything writable | `READ_ONLY=true` for a read-only connector |

In Claude you can additionally set the send, reply, forward and delete tools to "ask before use". Anyone who obtains the app-specific password has **full mail, calendar and contact access** (Apple offers no narrower scope), so protect the server and its `.env` accordingly.

## Tools (24, plus 11 with the optional Mac helper)

| Area | Tools |
|---|---|
| Mail, read | `mail_list_folders`, `mail_search`, `mail_find_correspondent`, `mail_get_message`, `mail_get_thread`, `mail_get_attachment` |
| Mail, write | `mail_send`, `mail_reply` (incl. reply-all), `mail_forward`, `mail_mark`, `mail_move`, `mail_delete` (to Trash), `mail_create_folder` |
| Calendar | `calendar_list_calendars`, `calendar_list_events`, `calendar_get_event`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event` |
| Contacts | `contacts_search`, `contacts_get`, `contacts_create`, `contacts_update`, `contacts_delete` (notes and photos are never returned) |
| Reminders (Mac helper) | `reminders_lists`, `reminders_list` (active reminders only), `reminders_create`, `reminders_update`, `reminders_complete`, `reminders_delete` |
| Notes (Mac helper) | `notes_folders`, `notes_list`, `notes_read`, `notes_create`, `notes_create_folder`, `notes_move`, `notes_delete` (note text is never edited; move and delete act on one note at a time and need its current title; delete moves it to Recently Deleted and refuses locked notes and notes already there; nothing is ever moved into Recently Deleted) |
| Helper status | `mac_helper_status` (is the Mac helper online?) |

Behaviour worth knowing:

* Replies keep the `Re:` subject, `In-Reply-To`/`References`, the right recipients and the quoted original in plain text and HTML. Sent mail is copied to Sent and the original is flagged Answered (forwards get `$Forwarded`). `draft=true` saves to Drafts instead of sending.
* Reading a message does not mark it read. Bcc recipients receive the mail but the header is stripped on the wire.
* Calendar: multiple calendars, recurring events expanded when listing, all-day events, reminders, links, notes, attendees (invitations are emailed by iCloud itself, see the security table). Editing a recurring event changes the whole series.
* Contacts are fetched whole, cached, and searched locally (name, nickname, company, email, phone; accent-insensitive). A contact with no email is returned with `has_email: false` so an agent asks instead of guessing. When the connector is writable, agents can create and update contacts; updates retain fields outside the changed subset and use ETags to refuse stale overwrites.
* **Misspelled names are handled.** `contacts_search` offers similar-sounding names (`similar` / `did_you_mean`) when nothing matches exactly, and `mail_find_correspondent` finds people you have emailed with by approximate name, address or company, reading only message headers. Approximate matches are labelled, and the agent instructions require asking you to confirm before sending, inviting or editing on one.
* Recipients and attendees accept `a@b.com`, `Name <a@b.com>` or `mailto:a@b.com`. Anything else is rejected with an actionable error and never silently dropped.

## Reminders and Notes (optional, through your Mac)

Apple exposes Reminders and Notes only through their own apps. They are scriptable on a Mac, so a small helper ([`mac-helper/`](mac-helper/README.md)) runs there and does the work when the server asks. The helper connects *out* to a **private HTTPS port** of the server (never the public one, never the tunnel) and long-polls for jobs, so the Mac opens no listening port. The server never sends script text: only an operation name and validated arguments from a fixed list. Reminders operations run a small EventKit program the installer builds on the Mac; Notes operations run a static script. Either way the arguments arrive as one JSON value, never as code. The channel is TLS with a self-signed certificate the helper pins by fingerprint, plus a bearer token. It works while the Mac is on and reachable (home network or VPN); when it is not, the tools say so. Due dates are validated as real calendar dates (a bare date means 09:00 local time that day); locked notes are never read; `READ_ONLY=true` hides every write tool.

**Reminders through EventKit.** Reminders operations use Apple's EventKit framework, so every read is live and takes about 20-40 ms per operation whatever the size of a list (the earlier scripted path scanned a whole list per request, 15 to 70 seconds on a list with a thousand completed reminders). Only *active* reminders are returned. EventKit needs its own permission, **Full Access to Reminders** for "iCloud Mac Helper (Reminders)", which the installer asks for and the self-test checks. Reminder ids in the older `x-apple-reminder://...` form are still accepted. List names are not unique across accounts, so tools accept `list_id` and refuse an ambiguous name.

Enable it in `.env` with `ENABLE_REMINDERS=true` (and/or `ENABLE_NOTES=true`), a `BRIDGE_TOKEN` of at least 32 random characters, and `BRIDGE_BIND` set to the address the Mac reaches the server on. The server logs, and writes to `bridge_fingerprint.txt` in its data folder, the certificate fingerprint to give the installer. The operation scripts are adapted from [MrGo2/icloud-mcp](https://github.com/MrGo2/icloud-mcp) (MIT), see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Requirements

* Docker with the compose plugin (or Python 3.11+)
* An Apple Account with two-factor authentication, and an **app-specific password** (account.apple.com > Sign-In and Security > App-Specific Passwords)
* A public **HTTPS** address for the server. Claude connects from Anthropic's cloud, so a VPN or LAN address does not work. A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) is the simplest option (outbound only, no port forwarding). **Do not put Cloudflare Access or any login wall in front of it:** Claude's servers cannot pass an interactive login, and the server has its own OAuth.
* A Claude plan that supports custom connectors

## Quick start

```bash
git clone <this repository> && cd icloud-mcp
cp .env.example .env            # then fill in ICLOUD_USERNAME, ICLOUD_APP_PASSWORD, ICLOUD_DISPLAY_NAME,
                                # MCP_PUBLIC_URL (your https address, no trailing slash), MCP_OWNER_PASSWORD
chmod 600 .env
```

`MCP_OWNER_PASSWORD` is a new random password (12+ characters) that **you** type when approving a client and in the outbox page. It is not your Apple password. `./configure.sh` is an optional helper that asks for the secrets with silent prompts.

**Check your credentials against real iCloud before exposing anything:**

```bash
docker build -t icloud-mcp:local .
docker run --rm --env-file .env icloud-mcp:local python -m icloud_mcp.selftest              # IMAP + SMTP login, CalDAV, CardDAV; sends nothing
docker run --rm --env-file .env icloud-mcp:local python -m icloud_mcp.selftest --probe-sent you@example.com   # sends ONE test mail (see below)
```

If a login fails, the login name is the usual cause: set `IMAP_USERNAME`, `SMTP_USERNAME`, `CALDAV_USERNAME` or `CARDDAV_USERNAME` separately.

**Run it:**

```bash
docker compose up -d --build                      # listens on 127.0.0.1:8000 only
```

To use the bundled Cloudflare Tunnel: create a tunnel, point its public hostname (the host in `MCP_PUBLIC_URL`) at `http://icloud-mcp:8000`, put the tunnel token in `.tunnel.env` as `TUNNEL_TOKEN=...`, and start with `docker compose --profile tunnel up -d --build`. Any TLS front (Caddy, nginx) works too as long as the `Host` header is preserved. `MCP_PUBLIC_URL` must match the public address exactly.

**Connect Claude:** Settings > Connectors > Add custom connector > `https://<your-host>/mcp`. Your server shows an approval page; enter the owner password. Reconnect the connector in Claude whenever you change tools or settings, because Claude caches tool definitions.

To revoke every connected client, delete `oauth_state.json` in the data volume and restart.

## Approving outgoing mail

With the default `SEND_REQUIRES_APPROVAL=true`, `mail_send`, `mail_reply` and `mail_forward` return `queued_for_owner_approval` and nothing leaves. Open `https://<your-host>/outbox` (bookmark it, and only type the password there, never on a link an agent gives you), enter the owner password, review the exact recipients and text, then approve or discard. Queued messages expire (`OUTBOX_TTL_SECONDS`, default 24 h) and are released at most once.

## Configuration

Everything is an environment variable; see [`.env.example`](.env.example) for comments.

| Variable | Default | Meaning |
|---|---|---|
| `ICLOUD_USERNAME` | required | Apple Account you sign in with |
| `ICLOUD_APP_PASSWORD` | required | App-specific password |
| `ICLOUD_EMAIL_ADDRESS` | username | From address (your iCloud address or alias) |
| `ICLOUD_DISPLAY_NAME`, `EMAIL_SIGNATURE` | empty | Sender name; plain-text signature appended to sent mail (`\n` = newline) |
| `IMAP_HOST/PORT/SECURITY/USERNAME` | `imap.mail.me.com`, 993, ssl, username | IMAP |
| `SMTP_HOST/PORT/SECURITY/USERNAME` | `smtp.mail.me.com`, 587, starttls, username | SMTP |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_REQUIRE_TLS` | `https://caldav.icloud.com`, username, true | CalDAV |
| `CARDDAV_URL`, `CARDDAV_USERNAME` | `https://contacts.icloud.com`, username | CardDAV |
| `DEFAULT_TIMEZONE`, `DEFAULT_CALENDAR` | `UTC`, auto | Timezone for times without an offset; calendar for new events (else "Calendar"/"Home", else the first) |
| `ENABLE_MAIL`, `ENABLE_CALENDAR`, `ENABLE_CONTACTS` | true | Switch whole areas off |
| `READ_ONLY` | false | No sending, moving, deleting, calendar changes, or contact changes |
| `ALLOW_SEND` | true | false = agents can only save drafts |
| `SEND_REQUIRES_APPROVAL` | true | Queue outgoing mail for browser approval |
| `OUTBOX_TTL_SECONDS`, `OUTBOX_MAX` | 86400, 20 | Queue lifetime and size |
| `ALLOW_CALENDAR_INVITES` | false | Allow attendees (iCloud then emails invitations, updates, cancellations) |
| `SEND_ALLOWLIST` | empty | Only these addresses/domains may receive mail (`@example.org,friend@example.com`) |
| `MAX_RECIPIENTS` | 25 | Per message |
| `ALLOW_PERMANENT_DELETE` | false | Allow deleting from Trash |
| `SAVE_SENT_COPY` | true | Append sent mail to Sent (iCloud does not do it itself) |
| `MAX_BODY_CHARS`, `MAX_ATTACHMENT_BYTES` | 30000, 5 MiB | Result size caps |
| `MCP_PUBLIC_URL`, `MCP_OWNER_PASSWORD` | required | Public https address; owner password (12+ chars) |
| `MCP_HOST`, `MCP_PORT`, `MCP_EXTRA_ALLOWED_HOSTS` | 0.0.0.0, 8000, empty | Bind address and extra allowed Host headers |
| `MCP_STATELESS` | true | No server-side MCP sessions, so restarting the server never breaks a connected client ("Missing session ID") |
| `TOOL_TIMEOUT_SECONDS` | 90 | A tool call running longer is abandoned with an error instead of hanging |
| `DATA_DIR` | `./data` (`/data` in Docker) | OAuth state and the outbox |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | `claude.ai,claude.com,localhost,127.0.0.1` | Clients that may register |
| `ACCESS_TOKEN_TTL`, `REFRESH_TOKEN_TTL` | 3600, 30 days | Token lifetimes (refresh tokens rotate) |
| `LOG_LEVEL` | INFO | |

## Security model

* iCloud credentials exist only in the server environment. Clients hold short-lived bearer tokens for *this* server.
* Clients may register dynamically, but nothing is authorized without the owner password. Redirect hosts are restricted. Tokens are stored as SHA-256 hashes (file mode 600). The approval and outbox pages lock after 10 wrong passwords in 15 minutes (server-wide; existing tokens keep working).
* Every refused sign-in renewal is logged with its reason (already rotated, expired, wrong client, not recognised), never with token values, and unauthenticated callers cannot flood the log. Read them with `docker compose logs icloud-mcp | grep 'oauth:'`.
* The MCP endpoint validates `Host` and `Origin`. Tool results carry an untrusted-content notice. HTTP-client request logging is disabled so account identifiers do not reach the logs.
* This is a single-owner design: one deployment serves one iCloud account. It is not multi-tenant, and storing other people's app-specific passwords is deliberately out of scope.

## iCloud quirks this project works around

These only showed up against the real service, not against local test servers:

* **CalDAV** rejects UID-filtered queries (`412`), so events are fetched by their resource name with a scan fallback. An attendee who is the account owner is rewritten to an internal path with the address in the `EMAIL` parameter.
* **IMAP** has no `MOVE`. Moving and deleting use COPY, flag `\Deleted`, then `UID EXPUNGE` of exactly those messages (never a plain `EXPUNGE`). iCloud does not file sent mail by itself.
* **CardDAV** discovery ends on a different host than the one you start on (follow the returned links), returns the whole address book in one request, and stores about half of all emails in grouped `itemN.EMAIL` properties with labels in `itemN.X-ABLabel`.

## Limits

* Reminders, Notes and iCloud Drive are not reachable over these protocols (Reminders and Notes are covered by the optional Mac helper; iCloud Drive is not). Contact photos and notes are deliberately not exposed to agents; contact deletion is permanent.
* One identity; aliases as From are not supported. Attachments other than text return base64 and are size-capped.
* Each tool call opens a fresh connection (about 1.5 to 5 seconds per call against iCloud).
* Claude currently shows no custom icon for custom connectors, whatever the server advertises ([open request](https://github.com/anthropics/claude-ai-mcp/issues/152)). Optional icon files placed in `src/icloud_mcp/static/` are served and advertised anyway; none ship with the source.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest tests --ignore=tests/integration          # offline tests, no network
sudo apt install dovecot-imapd && dev/start_local_stack.sh
pytest tests                                     # also runs the integration tests against local Dovecot, an SMTP sink and Radicale
```

`dev/e2e_http.py` drives a running server over HTTP (OAuth plus tool calls). Local test servers accept things iCloud does not (see the quirks above), so treat `selftest` and a manual run against a real account as part of testing any change.

## License

MIT, see [LICENSE](LICENSE).
