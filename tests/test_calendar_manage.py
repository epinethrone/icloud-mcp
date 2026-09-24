"""Creating, renaming and deleting calendars: names never clash, the default calendar is never deleted, a calendar with events
is only deleted after a preview and its token, and every cached calendar list is dropped after a change."""
import contextlib

import pytest

from icloud_mcp.cal import CalendarError, CalendarService
from icloud_mcp.config import Settings


class Cal:
    def __init__(self, name, events=0):
        self.name, self.url, self.events, self.deleted, self.props = name, f"https://caldav.example/1/calendars/{name.lower()}/", events, False, []

    def search(self, **kw):
        return [object()] * self.events

    def set_properties(self, props):
        self.props += props
        self.name = str(props[0].value)

    def delete(self):
        self.deleted = True


class Principal:
    def __init__(self, cals):
        self.cals, self.made = cals, []

    def make_calendar(self, name, cal_id):
        c = Cal(name)
        self.made.append((name, cal_id))
        self.cals.append(c)
        return c


@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, DEFAULT_TIMEZONE="UTC").items():
        monkeypatch.setenv(k, v)
    s = CalendarService(Settings.from_env())
    p = Principal([Cal("Calendar"), Cal("Personal", events=5), Cal("Old")])

    @contextlib.contextmanager
    def principal():
        yield p
    monkeypatch.setattr(s, "_principal", principal)
    monkeypatch.setattr(s, "_calendars", lambda pr: [c for c in p.cals if not c.deleted])
    monkeypatch.setattr(s, "_event_calendars", lambda pr: [c for c in p.cals if not c.deleted])
    monkeypatch.setattr(s, "_cal_name", lambda c: c.name)
    monkeypatch.setattr(s, "_occurrences", lambda *a, **k: iter([]))
    return s, p


def test_create_refuses_a_name_in_use_and_drops_cached_lists(svc):
    s, p = svc
    gen = s._cals_gen
    with pytest.raises(CalendarError, match="already a calendar called 'personal'"):
        s.create_calendar("personal")
    r = s.create_calendar("  Trips  ")
    assert r["created"] is True and r["name"] == "Trips" and p.made[0][0] == "Trips" and s._cals_gen == gen + 1
    with pytest.raises(CalendarError, match="1 to 100 characters"):
        s.create_calendar("bad\x07name")
    assert s.create_calendar("two\nlines")["name"] == "two lines"             # a line break is folded into a space


def test_rename_keeps_the_calendar_and_refuses_clashes(svc):
    s, p = svc
    with pytest.raises(CalendarError, match="already a calendar called 'Calendar'"):
        s.update_calendar("Old", "Calendar")
    assert s.update_calendar("Old", "Archive 2025") == {"renamed": True, "from": "Old", "to": "Archive 2025"}


def test_the_default_calendar_is_never_deleted(svc):
    s, p = svc
    with pytest.raises(CalendarError, match="where new events go by default"):
        s.delete_calendar("Calendar")
    assert not p.cals[0].deleted


def test_an_empty_calendar_goes_at_once_one_with_events_needs_the_token(svc):
    s, p = svc
    assert s.delete_calendar("Old") == {"deleted": True, "calendar": "Old"}
    preview = s.delete_calendar("Personal")
    assert preview["deleted"] is False and preview["events"] == 5 and preview["confirm_token"] and not p.cals[1].deleted
    assert "Restore Calendars" in preview["note"]
    with pytest.raises(CalendarError, match="confirm_token"):
        s.delete_calendar("Personal", confirm_token="1.x")
    p.cals[1].events = 6                                                    # an event was added after the preview
    with pytest.raises(CalendarError, match="changed since the preview"):
        s.delete_calendar("Personal", confirm_token=preview["confirm_token"])
    again = s.delete_calendar("Personal")
    done = s.delete_calendar("Personal", confirm_token=again["confirm_token"])
    assert done["deleted"] is True and done["events_deleted"] == 6 and p.cals[1].deleted


def test_only_a_real_default_is_protected_not_whichever_calendar_is_listed_first(svc):
    s, p = svc
    p.cals[0].name = "Work"                                   # no "Calendar" or "Home" left, no DEFAULT_CALENDAR set
    assert s.delete_calendar("Work")["deleted"] is True       # the first in the list is not a default worth protecting
