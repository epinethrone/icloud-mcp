"""Shortcuts: only names on both the server's SHORTCUTS_ALLOW and the Mac's own allowlist file ever run."""
import asyncio
import dataclasses
import json
import os
import pathlib
import subprocess
import sys

import pytest

from icloud_mcp.config import Settings
from icloud_mcp.server import create_server

SCRIPT = pathlib.Path(__file__).parent.parent / "mac-helper" / "ops" / "shortcut.py"
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


@pytest.fixture
def mac(tmp_path):
    allow = tmp_path / "shortcuts-allow.txt"
    log = tmp_path / "ran.txt"
    fake = tmp_path / "shortcuts"
    fake.write_text(f"""#!{sys.executable}
import sys
args = sys.argv[1:]
open({str(log)!r}, "a").write(" ".join(args) + "\\n")
out = args[args.index("--output-path") + 1]
text = open(args[args.index("--input-path") + 1]).read() if "--input-path" in args else ""
if args[1] == "Broken":
    sys.stderr.write("The action could not run"); sys.exit(1)
open(out, "w").write("done: " + args[1] + (" with " + text if text else ""))
""")
    fake.chmod(0o755)
    return allow, log, fake


def run(mac, args):
    allow, _, fake = mac
    env = {**os.environ, "ICLOUD_SHORTCUTS_ALLOW_FILE": str(allow), "ICLOUD_SHORTCUTS_BIN": str(fake)}
    p = subprocess.run([PY, "-I", str(SCRIPT), "shortcut_run", json.dumps(args)], capture_output=True, text=True, env=env, timeout=30)
    return (True, json.loads(p.stdout)) if p.returncode == 0 else (False, p.stderr)


def test_nothing_runs_until_the_mac_lists_it(mac):
    allow, log, _ = mac
    ok, err = run(mac, {"name": "Lights off"})
    assert not ok and "no shortcuts are allowed on this Mac" in err and not log.exists()
    allow.write_text("# assistant may run these\nLights off\n")
    ok, err = run(mac, {"name": "Delete everything"})
    assert not ok and "not in this Mac's list" in err and not log.exists()


def test_an_allowed_shortcut_runs_with_input_and_returns_its_output(mac):
    allow, log, _ = mac
    allow.write_text("Lights off\nSay\n")
    ok, out = run(mac, {"name": "Say", "input": "hello there", "budget": 30})
    assert ok and out == {"shortcut": "Say", "ran": True, "output": "done: Say with hello there", "truncated": False}
    assert log.read_text().startswith("run Say --output-path")


def test_a_failing_shortcut_reports_its_error(mac):
    allow, _, _ = mac
    allow.write_text("Broken\n")
    ok, err = run(mac, {"name": "Broken"})
    assert not ok and "could not run" in err


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, BRIDGE_TOKEN="b" * 40).items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def names(settings):
    mcp, _ = create_server(settings)
    return {t.name for t in asyncio.run(mcp.list_tools())}, mcp


def test_tools_exist_only_with_an_allowlist_and_never_read_only(s, monkeypatch):
    assert not {"shortcuts_list", "shortcuts_run"} & names(s)[0]
    on = dataclasses.replace(s, shortcuts_allow=("Lights off",))
    assert on.bridge_enabled and {"shortcuts_list", "shortcuts_run"} <= names(on)[0]
    assert not {"shortcuts_list", "shortcuts_run"} & names(dataclasses.replace(on, read_only=True))[0]
    monkeypatch.setenv("SHORTCUTS_ALLOW", "Lights off, Say")
    assert Settings.from_env().shortcuts_allow == ("Lights off", "Say")
    monkeypatch.setenv("SHORTCUTS_ALLOW", "Lights off; Say hi, then leave")                  # ';' when a name has a comma
    assert Settings.from_env().shortcuts_allow == ("Lights off", "Say hi, then leave")


def test_the_server_refuses_names_it_does_not_allow_before_asking_the_mac(s):
    _, mcp = names(dataclasses.replace(s, shortcuts_allow=("Lights off",)))
    seen = []
    mcp._icloud_bridge.call = lambda op, a=None: seen.append((op, a)) or {"ran": True, "output": "ok"}
    r = json.loads(asyncio.run(mcp.call_tool("shortcuts_run", {"name": "Delete everything"})).content[0].text)
    assert r["ran"] is False and seen == []
    r = json.loads(asyncio.run(mcp.call_tool("shortcuts_run", {"name": "Lights off"})).content[0].text)
    assert r["ran"] is True and seen == [("shortcut_run", {"name": "Lights off"})]
