"""Apple Health on the server: the tools exist only with ENABLE_HEALTH, go to the Mac with exactly the given arguments, and carry the
private-data notice and instructions. The figures themselves are computed on the Mac (tests/test_health_ops.py)."""
import asyncio
import dataclasses
import json

import pytest

from icloud_mcp.config import Settings
from icloud_mcp.server import create_server


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, BRIDGE_TOKEN="t" * 40,
                     ENABLE_HEALTH="true").items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_HEALTH", raising=False)
    assert Settings.from_env().enable_health is False


def test_tools_and_instructions_only_when_enabled(s):
    def names(settings):
        async def go():
            mcp, _ = create_server(settings)
            return {t.name for t in await mcp.list_tools()}, mcp.instructions
        return asyncio.run(go())
    tools, text = names(s)
    assert {"health_get_summary", "health_get_day", "health_get_status", "health_refresh"} <= tools
    assert "HEALTH:" in text and "Apple Health" in text and "never put them in a message" in text
    tools, text = names(dataclasses.replace(s, enable_health=False))
    assert not {t for t in tools if t.startswith("health_")} and "HEALTH:" not in text
    assert "icloud_check_health" in tools


def test_calls_go_to_the_mac_with_exactly_the_given_arguments(s):
    seen = []

    async def go():
        mcp, _ = create_server(s)
        mcp._icloud_bridge.call = lambda op, a=None: seen.append((op, a)) or {"days": []}
        a = json.loads((await mcp.call_tool("health_get_summary", {"start": "2026-09-01", "end": "2026-09-07"})).content[0].text)
        b = json.loads((await mcp.call_tool("health_get_day", {"date": "2026-09-02", "metric": "sleep"})).content[0].text)
        await mcp.call_tool("health_refresh", {})
        return a, b
    a, b = asyncio.run(go())
    assert seen == [("health_summary", {"start": "2026-09-01", "end": "2026-09-07"}),
                    ("health_day", {"date": "2026-09-02", "metric": "sleep"}), ("health_refresh", {})]
    assert "private" in a["notice"] and "private" in b["notice"]
