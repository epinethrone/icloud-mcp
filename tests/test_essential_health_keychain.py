"""A smaller tool list on request, one-call health checks, and the app password kept in the macOS Keychain."""
import asyncio
import dataclasses
import json
import subprocess
import sys

import pytest

import icloud_mcp.config as config_mod
from icloud_mcp.cal import CalendarService
from icloud_mcp.config import Settings, keychain_password
from icloud_mcp.mail import MailService
from icloud_mcp.server import ESSENTIAL_TOOLS, create_server


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TOOLS", raising=False)
    return Settings.from_env()


def names(settings):
    mcp, _ = create_server(settings)
    return {t.name for t in asyncio.run(mcp.list_tools())}


# ------------------------------------------------------------------ TOOLS
def test_essential_keeps_a_small_core_of_the_enabled_areas(s):
    full = names(s)
    essential = names(dataclasses.replace(s, tools=("essential",)))
    assert essential < full and len(essential) <= len(ESSENTIAL_TOOLS)
    assert {"mail_search", "calendar_find_free_time", "contacts_search", "icloud_check_health"} <= essential
    assert not any(n.startswith(("reminders_", "notes_", "drive_")) for n in essential)   # Mac areas are off here
    assert "mail_delete" not in essential and "contacts_delete" not in essential          # nothing destructive by default


def test_named_tools_can_be_added_and_unknown_names_stop_startup(s):
    picked = names(dataclasses.replace(s, tools=("essential", "mail_move")))
    assert "mail_move" in picked and "mail_delete" not in picked
    assert names(dataclasses.replace(s, tools=("mail_search",))) == {"mail_search"}
    with pytest.raises(SystemExit, match="no such tool: mail_explode.*Available:"):
        create_server(dataclasses.replace(s, tools=("mail_explode",)))


def test_tools_setting_is_read_from_the_environment(monkeypatch, s):
    monkeypatch.setenv("TOOLS", "essential, mail_move")
    assert Settings.from_env().tools == ("essential", "mail_move")


# ------------------------------------------------------------------ icloud_check_health
def test_health_reports_each_area_and_never_raises(s, monkeypatch):
    monkeypatch.setattr(MailService, "health", lambda self: {"inbox_messages": 12, "can_move": True})
    def down(self):
        raise ConnectionError("caldav.icloud.com unreachable")
    monkeypatch.setattr(CalendarService, "list_calendars", down)
    mcp, _ = create_server(dataclasses.replace(s, enable_contacts=False))
    result = asyncio.run(mcp.call_tool("icloud_check_health", {}))
    payload = getattr(result, "structured_content", None) or json.loads(result.content[0].text)
    payload = payload.get("result", payload)
    assert payload["ok"] is False
    assert payload["areas"]["mail"]["ok"] is True and payload["areas"]["mail"]["inbox_messages"] == 12
    assert payload["areas"]["calendar"]["ok"] is False and "unreachable" in payload["areas"]["calendar"]["error"]
    assert "contacts" not in payload["areas"] and all("ms" in a for a in payload["areas"].values())


# ------------------------------------------------------------------ Keychain
class FakeRun:
    def __init__(self, stdout="", code=0):
        self.calls, self.stdout, self.code = [], stdout, code

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, stdout=self.stdout, stderr="")


def test_password_comes_from_the_keychain_only_when_not_set(monkeypatch, tmp_path):
    fake = FakeRun("wwww-xxxx-yyyy-zzzz\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ICLOUD_KEYCHAIN", raising=False)
    assert Settings.from_env().app_password == "wwww-xxxx-yyyy-zzzz"
    assert fake.calls[0][:6] == ["/usr/bin/security", "find-generic-password", "-s", "icloud-mcp", "-a", "me@icloud.com"]
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "aaaa-bbbb-cccc-dddd")                  # an explicit value always wins
    fake.calls.clear()
    assert Settings.from_env().app_password == "aaaa-bbbb-cccc-dddd" and fake.calls == []
    monkeypatch.setenv("ICLOUD_KEYCHAIN", "false")
    assert keychain_password("me@icloud.com") == ""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("ICLOUD_KEYCHAIN")
    assert keychain_password("me@icloud.com") == ""


def test_a_missing_keychain_item_just_means_no_password(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", FakeRun("", code=44))
    monkeypatch.delenv("ICLOUD_KEYCHAIN", raising=False)
    assert keychain_password("me@icloud.com") == ""


def test_store_password_never_puts_the_password_on_the_command_line(monkeypatch, capsys):
    from icloud_mcp import server
    fake = FakeRun()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    server.store_password()
    (argv,) = fake.calls
    assert argv[0] == "/usr/bin/security" and argv[1] == "add-generic-password" and argv[-1] == "-w"   # security prompts itself
    assert "Stored in the login Keychain" in capsys.readouterr().out


def test_health_errors_never_carry_secrets_or_account_ids():
    from icloud_mcp.server import redact_error
    msg = ("AuthorizationError: 401 for https://p48-caldav.icloud.com/123456789/calendars/home/ as me@icloud.com "
           "with aaaa-bbbb-cccc-dddd")
    out = redact_error(msg, ("aaaa-bbbb-cccc-dddd", "me@icloud.com", ""))
    assert "aaaa-bbbb" not in out and "me@icloud.com" not in out and "123456789" not in out
    assert "https://p48-caldav.icloud.com/…" in out and out.startswith("AuthorizationError: 401")
