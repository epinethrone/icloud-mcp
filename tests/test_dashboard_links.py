"""With the dashboard on, the owner meets one interface: approvals live on /dashboard/approvals, every /outbox address leads
there, tools point there, and the connect page and the site root share the dashboard's look and link to it."""
import contextlib
import re

import httpx
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.imessage import IMessageService
from icloud_mcp.outbox import Outbox
from icloud_mcp.server import create_server

BASE = "https://mcp.example.com"
PASSWORD = "correct-horse-battery"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    def make(**env):
        for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                         MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path), ALLOW_SEND="true", **env).items():
            monkeypatch.setenv(k, v)
        return Settings.from_env()
    return make


@contextlib.asynccontextmanager
async def client(s):
    mcp, provider = create_server(s)
    app = mcp.streamable_http_app(host="0.0.0.0")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False) as c:
            yield c


def test_tools_point_to_the_dashboard_or_to_outbox_without_it(settings):
    assert settings().approvals_url == f"{BASE}/dashboard/approvals"
    assert settings(DASHBOARD="false").approvals_url == f"{BASE}/outbox"


def test_a_queued_imessage_points_to_the_dashboard(settings, tmp_path):
    s = settings(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org", BRIDGE_TOKEN="t" * 40)

    class Bridge:
        def call(self, op, args=None):
            return {"chat": {"chat_id": "anna@example.org", "participants": ["anna@example.org"], "services": ["iMessage"]},
                    "messages": [], "complete": True}
    box = Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    r = IMessageService(s, Bridge(), None).send("Hi", chat_id="anna@example.org", outbox=box, public_url=s.public_url)
    assert r["status"] == "queued_for_owner_approval" and r["approve_at"] == f"{BASE}/dashboard/approvals"


@pytest.mark.parametrize("method, path", [("GET", "/outbox"), ("POST", "/outbox"), ("POST", "/outbox/act")])
async def test_every_outbox_address_leads_to_the_dashboard(settings, method, path):
    async with client(settings()) as c:
        r = await c.request(method, path, data={"password": PASSWORD, "id": "x", "action": "approve"} if method == "POST" else None)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard/approvals"
        assert "Owner password" not in r.text                     # the password no longer opens a queue outside the dashboard


async def test_without_the_dashboard_the_outbox_page_stays(settings):
    async with client(settings(DASHBOARD="false")) as c:
        r = await c.get("/outbox")
        assert r.status_code == 200 and "Owner password" in r.text and "/dashboard" not in r.text


async def _consent_page(c):
    cid = (await c.post("/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
                                           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})).json()["client_id"]
    r = await c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": "s",
                                          "code_challenge": "x" * 43, "code_challenge_method": "S256", "scope": "icloud"})
    return await c.get(r.headers["location"])


async def test_the_connect_page_wears_the_dashboard_look(settings):
    async with client(settings()) as c:
        page = await _consent_page(c)
        assert page.status_code == 200 and 'class="auth"' in page.text and "Owner password" in page.text
        css = re.search(r'href="(/dashboard/dashboard\.css\?v=[0-9a-f]+)"', page.text)
        assert css and (await c.get(css[1])).status_code == 200    # the stylesheet it links is served
        csp = page.headers["content-security-policy"]
        assert "style-src 'self'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
        assert "<script" not in page.text
        bad = await c.post("/login", data={"p": re.search(r'name="p" value="([^"]+)"', page.text)[1], "password": "wrong", "action": "approve"})
        assert bad.status_code == 401 and 'class="banner bad"' in bad.text and "Incorrect password." in bad.text
        gone = await c.get("/login?p=nope")
        assert gone.status_code == 400 and "expired" in gone.text and 'class="auth"' in gone.text


async def test_without_the_dashboard_the_connect_page_keeps_its_own_style(settings):
    async with client(settings(DASHBOARD="false")) as c:
        page = await _consent_page(c)
        assert page.status_code == 200 and "dashboard.css" not in page.text and "<style>" in page.text
        assert "style-src 'unsafe-inline'" in page.headers["content-security-policy"]


async def test_the_site_root_links_the_dashboard_only_when_it_exists(settings):
    async with client(settings()) as c:
        assert 'href="/dashboard"' in (await c.get("/")).text
    async with client(settings(DASHBOARD="false")) as c:
        assert "/dashboard" not in (await c.get("/")).text


def test_a_full_calendar_queue_names_only_the_dashboard(settings):
    import icalendar

    from icloud_mcp.cal import CalendarError
    s = settings(ALLOW_CALENDAR_INVITES="approve")
    mcp, _ = create_server(s)
    svc = mcp._icloud_dashboard.sources["calendar"].cal
    svc.outbox.max_items = 0
    ev = icalendar.Event()
    ev.add("uid", "u1")
    ev.add("summary", "Autumn fair")
    ev.add("attendee", "mailto:jana@example.org")
    cal = type("Cal", (), {"url": "https://caldav.example.com/cal/", "name": "Club"})()
    with pytest.raises(CalendarError) as e:
        svc._queue("create", cal=cal, ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", uid="u1", ev=ev)
    assert "Approvals page" in str(e.value) and "/outbox" not in str(e.value) and "Do not retry" in str(e.value)
