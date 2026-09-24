"""Contacts after an edit elsewhere: only the changed cards are downloaded (ETags, then one multiget), writes made here update the
cached address book in place, and search fields are worked out once per card."""
import html
import re

import httpx
import pytest

import icloud_mcp.contacts as contacts_mod
from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsService
from test_contacts import CARDS, FakeICloud, card, multistatus


class ChangesICloud(FakeICloud):
    """FakeICloud plus what change detection uses: a Depth-1 getetag PROPFIND and addressbook-multiget."""

    multiget_ok = True

    def __call__(self, request):
        body = request.content.decode()
        if request.method == "PROPFIND" and "getetag" in body and "getctag" not in body:
            self.log.append(("PROPFIND", request.url.host, request.url.path, "etags"))
            return httpx.Response(207, content=multistatus(
                '<d:response><d:href>/123/carddavhome/card/</d:href><d:propstat><d:prop></d:prop></d:propstat></d:response>',
                *[f'<d:response><d:href>/123/carddavhome/card/{i}.vcf</d:href><d:propstat><d:prop><d:getetag>{self.etag(i)}'
                  f'</d:getetag></d:prop></d:propstat></d:response>' for i in range(len(self.cards))]).encode())
        if request.method == "REPORT" and "addressbook-multiget" in body:
            self.log.append(("MULTIGET", request.url.host, request.url.path, ""))
            if not self.multiget_ok:
                return httpx.Response(501)
            asked = [int(m) for m in re.findall(r"/card/(\d+)\.vcf", body)]
            return httpx.Response(207, content=multistatus(*[
                f'<d:response><d:href>/123/carddavhome/card/{i}.vcf</d:href><d:propstat><d:prop><d:getetag>{self.etag(i)}</d:getetag>'
                f'<c:address-data>{html.escape(self.cards[i])}</c:address-data></d:prop></d:propstat></d:response>'
                for i in asked if i < len(self.cards)]).encode())
        if request.method in ("PUT", "DELETE", "GET"):
            self.log.append((request.method, request.url.host, request.url.path, ""))
            return httpx.Response(201 if request.method == "PUT" else 200 if request.method == "GET" else 204,
                                  headers={"etag": '"new"'} if request.method == "PUT" else {},
                                  text=self.cards[0] if request.method == "GET" else "")
        return super().__call__(request)

    def count(self, method, path_part=""):
        return sum(1 for m, _, p, _ in self.log if m == method and path_part in p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts_mod, "_CHANGES_FROM_CARDS", 0)                     # the test address book is small
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaabbbbccccdddd", MCP_PUBLIC_URL="https://mcp.example.com",
                     MCP_OWNER_PASSWORD="x" * 16, DATA_DIR=str(tmp_path), CARDDAV_URL="https://contacts.example.test").items():
        monkeypatch.setenv(k, v)
    fake = ChangesICloud()
    return fake, ContactsService(Settings.from_env(), transport=httpx.MockTransport(fake))


def later(monkeypatch, seconds):
    t = contacts_mod.time.monotonic() + seconds
    monkeypatch.setattr(contacts_mod.time, "monotonic", lambda: t)


def names(r):
    return [c["name"] for c in r["contacts"]]


def test_an_edit_elsewhere_downloads_only_the_changed_card(env, monkeypatch):
    fake, svc = env
    svc.search("anna")
    assert fake.count("REPORT") == 1
    fake.ctag, fake.etags = "ctag-2", {0: '"0-edited"'}
    fake.cards = [card("11111111-2222-3333-4444-555555555555", "Anna Lee-Smith", "EMAIL:anna@example.org")] + CARDS[1:]
    later(monkeypatch, 10_000)
    assert "Anna Lee-Smith" in names(svc.search("anna"))
    assert fake.count("REPORT") == 1 and fake.count("MULTIGET") == 1                 # no full download
    assert len(names(svc.search(""))) == len(CARDS)


def test_new_and_vanished_cards_are_picked_up(env, monkeypatch):
    fake, svc = env
    svc.search("")
    fake.ctag = "ctag-2"
    fake.cards = CARDS + [card("u-new", "Zoe New", "EMAIL:zoe@example.org")]         # added elsewhere (a new href)
    later(monkeypatch, 10_000)
    assert "Zoe New" in names(svc.search("", limit=50))
    fake.ctag = "ctag-3"
    fake.cards = CARDS                                                                # and deleted again (its href is gone)
    later(monkeypatch, 20_000)
    everyone = names(svc.search("", limit=50))
    assert "Zoe New" not in everyone and len(everyone) == len(CARDS)
    assert fake.count("REPORT") == 1 and fake.count("MULTIGET") == 1                 # the deletion needed no download at all


def test_when_multiget_fails_the_book_is_downloaded_as_before(env, monkeypatch):
    fake, svc = env
    svc.search("")
    fake.ctag, fake.multiget_ok = "ctag-2", False
    fake.cards = CARDS + [card("u-new", "Zoe New", "EMAIL:zoe@example.org")]
    later(monkeypatch, 10_000)
    assert names(svc.search("zoe")) == ["Zoe New"] and fake.count("REPORT") == 2


def test_writes_here_update_the_cache_without_a_download(env):
    fake, svc = env
    svc.search("")
    made = svc.create(name="Nia Example", emails=["nia@example.org"])
    assert names(svc.search("nia")) == ["Nia Example"]
    svc.delete(made["uid"])
    assert svc.search("nia")["total_matches"] == 0
    assert fake.count("REPORT") == 1                                                 # still the one first download


def test_search_fields_are_worked_out_once_per_card(env, monkeypatch):
    fake, svc = env
    svc.search("")
    calls = []
    real = contacts_mod._norm
    monkeypatch.setattr(contacts_mod, "_norm", lambda s: calls.append(1) or real(s))
    for q in ("anna", "lee", "globex", "renee"):
        svc.search(q)
    assert len(calls) <= 8                                                           # the queries only, never the cards again
    assert "_n" not in svc.get("11111111-2222-3333-4444-555555555555")


def test_a_small_address_book_is_simply_downloaded_again(env, monkeypatch):
    fake, svc = env
    monkeypatch.setattr(contacts_mod, "_CHANGES_FROM_CARDS", len(CARDS) + 1)
    svc.search("")
    fake.ctag = "ctag-2"
    fake.cards = CARDS + [card("u-new", "Zoe New", "EMAIL:zoe@example.org")]
    later(monkeypatch, 10_000)
    assert names(svc.search("zoe")) == ["Zoe New"] and fake.count("REPORT") == 2 and fake.count("MULTIGET") == 0
