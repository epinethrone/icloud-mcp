"""calendar_move_event: WebDAV MOVE between calendars, a safe copy-then-delete fallback, and the invitation rule."""
import contextlib
import dataclasses

import pytest

from icloud_mcp.cal import CalendarError, CalendarService
from icloud_mcp.config import Settings

ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:ev1\r\nDTSTAMP:20260924T120000Z\r\n"
       "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\nSUMMARY:Dentist\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


class Resp:
    def __init__(self, status):
        self.status = status


class Client:
    def __init__(self, status):
        self.status, self.calls = status, []

    def request(self, url, method, body, headers):
        self.calls.append((method, url, headers))
        return Resp(self.status)


class Obj:
    def __init__(self, url, data, client, fail_delete=False):
        self.url, self.data, self.client, self.fail_delete, self.deleted = url, data, client, fail_delete, False

    def delete(self):
        if self.fail_delete:
            raise RuntimeError("403")
        self.deleted = True


class Cal:
    def __init__(self, name):
        self.name, self.url, self.saved = name, f"https://caldav.example/123/calendars/{name.lower()}/", []

    def save_event(self, data):
        o = Obj(self.url + "copy.ics", data, None)
        self.saved.append(o)
        return o


@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    s = CalendarService(Settings.from_env())
    cals = {n: Cal(n) for n in ("Calendar", "Personal", "Health")}

    @contextlib.contextmanager
    def principal():
        yield object()
    monkeypatch.setattr(s, "_principal", principal)
    monkeypatch.setattr(s, "_pick", lambda p, name: [cals[name]] if name else list(cals.values()))
    monkeypatch.setattr(s, "_cal_name", lambda c: c.name)
    return s, cals


def use(s, monkeypatch, cals, status, extra="", fail_delete=False):
    client = Client(status)
    obj = Obj(cals["Calendar"].url + "ev1.ics", ICS.format(extra=extra), client, fail_delete)
    monkeypatch.setattr(s, "_find", lambda p, uid, cal: (cals["Calendar"], obj))
    return client, obj


def test_move_uses_webdav_move_to_the_same_name_in_the_target(svc, monkeypatch):
    s, cals = svc
    client, obj = use(s, monkeypatch, cals, 201)
    r = s.move_event("ev1", "Personal")
    assert r == {"moved": True, "uid": "ev1", "summary": "Dentist", "from": "Calendar", "to": "Personal"}
    (method, url, headers), = client.calls
    assert method == "MOVE" and url.endswith("/calendar/ev1.ics")
    assert headers == {"Destination": cals["Personal"].url + "ev1.ics", "Overwrite": "F"}   # never overwrites
    assert not obj.deleted and cals["Personal"].saved == []                                 # nothing recreated


def test_already_there_and_name_clash_change_nothing(svc, monkeypatch):
    s, cals = svc
    client, _ = use(s, monkeypatch, cals, 201)
    assert s.move_event("ev1", "Calendar")["moved"] is False and client.calls == []
    use(s, monkeypatch, cals, 412)
    with pytest.raises(CalendarError, match="already holds an event"):
        s.move_event("ev1", "Personal")


def test_without_move_it_copies_first_then_deletes_and_rolls_back(svc, monkeypatch):
    s, cals = svc
    _, obj = use(s, monkeypatch, cals, 405)
    assert s.move_event("ev1", "Health")["moved"] is True
    assert obj.deleted and cals["Health"].saved[0].data == obj.data
    _, obj = use(s, monkeypatch, cals, 501, fail_delete=True)
    with pytest.raises(CalendarError, match="nothing was moved"):
        s.move_event("ev1", "Personal")
    assert cals["Personal"].saved[0].deleted                                                 # the copy was removed again


def test_events_with_guests_follow_the_invitation_rule(svc, monkeypatch):
    s, cals = svc
    guest = "ORGANIZER:mailto:me@icloud.com\r\nATTENDEE:mailto:anna@example.org\r\n"
    client, _ = use(s, monkeypatch, cals, 201, extra=guest)
    with pytest.raises(CalendarError, match="ALLOW_CALENDAR_INVITES"):
        s.move_event("ev1", "Personal")
    assert client.calls == []
    s.s = dataclasses.replace(s.s, allow_calendar_invites=True)
    assert s.move_event("ev1", "Personal")["moved"] is True


def test_other_server_errors_are_reported_not_hidden(svc, monkeypatch):
    s, cals = svc
    use(s, monkeypatch, cals, 403)
    with pytest.raises(CalendarError, match="HTTP 403"):
        s.move_event("ev1", "Personal")
