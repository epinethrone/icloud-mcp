"""Settings on the dashboard: stored in DATA_DIR/settings.json, only what DASHBOARD_EDITABLE allows, checked before saving,
and a change that weakens protection asks for the owner again."""
import json

import pytest

from icloud_mcp.config import DASHBOARD_EDITABLE, Settings

from test_dashboard import SAME, client, signed_in, web  # noqa: F401  (the web fixture)


def test_dashboard_values_win_over_the_environment_but_only_for_editable_keys(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MAX_RECIPIENTS="25", OAUTH_ALLOWED_REDIRECT_HOSTS="claude.ai").items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"MAX_RECIPIENTS": "5", "OAUTH_ALLOWED_REDIRECT_HOSTS": "evil.example.net",
                                                        "MCP_PORT": "1"}))
    s = Settings.from_env()
    assert s.max_recipients == 5 and s.settings_active == ("MAX_RECIPIENTS",)
    assert s.allowed_redirect_hosts == ("claude.ai",) and s.port == 8000
    assert "OAUTH_ALLOWED_REDIRECT_HOSTS" not in DASHBOARD_EDITABLE and "MCP_PUBLIC_URL" not in DASHBOARD_EDITABLE
    monkeypatch.setenv("DASHBOARD_SETTINGS", "off")
    assert Settings.from_env().max_recipients == 25


async def test_a_change_is_saved_converted_and_logged(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/settings/mail")).text
        assert 'name="MAX_ATTACHMENT_BYTES"' in page and 'value="5"' in page and "Default" not in page   # only non-default origins are named
        r = await c.post("/dashboard/settings/mail", headers=SAME, data={
            "csrf": token, "ALLOW_SEND": "true", "SEND_REQUIRES_APPROVAL": "true", "SAVE_SENT_COPY": "true",
            "MAX_RECIPIENTS": "25", "MAIL_MAX_AGE_DAYS": "0", "MAX_BODY_CHARS": "30000", "MAX_ATTACHMENT_BYTES": "20",
            "OUTBOX_TTL_SECONDS": "24", "OUTBOX_MAX": "20", "SEND_ALLOWLIST": ""})
        assert r.status_code == 303 and r.headers["location"] == "/dashboard/settings?m=settings_saved"
        stored = json.loads((mcp._icloud_dashboard.sessions.path.parent / "settings.json").read_text())
        assert stored == {"MAX_ATTACHMENT_BYTES": str(20 * 1024 * 1024)}
        page = (await c.get("/dashboard/settings")).text
        assert "Restart to apply" in page and "1 changed" in page
    entry = mcp._icloud_dashboard.audit.entries()[0]
    assert entry["action"] == "settings.changed" and "Largest attachment 5 MiB → 20 MiB" in entry["summary"]


async def test_weakening_protection_asks_again(web):  # noqa: F811
    app, mcp, _, _ = web()
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        for rec in d.sessions._items.values():
            rec["auth_at"] -= 600
        form = {"csrf": token, "ALLOW_SEND": "true", "SAVE_SENT_COPY": "true", "MAX_RECIPIENTS": "25", "MAIL_MAX_AGE_DAYS": "0",
                "MAX_BODY_CHARS": "30000", "MAX_ATTACHMENT_BYTES": "5", "OUTBOX_TTL_SECONDS": "24", "OUTBOX_MAX": "20", "SEND_ALLOWLIST": ""}
        r = await c.post("/dashboard/settings/mail", headers=SAME, data=form)            # SEND_REQUIRES_APPROVAL unticked
        assert "Confirm it’s you" in r.text and "Send only after my approval" in r.text
        assert not (d.sessions.path.parent / "settings.json").exists()
        await c.post("/dashboard/signin", headers=SAME, data={"csrf": token, "password": "correct-horse-battery", "next": "/dashboard/settings/mail"})
        r = await c.post("/dashboard/settings/mail", headers=SAME, data=form)
        assert r.status_code == 303
    assert json.loads((d.sessions.path.parent / "settings.json").read_text()) == {"SEND_REQUIRES_APPROVAL": "false"}
    assert d.audit.entries()[0]["details"]["weakened"] == ["SEND_REQUIRES_APPROVAL"]


async def test_values_the_server_could_not_start_with_are_refused(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post("/dashboard/settings/calendar", headers=SAME, data={
            "csrf": token, "DEFAULT_TIMEZONE": "Mars/Olympus", "DEFAULT_CALENDAR": "", "ALLOW_CALENDAR_INVITES": "false",
            "INVITE_ALLOWLIST": "", "MAX_ATTENDEES": "10", "OWNER_ADDRESSES": ""})
        assert r.status_code == 400 and "not a time zone" in r.text
        r = await c.post("/dashboard/settings/areas", headers=SAME, data={
            "csrf": token, "ENABLE_MAIL": "true", "ENABLE_CALENDAR": "true", "ENABLE_CONTACTS": "true", "TOOLS": "mail,no_such_tool"})
        assert r.status_code == 400 and "no_such_tool" in r.text
        r = await c.post("/dashboard/settings/mail", headers=SAME, data={"csrf": token, "MAX_RECIPIENTS": "abc"})
        assert r.status_code == 400 and "enter a number" in r.text
    assert not (mcp._icloud_dashboard.sessions.path.parent / "settings.json").exists()


async def test_a_refused_save_keeps_what_was_typed_and_marks_the_field(web):  # noqa: F811
    app, mcp, _, _ = web()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post("/dashboard/settings/calendar", headers=SAME, data={
            "csrf": token, "DEFAULT_TIMEZONE": "Mars/Olympus", "DEFAULT_CALENDAR": "Club", "ALLOW_CALENDAR_INVITES": "false",
            "INVITE_ALLOWLIST": "jana@example.org", "MAX_ATTENDEES": "4", "OWNER_ADDRESSES": ""})
        assert r.status_code == 400
        assert 'value="Mars/Olympus" autocomplete="off" aria-invalid="true" aria-describedby="f-default_timezone-err"' in r.text
        assert 'value="Club"' in r.text and 'value="jana@example.org"' in r.text and 'value="4"' in r.text
        assert 'name="ALLOW_CALENDAR_INVITES" value="false" checked' in r.text
        r = await c.post("/dashboard/settings/mail", headers=SAME, data={
            "csrf": token, "SEND_MODE": "drafts", "SAVE_SENT_COPY": "true", "MAX_RECIPIENTS": "abc", "MAIL_MAX_AGE_DAYS": "0",
            "MAX_BODY_CHARS": "30000", "MAX_ATTACHMENT_BYTES": "9999", "OUTBOX_TTL_SECONDS": "24", "OUTBOX_MAX": "20", "SEND_ALLOWLIST": ""})
        assert r.status_code == 400 and "(1 more marked below)" in r.text
        assert 'value="abc"' in r.text and 'value="9999"' in r.text and r.text.count('aria-invalid="true"') == 2
        assert 'name="SEND_MODE" value="drafts" checked' in r.text
    assert not (mcp._icloud_dashboard.sessions.path.parent / "settings.json").exists()


async def test_reset_goes_back_to_the_environment(web):  # noqa: F811
    app, mcp, _, _ = web()
    path = mcp._icloud_dashboard.sessions.path.parent / "settings.json"
    path.write_text(json.dumps({"DEFAULT_TIMEZONE": "Asia/Tokyo"}))
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/settings/calendar")).text
        assert "Changed here" in page and "Asia/Tokyo" in page
        r = await c.post("/dashboard/settings/calendar/reset", headers=SAME, data={"csrf": token, "key": "DEFAULT_TIMEZONE"})
        assert "follows the server again (Europe/Berlin)" in r.text
    assert json.loads(path.read_text()) == {}


async def test_view_only_and_the_server_page(web):  # noqa: F811
    app, mcp, _, _ = web(DASHBOARD_SETTINGS="view")
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/settings/mail")).text
        assert " disabled" in page and 'action="/dashboard/settings/mail"' not in page
        r = await c.post("/dashboard/settings/mail", headers=SAME, data={"csrf": token, "MAX_RECIPIENTS": "3"})
        assert r.status_code == 403
        page = (await c.get("/dashboard/settings/server")).text
        assert "https://mcp.example.com" in page and "<form" not in page.split("<main>")[1].split("</main>")[0]


async def test_settings_can_be_left_out(web):  # noqa: F811
    app, *_ = web(DASHBOARD_SETTINGS="off")
    async with client(app) as c:
        await signed_in(c)
        assert (await c.get("/dashboard/settings")).status_code == 404


async def test_the_sending_choice_sets_both_switches(web):  # noqa: F811
    app, mcp, _, _ = web()
    path = mcp._icloud_dashboard.sessions.path.parent / "settings.json"
    async with client(app) as c:
        token = await signed_in(c)
        page = (await c.get("/dashboard/settings/mail")).text
        assert 'name="SEND_MODE" value="approval" checked' in page and 'name="ALLOW_SEND"' not in page
        base = {"csrf": token, "SAVE_SENT_COPY": "true", "MAX_RECIPIENTS": "25", "MAIL_MAX_AGE_DAYS": "0", "MAX_BODY_CHARS": "30000",
                "MAX_ATTACHMENT_BYTES": "5", "OUTBOX_TTL_SECONDS": "24", "OUTBOX_MAX": "20", "SEND_ALLOWLIST": ""}
        assert (await c.post("/dashboard/settings/mail", headers=SAME, data={**base, "SEND_MODE": "drafts"})).status_code == 303
        assert json.loads(path.read_text()) == {"ALLOW_SEND": "false"}
        assert (await c.post("/dashboard/settings/mail", headers=SAME, data={**base, "SEND_MODE": "direct"})).status_code == 303
        assert json.loads(path.read_text()) == {"ALLOW_SEND": "true", "SEND_REQUIRES_APPROVAL": "false"}
