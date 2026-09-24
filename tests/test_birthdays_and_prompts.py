"""Upcoming birthdays from contacts, and the ready-made workflow prompts."""
import asyncio
import dataclasses
from datetime import date

import pytest

from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsService, next_birthday, parse_birthday
from icloud_mcp.server import create_server


@pytest.mark.parametrize("raw, want", [
    ("1990-05-12", (1990, 5, 12)), ("19900512", (1990, 5, 12)), ("--05-12", (None, 5, 12)), ("--0512", (None, 5, 12)),
    ("1604-11-03", (None, 11, 3)), ("2000-02-29", (2000, 2, 29)), ("1990-13-01", None), ("", None), ("soon", None),
])
def test_birthday_formats_including_apples_unknown_year(raw, want):
    assert parse_birthday(raw) == want


def test_next_birthday_counts_today_and_moves_29_february_to_the_28th():
    today = date(2026, 9, 24)
    assert next_birthday(9, 24, today) == today
    assert next_birthday(9, 23, today) == date(2027, 9, 23)
    assert next_birthday(2, 29, today) == date(2027, 2, 28)
    assert next_birthday(2, 29, date(2028, 1, 1)) == date(2028, 2, 29)


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_upcoming_birthdays_soonest_first_with_age_when_known(s, monkeypatch):
    people = [
        {"uid": "a", "name": "Anna", "birthday": "1990-10-01", "has_email": True},
        {"uid": "b", "name": "Bram", "birthday": "--09-24", "has_email": False},
        {"uid": "c", "name": "Cleo", "birthday": "1985-12-30", "has_email": True},
        {"uid": "d", "name": "Dana", "birthday": "", "has_email": True},
    ]
    monkeypatch.setattr(ContactsService, "_all", lambda self: people)
    r = ContactsService(s).upcoming_birthdays(30, today=date(2026, 9, 24))
    assert [(b["name"], b["days_until"]) for b in r["birthdays"]] == [("Bram", 0), ("Anna", 7)]
    assert r["birthdays"][1]["turns"] == 36 and "turns" not in r["birthdays"][0]
    assert ContactsService(s).upcoming_birthdays(1, today=date(2026, 1, 2))["note"].startswith("No birthdays")


def test_prompts_follow_the_enabled_areas_and_always_ask_first(s):
    def prompts(settings):
        mcp, _ = create_server(settings)
        return {p.name: p for p in asyncio.run(mcp.list_prompts())}
    everything = prompts(s)
    assert set(everything) == {"triage_inbox", "plan_my_week", "prepare_for_event", "birthdays_coming_up"}
    assert set(prompts(dataclasses.replace(s, enable_mail=False))) == {"plan_my_week", "birthdays_coming_up"}
    mcp, _ = create_server(s)
    text = asyncio.run(mcp.get_prompt("triage_inbox", {"days": "2"})).messages[0].content.text
    assert "last 2 days" in text and "without asking me first" in text and "untrusted" in text


def test_the_birthday_tool_is_registered_with_contacts(s):
    mcp, _ = create_server(s)
    assert "contacts_upcoming_birthdays" in {t.name for t in asyncio.run(mcp.list_tools())}
