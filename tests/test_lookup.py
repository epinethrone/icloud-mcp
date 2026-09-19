"""Approximate lookup: a misspelled name must lead to 'did you mean ...?', never to silence, and never to a silent guess."""
import contextlib
import dataclasses
from datetime import datetime, timezone

import httpx
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsService
from icloud_mcp.mail import MailError, MailService
import icloud_mcp.mail as mail_mod
from icloud_mcp.matching import fuzzy_match_all, phonetic_key, similar_enough
import test_contacts as TC


# ------------------------------------------------------------------ the matcher
@pytest.mark.parametrize("a,b", [("katryn", "katrien"), ("katryn", "katrin"), ("stefan", "stephan"), ("jon", "john"), ("marc", "mark"),
                                 ("sara", "sarah"), ("katherine", "catherine"), ("sofie", "sophie"), ("renee", "Renée"), ("peter", "pieter")])
def test_variant_spellings_of_a_name_are_linked(a, b):
    assert similar_enough(a, b) >= 0.8


@pytest.mark.parametrize("a,b", [("mark", "mary"), ("jan", "jen"), ("emma", "emily"), ("sam", "sim"), ("eva", "ava"), ("nora", "nina"), ("katryn", "kirsten")])
def test_different_names_are_not_linked(a, b):
    assert similar_enough(a, b) == 0.0


def test_every_word_of_the_query_must_match_and_non_latin_names_still_work():
    assert fuzzy_match_all(["katryn", "whitfeld"], ["katrien", "whitfield"]) > 0.8
    assert fuzzy_match_all(["katryn", "smith"], ["katrien", "whitfield"]) == 0.0
    assert similar_enough("田中", "田中") == 1.0 and similar_enough("田中", "山田") == 0.0
    assert phonetic_key("田中") == "田中"


# ------------------------------------------------------------------ contacts: similar names
@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaabbbbccccdddd", MCP_PUBLIC_URL="https://mcp.example.com",
                     MCP_OWNER_PASSWORD="x" * 16, DATA_DIR=str(tmp_path), CARDDAV_URL="https://contacts.example.test").items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    return ContactsService(s, transport=httpx.MockTransport(TC.FakeICloud()))


def test_a_misspelled_name_offers_did_you_mean_instead_of_nothing(svc):
    r = svc.search("Renne")                                            # fixture has "Renée Dupont"
    assert r["total_matches"] == 0 and r["did_you_mean"] == ["Renée Dupont"]
    assert r["similar"][0]["emails"] and r["similar"][0]["similarity"] >= 0.8
    assert "Ask the user which person" in r["note"] and "mail_find_correspondent" in r["note"]
    assert TC.names(svc.search("Tanaca")) == [] and svc.search("Tanaca")["did_you_mean"] == ["田中 Tanaka"]
    assert svc.search("Ann Jonez")["did_you_mean"] == ["Ann Jones"]    # every word still has to fit


def test_an_exact_match_is_never_diluted_by_similar_guesses(svc):
    r = svc.search("renee")
    assert TC.names(r) == ["Renée Dupont"] and "similar" not in r and "did_you_mean" not in r


def test_nothing_similar_gives_the_plain_hint_and_no_invented_people(svc):
    r = svc.search("qqqzzzxx")
    assert r["total_matches"] == 0 and "similar" not in r and "mail_find_correspondent" in r["hint"]


def test_similar_names_respect_the_with_email_filter(svc):
    everyone = svc.search("Hana")                                       # 'hana' is not a substring of any name, so this is a pure similar-name lookup
    assert everyone["total_matches"] == 0 and "Hannah Ford" in everyone["did_you_mean"]
    hannah = next(c for c in everyone["similar"] if c["name"] == "Hannah Ford")
    assert hannah["has_email"] is False                                 # she has no email on file (and is still shown, flagged)
    only_email = svc.search("Hana", with_email=True)
    assert all(c["has_email"] for c in only_email["similar"]) and "Hannah Ford" not in only_email["did_you_mean"]


# ------------------------------------------------------------------ mail: find people you have corresponded with
def header(frm=None, to=None, cc=None):
    lines = [f"From: {frm}"] if frm else []
    lines += ([f"To: {to}"] if to else []) + ([f"Cc: {cc}"] if cc else [])
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FakeMailbox:
    def __init__(self, folders):
        self.folders, self.current, self.fetch_calls = folders, None, 0

    def select_folder(self, name, readonly=False):
        self.current = name

    def search(self, crit):
        return list(self.folders[self.current])

    def fetch(self, uids, items):
        self.fetch_calls += 1
        rows = self.folders[self.current]
        return {u: {b"INTERNALDATE": rows[u][1], b"BODY[HEADER.FIELDS (FROM TO CC)]": rows[u][0]} for u in uids}


D = lambda m, d: datetime(2026, m, d, 12, 0, tzinfo=timezone.utc)
INBOX = {
    1: (header(frm="Old Friend <old.friend@elsewhere.example>", to="me@icloud.com"), D(1, 5)),                    # long ago
    2: (header(frm="Katrien Whitfield <l.whitfield@clinic.example>", to="me@icloud.com"), D(7, 2)),
    3: (header(frm="Katrien Whitfield <l.whitfield@clinic.example>", to="me@icloud.com", cc="Annelise Ward <a.ward@clinic.example>"), D(7, 9)),
    4: (header(frm="Annelise Ward <a.ward@clinic.example>", to="me@icloud.com"), D(8, 1)),
    5: (header(frm="Shop <noreply@shop.example>", to="me@icloud.com"), D(9, 1)),
    6: (header(frm="Katrien Whitfield <l.whitfield@clinic.example>", to="me@icloud.com"), D(9, 3)),
}
SENT = {1: (header(frm="Me <me@icloud.com>", to="\"Katrien O.\" <l.whitfield@clinic.example>, bob@example.org"), D(9, 4))}


@pytest.fixture
def mail(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaabbbbccccdddd", MCP_PUBLIC_URL="https://mcp.example.com",
                     MCP_OWNER_PASSWORD="x" * 16, DATA_DIR=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    box = FakeMailbox({"INBOX": INBOX, "Sent Messages": SENT})

    @contextlib.contextmanager
    def fake_imap(self):
        yield box

    monkeypatch.setattr(MailService, "imap", fake_imap)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, n: {"inbox": "INBOX", "sent": "Sent Messages"}.get(n.lower(), n))
    return MailService(Settings.from_env()), box


def test_a_misspelled_first_name_finds_the_right_person_and_asks(mail):
    svc, _ = mail
    r = svc.find_correspondents("Katryn")
    top = r["matches"][0]
    assert top["address"] == "l.whitfield@clinic.example" and top["match"] == "similar" and top["name"] == "Katrien Whitfield"
    assert top["messages_from_them"] == 3 and top["messages_to_them"] == 1     # three received (INBOX), one addressed to them (Sent)
    assert top["last_contact"] == "2026-09-04" and top["also_written_as"] == ["Katrien O."]
    assert "Ask the user which person" in r["note"] and "untrusted" in r["notice"]


def test_exact_and_partial_names_are_exact_matches_without_a_warning(mail):
    svc, _ = mail
    for q in ("katrien", "Whitfield", "l.whitfield", "WHITFIELD katrien"):
        r = svc.find_correspondents(q)
        assert [m["address"] for m in r["matches"]] == ["l.whitfield@clinic.example"] and r["matches"][0]["match"] == "exact" and "note" not in r


def test_a_company_or_domain_finds_everyone_there_most_active_first(mail):
    svc, _ = mail
    r = svc.find_correspondents("clinic")
    assert [m["address"] for m in r["matches"]] == ["l.whitfield@clinic.example", "a.ward@clinic.example"] and all(m["match"] == "exact" for m in r["matches"])


def test_the_owner_is_never_listed_and_unknowns_return_a_hint(mail):
    svc, _ = mail
    assert all(m["address"] != "me@icloud.com" for m in svc.find_correspondents("me")["matches"])
    assert all(m["address"] != "me@icloud.com" for m in svc.find_correspondents("icloud")["matches"])
    r = svc.find_correspondents("zzzzqqqq")
    assert r["matches"] == [] and "Ask the user for the address" in r["hint"]
    with pytest.raises(MailError, match="Give a name"):
        svc.find_correspondents("   ")


def test_recent_scan_can_miss_old_mail_and_says_how_to_search_everything(mail, monkeypatch):
    svc, _ = mail
    monkeypatch.setattr(mail_mod, "_SCAN_INBOX", 2)                    # only the 2 newest received messages
    r = svc.find_correspondents("friend")
    assert r["matches"] == [] and r["scanned"] == {"received": 2, "of_received": 6, "sent": 1, "of_sent": 1} and "search_all_history=true" in r["hint"]
    deep = svc.find_correspondents("friend", search_all_history=True)
    assert [m["address"] for m in deep["matches"]] == ["old.friend@elsewhere.example"] and deep["scanned"]["received"] == 6


def test_the_scan_is_cached_between_lookups(mail):
    svc, box = mail
    svc.find_correspondents("katrien")
    calls = box.fetch_calls
    svc.find_correspondents("annelise"); svc.find_correspondents("clinic")
    assert box.fetch_calls == calls                                    # no new mailbox reads within the cache lifetime


def test_only_headers_are_ever_requested(mail):
    svc, box = mail
    seen = []
    orig = box.fetch
    box.fetch = lambda uids, items: (seen.append(items), orig(uids, items))[1]
    svc.find_correspondents("katrien")
    assert seen and all("BODY.PEEK[HEADER.FIELDS (FROM TO CC)]" in items and not any("TEXT" in i for i in items) for items in seen)


def test_the_tool_is_registered_and_documented(mail):
    import asyncio
    from icloud_mcp.server import create_server
    svc, _ = mail
    async def go():
        mcp, _ = create_server(Settings.from_env())
        return {t.name: t for t in await mcp.list_tools()}
    tools = asyncio.run(go())
    t = tools["mail_find_correspondent"]
    assert "misspell" in (t.description + str(t.input_schema)).lower() and "confirm" in t.description.lower()
    assert all(v.get("description") for v in (getattr(t, "input_schema", None) or t.inputSchema)["properties"].values())
