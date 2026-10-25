"""A refresh token sent twice in quick succession returns the same new pair instead of signing the client out.

Clients and gateways refresh from several requests at once, or retry when the response got lost. Without a grace period
the second request finds the token already rotated and the app has to be connected again by the owner.
"""
import asyncio
import logging

import pytest

import test_oauth_flow as T
from icloud_mcp.config import Settings
from icloud_mcp.server import build_app, create_server
from test_oauth_logging import refresh, sign_in


def _make(tmp_path, monkeypatch, **env):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=T.BASE,
                     MCP_OWNER_PASSWORD=T.PASSWORD, DATA_DIR=str(tmp_path), **env).items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    mcp, provider = create_server(s)
    return build_app(s, mcp), provider


@pytest.fixture
def setup(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="icloud_mcp.auth")
    return _make(tmp_path, monkeypatch)


def _expire(provider):
    for g in provider._grace.values():
        g["rotated_at"] -= provider.s.refresh_grace_seconds + 1


async def _mcp_ok(c, access):
    h = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream"}
    r = await c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    return r.status_code == 200


async def test_a_repeated_refresh_returns_the_same_working_pair(setup, caplog):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        first = (await refresh(c, cid, rt)).json()
        second = await refresh(c, cid, rt)
        assert second.status_code == 200
        body = second.json()
        assert body["access_token"] == first["access_token"] and body["refresh_token"] == first["refresh_token"]
        assert 0 < body["expires_in"] <= provider.s.access_token_ttl
        assert await _mcp_ok(c, body["access_token"])
        assert (await refresh(c, cid, body["refresh_token"])).status_code == 200      # the new refresh token still rotates
    assert "repeated a refresh" in caplog.text and rt not in caplog.text and first["refresh_token"] not in caplog.text


async def test_after_the_grace_period_the_old_token_is_refused(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        _expire(provider)
        r = await refresh(c, cid, rt)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_grace_never_outlives_the_pair_it_points_to(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        new = (await refresh(c, cid, rt)).json()
        assert (await refresh(c, cid, new["refresh_token"])).status_code == 200     # the client rotated again
        assert (await refresh(c, cid, rt)).status_code == 400

        cid, _, rt = await sign_in(c)
        new = (await refresh(c, cid, rt)).json()
        rv = await c.post("/revoke", data={"token": new["refresh_token"], "client_id": cid, "client_secret": ""})
        assert rv.status_code == 200
        assert (await refresh(c, cid, rt)).status_code == 400                         # revoked: the old token gets nothing

        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        assert provider.sign_out(cid)
        assert (await refresh(c, cid, rt)).status_code in (400, 401)


async def test_another_client_cannot_use_the_grace(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        other, _, _ = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        assert (await refresh(c, other, rt)).status_code == 400


async def test_parallel_refreshes_all_get_the_one_pair(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        replies = await asyncio.gather(*(refresh(c, cid, rt) for _ in range(5)))
        assert [r.status_code for r in replies] == [200] * 5
        assert len({r.json()["refresh_token"] for r in replies}) == 1
        assert len([v for v in provider.refresh.values() if v["client_id"] == cid]) == 1   # one rotation, not five


async def test_the_repeat_gets_the_same_scope_or_nothing(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        wider = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "scope": "icloud admin"})
        assert wider.status_code == 400
        same = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "scope": "icloud"})
        assert same.status_code == 200


async def test_sign_out_all_and_revoking_the_new_access_token_end_the_grace(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        new = (await refresh(c, cid, rt)).json()
        assert (await c.post("/revoke", data={"token": new["access_token"], "client_id": cid, "client_secret": ""})).status_code == 200
        assert (await refresh(c, cid, rt)).status_code == 400

        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        provider.sign_out_all()
        assert not provider._grace


async def test_old_entries_do_not_pile_up(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        for _ in range(5):
            rt = (await refresh(c, cid, rt)).json()["refresh_token"]
        assert len(provider._grace) == 1                     # each rotation drops the entry that pointed at the pair it replaced
        _expire(provider)
        cid2, _, rt2 = await sign_in(c)                      # any later token activity prunes expired entries
        assert len(provider._grace) == 0


async def test_the_grace_is_never_written_to_disk(setup):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        new = (await refresh(c, cid, rt)).json()
    raw = provider.path.read_text()
    assert new["access_token"] not in raw and new["refresh_token"] not in raw and rt not in raw


async def test_a_restart_ends_the_grace(setup, tmp_path, monkeypatch):
    app, provider = setup
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
    app2, _ = _make(tmp_path, monkeypatch)
    async with T.client_for(app2) as c:
        assert (await refresh(c, cid, rt)).status_code == 400


async def test_zero_turns_the_grace_off(tmp_path, monkeypatch):
    app, provider = _make(tmp_path, monkeypatch, REFRESH_TOKEN_GRACE_SECONDS="0")
    assert provider.s.refresh_grace_seconds == 0
    async with T.client_for(app) as c:
        cid, _, rt = await sign_in(c)
        assert (await refresh(c, cid, rt)).status_code == 200
        assert (await refresh(c, cid, rt)).status_code == 400
    assert not provider._grace


@pytest.mark.parametrize("value, expected", [("-5", 0), ("30", 30), ("9999", 300)])
def test_the_setting_is_bounded(tmp_path, monkeypatch, value, expected):
    _, provider = _make(tmp_path, monkeypatch, REFRESH_TOKEN_GRACE_SECONDS=value)
    assert provider.s.refresh_grace_seconds == expected
