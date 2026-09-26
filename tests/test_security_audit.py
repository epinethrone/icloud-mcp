"""Regression tests for the findings of the September 2026 security audit. Each test names the gap it closes."""
from __future__ import annotations

import asyncio
import dataclasses
import time

import icalendar
import pytest

from icloud_mcp.bridge import _iso_ok
from icloud_mcp.cal import CalendarError, CalendarService, occurs_in_series, parse_rrule
from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsError, build_vcard
from icloud_mcp.extract import MAX_ITEMS, from_json_ld
from icloud_mcp.mail import MailError, MailService
from icloud_mcp import mailbulk
from icloud_mcp.server import create_server, redact_error, scrub_error


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    for k in ("READ_ONLY", "ALLOW_SEND", "MCP_HOST", "BRIDGE_HOST", "ALLOW_PERMANENT_DELETE"):
        monkeypatch.delenv(k, raising=False)
    return Settings.from_env()


def tool_names(settings: Settings) -> set[str]:
    return {t.name for t in asyncio.run(create_server(settings)[0].list_tools())}


# ---------------------------------------------------------------- READ_ONLY removes every way of sending or drafting
def test_read_only_wins_over_allow_send(monkeypatch, s):
    monkeypatch.setenv("READ_ONLY", "true")
    monkeypatch.setenv("ALLOW_SEND", "true")
    ro = Settings.from_env()
    assert ro.read_only and not ro.allow_send
    send_tools = {"mail_send_message", "mail_reply_to_message", "mail_forward_message", "mail_save_draft", "mail_unsubscribe_from_list"}
    assert {"mail_send_message", "mail_reply_to_message", "mail_forward_message", "mail_unsubscribe_from_list"} <= tool_names(s)
    assert not send_tools & tool_names(ro)
    # even a Settings object built by hand with both flags on registers nothing that sends
    assert not send_tools & tool_names(dataclasses.replace(s, read_only=True, allow_send=True))


def test_reminders_delete_and_move_are_on_by_default_and_gone_when_read_only(s):
    # a reminder is easily recreated, so deleting one does not need ALLOW_PERMANENT_DELETE (mail from Trash still does)
    mac = dataclasses.replace(s, enable_reminders=True, bridge_token="t" * 40)
    assert {"reminders_delete_reminder", "reminders_move_reminder", "reminders_complete_reminder"} <= tool_names(mac) and not s.allow_permanent_delete
    assert not {"reminders_delete_reminder", "reminders_move_reminder"} & tool_names(dataclasses.replace(mac, read_only=True))


def test_reminders_move_sends_only_the_target_list_and_needs_one(s):
    import json

    seen = []

    async def go(args):
        mcp, _ = create_server(dataclasses.replace(s, enable_reminders=True, bridge_token="t" * 40))
        mcp._icloud_bridge.call = lambda op, a=None: seen.append((op, a)) or {"id": "r1", "moved": True, "from": "To Do", "list": "Reminders"}
        return json.loads((await mcp.call_tool("reminders_move_reminder", args)).content[0].text)
    out = asyncio.run(go({"id": "r1", "list_name": "Reminders"}))
    assert seen[-1] == ("reminder_move", {"id": "r1", "list": "Reminders"}) and out["reminder"]["moved"] is True
    asyncio.run(go({"id": "r1", "list_id": "L-2"}))
    assert seen[-1] == ("reminder_move", {"id": "r1", "list_id": "L-2"})
    with pytest.raises(Exception, match="list_name or list_id"):
        asyncio.run(go({"id": "r1"}))
    assert len(seen) == 2                                    # nothing reached the Mac without a target list


def test_bind_addresses_are_loopback_unless_set(s):
    assert s.host == "127.0.0.1" and s.bridge_host == "127.0.0.1"


# ---------------------------------------------------------------- IMAP SEARCH: no command injection
@pytest.mark.parametrize("field", ["from_", "to", "subject", "text", "message_id"])
def test_search_strings_with_control_characters_are_refused(field):
    with pytest.raises(MailError, match="control characters"):
        MailService.criteria(**{field: "x\r\nA9 UID STORE 1:* +FLAGS (\\Deleted)\r\nA10 EXPUNGE"})
    crit, _ = MailService.criteria(**{field: "plain words"})
    assert "plain words" in crit


# ---------------------------------------------------------------- calendar: the invitation gate and repeat rules
SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly@test
DTSTAMP:20260901T000000Z
DTSTART:20260907T090000Z
DTEND:20260907T100000Z
RRULE:FREQ=WEEKLY;COUNT=10
SUMMARY:Standup
ORGANIZER:mailto:anna@example.org
ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@icloud.com
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
RECURRENCE-ID:20260914T090000Z
DTSTAMP:20260901T000000Z
DTSTART:20260914T090000Z
DTEND:20260914T100000Z
SUMMARY:Standup (guests removed from this one)
END:VEVENT
END:VCALENDAR
"""


def test_the_invite_gate_looks_at_every_component_of_the_object():
    parsed = icalendar.Calendar.from_ical(SERIES)
    override = next(v for v in parsed.walk("VEVENT") if v.get("recurrence-id") is not None)
    assert not override.get("attendee")
    guests = CalendarService._guests(parsed, override)
    assert guests.get("attendee"), "the master's guests decide, not the guest-free occurrence being edited"
    alone = icalendar.Calendar.from_ical(SERIES.replace("ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@icloud.com\n", "").replace("ORGANIZER:mailto:anna@example.org\n", ""))
    assert CalendarService._guests(alone, "fallback") == "fallback"


def test_repeat_rules_are_one_line_and_never_finer_than_hourly():
    assert parse_rrule("RRULE:FREQ=WEEKLY;COUNT=3")["FREQ"] == ["WEEKLY"]
    with pytest.raises(CalendarError, match="more than 48 times a day"):
        parse_rrule("FREQ=MINUTELY")
    with pytest.raises(CalendarError):
        parse_rrule("FREQ=DAILY\r\nATTENDEE:mailto:victim@example.org")
    master = icalendar.Calendar.from_ical(SERIES.replace("FREQ=WEEKLY;COUNT=10", "FREQ=SECONDLY")).walk("VEVENT")[0]
    assert occurs_in_series(master, master.get("dtstart").dt) is False


# ---------------------------------------------------------------- contacts: nothing but the intended properties reach the card
def test_vcard_values_cannot_start_a_new_property():
    raw = build_vcard(uid="u1", name="Ann\rNOTE:injected", emails=["ann@example.org"])
    lines = raw.split("\r\n")
    assert "FN:AnnNOTE:injected" in lines and not any(line.startswith("NOTE:") for line in lines)
    with pytest.raises(ContactsError, match="not a plain email address"):
        build_vcard(uid="u2", name="Ann", emails=["ann\r\nb@example.org"])
    with pytest.raises(ContactsError, match="birthday"):
        build_vcard(uid="u3", name="Ann", birthday="next tuesday")
    assert "BDAY:--05-17" in build_vcard(uid="u4", name="Ann", birthday="--05-17")


# ---------------------------------------------------------------- bridge, bulk tokens, error text, extractor bounds
def test_iso_dates_do_not_accept_a_trailing_newline():
    assert _iso_ok("2026-01-01") and not _iso_ok("2026-01-01\n")


def test_bulk_confirm_tokens_are_keyed_and_expire():
    uids = [5, 3, 9]
    tok = mailbulk._token("INBOX", 7, "archive", "", uids)
    assert mailbulk._token_ok(tok, "INBOX", 7, "archive", "", uids) is None
    assert "does not match" in mailbulk._token_ok(tok, "INBOX", 7, "archive", "", [5, 3])
    assert "not one this server issued" in mailbulk._token_ok("0123456789abcdef", "INBOX", 7, "archive", "", uids)
    old = mailbulk._token("INBOX", 7, "archive", "", uids, issued=int(time.time()) - mailbulk.TOKEN_TTL - 5)
    assert "older than" in mailbulk._token_ok(old, "INBOX", 7, "archive", "", uids)


def test_tool_errors_never_carry_secrets_or_dav_paths():
    msg = "PUT https://p12-caldav.icloud.com/123456789/calendars/work/x.ics failed for me@icloud.com with aaaa-bbbb-cccc-dddd"
    out = scrub_error(msg, ("aaaa-bbbb-cccc-dddd",))
    assert "aaaa-bbbb" not in out and "/123456789/" not in out and "me@icloud.com" in out and out.startswith("PUT https://p12-caldav.icloud.com/")
    full = redact_error(msg, ("aaaa-bbbb-cccc-dddd", "me@icloud.com"))
    assert "me@icloud.com" not in full and "123456789" not in full
    assert scrub_error("hidden\u200b text", ()) == "hidden text"           # invisible steering characters are stripped too


def test_json_ld_extraction_is_bounded():
    one = ('{"@type":"EventReservation","reservationNumber":"R%d","reservationFor":{"@type":"Event","name":"E%d",'
           '"startDate":"2026-10-01T09:00:00+02:00"}}')
    html = "".join(f'<script type="application/ld+json">{one % (i, i)}</script>' for i in range(MAX_ITEMS * 3))
    assert len(from_json_ld(html)) == MAX_ITEMS
    assert from_json_ld("<p>" + "x" * 3_000_000 + '<script type="application/ld+json">' + one % (1, 1) + "</script>") == []
