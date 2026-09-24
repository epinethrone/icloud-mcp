"""The tool surface every client loads: slim, still valid JSON Schema, still accepting an explicit null, and still carrying the
sentences that keep an agent from doing damage."""
import asyncio

import jsonschema
import pytest

from icloud_mcp.config import Settings
from icloud_mcp.server import create_server, slim_schema

SCHEMA_BUDGET = 38000


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, BRIDGE_TOKEN="t" * 40,
                     ENABLE_CONTACTS="true", ENABLE_REMINDERS="true", ENABLE_NOTES="true", ENABLE_DRIVE="true",
                     ALLOW_CALENDAR_INVITES="true", SHORTCUTS_ALLOW="Example shortcut").items():
        monkeypatch.setenv(k, v)
    server, _ = create_server(Settings.from_env())
    return server


def listed(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def leftovers(node):
    """Derived titles, 'default: null' and 'anyOf [X, null]' that slimming should have removed."""
    if isinstance(node, list):
        for x in node:
            yield from leftovers(x)
    elif isinstance(node, dict):
        for k, v in node.items():
            if (k == "title" and isinstance(v, str)) or (k == "default" and v is None) or (k == "anyOf" and {"type": "null"} in v):
                yield k
            yield from leftovers(v)


def test_every_schema_is_valid_and_the_total_stays_in_budget(mcp):
    import json
    total = 0
    for tool in listed(mcp).values():
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        text = json.dumps(tool.input_schema, separators=(",", ":"), ensure_ascii=False)
        assert not list(leftovers(tool.input_schema)), (tool.name, list(leftovers(tool.input_schema)))
        total += len(text)
    assert total <= SCHEMA_BUDGET, total


def test_slimming_keeps_meaning():
    node = {"title": "Args", "type": "object", "required": ["a"], "properties": {
        "a": {"title": "A", "type": "integer", "description": "An a."},
        "b": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "description": "A b.", "title": "B"},
        "c": {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": 3},
        "title": {"type": "string", "title": "Title"}}}
    assert slim_schema(node) == {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "integer", "description": "An a."},
        "b": {"type": "string", "description": "A b."},
        "c": {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": 3},
        "title": {"type": "string"}}}                                          # a parameter called 'title' survives


def test_an_explicit_null_is_still_accepted(mcp):
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    model = tools["calendar_list_events"].fn_metadata.arg_model
    got = model.model_validate({"start": "2026-09-01", "end": "2026-09-02", "calendar": None})
    assert got.calendar is None
    model = tools["mail_move"].fn_metadata.arg_model
    assert model.model_validate({"folder": "INBOX", "uids": [1], "destination": "Archive", "uidvalidity": None}).uidvalidity is None


@pytest.mark.parametrize("tool, param, phrase", [
    ("mail_move", "uidvalidity", "renumbered folder is then refused"),
    ("mail_delete", "uidvalidity", "renumbered folder is then refused"),
    ("calendar_create_event", "attendees", "iCloud emails each one an invitation"),
    ("calendar_create_event", "request_id", "never a second copy"),
    ("contacts_create", "request_id", "never a second copy"),
    ("calendar_delete_event", "occurrence_start", "Omit for the whole series"),
    ("calendar_update_event", "occurrence_start", "Omit for the whole series"),
])
def test_the_safety_sentences_survive(mcp, tool, param, phrase):
    assert phrase in listed(mcp)[tool].input_schema["properties"][param]["description"]
