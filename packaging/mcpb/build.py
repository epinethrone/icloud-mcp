"""Build the Claude Desktop bundle (icloud-mcp-<version>.mcpb) for one-click install.

The bundle uses the MCPB `uv` server type: it ships a manifest, a two-line entry script and a pyproject.toml that pins
icloud-mcp-server from PyPI, and Claude Desktop installs the dependencies with uv. Settings come from the install form
(`user_config`); the app-specific password is a `sensitive` field, which Claude Desktop keeps in the OS keychain.

    python packaging/mcpb/build.py                       # dist/icloud-mcp-<version>.mcpb, pinned to that PyPI release
    python packaging/mcpb/build.py --dependency "icloud-mcp-server @ file:///path/to/checkout"   # for local testing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "https://github.com/epinethrone/icloud-mcp"

USER_CONFIG = {
    "apple_id": {"type": "string", "title": "Apple Account email", "required": True,
                 "description": "The email you sign in to iCloud with."},
    "app_password": {"type": "string", "title": "App-specific password", "required": True, "sensitive": True,
                     "description": "Create one at account.apple.com > Sign-In and Security > App-Specific Passwords. "
                                    "Not your Apple Account password."},
    "display_name": {"type": "string", "title": "Your name", "required": False, "default": "",
                     "description": "Shown as the sender of mail Claude sends for you."},
    "timezone": {"type": "string", "title": "Time zone", "required": False, "default": "UTC",
                 "description": "IANA name for calendar times, e.g. Europe/Amsterdam or America/New_York."},
    "approve_sending": {"type": "boolean", "title": "Save mail to Drafts instead of sending", "default": True,
                        "description": "On: every message Claude writes is saved to your Drafts for you to send from Mail. "
                                       "Off: Claude sends directly."},
    "allow_invites": {"type": "boolean", "title": "Allow calendar invitations", "default": False,
                      "description": "Let Claude add guests to events. iCloud then emails them invitations and updates."},
    "read_only": {"type": "boolean", "title": "Read only", "default": False,
                  "description": "Claude can read mail, calendars and contacts but change nothing."},
    "tools": {"type": "string", "title": "Tools to load", "required": False, "default": "",
              "description": "Leave empty for all tools, or type 'essential' for a smaller core set that clients choose from "
                             "more reliably."},
}

ENV = {
    "ICLOUD_USERNAME": "${user_config.apple_id}",
    "ICLOUD_APP_PASSWORD": "${user_config.app_password}",
    "ICLOUD_DISPLAY_NAME": "${user_config.display_name}",
    "DEFAULT_TIMEZONE": "${user_config.timezone}",
    "SEND_REQUIRES_APPROVAL": "${user_config.approve_sending}",
    "ALLOW_CALENDAR_INVITES": "${user_config.allow_invites}",
    "READ_ONLY": "${user_config.read_only}",
    "TOOLS": "${user_config.tools}",
}

LONG_DESCRIPTION = """Connects Claude to your iCloud **Mail, Calendar and Contacts** using Apple's own protocols (IMAP, SMTP,
CalDAV, CardDAV) and an app-specific password. Everything runs on your computer; nothing goes through a third-party server.

**Approval-first by default:** mail Claude writes is saved to your Drafts for you to send, calendar invitations to other
people are off, and deletes go to the Trash. Text from emails, events and contacts is treated as untrusted data.

Reminders, Notes and iCloud Drive need the optional Mac helper, see the project README."""


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def tool_list() -> list[dict[str, str]]:
    """The tools the bundle exposes with its default settings, for directories and the install screen."""
    sys.path.insert(0, str(ROOT / "src"))
    saved = dict(os.environ)
    try:
        return _tool_list()
    finally:                      # never leak the placeholder settings into whatever runs next
        os.environ.clear()
        os.environ.update(saved)


def _tool_list() -> list[dict[str, str]]:
    os.environ.update(ICLOUD_USERNAME="build@example.com", ICLOUD_APP_PASSWORD="abcd-efgh-ijkl-mnop", ICLOUD_KEYCHAIN="false",
                      MCP_PUBLIC_URL="", MCP_OWNER_PASSWORD="", DATA_DIR=tempfile.mkdtemp())
    for k in ("ENABLE_REMINDERS", "ENABLE_NOTES", "ENABLE_DRIVE", "TOOLS", "READ_ONLY"):
        os.environ.pop(k, None)
    import dataclasses

    from icloud_mcp.config import Settings
    from icloud_mcp.server import create_server

    mcp, _ = create_server(dataclasses.replace(Settings.from_env(), local_mode=True, allow_calendar_invites=True))
    out = []
    for t in sorted(asyncio.run(mcp.list_tools()), key=lambda t: t.name):
        first = " ".join((t.description or "").split())
        first = first.split(". ")[0].rstrip(".") + "."
        out.append({"name": t.name, "description": first[:200]})
    return out


def manifest(ver: str) -> dict:
    return {
        "manifest_version": "0.4",
        "name": "icloud-mcp",
        "display_name": "iCloud",
        "version": ver,
        "description": "Your iCloud Mail, Calendar and Contacts in Claude. Local, approval-first, no third-party server.",
        "long_description": LONG_DESCRIPTION,
        "author": {"name": "epinethrone", "url": "https://github.com/epinethrone"},
        "repository": {"type": "git", "url": f"{REPO}.git"},
        "homepage": REPO,
        "documentation": f"{REPO}#run-it-locally-claude-desktop-and-claude-code",
        "support": f"{REPO}/issues",
        "icon": "icon.png",
        "license": "MIT",
        "keywords": ["icloud", "apple", "mail", "email", "calendar", "contacts", "caldav", "carddav", "imap"],
        "privacy_policies": ["https://www.apple.com/legal/privacy/"],
        "server": {
            "type": "uv",
            "entry_point": "src/server.py",
            "mcp_config": {"command": "uv", "args": ["run", "--directory", "${__dirname}", "src/server.py"], "env": ENV},
        },
        "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.11"}},
        "user_config": USER_CONFIG,
        "tools": tool_list(),
    }


ENTRY = '''"""Claude Desktop bundle entry point: runs icloud-mcp in local (stdio) mode with the settings from the install form."""
from icloud_mcp.server import main

if __name__ == "__main__":
    main(["--local"])
'''


def build(out_dir: Path, dependency: str | None) -> Path:
    ver = version()
    dep = dependency or f"icloud-mcp-server=={ver}"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        (stage / "src").mkdir()
        (stage / "src" / "server.py").write_text(ENTRY)
        (stage / "pyproject.toml").write_text(
            f'[project]\nname = "icloud-mcp-bundle"\nversion = "{ver}"\nrequires-python = ">=3.11"\n'
            f'dependencies = [{json.dumps(dep)}]\n')
        shutil.copy(ROOT / "assets" / "logo-512.png", stage / "icon.png")
        (stage / "manifest.json").write_text(json.dumps(manifest(ver), indent=2, ensure_ascii=False) + "\n")
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"icloud-mcp-{ver}.mcpb"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(p for p in stage.rglob("*") if p.is_file()):
                z.write(f, f.relative_to(stage).as_posix())
    return target


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"), help="output directory (default: dist/)")
    ap.add_argument("--dependency", help="override the pinned icloud-mcp-server requirement, e.g. a local path for testing")
    args = ap.parse_args()
    print(build(Path(args.out), args.dependency))
