"""End-to-end test of the OAuth layer: DCR -> authorize -> owner login -> token -> /mcp call -> refresh -> revoke."""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.server import create_server

BASE = "https://mcp.example.com"
PASSWORD = "correct-horse-battery"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in dict(
        ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
        MCP_OWNER_PASSWORD=PASSWORD, DATA_DIR=str(tmp_path),
    ).items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    mcp, provider = create_server(s)
    return mcp.streamable_http_app(host="0.0.0.0"), provider, s


import contextlib


@contextlib.asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):  # starts the MCP session manager
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False) as c:
            yield c


def client_for(app):
    return _client(app)


async def register(c, redirect=REDIRECT):
    r = await c.post("/register", json={"client_name": "Claude", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                                        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    return r


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def authorize(c, client_id, challenge, password=PASSWORD, state="xyz"):
    r = await c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                          "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "scope": "icloud"})
    assert r.status_code == 302, r.text
    login_url = r.headers["location"]
    assert login_url.startswith(f"{BASE}/login?p=")
    page = await c.get(login_url)
    assert page.status_code == 200 and "Owner password" in page.text
    pid = parse_qs(urlsplit(login_url).query)["p"][0]
    return await c.post("/login", data={"p": pid, "password": password, "action": "approve"}), pid


async def test_metadata_and_unauthenticated_rejected(app):
    a, _, _ = app
    async with client_for(a) as c:
        md = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert md["issuer"].rstrip("/") == BASE and md["code_challenge_methods_supported"] == ["S256"]
        assert "registration_endpoint" in md
        prm = await c.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert (await c.get("/healthz")).text == "ok"


async def test_full_flow(app):
    a, provider, _ = app
    async with client_for(a) as c:
        reg = await register(c)
        assert reg.status_code == 201, reg.text
        cid = reg.json()["client_id"]
        verifier, challenge = pkce()

        # wrong password is rejected and does not yield a code
        bad, pid = await authorize(c, cid, challenge, password="nope")
        assert bad.status_code == 401
        # right password on a fresh request
        ok, _ = await authorize(c, cid, challenge)
        assert ok.status_code == 302
        loc = urlsplit(ok.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
        q = parse_qs(loc.query)
        assert q["state"] == ["xyz"]
        code = q["code"][0]

        # wrong verifier fails
        r = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                         "client_id": cid, "code_verifier": secrets.token_urlsafe(48)})
        assert r.status_code == 400
        # (a failed PKCE attempt burns nothing we care about; get a new code)
        ok, _ = await authorize(c, cid, challenge)
        code = parse_qs(urlsplit(ok.headers["location"]).query)["code"][0]
        r = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                         "client_id": cid, "code_verifier": verifier})
        assert r.status_code == 200, r.text
        tok = r.json()
        assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]

        # code is single-use
        r2 = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                          "client_id": cid, "code_verifier": verifier})
        assert r2.status_code == 400

        # authenticated MCP call
        h = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json, text/event-stream"}
        init = await c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert init.status_code == 200, init.text

        # bad bearer rejected
        assert (await c.post("/mcp", headers={**h, "Authorization": "Bearer nope"}, json={})).status_code == 401

        # refresh rotates
        r = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
        assert r.status_code == 200, r.text
        tok2 = r.json()
        assert tok2["access_token"] != tok["access_token"]
        old = await c.post("/mcp", headers=h, json={})
        assert old.status_code == 401  # old access token dropped on rotation
        again = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
        assert again.status_code == 400  # old refresh token dead

        # revoke
        rv = await c.post("/revoke", data={"token": tok2["access_token"], "client_id": cid, "client_secret": ""})
        assert rv.status_code == 200, rv.text
        h2 = {**h, "Authorization": f"Bearer {tok2['access_token']}"}
        assert (await c.post("/mcp", headers=h2, json={})).status_code == 401

    # tokens are stored hashed
    raw = provider.path.read_text()
    assert tok["access_token"] not in raw and tok2["refresh_token"] not in raw


async def test_redirect_host_allowlist(app):
    a, _, _ = app
    async with client_for(a) as c:
        r = await register(c, redirect="https://evil.example.net/cb")
        assert r.status_code == 400
        assert (await register(c, redirect="http://localhost:6274/callback")).status_code == 201


async def test_brute_force_lockout(app):
    a, provider, _ = app
    async with client_for(a) as c:
        cid = (await register(c)).json()["client_id"]
        _, challenge = pkce()
        for _ in range(10):
            resp, pid = await authorize(c, cid, challenge, password="wrong")
            assert resp.status_code == 401
        resp, pid = await authorize(c, cid, challenge, password=PASSWORD)  # even the right one is locked out now
        assert resp.status_code == 429


async def test_deny_returns_access_denied(app):
    a, _, _ = app
    async with client_for(a) as c:
        cid = (await register(c)).json()["client_id"]
        _, challenge = pkce()
        r = await c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                              "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
        pid = parse_qs(urlsplit(r.headers["location"]).query)["p"][0]
        d = await c.post("/login", data={"p": pid, "action": "deny"})
        assert d.status_code == 302 and "error=access_denied" in d.headers["location"]
