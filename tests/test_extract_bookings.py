"""mail_extract_bookings: exact bookings from schema.org booking data and .ics attachments, never guessed from the text."""
import asyncio
import contextlib
import json
from email.message import EmailMessage

import pytest

from icloud_mcp.config import Settings
from icloud_mcp.extract import extract, from_ics, from_json_ld
from icloud_mcp.mail import MailService
from icloud_mcp.server import create_server

FLIGHT = {
    "@context": "http://schema.org", "@type": "FlightReservation", "reservationNumber": "RXJ34P",
    "reservationStatus": "http://schema.org/ReservationConfirmed", "underName": {"@type": "Person", "name": "Test Person"},
    "reservationFor": {"@type": "Flight", "flightNumber": "1234", "airline": {"@type": "Airline", "name": "KLM", "iataCode": "KL"},
                       "departureAirport": {"@type": "Airport", "name": "Amsterdam Schiphol", "iataCode": "AMS"},
                       "departureTime": "2026-10-03T08:15:00+02:00",
                       "arrivalAirport": {"@type": "Airport", "name": "London Heathrow", "iataCode": "LHR"},
                       "arrivalTime": "2026-10-03T08:35:00+01:00"},
}
HOTEL = {
    "@context": "http://schema.org", "@type": "LodgingReservation", "reservationNumber": "H-778",
    "reservationFor": {"@type": "LodgingBusiness", "name": "Hotel Canal",
                       "address": {"@type": "PostalAddress", "streetAddress": "Herengracht 1", "postalCode": "1015 BA",
                                   "addressLocality": "Amsterdam", "addressCountry": "NL"}},
    "checkinTime": "2026-10-10T15:00:00+02:00", "checkoutTime": "2026-10-12T11:00:00+02:00",
}
CONCERT = {"@context": "https://schema.org", "@graph": [
    {"@type": "Organization", "name": "Tickets Ltd"},
    {"@type": "EventReservation", "reservationNumber": "T-9",
     "reservationFor": {"@type": "Event", "name": "Evanescence", "startDate": "2026-09-22T20:00:00+02:00",
                        "location": {"@type": "Place", "name": "Ziggo Dome",
                                     "address": {"@type": "PostalAddress", "streetAddress": "De Passage 100", "addressLocality": "Amsterdam"}}}},
]}
ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:abc-123@clinic.example
DTSTART:20261001T090000Z
DTEND:20261001T093000Z
SUMMARY:Dentist check-up
LOCATION:Oudegracht 10, Utrecht
ORGANIZER:mailto:desk@clinic.example
END:VEVENT
END:VCALENDAR
"""


def html_with(*docs, broken=False):
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(d)}</script>' for d in docs)
    if broken:
        blocks += '<script type="application/ld+json">{not json</script>'
    return f"<html><head>{blocks}</head><body><p>Your trip is on 5 October at 7am (the text says so, the data does not).</p></body></html>"


def message(html=None, ics=None, text="See you soon."):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bookings <noreply@example.org>", "me@icloud.com", "Your booking"
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    if ics:
        m.add_attachment(ics, maintype="text", subtype="calendar", filename="invite.ics")
    return bytes(m)


def test_a_flight_is_copied_exactly_from_the_booking_data():
    (f,) = from_json_ld(html_with(FLIGHT))
    assert f["kind"] == "flight" and f["flight"] == "KL1234" and f["reservation_number"] == "RXJ34P" and f["status"] == "ReservationConfirmed"
    assert f["departure"] == "2026-10-03T08:15:00+02:00" and f["arrival"] == "2026-10-03T08:35:00+01:00"
    ev = f["calendar_event"]
    assert ev["summary"] == "Flight KL1234 AMS to LHR" and ev["start"] == FLIGHT["reservationFor"]["departureTime"]
    assert ev["location"] == "Amsterdam Schiphol" and "RXJ34P" in ev["description"]


def test_a_hotel_stay_and_an_event_inside_a_graph():
    hotel, concert = from_json_ld(html_with(HOTEL, CONCERT))
    assert hotel["check_in"] == "2026-10-10T15:00:00+02:00" and hotel["calendar_event"]["end"] == "2026-10-12T11:00:00+02:00"
    assert hotel["address"] == "Hotel Canal, Herengracht 1, 1015 BA, Amsterdam, NL"
    assert concert["name"] == "Evanescence" and concert["venue"] == "Ziggo Dome, De Passage 100, Amsterdam"
    assert concert["calendar_event"]["summary"] == "Evanescence"


def test_an_ics_invitation_keeps_its_own_times_and_organizer():
    (inv,) = from_ics(ICS)
    assert inv["kind"] == "invitation" and inv["uid"] == "abc-123@clinic.example" and inv["organizer"] == "desk@clinic.example"
    assert inv["start"] == "2026-10-01T09:00:00+00:00" and inv["calendar_event"]["location"] == "Oudegracht 10, Utrecht"


def test_a_whole_message_with_both_sources_and_broken_json_in_between():
    out = extract(message(html=html_with(FLIGHT, broken=True), ics=ICS))
    kinds = [i["kind"] for i in out["items"]]
    assert kinds == ["flight", "invitation"] and out["found"] == 2 and "never guessed" in out["note"]


def test_nothing_is_invented_from_the_text():
    out = extract(message(html="<p>Your flight KL1234 leaves on 5 October at 7am.</p>", text="Flight on 5 October"))
    assert out["items"] == [] and out["found"] == 0 and "no structured booking data" in out["note"]
    assert from_json_ld(html_with({"@type": "FlightReservation", "reservationNumber": "X"})) == []     # no time, no item


def test_the_tool_reads_the_message_without_marking_it_read(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    raw = message(html=html_with(HOTEL))
    seen = []

    class C:
        def select_folder(self, f, readonly=False):
            seen.append(readonly)
            return {b"UIDVALIDITY": 3}

        def fetch(self, uids, items):
            seen.append(items)
            return {uids[0]: {b"BODY[]": raw, b"FLAGS": (), b"INTERNALDATE": None}}

    @contextlib.contextmanager
    def fake(self, fresh=False):
        yield C()
    monkeypatch.setattr(MailService, "imap", fake)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, n: n)
    mcp, _ = create_server(Settings.from_env())
    r = json.loads(asyncio.run(mcp.call_tool("mail_extract_bookings", {"folder": "INBOX", "uid": 9})).content[0].text)
    assert r["items"][0]["kind"] == "hotel" and r["uidvalidity"] == 3 and "untrusted" in r["notice"].lower()
    assert seen[0] is True and "BODY.PEEK[]" in seen[1]                                   # read-only, PEEK: not marked read


def test_a_string_airport_does_not_hide_the_rest_of_the_mail():
    flight = json.loads(json.dumps(FLIGHT))
    flight["reservationFor"]["departureAirport"], flight["reservationFor"]["arrivalAirport"] = "AMS", "LHR"
    items = from_json_ld(html_with(flight, HOTEL))
    assert [i["kind"] for i in items] == ["flight", "hotel"] and items[0]["calendar_event"]["summary"] == "Flight KL1234 AMS to LHR"


def test_cancellations_are_never_handed_over_as_bookable():
    cancelled = dict(HOTEL, reservationStatus="http://schema.org/ReservationCancelled")
    (c,) = from_json_ld(html_with(cancelled))
    assert c["kind"] == "cancellation" and c["cancelled_booking"] == "hotel" and "calendar_event" not in c
    (ics,) = from_ics(ICS.replace(b"METHOD:REQUEST", b"METHOD:CANCEL"))
    assert ics["kind"] == "cancellation" and "calendar_event" not in ics and ics["uid"] == "abc-123@clinic.example"
