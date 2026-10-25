"""An update that does not touch the location must leave Apple's structured location (map card, MapKit handle) alone."""
import contextlib

import icalendar
import pytest

from icloud_mcp.cal import CalendarService
from icloud_mcp.config import Settings

# What Apple Calendar writes for a place picked from Maps: LOCATION is "name\naddress", X-APPLE-STRUCTURED-LOCATION carries
# the MapKit handle and the coordinates but no X-ADDRESS equal to LOCATION. Shape taken from a real iCloud event.
APPLE_EVENT = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Apple Inc.//iPhone OS 26.0//EN",
    "BEGIN:VEVENT",
    "UID:D25E0387-D672-4AB4-A4E6-1043716ED551",
    "DTSTAMP:20260930T080000Z",
    "DTSTART:20300930T100000Z",
    "DTEND:20300930T103000Z",
    "SUMMARY:Test",
    "LOCATION:Example Street 1\\nExampletown\\, Germany",
    "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-MAPKIT-HANDLE=CAESlgII2TIaEgk3HPG;X-APPLE-RADIUS=70.5;"
    "X-APPLE-REFERENCEFRAME=1;X-TITLE=Example Street 1:geo:52.5,13.4",
    "SEQUENCE:0",
    "END:VEVENT", "END:VCALENDAR", ""])


@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16,
                     DEFAULT_TIMEZONE="Europe/Berlin").items():
        monkeypatch.setenv(k, v)
    obj = type("Obj", (), {"data": APPLE_EVENT, "save": lambda self, **kw: None})()
    fake_cal = type("Cal", (), {"url": "https://caldav.example/home/", "name": "Home"})()
    monkeypatch.setattr(CalendarService, "_principal", lambda self: contextlib.nullcontext(object()))
    monkeypatch.setattr(CalendarService, "_find", lambda self, p, uid, calendar: (fake_cal, obj))
    return CalendarService(Settings.from_env()), obj


def structured(obj):
    ev = next(iter(icalendar.Calendar.from_ical(obj.data).walk("VEVENT")))
    return ev.get("X-APPLE-STRUCTURED-LOCATION")


def test_renaming_keeps_the_map_card(svc):
    service, obj = svc
    service.update_event("D25E0387-D672-4AB4-A4E6-1043716ED551", summary="Test (renamed)")
    prop = structured(obj)
    assert prop is not None
    assert prop.params.get("X-APPLE-MAPKIT-HANDLE") == "CAESlgII2TIaEgk3HPG"
    assert str(prop) == "geo:52.5,13.4"


def test_an_explicit_new_location_still_replaces_it(svc):
    service, obj = svc
    service.update_event("D25E0387-D672-4AB4-A4E6-1043716ED551", location="Example Street 2, 10115 Exampletown")
    prop = structured(obj)
    assert prop.params.get("X-APPLE-MAPKIT-HANDLE") is None
    assert prop.params.get("X-ADDRESS") == "Example Street 2, 10115 Exampletown"


def test_a_rename_still_adds_the_map_card_an_event_never_had(svc):
    service, obj = svc
    obj.data = "\r\n".join(line for line in obj.data.split("\r\n") if not line.startswith("X-APPLE-STRUCTURED-LOCATION"))
    assert structured(obj) is None
    service.update_event("D25E0387-D672-4AB4-A4E6-1043716ED551", summary="Test (renamed)")
    prop = structured(obj)
    assert prop is not None and prop.params.get("X-ADDRESS") == "Example Street 1, Exampletown, Germany"
