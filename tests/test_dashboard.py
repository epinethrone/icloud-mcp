"""The owner's dashboard: sessions, CSRF and origin checks, passkey sign-in, approvals, activity and security steps."""
import base64
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
        overview = (await c.get("/dashboard")).text
        assert "Autumn fair" in overview and ">Add and invite</button>" in overview        # the same words where it is approved in its row
        q = src.pending()[0]
        await c.post("/dashboard/approvals/act", headers=SAME,
                     data={"csrf": token, "kind": "calendar", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert "Added “Autumn fair”." in (await c.get("/dashboard/approvals")).text and released == ["create"] and src.pending() == []


async def test_the_app_files_are_served_to_anyone_and_the_pages_link_them(web):
    app, *_ = web()
    async with client(app) as c:
        for path, ctype in (("/dashboard/dashboard.css", "text/css"), ("/dashboard/dashboard.js", "text/javascript"),
                            ("/dashboard/install-hint.js", "text/javascript"), ("/dashboard/sw.js", "text/javascript"),
                            ("/dashboard/manifest.webmanifest", "application/manifest+json"), ("/icon-192.png", "image/png"),
                            ("/icon-512.png", "image/png")):
            r = await c.get(path)                                     # browsers fetch the manifest and the worker without the cookie
            assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
        worker = (await c.get("/dashboard/sw.js")).headers
        assert worker["cache-control"] == "no-cache" and worker["service-worker-allowed"] == "/dashboard"
        assert "caches." not in (await c.get("/dashboard/sw.js")).text                         # the worker stores nothing
        manifest = (await c.get("/dashboard/manifest.webmanifest")).json()
        assert manifest["start_url"] == manifest["scope"] == "/dashboard" and manifest["display"] == "standalone"
        assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
        await signed_in(c)
        r = await c.get("/dashboard")
        csp = r.headers["content-security-policy"]
        assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp and "style-src 'self'" in csp
        assert '<link rel="manifest" href="/dashboard/manifest.webmanifest">' in r.text and "<style>" not in r.text
        assert re.search(r'href="/dashboard/dashboard.css\?v=[0-9a-f]{10}"', r.text) and 'src="/dashboard/dashboard.js?v=' in r.text


async def test_the_home_screen_icon_rides_in_the_page_so_a_sign_in_proxy_cannot_swallow_it(web):
    """iOS asks for the icon without the session cookie, and a proxy in front answers that with its own login page."""
    app, *_ = web()
    async with client(app) as c:
        assert (await c.get("/apple-touch-icon.png")).status_code == 200        # the route stays for everything else
        await signed_in(c)
        r = await c.get("/dashboard")
        embedded = re.search(r'<link rel="apple-touch-icon" href="data:image/png;base64,([A-Za-z0-9+/=]+)">', r.text)
        assert embedded and base64.b64decode(embedded.group(1)).startswith(b"\x89PNG")
        assert "/apple-touch-icon.png" not in r.text                            # nothing left for a proxy to intercept
        assert "img-src 'self' data:" in r.headers["content-security-policy"]   # and the page is allowed to show it


async def test_the_live_state_changes_when_a_request_arrives_and_does_not_keep_the_session_alive(web):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        assert (await c.get("/dashboard/state.json")).status_code == 401
        await signed_in(c)
        first = (await c.get("/dashboard/state.json", headers={"x-live": "1"})).json()
        assert first["waiting"] == 0
        queue(mcp)
        second = (await c.get("/dashboard/state.json", headers={"x-live": "1"})).json()
        assert second["waiting"] == 1 and second["v"] != first["v"]
        assert "(1) Overview · iCloud connector" in (await c.get("/dashboard")).text
        rec = next(iter(d.sessions._items.values()))
        rec["last"] -= 120
        stale = rec["last"]
        await c.get("/dashboard/state.json", headers={"x-live": "1"})
        await c.get("/dashboard", headers={"x-live": "1"})            # the page's own background refresh
        assert rec["last"] == stale                                     # a page left open still signs out when idle
        await c.get("/dashboard")
        assert rec["last"] > stale


async def test_a_request_is_approved_on_the_overview_and_the_result_shows_there(web):
    app, mcp, _, sent = web()
    qid = queue(mcp, subject="Kitchen quote", body="Thursday works for us.")
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard")).text
        assert "Open Approvals (" not in page                                          # no second way to the same list
        assert f'data-key="mail:{qid}"' in page and "<details>" in page and "Thursday works for us." in page
        assert 'name="back" value="/dashboard"' in page
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        form = {"csrf": token, "kind": "mail", "id": qid, "sha": sha, "action": "approve", "back": "/dashboard"}
        r = await c.post("/dashboard/approvals/act", data=form, headers=SAME)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard" and len(sent) == 1
        assert "Sent to bob@example.org" in (await c.get("/dashboard")).text


async def test_a_request_with_a_warning_is_not_approved_from_the_overview(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals
    real = MailApprovals.describe
    monkeypatch.setattr(MailApprovals, "describe", lambda self, q, ctx=None: {**real(self, q, ctx), "warnings": ["Check the address"]})
    app, mcp, _, _ = web()
    qid = queue(mcp)
    async with client(app) as c:
        await signed_in(c)
        page = (await c.get("/dashboard")).text
        assert f'href="/dashboard/approvals?item=mail:{qid}">Review</a>' in page and 'value="approve"' not in page


async def test_the_page_script_gets_json_answers(web):
    app, mcp, _, sent = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        form = {"csrf": token, "kind": "mail", "id": qid, "sha": sha, "action": "approve"}
        js = {**SAME, "accept": "application/json"}
        r = await c.post("/dashboard/approvals/act", data=form, headers=js)
        assert r.status_code == 200 and r.json() == {"ok": True, "note": "Sent to bob@example.org."} and len(sent) == 1
        r = await c.post("/dashboard/approvals/act", data=form, headers=js)
        assert r.status_code == 409 and r.json()["ok"] is False and len(sent) == 1
        qid = queue(mcp)
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        r = await c.post("/dashboard/approvals/act", data={**form, "id": qid, "sha": sha, "action": "discard"}, headers=js)
        assert r.json() == {"ok": True, "note": "Discarded. Nothing was sent."} and len(sent) == 1


async def test_after_an_action_only_the_overview_or_approvals_is_a_destination(web):
    app, mcp, _, sent = web()
    async with client(app) as c:
        token = await signed_in(c)
        for back in ("https://evil.example.net/", "//evil.example.net", "/dashboard/security"):
            qid = queue(mcp)
            sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
            r = await c.post("/dashboard/approvals/act", headers=SAME,
                             data={"csrf": token, "kind": "mail", "id": qid, "sha": sha, "action": "discard", "back": back})
            assert r.status_code == 303 and r.headers["location"] == "/dashboard/approvals", back
        assert sent == []


async def test_the_page_script_gets_json_when_nothing_was_sent(web, monkeypatch):
    from icloud_mcp.dashboard import MailApprovals
    real = MailApprovals.describe
    app, mcp, _, sent = web()
    qid = queue(mcp)
    js = {**SAME, "accept": "application/json"}
    async with client(app) as c:
        token = await signed_in(c)
        src = mcp._icloud_dashboard.sources["mail"]
        form = {"csrf": token, "kind": "mail", "id": qid, "sha": src.pending()[0].sha256, "action": "approve"}
        monkeypatch.setattr(MailApprovals, "describe", lambda self, q, ctx=None: {**real(self, q, ctx), "needs_override": True})
        r = await c.post("/dashboard/approvals/act", data=form, headers=js)
        assert r.status_code == 422 and r.json()["ok"] is False and sent == [] and len(src.pending()) == 1
        monkeypatch.setattr(MailApprovals, "describe", real)


        def fail(self, q):
            raise RuntimeError("SMTP said no")
        monkeypatch.setattr(MailApprovals, "release", fail)
        r = await c.post("/dashboard/approvals/act", data=form, headers=js)
        assert r.status_code == 502 and r.json()["error"].startswith("Not sent (still waiting)") and sent == []


async def test_live_pages_name_their_own_address_and_a_background_refresh_takes_no_result(web):
    app, mcp, _, _ = web()
    qid = queue(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get(f"/dashboard/approvals?item=mail:{qid}")).text
        assert f'data-live="/dashboard/approvals?item=mail%3A{qid}"' in page and re.search(r'data-v="[0-9a-f]{16}"', page)
        assert f'<form class="card" data-key="mail:{qid}" data-title="Lunch &lt;b&gt;now&lt;/b&gt;"' in page
        assert 'data-live=' not in (await c.get("/dashboard/security")).text        # counts only: never re-requested
        sha = mcp._icloud_dashboard.sources["mail"].pending()[0].sha256
        r = await c.post("/dashboard/approvals/act", headers=SAME,
                         data={"csrf": token, "kind": "mail", "id": qid, "sha": "0" * 64, "action": "approve"})
        assert r.status_code == 409 and f'data-live="/dashboard/approvals?item=mail%3A{qid}"' in r.text  # a GET address, pinned to what it shows
        await c.post("/dashboard/approvals/act", headers=SAME,
                     data={"csrf": token, "kind": "mail", "id": qid, "sha": sha, "action": "discard", "back": "/dashboard"})
        assert "Discarded" not in (await c.get("/dashboard", headers={"x-live": "1"})).text
        assert "Discarded" in (await c.get("/dashboard")).text                          # still there for the real visit


async def test_a_request_that_has_gone_is_not_replaced_by_another(web):
    app, mcp, _, _ = web()
    gone = queue(mcp, subject="First")
    queue(mcp, subject="Second")
    mcp._icloud_dashboard.sources["mail"].discard(next(q for q in mcp._icloud_dashboard.sources["mail"].pending() if q.id == gone))
    async with client(app) as c:
        await signed_in(c)
        page = (await c.get(f"/dashboard/approvals?item=mail:{gone}")).text
        assert "No longer waiting" in page and 'value="approve"' not in page and "Second" in page


async def test_only_the_whole_queue_prunes_what_is_kept_for_the_approve_check(web, monkeypatch):
    from icloud_mcp.mail import MailService
    calls = []
    real = MailService.approval_contexts
    monkeypatch.setattr(MailService, "approval_contexts", lambda self, qs, prune=False: calls.append((len(qs), prune)) or real(self, qs, prune=prune))
    app, mcp, _, _ = web()
    for i in range(7):
        queue(mcp, subject=f"Request {i}")
    async with client(app) as c:
        token = await signed_in(c)
        calls.clear()
        await c.get("/dashboard")
        assert calls == [(5, False)]                                     # the Overview describes the five it shows
        calls.clear()
        await c.get("/dashboard/approvals")
        assert calls == [(7, True)]
        calls.clear()
        q = mcp._icloud_dashboard.sources["mail"].pending()[0]
        await c.post("/dashboard/approvals/act", headers=SAME, data={"csrf": token, "kind": "mail", "id": q.id, "sha": q.sha256, "action": "discard"})
        assert (1, True) not in calls


async def test_the_count_of_unrecorded_failures_is_written_when_the_window_ends(web):
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        for _ in range(12):
            await c.post("/dashboard/passkey/verify", headers=SAME, json={"purpose": "signin", "ticket": "junk", "credential": {}})
    lines = [e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey"]
    assert len(lines) == 5                                                           # the flood has stopped here
    d.flush_failure_lines(time.time() + 601)                                         # what the timer does at the window's end
    lines = [e for e in d.audit.entries(limit=1000) if e["action"] == "signin.passkey"]
    assert len(lines) == 6 and lines[0]["summary"] == "7 more like this in 10 minutes were not recorded"
