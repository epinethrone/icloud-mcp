"""The owner's dashboard: sessions, CSRF and origin checks, passkey sign-in, approvals, activity and security steps."""
import contextlib
import json
import re
import time

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
        assert r.status_code == 200 and "Sent to bob@example.org" in r.text and len(sent) == 1
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
        assert "Discarded" in r.text and sent == [] and mcp._icloud_dashboard.sources["mail"].pending() == []


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
