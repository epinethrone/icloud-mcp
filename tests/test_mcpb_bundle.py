"""The Claude Desktop bundle: a valid uv-type manifest whose install form maps onto real settings."""
import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("mcpb_build", ROOT / "packaging" / "mcpb" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = mod.build(tmp_path_factory.mktemp("mcpb"), None)
    with zipfile.ZipFile(path) as z:
        return path, {n: z.read(n) for n in z.namelist()}, mod.version()


def test_bundle_contains_exactly_the_uv_layout(bundle):
    path, files, ver = bundle
    assert path.name == f"icloud-mcp-{ver}.mcpb"
    assert set(files) == {"manifest.json", "pyproject.toml", "src/server.py", "icon.png"}
    assert f'"icloud-mcp-server=={ver}"' in files["pyproject.toml"].decode()       # pinned to the same release
    assert 'main(["--local"])' in files["src/server.py"].decode()


def test_manifest_is_well_formed_and_every_placeholder_is_backed_by_the_form(bundle):
    _, files, ver = bundle
    m = json.loads(files["manifest.json"])
    assert m["manifest_version"] == "0.4" and m["version"] == ver and m["server"]["type"] == "uv"
    assert m["server"]["mcp_config"]["args"] == ["run", "--directory", "${__dirname}", "src/server.py"]
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(m["server"]["mcp_config"])))
    assert used == set(m["user_config"])                                            # no dangling or unused fields
    assert m["user_config"]["app_password"]["sensitive"] is True
    assert m["user_config"]["approve_sending"]["default"] is True and m["user_config"]["allow_invites"]["default"] is False
    names = {t["name"] for t in m["tools"]}
    assert {"mail_search_messages", "calendar_find_free_time", "contacts_search_contacts", "icloud_check_health"} <= names
    assert not any(n.startswith(("reminders_", "notes_", "drive_")) for n in names)  # Mac areas are not in the bundle
    assert m["privacy_policies"] and m["icon"] == "icon.png"


def test_empty_optional_fields_are_treated_as_unset(monkeypatch):
    import os

    from icloud_mcp.server import drop_unfilled_placeholders
    monkeypatch.setenv("TOOLS", "${user_config.tools}")
    monkeypatch.setenv("ICLOUD_DISPLAY_NAME", "Alex")
    drop_unfilled_placeholders()
    assert "TOOLS" not in os.environ and os.environ["ICLOUD_DISPLAY_NAME"] == "Alex"
