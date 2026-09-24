"""Contact groups in Apple's format: groups are never mistaken for people, a contact shows the groups it is in, and group
edits keep everything else on the card and never touch a member's own contact."""
import html

import httpx
import pytest
from test_contacts import CARDS, FakeICloud, card, multistatus

from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsError, ContactsService, _rewrite_group, build_group_vcard, parse_vcard

BOOK = "/123/carddavhome/card/"
FRIENDS = "\r\n".join(["BEGIN:VCARD", "VERSION:3.0", "N:Friends;;;;", "FN:Friends", "UID:g-friends", "X-ADDRESSBOOKSERVER-KIND:group",
                       "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:u-lee", "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:u-gone",
                       "X-SOMETHING-FROM-APPLE:keep me", "END:VCARD", ""])


class FakeBook(FakeICloud):
    """FakeICloud plus a writable store: PUT, GET and DELETE on card URLs, with ETags."""

    def __init__(self):
        super().__init__(CARDS + [FRIENDS])
        self.store = {f"{BOOK}{i}.vcf": c for i, c in enumerate(self.cards)}
        self.version = {}

    def tag(self, path):
        return f'"v{self.version.get(path, 0)}"'

    def __call__(self, request):
        path = request.url.path
        if request.method == "REPORT" and "getctag" not in request.content.decode():
            ok = [f'<d:response><d:href>{p}</d:href><d:propstat><d:prop><d:getetag>{self.tag(p)}</d:getetag>'
                  f'<c:address-data>{html.escape(c)}</c:address-data></d:prop></d:propstat></d:response>' for p, c in self.store.items()]
            return httpx.Response(207, content=multistatus(*ok).encode(), headers={"content-type": "application/xml"})
        if request.method == "PUT":
            if request.headers.get("if-match") and request.headers["if-match"] != self.tag(path):
                return httpx.Response(412)
            new = path not in self.store
            self.store[path] = request.content.decode()
            self.version[path] = self.version.get(path, 0) + 1
            self.ctag += "x"
            return httpx.Response(201 if new else 204, headers={"etag": self.tag(path)})
        if request.method == "GET" and path in self.store:
            return httpx.Response(200, content=self.store[path].encode(), headers={"etag": self.tag(path)})
        if request.method == "DELETE" and path in self.store:
            del self.store[path]
            self.ctag += "x"
            return httpx.Response(204)
        return super().__call__(request)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaabbbbccccdddd", MCP_PUBLIC_URL="https://mcp.example.com",
                     MCP_OWNER_PASSWORD="x" * 16, DATA_DIR=str(tmp_path), CARDDAV_URL="https://contacts.example.test").items():
        monkeypatch.setenv(k, v)
    book = FakeBook()
    return book, ContactsService(Settings.from_env(), transport=httpx.MockTransport(book))


def test_a_group_card_is_parsed_as_a_group_with_its_members():
    g = parse_vcard(FRIENDS)
    assert g["kind"] == "group" and g["members"] == ["u-lee", "u-gone"] and g["name"] == "Friends"
    assert "kind" not in parse_vcard(card("u-x", "Someone"))


def test_groups_are_never_people_and_people_show_their_groups(env):
    book, svc = env
    assert "Friends" not in [c["name"] for c in svc.search("")["contacts"]]
    assert svc.search("friends")["total_matches"] == 0
    lee = next(c for c in svc.search("lee")["contacts"] if c["uid"] == "u-lee")
    assert lee["groups"] == ["Friends"] and svc.get("u-lee")["groups"] == ["Friends"]
    with pytest.raises(ContactsError, match="No contact"):
        svc.get("g-friends")
    with pytest.raises(ContactsError, match="No contact"):
        svc.delete("g-friends")                                               # the contact tools cannot touch a group


def test_listing_and_reading_groups(env):
    _, svc = env
    assert svc.list_groups()["groups"] == [{"uid": "g-friends", "name": "Friends", "members": 2}]
    g = svc.get_group("g-friends")
    assert [m["name"] for m in g["members"]] == ["Anna Lee"] and g["unresolved"] == ["u-gone"]


def test_create_update_and_delete_a_group(env):
    book, svc = env
    with pytest.raises(ContactsError, match="already a group"):
        svc.create_group("friends")
    with pytest.raises(ContactsError, match="Not contacts"):
        svc.create_group("Family", ["u-nobody"])
    made = svc.create_group("Family", ["u-hannah"])
    assert made["created"] is True and svc.get_group(made["uid"])["members"][0]["name"] == "Hannah Ford"
    r = svc.update_group(made["uid"], name="Family & co", add_members=["u-jo"], remove_members=["u-hannah"])
    assert r["updated"] and r["added"] == ["u-jo"] and r["removed"] == ["u-hannah"]
    assert [m["name"] for m in svc.get_group(made["uid"])["members"]] == ["Ann Jones"]
    assert svc.search("hannah")["contacts"][0]["name"] == "Hannah Ford"          # removing a member keeps the contact
    with pytest.raises(ContactsError, match="not 'Friends'"):
        svc.delete_group(made["uid"], "Friends")
    assert svc.delete_group(made["uid"], "family & co")["deleted"] is True
    assert [g["name"] for g in svc.list_groups()["groups"]] == ["Friends"]


def test_editing_a_group_keeps_lines_we_do_not_manage(env):
    book, svc = env
    svc.update_group("g-friends", add_members=["u-jo"])
    stored = next(c for c in book.store.values() if "UID:g-friends" in c)
    assert "X-SOMETHING-FROM-APPLE:keep me" in stored and stored.count("X-ADDRESSBOOKSERVER-MEMBER") == 3


def test_group_cards_escape_their_name_and_round_trip():
    raw = build_group_vcard(uid="G1", name="Ann; Bob\nand co", members=["a", "a", "b"])
    g = parse_vcard(raw)
    assert g["name"] == "Ann; Bob\nand co" and g["members"] == ["a", "b"] and raw.count("\r\nX-ADDRESSBOOKSERVER-MEMBER") == 2
    assert parse_vcard(_rewrite_group(raw, name="New", members=["c"]))["members"] == ["c"]
