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
