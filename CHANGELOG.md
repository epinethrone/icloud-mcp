# Changelog

What changed in each release, newest first. The GitHub release notes carry the full detail and the upgrade steps.
Update the Mac helper before the server whenever its version changes.

## 0.9.0 (in progress)

- Saved drafts: `mail_send_draft` sends a draft exactly as saved (same checks and approval as `mail_send`; the draft then goes to
  Trash), `mail_update_draft` changes one without losing it.
- Mail folders: `mail_update_folder` renames, `mail_delete_folder` deletes; a folder's mail always goes to Trash first, after a
  confirm step. The mailbox's own folders are refused.
- A server call that needs a newer Mac helper says so and asks for the update, instead of failing with "unknown operation".

## 0.8.0

- **Calendar reads cannot be stalled by a stranger's invitation:** a series that would repeat more than 48 times a day
  (every second or minute, or an hourly or daily rule multiplied up with BYMINUTE / BYSECOND lists) is no longer expanded.
  The calendar library expanded such rules before our guard saw them, so one invitation could hang a read until the tool
  timed out. Only the series' dated exceptions come through; the result counts the skipped series in `series_not_expanded`
  and never repeats their text. New and updated events refuse such rules too.
- **Stale message numbers are always caught on destructive mail actions:** `uidvalidity` is now required on `mail_delete`,
  `mail_move` and `mail_mark`. Breaking: a call without it is refused, and the agent retries with the value every search,
  thread and changes result carries.
- **Forwarded state is back:** mail results say `forwarded` again (lost in 0.6.0 when results became leaner).
- The Mac's hostname is no longer shown by `icloud_get_helper_status`.
- Mac helper 0.4.2: note backups older than 30 days move to the Trash once a day (never deleted outright).
- The sign-in and outbox pages follow the system light or dark mode.
- A failing integration test now fails the build; `CHANGELOG.md` and `CONTRIBUTING.md` added.

## 0.7.0

- Every tool name follows `area_verb_noun`; eleven tools renamed (`mail_changes` became `mail_list_changes`, `icloud_now`
  became `icloud_get_time`, and so on). `TOOLS` still accepts the old names.
- `mail_send` and `mail_reply` explain attachments; the instructions say which areas need the Mac helper.
- Mac helper 0.4.1.

## 0.6.0

- Calendar about four times faster on real iCloud (warm connections, all calendars read at once); mail reads fetch text
  only; sign-in at start (`WARMUP_ON_START`).
- Instructions built from the enabled tools; your own rules with `AGENT_NOTES_FILE`; `icloud_get_time` (then `icloud_now`),
  replies owed, clash and duplicate checks, invitations still to answer.
- One safe retry on a dropped connection, never after a write; timeouts name the slow step. Mac helper 0.4.0.

## 0.5.0

- Security hardening from a full audit (read-only mode, IMAP search text, invitations on recurring events, no duplicate
  events on retry, contact card fields, scrubbed errors). `reminders_move`.

## 0.4.2

- Privacy release.

## 0.4.1

- `calendar_move_event`; a privacy check on every pull request.

## 0.4.0

- Reused mail connections, what-changed tokens, newsletter radar, safe bulk clean-up with undo, safe unsubscribe, exact
  bookings, editable notes, search inside Drive files, birthdays, Shortcuts.

## 0.3.0

- One-click Claude Desktop bundle, free time, RSVP, single dates of a series, invitation delivery reports, all-folder
  search, the health check.

## 0.2.0

- Local mode for Claude Desktop and Claude Code, postal addresses, reading many messages at once, PyPI.
