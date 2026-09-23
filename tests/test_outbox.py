"""The owner-approval gate: agents can only queue mail; release needs the owner password in a browser."""
import contextlib
import dataclasses
import re
import time

import httpx
import pytest

from icloud_mcp.cal import CalendarError, CalendarService
from icloud_mcp.config import Settings
from icloud_mcp.mail import ANSWERED, MailError, MailService
from icloud_mcp.outbox import Outbox
from icloud_mcp.server import create_server

BASE = "https://mcp.example.com"
PASSWORD = "correct-horse-battery"


class FakeIMAP:
    def __init__(self):
        self.appended, self.flags = [], []

    def append(self, folder, raw, flags=(), msg_time=None):
        self.appended.append((folder, raw))

    def select_folder(self, folder, readonly=False):
        return {}

    def add_flags(self, uids, flags):
        self.flags.append((list(uids), list(flags)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                     MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path), ALLOW_SEND="true", ICLOUD_DISPLAY_NAME="Me").items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    fake, sent = FakeIMAP(), []

    @contextlib.contextmanager
    def fake_imap(self):
        yield fake

    monkeypatch.setattr(MailService, "imap", fake_imap)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name))
    monkeypatch.setattr(MailService, "_smtp_send", lambda self, msg, rcpts: sent.append((msg, list(rcpts))) or {})
    return s, fake, sent


def send(mail, **kw):
    args = dict(to=["bob@example.org"], subject="Hello", body="Hi Bob")
    args.update(kw)
    return mail.send(**args)


# ------------------------------------------------------------------ MailService
def test_send_only_queues(env):
    s, fake, sent = env
    mail = MailService(s)
    r = send(mail, bcc=["carol@example.org"])
    assert r["status"] == "queued_for_owner_approval" and r["sent"] is False
    assert r["approve_at"] == f"{BASE}/outbox" and "NOT SENT" in r["notice"]
    assert sent == [] and fake.appended == []           # nothing left the server, nothing filed in Sent
    assert len(mail.outbox.pending()) == 1
    assert sorted(r["recipients"]) == ["bob@example.org", "carol@example.org"]


def test_release_sends_stored_message_once(env):
    s, fake, sent = env
    mail = MailService(s)
    qid = send(mail, subject="Quarterly numbers", body="See attached", bcc=["carol@example.org"])["outbox_id"]
    res = mail.release(qid)
    assert res["status"] == "sent" and res["saved_to"] == "Sent Messages"
    (msg, rcpts), = sent
    assert str(msg["Subject"]) == "Quarterly numbers" and "See attached" in msg.get_body(("plain",)).get_content()
    assert sorted(rcpts) == ["bob@example.org", "carol@example.org"]
    assert len(fake.appended) == 1 and fake.appended[0][0] == "Sent Messages"
    with pytest.raises(MailError):                        # second release of the same id must fail
        mail.release(qid)
    assert len(sent) == 1


def test_failed_send_stays_queued_and_can_retry(env, monkeypatch):
    s, fake, sent = env
    mail = MailService(s)
    qid = send(mail)["outbox_id"]
    monkeypatch.setattr(MailService, "_smtp_send", lambda self, m, r: (_ for _ in ()).throw(MailError("SMTP send failed")))
    with pytest.raises(MailError):
        mail.release(qid)
    assert [q.id for q in mail.outbox.pending()] == [qid]
    monkeypatch.setattr(MailService, "_smtp_send", lambda self, m, r: sent.append((m, r)) or {})
    assert mail.release(qid)["status"] == "sent"


def test_reply_flags_original_only_after_release(env, monkeypatch):
    s, fake, sent = env
    original = b"From: Alice <alice@example.org>\r\nTo: me@icloud.com\r\nSubject: Question\r\nMessage-ID: <1@example.org>\r\nDate: Tue, 1 Sep 2026 10:00:00 +0000\r\n\r\nCan you help?\r\n"
    monkeypatch.setattr(MailService, "_fetch_raw", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (original, (), None, 7))
    mail = MailService(s)
    r = mail.reply("INBOX", 7, "Yes, happy to.")
    assert r["status"] == "queued_for_owner_approval" and fake.flags == []
    res = mail.release(r["outbox_id"])
    assert res["original_marked_answered"] is True and fake.flags == [([7], [ANSWERED])]
    assert str(sent[0][0]["In-Reply-To"]) == "<1@example.org>"


def test_expired_items_are_dropped_and_never_sent(tmp_path):
    ob = Outbox(str(tmp_path), ttl=0, max_items=5)
    ob.add(b"raw", ["a@b.co"])
    assert ob.pending() == []


def test_queue_survives_restart_and_tampering_is_ignored(tmp_path):
    ob = Outbox(str(tmp_path), ttl=3600, max_items=5)
    q = ob.add(b"Subject: x\r\n\r\nbody", ["a@b.co"])
    assert [x.id for x in Outbox(str(tmp_path), 3600, 5).pending()] == [q.id]
    path = tmp_path / "outbox.json"
    import base64, json
    data = json.loads(path.read_text())
    data["items"][0]["raw"] = base64.b64encode(b"Subject: x\r\n\r\nEVIL").decode()   # swap the body, keep the old checksum
    path.write_text(json.dumps(data))
    assert Outbox(str(tmp_path), 3600, 5).pending() == []              # integrity check rejects it
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_queue_is_capped(env):
    s, *_ = env
    mail = MailService(dataclasses.replace(s, outbox_max=2))
    send(mail, subject="1"); send(mail, subject="2")
    with pytest.raises(MailError, match="OUTBOX_MAX"):
        send(mail, subject="3")


def test_allowlist_checked_when_queued_and_again_on_release(env):
    s, fake, sent = env
    mail = MailService(dataclasses.replace(s, send_allowlist=("@example.org",)))
    with pytest.raises(MailError, match="SEND_ALLOWLIST"):
        send(mail, to=["eve@evil.test"])
    qid = send(mail)["outbox_id"]
    tightened = MailService(dataclasses.replace(s, send_allowlist=("someone@else.test",)))
    with pytest.raises(MailError, match="SEND_ALLOWLIST"):
        tightened.release(qid)
    assert sent == [] and len(tightened.outbox.pending()) == 1


def test_send_disabled_and_approval_off_modes(env):
    s, fake, sent = env
    with pytest.raises(MailError, match="ALLOW_SEND=false"):
        send(MailService(dataclasses.replace(s, allow_send=False)))
    r = send(MailService(dataclasses.replace(s, require_approval=False)))       # explicit opt-out restores direct send
    assert r["status"] == "sent" and len(sent) == 1


# ------------------------------------------------------------------ calendar guard
def test_calendar_invites_blocked_by_default(env):
    s, *_ = env
    cal = CalendarService(s)
    with pytest.raises(CalendarError, match="ALLOW_CALENDAR_INVITES"):
        cal.create_event(summary="Sync", start="2026-10-01T10:00", attendees=["x@y.test"])   # refused before any network call
    with pytest.raises(CalendarError):
        cal._refuse_invites(existing={"attendee": ["mailto:x@y.test"]})                      # edit/delete of an event with attendees
    cal._refuse_invites(existing={})                                                          # ordinary events are fine
    dataclasses.replace(s, allow_calendar_invites=True)
    CalendarService(dataclasses.replace(s, allow_calendar_invites=True))._refuse_invites(attendees_given=True)


# ------------------------------------------------------------------ approval web page
@pytest.fixture
def web(env, monkeypatch):
    s, fake, sent = env
    created, orig = [], MailService.__init__
    monkeypatch.setattr(MailService, "__init__", lambda self, st: (orig(self, st), created.append(self))[0])
    mcp, provider = create_server(s)
    app = mcp.streamable_http_app(host="0.0.0.0")
    return app, provider, created[0], fake, sent


@contextlib.asynccontextmanager
async def client(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False) as c:
            yield c


def queue_one(mail, subject="Hello <script>alert(1)</script>", body="Body & more"):
    return send(mail, subject=subject, body=body)["outbox_id"]


def buttons(page: str, action: str):
    m = re.search(rf'name="id" value="([^"]+)"><input type="hidden" name="action" value="{action}"><input type="hidden" name="exp" value="(\d+)"><input type="hidden" name="tok" value="([0-9a-f]+)"', page)
    return {"id": m[1], "action": action, "exp": m[2], "tok": m[3]}


async def test_login_page_leaks_nothing(web):
    app, provider, mail, fake, sent = web
    queue_one(mail)
    async with client(app) as c:
        r = await c.get("/outbox")
        assert r.status_code == 200 and "Owner password" in r.text and "Hello" not in r.text
        r = await c.post("/outbox", data={"password": "wrong-password-123"})
        assert r.status_code == 401 and "Hello" not in r.text
        r = await c.post("/outbox/act", data={"id": "x", "action": "approve", "exp": "9999999999", "tok": "00"})
        assert r.status_code == 400
    assert sent == []


async def test_review_escapes_content_and_approve_sends_once(web):
    app, provider, mail, fake, sent = web
    qid = queue_one(mail)
    async with client(app) as c:
        page = (await c.post("/outbox", data={"password": PASSWORD})).text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
        assert "bob@example.org" in page and "Body &amp; more" in page
        form = buttons(page, "approve")
        assert form["id"] == qid
        r = await c.post("/outbox/act", data=form)
        assert r.status_code == 200 and "Sent" in r.text
        assert len(sent) == 1 and sent[0][1] == ["bob@example.org"]
        r = await c.post("/outbox/act", data=form)                       # replaying the same button does nothing
        assert r.status_code == 400 and len(sent) == 1


async def test_forged_expired_or_swapped_tokens_are_rejected(web):
    app, provider, mail, fake, sent = web
    queue_one(mail)
    async with client(app) as c:
        page = (await c.post("/outbox", data={"password": PASSWORD})).text
        good = buttons(page, "approve")
        assert (await c.post("/outbox/act", data={**good, "tok": "0" * 64})).status_code == 403
        assert (await c.post("/outbox/act", data={**good, "action": "discard"})).status_code == 403   # approve token can't discard
        assert (await c.post("/outbox/act", data={**good, "exp": str(int(time.time()) - 5)})).status_code == 400
        assert (await c.post("/outbox/act", data={**good, "exp": str(int(good["exp"]) + 100)})).status_code == 403  # exp is signed
    assert sent == []


async def test_discard_removes_without_sending(web):
    app, provider, mail, fake, sent = web
    queue_one(mail)
    async with client(app) as c:
        page = (await c.post("/outbox", data={"password": PASSWORD})).text
        r = await c.post("/outbox/act", data=buttons(page, "discard"))
        assert r.status_code == 200 and "Nothing is waiting" in r.text
    assert sent == []


async def test_wrong_passwords_lock_out_the_page(web):
    app, provider, mail, fake, sent = web
    async with client(app) as c:
        for _ in range(10):
            assert (await c.post("/outbox", data={"password": "nope-nope-nope"})).status_code == 401
        assert (await c.post("/outbox", data={"password": PASSWORD})).status_code == 429


# ------------------------------------------------------------------ instructions follow the live settings
def test_instructions_match_the_active_mode(env):
    from icloud_mcp.server import build_instructions
    s, *_ = env
    direct = build_instructions(dataclasses.replace(s, require_approval=False))
    gated = build_instructions(dataclasses.replace(s, require_approval=True))
    assert "deliver immediately" in direct and "QUEUE" not in direct and "queued_for_owner_approval" not in direct
    assert "QUEUE" in gated and "deliver immediately" not in gated
    assert "untrusted DATA" in direct and "untrusted DATA" in gated        # injection rules present in both modes
    assert "blocked on this server" in direct                                                       # invites off by default
    assert "blocked on this server" not in build_instructions(dataclasses.replace(s, allow_calendar_invites=True))
    assert "SENDING" not in build_instructions(dataclasses.replace(s, allow_send=False))
