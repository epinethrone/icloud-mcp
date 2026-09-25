"""The admin API for the menu bar app: its own loopback listener with a token, never part of the public app; pause blocks every
tool but the diagnostics; credential changes are validated, stored in DATA_DIR and win over the environment; sign-out clears
the running server's sign-ins; restart answers before the process exits."""
import asyncio
import json
import os
import stat

import httpx
import pytest

from icloud_mcp import admin
from icloud_mcp.config import Settings
from icloud_mcp.server import build_app, create_server


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, ADMIN_PORT="8002",
                     WARMUP_ON_START="false").items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def served(s, exits=None):
    mcp, provider = create_server(s)
    token = admin.ensure_admin_token(s.data_dir)
    app = admin.build_admin_app(s, mcp, provider, token, exit_fn=(lambda: exits.append(1)) if exits is not None else None)
    return mcp, provider, token, app


def call(app, method, path, token=None, host="127.0.0.1:8002", **kw):
    async def go():
        headers = {"host": host, **({"authorization": f"Bearer {token}"} if token else {}), **kw.pop("headers", {})}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as c:
            return await c.request(method, path, headers=headers, **kw)
    return asyncio.run(go())


def test_the_token_is_private_and_every_request_needs_it(s):
    _, _, token, app = served(s)
    path = os.path.join(s.data_dir, "admin-token")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and len(token) >= 32
    assert admin.ensure_admin_token(s.data_dir) == token                                  # stable across restarts
    assert call(app, "GET", "/admin/v1/status").status_code == 401
    assert call(app, "GET", "/admin/v1/status", token="wrong" * 10).status_code == 401
    assert call(app, "GET", "/admin/v1/status", token=token, host="evil.example:8002").status_code == 403   # DNS rebinding
    assert call(app, "GET", "/admin/v1/status", token=token, headers={"origin": "https://evil.example"}).status_code == 403
    r = call(app, "GET", "/admin/v1/status", token=token).json()
    assert r["paused"] is False and r["tools"]["total"] > 20 and r["tools"]["by_area"]["mail"] > 5 and r["connected_apps"] == 0


def test_the_admin_api_is_not_part_of_the_public_app(s):
    mcp, _ = create_server(s)
    public = build_app(s, mcp)
    paths = {getattr(r, "path", "") for r in public.routes}
    assert not any(p.startswith("/admin") for p in paths)


def test_pause_blocks_tools_but_not_the_diagnostics(s, monkeypatch):
    monkeypatch.setenv("ENABLE_REMINDERS", "true")
    monkeypatch.setenv("BRIDGE_TOKEN", "b" * 40)
    mcp, _, token, app = served(Settings.from_env())
    assert call(app, "POST", "/admin/v1/pause", token=token, json={"paused": True}).json() == {"paused": True}

    async def run(name, args=None):
        return await mcp.call_tool(name, args or {})
    with pytest.raises(Exception, match="Paused by the owner"):
        asyncio.run(run("icloud_get_time"))
    helper = json.loads(asyncio.run(run("icloud_get_helper_status")).content[0].text)   # a diagnostic still answers
    assert helper["online"] is False
    assert call(app, "GET", "/admin/v1/status", token=token).json()["paused"] is True
    call(app, "POST", "/admin/v1/pause", token=token, json={"paused": False})
    assert "now" in json.loads(asyncio.run(run("icloud_get_time")).content[0].text)


def test_a_new_passcode_is_checked_stored_privately_and_wins_over_the_environment(s, monkeypatch):
    _, _, token, app = served(s)
    bad = call(app, "POST", "/admin/v1/owner-passcode", token=token, json={"passcode": "short"})
    assert bad.status_code == 400 and "12 characters" in bad.json()["error"]
    ok = call(app, "POST", "/admin/v1/owner-passcode", token=token, json={"passcode": "a brand new passcode"})
    assert ok.json() == {"saved": True, "restart_required": True}
    path = os.path.join(s.data_dir, "overrides.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = Settings.from_env()
    assert again.owner_password == "a brand new passcode" and again.overrides_active == ("MCP_OWNER_PASSWORD",)
    assert again.app_password == "aaaa-bbbb-cccc-dddd"                                   # untouched


def test_an_app_password_is_saved_only_after_icloud_accepts_it(s, monkeypatch):
    from icloud_mcp.mail import MailError, MailService
    _, _, token, app = served(s)
    monkeypatch.setattr(MailService, "_login", lambda self: (_ for _ in ()).throw(MailError("login failed for me@icloud.com")))
    r = call(app, "POST", "/admin/v1/app-password", token=token, json={"password": "wxyz wxyz wxyz wxyz"})
    assert r.status_code == 400 and "me@icloud.com" not in r.text and not os.path.exists(os.path.join(s.data_dir, "overrides.json"))
    assert call(app, "POST", "/admin/v1/app-password", token=token, json={"password": "nope"}).status_code == 400
    monkeypatch.setattr(MailService, "_login", lambda self: type("C", (), {"logout": lambda _: None})())
    assert call(app, "POST", "/admin/v1/app-password", token=token, json={"password": "wxyz wxyz wxyz wxyz"}).json()["saved"]
    assert Settings.from_env().app_password == "wxyz-wxyz-wxyz-wxyz"


def test_sign_out_all_clears_the_running_servers_sign_ins(s):
    _, provider, token, app = served(s)
    provider.clients["c1"] = {"client_id": "c1"}
    provider._issue("c1", ["icloud"], None)
    assert provider.connected_clients() == 1
    assert call(app, "POST", "/admin/v1/sign-out-all", token=token).json() == {"signed_out": 1}
    assert provider.connected_clients() == 0 and provider.clients == {}
    assert json.load(open(os.path.join(s.data_dir, "oauth_state.json")))["access"] == {}


def test_restart_answers_before_the_process_exits(s):
    exits = []
    _, _, token, app = served(s, exits)
    assert call(app, "POST", "/admin/v1/restart", token=token).json() == {"restarting": True} and exits == []
    import time
    time.sleep(0.8)
    assert exits == [1]


def test_admin_port_is_off_by_default_and_never_shares_a_port(s, monkeypatch):
    monkeypatch.delenv("ADMIN_PORT")
    assert Settings.from_env().admin_port == 0
    monkeypatch.setenv("ADMIN_PORT", "8000")
    with pytest.raises(SystemExit, match="ADMIN_PORT must differ"):
        Settings.from_env().validate_for_server()
