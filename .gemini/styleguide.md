# Review guide for icloud-mcp

icloud-mcp is a single-owner MCP server that gives AI agents access to a person's iCloud Mail, Calendar, Contacts, Reminders, Notes and iCloud Drive. It holds the owner's app-specific password, and much of what it reads (email, invitations, notes, file contents) is written by strangers. Review it as security-sensitive code first and as ordinary Python second.

## What matters most, in order

1. **Gates must hold even when the model misbehaves.** Sending mail, calendar invitations, permanent deletes and writes are controlled by settings (`SEND_REQUIRES_APPROVAL`, `ALLOW_CALENDAR_INVITES`, `ALLOW_PERMANENT_DELETE`, `READ_ONLY`, `ENABLE_*`). Flag any code path that sends mail, emails attendees, deletes permanently or writes while its gate says no, including indirect paths (an update that adds attendees, a retry that re-sends).
2. **Untrusted content stays data.** Text from mail, events, contacts, notes and files must never be turned into code, shell commands, AppleScript/JXA source, file paths outside the allowed root, or URLs the server fetches. Tool results that carry such text must keep the untrusted-content notice.
3. **Secrets never leak.** The app-specific password, owner password, OAuth tokens and the bridge token must never appear in logs, errors, tool results or exceptions. HTTP client request logging stays off because URLs contain account ids.
4. **Nothing is destroyed silently.** Deletes move to Trash / Recently Deleted unless explicitly allowed. Writes to CalDAV/CardDAV use ETags (`If-Match` / `If-None-Match`) so a concurrent edit is reported, not overwritten. Unknown vCard/iCalendar properties must survive an update.
5. **Wrong-target bugs.** Mail uids are only meaningful with the folder's UIDVALIDITY; calendar/contact ids can collide across calendars. Flag code that could act on a different message, event or contact than the one the caller meant.
6. **The Mac bridge stays private.** The bridge port must never be served on the public address or through the tunnel; the helper only runs operations from its fixed table with validated arguments, never script text sent by the server.

## iCloud facts reviewers should not "correct"

- iCloud does not implement IMAP `MOVE`; moves are `COPY` + `\Deleted` + `UID EXPUNGE` of exactly those uids (never a plain `EXPUNGE`).
- iCloud answers CalDAV UID `REPORT` queries with 412, so events are found by resource name first, then by scan.
- The CardDAV home set can live on a different host than the discovery URL.
- Reminders written after iOS 13 live in CloudKit and are not reachable over CalDAV, which is why Reminders go through the Mac helper.

## Style

- Python 3.11+, type hints, small pure helpers that are unit tested without a network.
- Tool descriptions are written for a model: say what the tool does, what it will not do, and what to pass. Keep them accurate when behaviour changes.
- Every behaviour change needs an offline test; anything that depends on Apple's real behaviour also notes how it was checked live.
- Comments explain why, not what. No personal names, email addresses, home-directory paths or LAN addresses anywhere in the repository.

## Please skip

Formatting nits the linters would catch, renaming suggestions without a concrete bug behind them, and generic advice ("consider adding more tests") without a specific case.
