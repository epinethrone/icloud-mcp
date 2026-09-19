"""Refused sign-in renewals must say WHY in the log, without ever leaking a token or its fingerprint."""
import hashlib
import logging
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import test_oauth_flow as T
from icloud_mcp.auth import _h
from icloud_mcp.config import Settings
from icloud_mcp.server import build_app, create_server


@pytest.fixture
def setup(tmp_path, monkeypatch, caplog):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=T.BASE,
                     MCP_OWNER_PASSWORD=T.PASSWORD, DATA_DIR=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    mcp, provider = create_server(s)
    caplog.set_level(logging.INFO, logger="icloud_mcp.auth")
    return build_app(s, mcp), provider, caplog


async def sign_in(c):
    """Full authorization-code flow; returns (client_id, access_token, refresh_token)."""
    cid = (await T.register(c)).json()["client_id"]
    verifier, challenge = T.pkce()
    ok, _ = await T.authorize(c, cid, challenge)
    code = parse_qs(urlsplit(ok.headers["location"]).query)["code"][0]
    r = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": T.REDIRECT, "client_id": cid, "code_verifier": verifier})
    body = r.json()
    return cid, body["access_token"], body["refresh_token"]


def refresh(c, cid, token):
    return c.post("/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": cid})


def warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_successful_renewal_is_logged_with_client_and_grant(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
    text = caplog.text
    assert f"issued tokens to client {cid[:8]} via authorization_code" in text and f"issued tokens to client {cid[:8]} via refresh_token" in text


async def test_reusing_an_already_rotated_refresh_token_says_it_was_rotated(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200                    # first user of this token wins and rotates it
        r = await refresh(c, cid, rt)                                              # a second holder of the same copy
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    msgs = warnings(caplog)
    assert any("already used" in m and "rotated" in m and "stale copy" in m and cid[:8] in m for m in msgs), msgs


async def test_an_unknown_refresh_token_is_distinguished_from_a_rotated_one(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, _, _ = await sign_in(c)
        assert (await refresh(c, cid, "definitely-not-a-real-token")).status_code == 400
    msgs = warnings(caplog)
    assert any("not recognised" in m and "already used" not in m for m in msgs), msgs


async def test_a_refresh_token_presented_by_the_wrong_client_names_both_clients(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid_a, _, rt_a = await sign_in(c)
        cid_b = (await T.register(c)).json()["client_id"]
        assert (await refresh(c, cid_b, rt_a)).status_code == 400
    assert any(f"presented by client {cid_b[:8]} but issued to client {cid_a[:8]}" in m for m in warnings(caplog))


async def test_an_expired_refresh_token_says_how_long_ago(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        provider.refresh[_h(rt)]["expires_at"] = time.time() - 7200
        assert (await refresh(c, cid, rt)).status_code == 400
    assert any("expired" in m and "h ago" in m for m in warnings(caplog))


async def test_a_replaced_access_token_is_reported_as_a_stale_copy(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, at, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {at}", "Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
    assert any("replaced by a refresh" in m and "old copy" in m for m in warnings(caplog))


async def test_no_token_value_or_fingerprint_ever_reaches_the_log(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        cid, at, rt = await sign_in(c)
        await refresh(c, cid, rt)
        await refresh(c, cid, rt)
        await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {at}"})
    text = caplog.text
    for secret in (at, rt, _h(at), _h(rt), hashlib.sha256(at.encode()).hexdigest()[:16], hashlib.sha256(rt.encode()).hexdigest()[:16]):
        assert secret not in text
    assert "aaaa-bbbb-cccc-dddd" not in text and T.PASSWORD not in text


async def test_unauthenticated_callers_cannot_flood_the_log(setup):
    app, provider, caplog = setup
    async with T.client_for(app) as c:
        for i in range(60):
            await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer junk-{i}"})
        for i in range(30):
            await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": f"not-registered-{i}"})
    noisy = [r for r in caplog.records if "not recognised" in r.getMessage() or "unknown client_id" in r.getMessage()]
    assert 1 <= len(noisy) <= 2                                                # one line per kind per minute, not one per request
