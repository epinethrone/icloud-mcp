"""The owner's dashboard: sessions, CSRF and origin checks, passkey sign-in, approvals, activity and security steps."""
import contextlib
import json
import re

import httpx
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.mail import MailService
from icloud_mcp.server import build_app, create_server

from softauthn import SoftAuthenticator
from test_outbox import FakeIMAP

BASE = "https://mcp.example.com"
PASSWORD = "correct-horse-battery"
SAME = {"origin": BASE}


@pytest.fixture
def web(tmp_path, monkeypatch):
    def make(**extra):
        env = dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                   MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path), ALLOW_SEND="true", WARMUP_ON_START="false",
                   DEFAULT_TIMEZONE="Europe/Berlin", **extra)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        s = Settings.from_env()
        fake, sent = FakeIMAP(), []

        @contextlib.contextmanager
        def fake_imap(self):
            yield fake

        monkeypatch.setattr(MailService, "imap", fake_imap)
        monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name))
        monkeypatch.setattr(MailService, "_smtp_send", lambda self, msg, rcpts: sent.append((msg, list(rcpts))) or {})
        mcp, provider = create_server(s)
        return build_app(s, mcp), mcp, provider, sent
    return make


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False)


def csrf(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page)[1]


async def signed_in(c):
    r = await c.post("/dashboard/signin", data={"password": PASSWORD, "next": "/dashboard"}, headers=SAME)
    assert r.status_code == 303, r.text
    page = (await c.get("/dashboard")).text
    return csrf(page)


def queue(mcp, subject="Lunch <b>now</b>", body="See you & bye"):
    mail = mcp._icloud_dashboard.sources["mail"].mail
    return mail.send(to=["bob@example.org"], subject=subject, body=body)["outbox_id"]


async def test_everything_needs_a_session(web):
    app, *_ = web()
    async with client(app) as c:
        for path in ("/dashboard", "/dashboard/approvals", "/dashboard/activity", "/dashboard/security", "/dashboard/activity.json"):
            r = await c.get(path)
            assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/signin")
        r = await c.get("/dashboard/signin")
        assert r.status_code == 200 and "Owner password" in r.text and 'data-passkey="signin"' not in r.text   # no passkey yet
        assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"     # with no-referrer, browsers post forms with Origin: null
        r = await c.post("/dashboard/approvals/act", data={"kind": "mail", "id": "x", "action": "approve"}, headers=SAME)
        assert r.status_code == 303


async def test_password_sign_in_sets_a_strict_cookie_and_wrong_passwords_lock_out(web):
    app, mcp, provider, _ = web()
    async with client(app) as c:
        r = await c.post("/dashboard/signin", data={"password": "wrong-password-1"}, headers=SAME)
        assert r.status_code == 401
        r = await c.post("/dashboard/signin", data={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 303
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-icloud-dashboard=") and "HttpOnly" in cookie and "Secure" in cookie
        assert "samesite=strict" in cookie.lower() and "Path=/" in cookie
        assert "Overview" in (await c.get("/dashboard")).text
    stored = json.loads((mcp._icloud_dashboard.sessions.path).read_text())
    token = cookie.split(";")[0].split("=", 1)[1]
    assert token not in json.dumps(stored)                      # only the hash is on disk
    for _ in range(9):
        provider.record_failure()
    async with client(app) as c:
        r = await c.post("/dashboard/signin", data={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 429
    log = mcp._icloud_dashboard.audit.entries()
    assert [e["ok"] for e in log if e["action"] == "signin.password"] == [True, False]


async def test_other_origins_and_missing_csrf_are_refused(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        r = await c.post("/dashboard/signin", data={"password": PASSWORD}, headers={"origin": "https://evil.example.net"})
        assert r.status_code == 403
        token = await signed_in(c)
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        form = {"kind": "mail", "id": qid, "sha": sha, "action": "approve"}
        assert (await c.post("/dashboard/approvals/act", data=form, headers=SAME)).status_code == 403              # no csrf
        assert (await c.post("/dashboard/approvals/act", data={**form, "csrf": token},
                             headers={"origin": "https://evil.example.net"})).status_code == 403
        assert sent == []


async def test_approve_sends_once_and_is_logged(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/approvals")).text
        assert "Lunch &lt;b&gt;now&lt;/b&gt;" in page and "<b>now</b>" not in page and "See you &amp; bye" in page
        sha = re.search(r'name="sha" value="([0-9a-f]+)"', page)[1]
        form = {"csrf": token, "kind": "mail", "id": qid, "sha": sha, "action": "approve"}
        r = await c.post("/dashboard/approvals/act", data=form, headers=SAME)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard/approvals" and len(sent) == 1   # reloading never re-posts
        assert "Sent to bob@example.org" in (await c.get("/dashboard/approvals")).text
        assert "Sent to bob@example.org" not in (await c.get("/dashboard/approvals")).text                     # shown once
        r = await c.post("/dashboard/approvals/act", data=form, headers=SAME)              # a replay finds nothing waiting
        assert r.status_code == 409 and len(sent) == 1
        page = (await c.get("/dashboard/activity")).text
        assert "<b>You</b> approved email “Lunch &lt;b&gt;now&lt;/b&gt;”" in page and "none changed since they were written" in page


async def test_a_changed_message_is_not_sent_and_discard_sends_nothing(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        form = {"csrf": token, "kind": "mail", "id": qid, "sha": "0" * 64, "action": "approve"}
        assert (await c.post("/dashboard/approvals/act", data=form, headers=SAME)).status_code == 409
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        r = await c.post("/dashboard/approvals/act", data={**form, "sha": sha, "action": "discard"}, headers=SAME)
        assert r.status_code == 303 and "Discarded" in (await c.get("/dashboard/approvals")).text and sent == [] and mcp._icloud_dashboard.sources["mail"].pending() == []


async def test_sessions_survive_a_restart_and_sign_out_ends_them(web):
    app, *_ = web()
    async with client(app) as c:
        token = await signed_in(c)
        cookies = dict(c.cookies)
    app2, *_ = web()                                            # a new process reading the same data folder
    async with client(app2) as c:
        c.cookies.update(cookies)
        assert (await c.get("/dashboard")).status_code == 200
        r = await c.post("/dashboard/logout", data={"csrf": token}, headers=SAME)
        assert r.status_code == 303
        c.cookies.update(cookies)
        assert (await c.get("/dashboard")).status_code == 303


async def test_sensitive_steps_ask_again_after_five_minutes(web, monkeypatch):
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        d = mcp._icloud_dashboard
        for rec in d.sessions._items.values():
            rec["auth_at"] -= 600
        r = await c.post("/dashboard/security/password", data={"csrf": token, "password": "a-new-owner-password"}, headers=SAME)
        assert "Confirm it’s you" in r.text
        r = await c.post("/dashboard/signin", data={"csrf": token, "password": PASSWORD, "next": "/dashboard/security"}, headers=SAME)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard/security"
        r = await c.post("/dashboard/security/password", data={"csrf": token, "password": "short"}, headers=SAME)
        assert r.status_code == 400 and "at least 12" in r.text
        r = await c.post("/dashboard/security/password", data={"csrf": token, "password": "a-new-owner-password"}, headers=SAME)
        assert "applies after the server restarts" in r.text
    assert json.loads((mcp._icloud_dashboard.sessions.path.parent / "overrides.json").read_text())["owner_password"] == "a-new-owner-password"


async def test_passkey_registration_and_sign_in_through_the_pages(web):
    pytest.importorskip("webauthn")
    app, mcp, _, _ = web()
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post("/dashboard/passkey/options", json={"purpose": "register"}, headers={**SAME, "x-csrf": token})
        opts = r.json()
        r = await c.post("/dashboard/passkey/verify", headers={**SAME, "x-csrf": token},
                         json={"purpose": "register", "ticket": opts["ticket"], "credential": dev.create(opts["publicKey"]), "name": "Desk key"})
        assert r.status_code == 200, r.text
        assert "Desk key" in (await c.get("/dashboard/security")).text
    async with client(app) as c:                                # a fresh browser
        page = (await c.get("/dashboard/signin")).text
        assert 'data-passkey="signin"' in page
        assert (await c.post("/dashboard/passkey/options", json={"purpose": "signin"},
                             headers={"origin": "https://evil.example.net"})).status_code == 403
        opts = (await c.post("/dashboard/passkey/options", json={"purpose": "signin"}, headers=SAME)).json()
        r = await c.post("/dashboard/passkey/verify", headers=SAME,
                         json={"purpose": "signin", "ticket": opts["ticket"], "credential": dev.get(opts["publicKey"]), "next": "/dashboard/approvals"})
        assert r.status_code == 200 and r.json()["next"] == "/dashboard/approvals" and "__Host-icloud-dashboard" in r.headers["set-cookie"]
        assert (await c.get("/dashboard/approvals")).status_code == 200
    via = [e.get("via") for e in mcp._icloud_dashboard.audit.entries() if e["action"] == "signin.passkey"]
    assert via == ["passkey “Desk key”"]


async def test_a_password_lockout_does_not_lock_out_the_passkey(web):
    pytest.importorskip("webauthn")
    app, mcp, provider, _ = web()
    store = mcp._icloud_dashboard.passkeys
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.registration_options()
    store.register(opts["ticket"], dev.create(opts["publicKey"]), "Phone")
    for _ in range(10):
        provider.record_failure()
    async with client(app) as c:
        r = await c.post("/dashboard/signin", data={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 429 and "or use your passkey" in r.text
        opts = (await c.post("/dashboard/passkey/options", json={"purpose": "signin"}, headers=SAME)).json()
        r = await c.post("/dashboard/passkey/verify", headers=SAME,
                         json={"purpose": "signin", "ticket": opts["ticket"], "credential": dev.get(opts["publicKey"])})
        assert r.status_code == 200 and (await c.get("/dashboard")).status_code == 200


async def test_password_sign_in_can_be_turned_off_once_a_passkey_exists(web):
    pytest.importorskip("webauthn")
    app, mcp, _, _ = web(OWNER_PASSWORD_LOGIN="false")
    async with client(app) as c:                                # no passkey yet: the password still works
        await signed_in(c)
    store = mcp._icloud_dashboard.passkeys
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.registration_options()
    store.register(opts["ticket"], dev.create(opts["publicKey"]), "Phone")
    async with client(app) as c:
        page = (await c.get("/dashboard/signin")).text
        assert "Owner password" not in page
        r = await c.post("/dashboard/signin", data={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 403


async def test_the_script_is_served_from_here(web):
    app, *_ = web()
    async with client(app) as c:
        r = await c.get("/dashboard/passkey.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
        assert "navigator.credentials" in r.text


async def test_dashboard_can_be_turned_off(web):
    app, *_ = web(DASHBOARD="false")
    async with client(app) as c:
        assert (await c.get("/dashboard")).status_code == 404
        assert (await c.get("/outbox")).status_code == 200          # the password page stays


async def test_a_failed_calendar_release_points_the_owner_to_the_overview(web, monkeypatch):
    from icloud_mcp.cal import CalendarError, CalendarService

    def fail(self, q):
        raise CalendarError("CalDAV authentication failed. Check ICLOUD_USERNAME and the app-specific password. "
                            "Run icloud_check_health to see which service is failing.")
    monkeypatch.setattr(CalendarService, "release", fail)
    app, mcp, _, _ = web(ALLOW_CALENDAR_INVITES="approve")
    src = mcp._icloud_dashboard.sources["calendar"]
    src.cal.outbox.add(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", ["jana@example.org"],
                       {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1",
                        "summary": "Autumn fair", "start": "2026-10-22T18:00:00+02:00", "end": None, "location": "",
                        "description": "", "organizer": None, "href": None, "etag": None})
    async with client(app) as c:
        token = await signed_in(c)
        q = src.pending()[0]
        r = await c.post("/dashboard/approvals/act", headers=SAME,
                         data={"csrf": token, "kind": "calendar", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert "Check now on the Overview shows which service is failing." in r.text and "icloud_check_health" not in r.text
        assert len(src.pending()) == 1                                              # still waiting, can be retried


async def test_a_failed_release_points_the_owner_to_the_overview(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals

    def fail(self, q):
        raise RuntimeError("SMTP authentication failed. Check ICLOUD_USERNAME and the app-specific password. "
                           "Run icloud_check_health to see which service is failing.")
    monkeypatch.setattr(MailApprovals, "release", fail)
    app, mcp, _, sent = web()
    queue(mcp)
    src = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        token = await signed_in(c)
        q = src.pending()[0]
        r = await c.post("/dashboard/approvals/act", headers=SAME,
                         data={"csrf": token, "kind": "mail", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert "Check now on the Overview shows which service is failing." in r.text and "icloud_check_health" not in r.text
        assert len(src.pending()) == 1 and sent == []                               # still waiting, can be retried


async def test_junk_passkey_attempts_neither_lock_the_password_nor_fill_the_log(web):
    pytest.importorskip("webauthn")
    app, mcp, provider, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        for _ in range(30):
            r = await c.post("/dashboard/passkey/verify", headers=SAME, json={"purpose": "signin", "ticket": "junk", "credential": {}})
            assert r.status_code == 401
        assert provider.login_allowed()                                              # the password still works
        assert (await c.post("/dashboard/signin", data={"password": PASSWORD}, headers=SAME)).status_code == 303
    assert len([e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey" and not e["ok"]]) == 5


async def test_confirming_issues_a_new_session_token_and_needs_the_csrf_token(web, monkeypatch):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        old = c.cookies.get(d.cookie)
        r = await c.post("/dashboard/signin", headers=SAME, data={"password": PASSWORD, "next": "/dashboard"})
        assert r.status_code == 403 and "This form has expired" in r.text                # a confirm without the form's CSRF token
        r = await c.post("/dashboard/signin", headers=SAME, data={"csrf": token, "password": PASSWORD, "next": "/dashboard"})
        assert r.status_code == 303 and d.cookie in r.headers["set-cookie"]
        new = c.cookies.get(d.cookie)
        assert new and new != old and d.sessions.get(old) is None and d.sessions.get(new) is not None


def test_event_times_read_like_a_calendar():
    from icloud_mcp.dashboard import event_when
    assert event_when("2026-11-20T18:00:00+00:00", "2026-11-20T20:00:00+00:00", "Europe/Berlin") == "Fri 20 Nov 2026, 19:00–21:00"
    assert event_when("2026-11-20T23:00:00+01:00", "2026-11-21T01:00:00+01:00", "Europe/Berlin") == \
        "Fri 20 Nov 2026, 23:00 – Sat 21 Nov 2026, 01:00"
    assert event_when("2026-11-20", "2026-11-21", "Europe/Berlin") == "Fri 20 Nov 2026"                 # one all-day day
    assert event_when("2026-11-20", "2026-11-23", "Europe/Berlin") == "Fri 20 Nov 2026 – Sun 22 Nov 2026"
    assert event_when("2026-11-20T18:00:00", None, "Europe/Berlin") == "Fri 20 Nov 2026, 18:00"          # floating time stays as is
    assert event_when("soon", None, "Europe/Berlin") == "soon"


async def test_held_calendar_changes_show_up_and_are_released(web, monkeypatch):
    from icloud_mcp.cal import CalendarService
    released = []
    monkeypatch.setattr(CalendarService, "release", lambda self, q: released.append(q.followup["op"]) or "Added “Autumn fair”.")
    app, mcp, _, _ = web(ALLOW_CALENDAR_INVITES="approve")
    src = mcp._icloud_dashboard.sources["calendar"]
    src.cal.outbox.add(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", ["jana@example.org", "tom@example.net"],
                       {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1",
                        "summary": "Autumn fair", "start": "2026-10-22T18:00:00+02:00", "end": "2026-10-22T19:30:00+02:00",
                        "location": "", "description": "Bring ideas", "organizer": None, "href": None, "etag": None})
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/approvals")).text
        assert "Autumn fair" in page and "Invitation" in page and "jana@example.org, tom@example.net" in page
        assert "Add and invite" in page and "Nothing is in your calendar yet." in page
        assert "Thu 22 Oct 2026, 18:00–19:30" in page and "T18:00" not in page          # the owner's clock, not ISO
        q = src.pending()[0]
        await c.post("/dashboard/approvals/act", headers=SAME,
                     data={"csrf": token, "kind": "calendar", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert "Added “Autumn fair”." in (await c.get("/dashboard/approvals")).text and released == ["create"] and src.pending() == []
