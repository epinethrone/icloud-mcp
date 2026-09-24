"""A stranger's invitation repeating every second must not be expanded (millions of occurrences, a hung calendar read), and
every ordinary series must expand exactly as caldav's own search(expand=True) does."""
import time
from datetime import datetime, timezone

import caldav
import icalendar

from icloud_mcp.cal import _search_expanded

UTC = timezone.utc
START, END = datetime(2030, 3, 1, tzinfo=UTC), datetime(2030, 4, 1, tzinfo=UTC)


def ics(uid, rule, extra=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20300101T000000Z\r\nDTSTART:20300304T190000Z\r\nDTEND:20300304T200000Z\r\n"
            f"SUMMARY:{uid}\r\nRRULE:{rule}\r\nEND:VEVENT\r\n{extra}END:VCALENDAR\r\n")


class Cal:
    """Hands out real caldav objects, as a server REPORT would, and a real caldav searcher."""
    def __init__(self, datas):
        self.real = caldav.Calendar(client=None, url="https://caldav.example.com/cal/")
        self.datas = datas

    def search(self, **kw):
        assert kw["expand"] is False                                   # never ask caldav to expand before we looked
        return [caldav.Event(client=None, data=d, parent=self.real) for d in self.datas]

    def searcher(self, **kw):
        return self.real.searcher(**kw)


def starts(datas):
    return sorted(str(c.get("dtstart").dt) for d in datas for c in icalendar.Calendar.from_ical(d).walk("VEVENT"))


def test_an_ordinary_series_expands_exactly_like_caldav_does():
    weekly = ics("weekly", "FREQ=WEEKLY;COUNT=10")
    ours = _search_expanded(Cal([weekly]), START, END, [])
    from caldav.search import filter_search_results
    cal = Cal([weekly])
    theirs = [o.data for o in filter_search_results(cal.search(expand=False), cal.searcher(start=START, end=END, event=True, expand=True))]
    assert starts(ours) == starts(theirs) and len(starts(ours)) == 4        # 4, 11, 18, 25 March


def test_a_series_repeating_every_second_is_set_aside_but_its_exceptions_come_through():
    moved = ("BEGIN:VEVENT\r\nUID:spam\r\nDTSTAMP:20300101T000000Z\r\nRECURRENCE-ID:20300304T190001Z\r\n"
             "DTSTART:20300310T120000Z\r\nDTEND:20300310T130000Z\r\nSUMMARY:moved one\r\nEND:VEVENT\r\n")
    folded = ics("spam", "FREQ=SECO\r\n NDLY", extra=moved)                 # a folded line must not hide the rule
    skipped = []
    t = time.monotonic()
    out = _search_expanded(Cal([folded, ics("weekly", "FREQ=WEEKLY;COUNT=2")]), START, END, skipped)
    assert time.monotonic() - t < 2
    assert skipped == ["spam"] and "moved one" in "".join(out) and len(starts(out)) == 3   # 2 weekly + the moved one
