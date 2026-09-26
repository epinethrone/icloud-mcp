"""The server must not start with the untouched placeholder values from .env.example."""
import dataclasses
import pathlib
import re

import pytest

from icloud_mcp.config import Settings


@pytest.fixture
def good(monkeypatch, tmp_path):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL="https://mcp.example.net",
                     MCP_OWNER_PASSWORD="a-long-random-owner-password", DATA_DIR=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_real_values_pass(good):
    good.validate_for_server()


@pytest.mark.parametrize("field,value,name", [
    ("owner_password", "change-me-to-a-long-random-string", "MCP_OWNER_PASSWORD"),
    ("app_password", "xxxx-xxxx-xxxx-xxxx", "ICLOUD_APP_PASSWORD"),
    ("username", "you@icloud.com", "ICLOUD_USERNAME"),
    ("public_url", "https://icloud-mcp.example.com", "MCP_PUBLIC_URL"),
])
def test_placeholder_values_are_refused(good, field, value, name):
    with pytest.raises(SystemExit, match=name):
        dataclasses.replace(good, **{field: value}).validate_for_server()


def test_the_shipped_env_example_cannot_be_used_as_is(monkeypatch, tmp_path):
    example = pathlib.Path(__file__).parent.parent / ".env.example"
    for line in example.read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            monkeypatch.setenv(m.group(1), m.group(2))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="placeholder"):
        Settings.from_env().validate_for_server()


def test_claude_chatgpt_and_local_clients_may_sign_in_by_default(good, monkeypatch):
    """The redirect allowlist covers the clients the README documents; setting it replaces the default entirely."""
    assert set(good.allowed_redirect_hosts) == {"claude.ai", "claude.com", "chatgpt.com", "chat.openai.com", "localhost", "127.0.0.1"}
    monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_HOSTS", "claude.ai")
    assert Settings.from_env().allowed_redirect_hosts == ("claude.ai",)


def test_the_origins_match_the_sign_in_clients(good):
    """A browser-based client's Origin must be accepted wherever its sign-in is: otherwise it signs in and then gets 403."""
    from starlette.testclient import TestClient

    from icloud_mcp.server import build_app, create_server
    from icloud_mcp.auth import SCOPE
    mcp, provider = create_server(good)
    token = provider._issue("test-client", [SCOPE], None).access_token
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}
    with TestClient(build_app(good, mcp), base_url=f"https://{good.public_host}") as client:
        for origin in ("https://claude.ai", "https://claude.com", "https://chatgpt.com", "https://chat.openai.com"):
            r = client.post("/mcp", json=body, headers={**headers, "Origin": origin})
            assert r.status_code == 200, (origin, r.status_code, r.text[:200])
        assert client.post("/mcp", json=body, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
