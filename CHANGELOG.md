# Changelog

What changed in each release, newest first. The GitHub release notes carry the full detail and the upgrade steps.
Update the Mac helper before the server whenever its version changes.

## Unreleased

- **iCloud MCP Control**, a menu bar app for the Mac (`menubar/`): status, health, pause, restart and stop, owner passcode,
  iCloud app-specific password, and the connected apps (which ones, when each was last used, sign out one or all).
- Admin API for it, off unless `ADMIN_PORT` is set: loopback only, on its own port, token in `DATA_DIR/admin-token`.
- Pause: while `DATA_DIR/paused` exists every tool except the diagnostics answers that the server is paused.
- A passcode or app-specific password changed through the admin API is stored in `DATA_DIR/overrides.json` and takes
  precedence over the environment.
- The approval page has a "Discard all" button per queue. It clears only the messages shown on the page, so one queued later
  stays. An exact repeat of a waiting message is queued once, and a full queue tells the agent not to retry.
- Safety warnings on a stranger's text in more places: unsubscribe results, the sample in a bulk-action preview, and the
  subjects shown before deleting a mail folder.
- A calendar event with an unreadable date (a malformed invitation) is left out of listings instead of failing them. The booking
  extractor keeps the other events of an attachment when one is broken.
- Fuzz tests for the parsers that read other people's data: contact cards, invitations and repeat rules, booking data,
  List-Unsubscribe headers and the Messages decoder.

## 0.10.0

- Apple Maps (`ENABLE_MAPS`, Mac helper 0.6.0): `maps_get_travel_time` (walking, cycling, driving, public transport; for a
  departure or arrival time) and `maps_search_places`. With Maps on, calendar travel time uses a measured value first, else a
  labelled Apple Maps estimate.
- iMessage (`ENABLE_IMESSAGE`): `imessage_list_chats`, `imessage_read_chat`, `imessage_search_messages` over your own Messages
  history (the whole history unless `IMESSAGE_MAX_AGE_DAYS` limits it; `IMESSAGE_HIDDEN_CHATS` hides chats; service senders
  such as banks and one-time codes are hidden unless `IMESSAGE_HIDE_SHORT_CODES=false`; handles match however they are
  written), and a "Catch up on my messages" prompt.
- iMessage sending (`imessage_send_message`), off by default: owner approval on `/outbox` by default (independent of mail),
  an allowlist that starts empty, and a never-send list on the server and on the Mac itself. iMessage only; each send confirmed.

## 0.9.0

- Saved drafts: `mail_send_draft` sends a draft exactly as saved (same checks and approval as `mail_send`; the draft then goes to
  Trash), `mail_update_draft` changes one without losing it.
- Mail folders: `mail_update_folder` renames, `mail_delete_folder` deletes; a folder's mail always goes to Trash first, after a
  confirm step. The mailbox's own folders are refused.
- Calendars: `calendar_create_calendar`, `calendar_update_calendar` (rename), `calendar_delete_calendar` (never the default
  calendar; one with events only after a preview and its token).
- Contact groups, in the format the Contacts app uses: list, read, create, rename, add and remove members, delete (only the
  group, never its members). A contact shows the groups it is in. Fixed: a group card was listed as if it were a person.
- Reminders (Mac helper 0.5.0): repeating reminders, extra alerts, listing completed reminders with when they were done, and
  creating, renaming and deleting lists (deleting a list with reminders needs a preview and its token: Reminders has no trash).
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
