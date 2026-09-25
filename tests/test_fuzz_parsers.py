"""Fuzzing the parsers that read a stranger's bytes: contact cards, calendar invitations and their repeat rules, booking data in
mail, List-Unsubscribe headers, and the Messages typedstream. Seeded (so a failure reproduces) and time-bounded per call (so a
regular-expression or expansion blow-up fails the test instead of hanging CI). A parser may refuse input with an ordinary
exception or return nothing; it may not crash in any other way, hang, or return the wrong type. Inputs are synthetic."""
import contextlib
import importlib.util
import pathlib
import random
import signal

import icalendar
import pytest

from icloud_mcp import extract, mailbulk
from icloud_mcp.cal import event_to_dict, readable_event, too_frequent
from icloud_mcp.contacts import parse_vcard

ROUNDS = 300
LIMIT_SECONDS = 2

VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna Example\r\nN:Example;Anna;;;\r\nEMAIL;TYPE=INTERNET:anna@example.org\r\n"
         "TEL;TYPE=CELL:+31 6 12345678\r\nBDAY:1990-04-01\r\nX-ADDRESSBOOKSERVER-KIND:group\r\n"
         "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:1234\r\nNOTE:line one\\nline two\r\nUID:abc\r\nEND:VCARD\r\n")
ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//y//EN\r\nBEGIN:VEVENT\r\nUID:u1@example.org\r\nDTSTAMP:20260901T100000Z\r\n"
       "DTSTART;TZID=Europe/Amsterdam:20260925T100000\r\nDTEND;TZID=Europe/Amsterdam:20260925T110000\r\nSUMMARY:Dinner\r\n"
       "LOCATION:Example Street 1\r\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10\r\nATTENDEE;CN=Anna;PARTSTAT=NEEDS-ACTION:mailto:anna@example.org\r\n"
       "ORGANIZER;CN=Ben:mailto:ben@example.org\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nEND:VEVENT\r\n"
       "END:VCALENDAR\r\n")
JSON_LD = ('<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"FlightReservation",'
           '"reservationFor":{"@type":"Flight","flightNumber":"123","departureTime":"2026-10-01T10:00:00+02:00",'
           '"departureAirport":{"@type":"Airport","name":"Schiphol"},"arrivalAirport":{"@type":"Airport","name":"Lisbon"}},'
           '"reservationStatus":"https://schema.org/ReservationConfirmed"}</script></html>')
RRULES = ["FREQ=DAILY;COUNT=5", "FREQ=HOURLY;BYMINUTE=0,15,30,45", "FREQ=MINUTELY", "FREQ=WEEKLY;BYDAY=MO,TU;INTERVAL=2",
          "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1", "FREQ=DAILY;BYHOUR=1,2,3;BYSECOND=0,1"]
UNSUB = ["<https://shop.example/u/1>, <mailto:u@shop.example?subject=stop>", "<mailto:leave@lists.example>", "junk", ""]
ALPHABET = list(b"\r\n;:,=<>\"\\{}[]@+-. ") + list(range(256))


class Slow(Exception):
    pass


@contextlib.contextmanager
def time_limit(seconds):
    def fire(*_):
        raise Slow()
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def mutate(rng, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 8)):
        op = rng.random()
        i = rng.randrange(len(b) + 1)
        if op < 0.3 and b:
            b[min(i, len(b) - 1)] = rng.choice(ALPHABET)
        elif op < 0.5:
            b[i:i] = bytes(rng.choice(ALPHABET) for _ in range(rng.randint(1, 16)))
        elif op < 0.7 and b:
            del b[i:i + rng.randint(1, 32)]
        elif op < 0.85 and b:
            j = rng.randrange(len(b))
            b[i:i] = b[j:j + rng.randint(1, 64)] * rng.randint(1, 20)          # repeat a slice: nesting and long lines
        else:
            b[i:i] = rng.choice([b"\r\n ", b"\\n", b"\\", b";;;;", b"\x00", "é😀‮".encode(), b"=" * 50])
    return bytes(b[:4096])


def run(fn, data, allowed=(ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, OverflowError, LookupError)):
    """Call fn under the time limit. Ordinary refusals are fine; anything else, or a hang, fails with the input shown."""
    try:
        with time_limit(LIMIT_SECONDS):
            return fn(data)
    except Slow:
        pytest.fail(f"{fn.__name__} took over {LIMIT_SECONDS}s on {data!r}")
    except allowed:
        return None
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"{fn.__name__} raised {type(e).__name__}: {e} on {data!r}")


def cases(seed, base):
    rng = random.Random(seed)
    yield base
    for _ in range(ROUNDS):
        yield mutate(rng, base)


def test_contact_cards():
    for data in cases(1, VCARD.encode()):
        out = run(lambda d: parse_vcard(d.decode("utf-8", "replace")), data)
        assert out is None or isinstance(out, dict)


def test_calendar_invitations_and_their_events():
    def events(d):
        try:
            return icalendar.Calendar.from_ical(d).walk("VEVENT")
        except Exception:  # noqa: BLE001 - icalendar refusing the whole object is fine; the listing skips it
            return []
    for data in cases(2, ICS.encode()):
        for comp in events(data):                            # whatever icalendar accepted must convert without raising
            out = run(lambda c: event_to_dict(c, "Personal"), comp, allowed=())
            assert isinstance(out, dict)
    for data in cases(3, ICS.encode()):
        out = run(extract.from_ics, data, allowed=())
        assert isinstance(out, list)


def test_a_broken_end_date_is_left_out_not_fatal():
    # Found by the fuzzer: icalendar keeps "DTEND:...T11\xbe000" as a placeholder that raises on .dt, which crashed event_to_dict
    # and could fail a whole calendar listing.
    data = ICS.encode().replace(b"DTEND;TZID=Europe/Amsterdam:20260925T110000", b"DTEND;TZID=Europe/Amsterdam:20260925T11\xbe000")
    comp = icalendar.Calendar.from_ical(data).walk("VEVENT")[0]
    assert event_to_dict(comp, "Personal")["start"].startswith("2026-09-25T10:00") and not readable_event(comp)
    got = extract.from_ics(data)                                            # a booking still comes through, without its end
    assert len(got) == 1 and got[0]["start"].startswith("2026-09-25T10:00") and "end" not in got[0]


def test_repeat_rules():
    for i, rule in enumerate(RRULES):
        for data in cases(10 + i, rule.encode()):
            out = run(lambda d: too_frequent(icalendar.vRecur.from_ical(d.decode("utf-8", "replace"))), data)
            assert out is None or isinstance(out, bool)


def test_booking_data_in_mail():
    for data in cases(4, JSON_LD.encode()):
        out = run(lambda d: extract.from_json_ld(d.decode("utf-8", "replace")), data)
        assert out is None or isinstance(out, list)


def test_list_unsubscribe_headers():
    for i, header in enumerate(UNSUB):
        for data in cases(20 + i, header.encode() or b"<>"):
            out = run(lambda d: mailbulk.parse_unsubscribe(d.decode("utf-8", "replace"), "List-Unsubscribe=One-Click"), data)
            assert out is None or isinstance(out, dict)


def test_messages_typedstream():
    spec = importlib.util.spec_from_file_location(
        "imessage_ops", pathlib.Path(__file__).parent.parent / "mac-helper" / "ops" / "imessage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for base in (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85"
                 b"\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x05Hello\x86\x84\x02iI\x01\x05\x92",
                 b"NSString\x01\x94\x84\x01+\x81\x10\x00" + b"x" * 16, b"NSString\x01\x94\x84\x01+\x83" + b"\xff" * 8):
        for data in cases(30, base):
            out = run(mod.decode_attributed_body, data, allowed=())                  # the decoder must never raise
            assert out is None or isinstance(out, str)
