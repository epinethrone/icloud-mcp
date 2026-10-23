"""ALLOW_CALENDAR_INVITES=approve against a real CalDAV server (Radicale from dev/start_local_stack.sh; only it is needed).

Writes that would make the server email guests are held, nothing reaches the calendar until release(), and release() writes
exactly the held object, conditional on the version it was based on.
"""
import dataclasses
import socket
from datetime import datetime, timedelta

import icalendar
import pytest

from icloud_mcp.cal import CalendarError, CalendarService
from icloud_mcp.config import Settings
from icloud_mcp.outbox import Outbox


def _up(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _up(5232), reason="Radicale not running (dev/start_local_stack.sh)")

ENV = dict(ICLOUD_USERNAME="test", ICLOUD_APP_PASSWORD="testpass", ICLOUD_EMAIL_ADDRESS="test@icloud.test", ICLOUD_DISPLAY_NAME="Test User",
           CALDAV_URL="http://127.0.0.1:5232", CALDAV_REQUIRE_TLS="false", DEFAULT_TIMEZONE="Europe/Berlin",
           MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, ALLOW_CALENDAR_INVITES="approve")


@pytest.fixture
def cal(tmp_path, monkeypatch):
    import caldav
    for k, v in {**ENV, "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.allow_calendar_invites and s.invites_require_approval
    with caldav.DAVClient(url=s.caldav_url, username="test", password="testpass", require_tls=False) as client:
        p = client.principal()
        if not p.calendars():
            p.make_calendar(name="Personal")
    c = CalendarService(s)
    c.outbox = Outbox(str(tmp_path), 3600, 20, filename="calendar-outbox.json")
    return c


def when(days=9, hour=18):
    d = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%dT%H:%M")


def stored(cal, uid):
    with cal._principal() as p:
        for c in cal._pick(p, None):
            obj = cal._by_href(c, uid)
            if obj is not None:
                return icalendar.Calendar.from_ical(obj.data)
    return None


def test_an_invitation_waits_and_release_writes_it_once(cal):
    r = cal.create_event(summary="Autumn fair planning", start=when(), attendees=["jana@example.org", "tom@example.net"])
    assert r["status"] == "queued_for_owner_approval" and r["created"] is False and r["written"] is False
    q = cal.outbox.pending()[0]
    uid = q.followup["uid"]
    assert q.recipients == ["jana@example.org", "tom@example.net"] and q.followup["op"] == "create"
    assert stored(cal, uid) is None                                       # nothing in the calendar yet
    assert "Added “Autumn fair planning”" in cal.release(cal.outbox.claim(q.id))
    ev = stored(cal, uid)
    assert ev is not None and len(list(ev.walk("VEVENT"))[0].get("attendee")) == 2
    with pytest.raises(CalendarError):                                    # the same create again: If-None-Match stops a second copy
        cal.release(q)
    assert cal.delete_event(uid)["deleted"] is False                      # deleting an event with guests waits too
    cal.release(cal.outbox.claim(cal.outbox.pending()[0].id))
    assert stored(cal, uid) is None


def test_events_without_guests_are_written_at_once(cal):
    r = cal.create_event(summary="Dentist", start=when(days=10, hour=9))
    assert r["created"] is True and cal.outbox.pending() == []
    assert cal.update_event(r["uid"], summary="Dentist (moved)")["updated"] is True
    cal.delete_event(r["uid"])


def test_a_held_update_is_not_written_over_a_newer_version(cal):
    r = cal.create_event(summary="Board meeting", start=when(days=11))
    uid = r["uid"]
    cal.s = dataclasses.replace(cal.s, invites_require_approval=False)   # add the guest directly first
    cal.update_event(uid, attendees=["jana@example.org"])
    cal.s = dataclasses.replace(cal.s, invites_require_approval=True)
    held = cal.update_event(uid, summary="Board meeting (new room)")
    assert held["status"] == "queued_for_owner_approval" and held["updated"] is False
    assert str(list(stored(cal, uid).walk("VEVENT"))[0]["summary"]) == "Board meeting"
    cal.s = dataclasses.replace(cal.s, invites_require_approval=False)   # someone edits it in the Calendar app
    cal.update_event(uid, location="Room 4")
    q = cal.outbox.claim(cal.outbox.pending()[0].id)
    with pytest.raises(CalendarError, match="changed in the calendar"):
        cal.release(q)
    ev = list(stored(cal, uid).walk("VEVENT"))[0]
    assert str(ev["summary"]) == "Board meeting" and str(ev["location"]) == "Room 4"   # nothing was overwritten
    cal.delete_event(uid)


def test_without_an_approval_page_such_writes_are_refused(cal):
    cal.outbox = None
    with pytest.raises(CalendarError, match="no approval page"):
        cal.create_event(summary="Sync", start=when(days=12), attendees=["jana@example.org"])
