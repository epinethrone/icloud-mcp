"""The activity log: append-only lines in a hash chain, pruned by age, never holding bodies or secrets."""
import json
import os
import time

from icloud_mcp import server
from icloud_mcp.audit import AuditLog


def test_entries_chain_and_verify(tmp_path):
    a = AuditLog(str(tmp_path))
    a.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
    a.record("tool.mail", actor="Claude", summary="Moved messages (3) to Archive", count=3, destination="Archive")
    assert oct(os.stat(tmp_path / "audit.jsonl").st_mode & 0o777) == "0o600"
    newest = a.entries()
    assert [e["action"] for e in newest] == ["tool.mail", "signin.password"]
    assert newest[0]["details"] == {"count": 3, "destination": "Archive"}
    assert a.verify() == {"ok": True, "entries": 2, "since": newest[1]["t"]}
    # a new process continues the same chain
    AuditLog(str(tmp_path)).record("server.paused", actor="owner", summary="Paused all tools")
    assert AuditLog(str(tmp_path)).verify()["ok"] is True


def test_a_changed_or_removed_line_breaks_the_chain(tmp_path):
    a = AuditLog(str(tmp_path))
    for i in range(4):
        a.record("tool.mail", actor="Claude", summary=f"Moved messages ({i})")
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    changed = json.loads(lines[1])
    changed["summary"] = "Nothing happened"
    path.write_text("\n".join([lines[0], json.dumps(changed), *lines[2:]]) + "\n")
    assert a.verify() == {"ok": False, "entries": 4, "broken_at": 2, "since": json.loads(lines[0])["t"]}
    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")           # a removed line
    assert a.verify()["ok"] is False


def test_old_entries_are_pruned_and_the_rest_still_verifies(tmp_path, monkeypatch):
    a = AuditLog(str(tmp_path), keep_days=1)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    a.record("tool.mail", actor="Claude", summary="Old")
    monkeypatch.setattr(time, "time", real)
    a._pruned_at = 0
    a.record("tool.mail", actor="Claude", summary="New")
    assert [e["summary"] for e in a.entries()] == ["New"]
    assert a.verify()["ok"] is True


def test_writing_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    a = AuditLog(str(blocker / "sub"))           # a data dir that cannot exist
    a.record("tool.mail", actor="Claude", summary="Moved")
    assert a.entries() == []


def test_disabled_log_records_nothing(tmp_path):
    a = AuditLog(str(tmp_path), enabled=False)
    a.record("tool.mail", actor="Claude", summary="Moved")
    assert not (tmp_path / "audit.jsonl").exists()


def test_tool_lines_name_the_target_but_never_the_body(tmp_path, monkeypatch):
    a = AuditLog(str(tmp_path))
    monkeypatch.setattr(server, "_audit", a)
    monkeypatch.setattr(server, "_WRITE_TOOLS", {"mail_send_message", "mail_move_messages"})
    server._audit_call("mail_send_message", {"to": ["bob@example.org"], "subject": "Lunch", "body": "secret plans",
                                             "attachments": [{"content_base64": "AAAA"}]}, True,
                       '{"status":"queued_for_owner_approval","sent":false}')
    server._audit_call("mail_move_messages", {"folder": "INBOX", "uids": [1, 2, 3], "destination": "Archive"}, True, "{}")
    server._audit_call("mail_get_message", {"folder": "INBOX", "uid": 1}, True, "{}")          # reads are not recorded
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "secret plans" not in raw and "AAAA" not in raw
    moved, sent = a.entries()
    assert sent["action"] == "tool.queued" and sent["summary"] == "Asked to send message “Lunch” (waiting for your approval)"
    assert sent["details"]["to"] == ["bob@example.org"] and sent["actor"] == "Local client"
    assert moved["summary"] == "Moved messages (3) to Archive" and moved["details"]["count"] == 3
