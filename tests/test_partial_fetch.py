"""Partial message fetches (mailparts): read attachments' headers but not their contents, fetch one attachment by itself, and
fall back to the whole message whenever the structure does not line up. A fake server answers BODYSTRUCTURE and section
requests from real messages, with the structure run through imapclient's own parser."""
import base64
import contextlib
import email
from email import policy
from email.message import EmailMessage

import pytest
from imapclient.response_parser import parse_fetch_response

from icloud_mcp import mailparts
from icloud_mcp.config import Settings
from icloud_mcp.mail import MailService, iter_attachment_parts, attachment_bytes


# ------------------------------------------------------------------------------------------------ a server that knows sections
def _q(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split(part):
    raw = part.as_bytes(policy=policy.compat32) if hasattr(part, "as_bytes") else bytes(part)
    head, _, body = raw.partition(b"\n\n") if b"\r\n\r\n" not in raw else raw.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", body


def structure(part):
    """BODYSTRUCTURE text for an email.message tree (compat32), as a server would send it."""
    maintype, subtype = part.get_content_maintype(), part.get_content_subtype()
    if part.is_multipart() and maintype == "multipart":
        return "(" + "".join(structure(p) for p in part.get_payload()) + f" {_q(subtype)} (\"boundary\" {_q(part.get_boundary())}) NIL NIL NIL)"
    _, body = _split(part)
    nl = body.count(b"\n")
    enc = (part.get("Content-Transfer-Encoding") or "7bit").lower()
    params = f"(\"charset\" {_q(part.get_content_charset())})" if part.get_content_charset() else "NIL"
    disp = part.get_content_disposition()
    fname = part.get_filename()
    ext = f"NIL ({_q(disp)} (\"filename\" {_q(fname)})) NIL NIL" if disp and fname else "NIL NIL NIL NIL"
    if maintype == "message" and subtype == "rfc822":
        inner = part.get_payload()[0]
        return (f"(\"message\" \"rfc822\" NIL NIL NIL {_q(enc)} {len(body)} (NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) "
                f"{structure(inner)} {nl} {ext})")
    lines = f" {nl}" if maintype == "text" else ""
    return f"({_q(maintype)} {_q(subtype)} {params} NIL NIL {_q(enc)} {len(body)}{lines} {ext})"


def sections(part, prefix=""):
    """section -> (mime header, raw body) for every part below the top."""
    out = {}
    if part.is_multipart() and part.get_content_maintype() == "multipart":
        for i, child in enumerate(part.get_payload(), 1):
            sec = f"{prefix}.{i}" if prefix else str(i)
            out[sec] = _split(child)
            out.update(sections(child, sec))
    return out


class SectionIMAP:
    """Answers FETCH for BODYSTRUCTURE, BODY.PEEK[...] (HEADER, n.MIME, n, whole) and flags, recording every request."""

    def __init__(self, store):
        self.store, self.fetches, self.sent = store, [], 0

    def select_folder(self, folder, readonly=False):
        return {b"UIDVALIDITY": 9}

    def search(self, crit, charset=None):
        return sorted(self.store)

    def fetch(self, uids, items):
        self.fetches.append((list(uids), list(items)))
        out = {}
        for u in uids:
            raw = self.store[u]
            msg = email.message_from_bytes(raw, policy=policy.compat32)
            secs = sections(msg)
            d = {b"FLAGS": (), b"INTERNALDATE": None, b"RFC822.SIZE": len(raw)}
            for it in items:
                if it == "BODYSTRUCTURE":
                    text = f"{u} (UID {u} BODYSTRUCTURE {structure(msg)})".encode()
                    d[b"BODYSTRUCTURE"] = parse_fetch_response([text], uid_is_key=True)[u][b"BODYSTRUCTURE"]
                elif it.startswith("BODY.PEEK["):
                    what = it[10:-1]
                    if what == "":
                        val = raw
                    elif what == "HEADER":
                        val = _split(msg)[0]
                    elif what.startswith("HEADER.FIELDS"):
                        val = _split(msg)[0]
                    elif what.endswith(".MIME"):
                        val = secs[what[:-5]][0]
                    else:
                        val = secs[what][1]
                    d[f"BODY[{what}]".encode()] = val
                    self.sent += len(val)
            out[u] = d
        return out


def rich_message():
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Anna <anna@example.org>", "me@icloud.com", "Q3", "<q3@example.org>"
    m["Date"] = "Mon, 05 Oct 2026 10:00:00 +0000"
    m.set_content("Plain body, see the report.")
    m.add_alternative("<html><body><p>HTML body, see the report.</p></body></html>", subtype="html")
    m.add_attachment(b"\x89PNG" + bytes(range(256)) * 40, maintype="image", subtype="png", filename="chart.png")
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 1200
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="report.pdf")
    inner = EmailMessage()
    inner["From"], inner["Subject"] = "Ben <ben@example.net>", "fwd"
    inner.set_content("forwarded text")
    m.add_attachment(inner)
    return bytes(m), pdf


@pytest.fixture
def svc(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    raw, pdf = rich_message()
    plain = EmailMessage()
    plain["From"], plain["To"], plain["Subject"] = "Eva <eva@example.net>", "me@icloud.com", "hi"
    plain.set_content("just text")
    fake = SectionIMAP({1: raw, 2: bytes(plain)})

    @contextlib.contextmanager
    def fake_imap(self, fresh=False):
        yield fake
    monkeypatch.setattr(MailService, "imap", fake_imap)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: name)
    return MailService(Settings.from_env()), fake, raw, pdf


def test_heavy_messages_come_without_attachment_contents_but_read_the_same(svc):
    s, fake, raw, pdf = svc
    whole = s._message_view("INBOX", 1, raw, (), None, body_chars=3000)
    got = s.get_messages("INBOX", [1, 2])
    lean = got["messages"][0]
    assert lean["text"] == whole["text"] and lean["subject"] == "Q3"
    assert [a["filename"] for a in lean["attachments"]] == [a["filename"] for a in whole["attachments"]]
    for a, b in zip(lean["attachments"], whole["attachments"]):
        assert a["content_type"] == b["content_type"] and abs(a["size"] - b["size"]) <= max(64, b["size"] // 50)
    assert got["messages"][1]["text"].strip() == "just text"
    assert fake.sent < len(raw) * 0.2                                             # the PDF and the image never crossed the wire


def test_after_a_search_the_structure_costs_nothing(svc):
    s, fake, raw, pdf = svc
    s.search("INBOX")
    fake.fetches.clear()
    s.get_messages("INBOX", [1, 2])
    assert not any(items == ["BODYSTRUCTURE"] for _, items in fake.fetches)
    assert len(fake.fetches) == 2                                                 # one skeleton FETCH, one whole FETCH


def test_one_attachment_is_fetched_by_itself_with_the_same_numbering(svc):
    s, fake, raw, pdf = svc
    full = iter_attachment_parts(email.message_from_bytes(raw, policy=policy.default))
    for index, part in enumerate(full):
        fake.fetches.clear()
        fake.sent = 0
        got = s.get_attachment("INBOX", 1, index)
        assert got["filename"] == (part.get_filename() or f"part-{index}") and got["content_type"] == part.get_content_type()
        data = base64.b64decode(got["content_base64"]) if "content_base64" in got else got["text"].encode()
        assert data == attachment_bytes(part) or got.get("text", "").strip() == attachment_bytes(part).decode().strip()
    fake.sent = 0
    got = s.get_attachment("INBOX", 1, 1)
    assert base64.b64decode(got["content_base64"]) == pdf and fake.sent < len(pdf) * 1.5       # only the PDF's own bytes
    with pytest.raises(Exception, match="out of range"):
        s.get_attachment("INBOX", 1, 9)


def test_planted_internal_headers_are_ignored(svc):
    s, fake, raw, pdf = svc
    evil = raw.replace(b"Content-Disposition: attachment; filename=\"report.pdf\"",
                       b"Content-Disposition: attachment; filename=\"report.pdf\"\r\nX-Icloud-Mcp-Size: 1\r\nX-Icloud-Mcp-Section: 1", 1)
    assert evil != raw
    fake.store[3] = evil
    atts = s.get_messages("INBOX", [3])["messages"][0]["attachments"]
    assert next(a for a in atts if a["filename"] == "report.pdf")["size"] > 100_000
    assert base64.b64decode(s.get_attachment("INBOX", 3, 1)["content_base64"]) == pdf


def test_anything_that_does_not_line_up_falls_back_to_the_whole_message(svc, monkeypatch):
    s, fake, raw, pdf = svc
    real = SectionIMAP.fetch

    def lossy(self, uids, items):                                                 # a server that drops one MIME header
        out = real(self, uids, items)
        for d in out.values():
            d.pop(b"BODY[2.MIME]", None)
        return out
    monkeypatch.setattr(SectionIMAP, "fetch", lossy)
    lean = s.get_messages("INBOX", [1])["messages"][0]
    assert [a["filename"] for a in lean["attachments"]][:2] == ["chart.png", "report.pdf"]
    assert any(items == ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"] for _, items in fake.fetches)
    assert base64.b64decode(s.get_attachment("INBOX", 1, 1)["content_base64"]) == pdf


def test_the_skeleton_matches_the_structure_exactly():
    raw, _ = rich_message()
    fake = SectionIMAP({1: raw})
    bs = fake.fetch([1], ["BODYSTRUCTURE"])[1][b"BODYSTRUCTURE"]
    root = mailparts.tree(bs)
    assert [n.section for n in root.leaves()] == ["1.1", "1.2", "2", "3", "4"]
    items = mailparts.skeleton_items(root, lambda n: n.ctype in mailparts.BODY_TEXT)
    data = fake.fetch([1], items)[1]
    skel = mailparts.assemble(root, data, lambda n: n.ctype in mailparts.BODY_TEXT)
    parsed = mailparts.parse(root, skel)
    full = email.message_from_bytes(raw, policy=policy.default)
    assert parsed is not None and [p.get_content_type() for p in iter_attachment_parts(parsed)] == \
        [p.get_content_type() for p in iter_attachment_parts(full)]
    assert mailparts.parse(mailparts.Node("", True, "multipart/mixed", children=root.children[:2]), skel) is None
