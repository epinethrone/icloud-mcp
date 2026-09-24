"""mail_changes: only what changed since the last token, via CONDSTORE, never guessing across a renumbered folder."""
import contextlib
from email.message import EmailMessage

import pytest

from icloud_mcp.config import Settings
from icloud_mcp.mail import MailError, MailService


def raw(n):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = f"S{n} <s{n}@example.org>", "me@icloud.com", f"Message {n}", f"<m{n}@x>"
    m.set_content("x")
    return bytes(m)


class Server:
    def __init__(self):
        self.msgs = {u: {"modseq": 10 + u, "flags": set()} for u in range(1, 6)}      # uids 1..5
        self.uidvalidity, self.enabled = 4, []

    def enable(self, *caps):
        self.enabled += caps
        return []                                                                     # like iCloud: no ENABLED response

    def select_folder(self, name, readonly=False):
        return {b"UIDVALIDITY": self.uidvalidity, b"HIGHESTMODSEQ": max(m["modseq"] for m in self.msgs.values()),
                b"UIDNEXT": max(self.msgs) + 1, b"EXISTS": len(self.msgs)}

    def search(self, crit, charset=None):
        if crit[0] == "UID":
            lo = int(crit[1].split(":")[0])
            hits = [u for u in self.msgs if u >= lo]
            return hits or [max(self.msgs)]                                            # IMAP: n:* always includes the highest uid
        if crit[0] == "MODSEQ":
            return [u for u, m in self.msgs.items() if m["modseq"] >= int(crit[1])]
        if crit == ["UNSEEN"]:
            return [u for u, m in self.msgs.items() if "\\Seen" not in m["flags"]]
        raise AssertionError(crit)

    def fetch(self, uids, items):
        return {u: {b"FLAGS": tuple(f.encode() for f in self.msgs[u]["flags"]), b"RFC822.SIZE": 1, b"INTERNALDATE": None,
                    b"BODYSTRUCTURE": "", b"BODY[HEADER.FIELDS (X)]": raw(u).split(b"\n\n")[0] + b"\n\n"} for u in uids}

    # changes on the server between two checks
    def arrive(self):
        u = max(self.msgs) + 1
        self.msgs[u] = {"modseq": self.top() + 1, "flags": set()}

    def mark_read(self, u):
        self.msgs[u]["flags"].add("\\Seen")
        self.msgs[u]["modseq"] = self.top() + 1

    def top(self):
        return max(m["modseq"] for m in self.msgs.values())


@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    srv = Server()

    @contextlib.contextmanager
    def fake(self, fresh=False):
        yield srv
    monkeypatch.setattr(MailService, "imap", fake)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, n: n)
    return MailService(Settings.from_env()), srv


def test_first_call_gives_a_token_and_the_state(svc):
    m, srv = svc
    r = m.changes("INBOX")
    assert r["first_call"] and r["token"] == "v2:INBOX:4:15:6" and r["messages"] == 5 and r["unread"] == 5 and "CONDSTORE" in srv.enabled


def test_only_new_and_changed_messages_come_back(svc):
    m, srv = svc
    token = m.changes("INBOX")["token"]
    srv.arrive(); srv.arrive(); srv.mark_read(2)
    r = m.changes("INBOX", token)
    assert [x["uid"] for x in r["new"]] == [7, 6] and r["new"][0]["subject"] == "Message 7"
    assert [x["uid"] for x in r["changed"]] == [2] and r["changed"][0]["unread"] is False
    again = m.changes("INBOX", r["token"])
    assert again["new_count"] == 0 and again["changed_count"] == 0                   # nothing since: nothing listed


def test_the_uid_star_quirk_does_not_invent_a_new_message(svc):
    m, _ = svc
    token = m.changes("INBOX")["token"]
    assert m.changes("INBOX", token)["new"] == []


def test_a_renumbered_folder_says_start_over_and_bad_tokens_are_refused(svc):
    m, srv = svc
    token = m.changes("INBOX")["token"]
    srv.uidvalidity = 5
    r = m.changes("INBOX", token)
    assert r["start_over"] and r["token"].startswith("v2:INBOX:5:") and "new" not in r
    with pytest.raises(MailError, match="not one this tool returned"):
        m.changes("INBOX", "banana")


def test_long_lists_are_capped(svc):
    m, srv = svc
    token = m.changes("INBOX")["token"]
    for _ in range(5):
        srv.arrive()
    r = m.changes("INBOX", token, limit=2)
    assert r["new_count"] == 5 and len(r["new"]) == 2 and "newest 2" in r["note"]


def test_a_server_without_condstore_says_so(svc, monkeypatch):
    m, srv = svc
    monkeypatch.setattr(Server, "select_folder", lambda self, name, readonly=False: {b"UIDVALIDITY": 4, b"UIDNEXT": 6})
    with pytest.raises(MailError, match="does not report changes"):
        m.changes("INBOX")


def test_a_token_only_works_for_its_own_folder(svc):
    m, _ = svc
    token = m.changes("INBOX")["token"]
    with pytest.raises(MailError, match="belongs to the folder 'INBOX'"):
        m.changes("Archive", token)
    with pytest.raises(MailError, match="not one this tool returned"):
        m.changes("INBOX", token.replace("v2:", "v1:"))
