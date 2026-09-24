"""The Mac helper, tested on Linux: everything except the real osascript, which is replaced by a fake runner."""
import ast
import importlib.util
import json
import pathlib
import re
import subprocess
import threading

import pytest

import icloud_mcp.bridge as bridge_mod
from icloud_mcp.bridge import BridgeError
from test_bridge import TOKEN, live, s  # noqa: F401  (fixtures)

HELPER_PATH = pathlib.Path(__file__).parent.parent / "mac-helper" / "icloud_mac_helper.py"
spec = importlib.util.spec_from_file_location("icloud_mac_helper", HELPER_PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


# ------------------------------------------------------------------ the two copies of the rules cannot drift apart
def test_helper_and_server_agree_on_the_operations():
    assert helper.OPS == bridge_mod.OPS
    # Reminders through EventKit, Notes through a script each, iCloud Drive through ops/drive.py; every operation in exactly one
    assert set(helper.OP_FILES) | helper.EVENTKIT_OPS | helper.DRIVE_OPS | helper.SHORTCUT_OPS == set(helper.OPS)
    assert not set(helper.OP_FILES) & helper.EVENTKIT_OPS and not (set(helper.OP_FILES) | helper.EVENTKIT_OPS) & helper.DRIVE_OPS
    assert pathlib.Path(helper.DRIVE_SCRIPT).is_file() and pathlib.Path(helper.SHORTCUT_SCRIPT).is_file()
    for op, filename in helper.OP_FILES.items():
        assert (HELPER_PATH.parent / "ops" / filename).is_file(), op


def test_helper_and_server_validate_arguments_identically(monkeypatch):
    ops = {"demo": {"title": ("str", True, 10), "count": ("int", False, 50), "flag": ("bool", False, 0), "due": ("iso", False, 40)}}
    monkeypatch.setattr(bridge_mod, "OPS", ops)
    monkeypatch.setattr(helper, "OPS", ops)
    cases = [{"title": "hi"}, {"title": "hi", "count": 50, "flag": False, "due": "2026-09-21"}, {"title": "x" * 11}, {"title": 1}, {"title": "a", "count": -1},
             {"title": "a", "count": True}, {"title": "a", "flag": 1}, {"title": "a", "due": "soon"}, {"title": "a", "extra": 1}, {}, {"title": None},
             {"title": "a", "due": "2026-13-45"}, {"title": "a", "due": "2026-02-30"}, {"title": "a", "due": "2028-02-29"}, {"title": "a", "due": "2026-09-21T25:00:00"},
             {"title": "a", "due": "2026-09-21T23:59:59Z"}, {"title": "a", "due": "2026-09-21T10:00:00+25:00"}, {"title": "a", "due": "2026-09-21T10:00:00-05:30"}]
    for case in cases:
        results = []
        for fn, err in ((bridge_mod.validate_args, BridgeError), (helper.validate_args, helper.HelperError)):
            try:
                results.append(("ok", fn("demo", case)))
            except err:
                results.append(("error", None))
        assert results[0] == results[1], case


def test_helper_source_stays_valid_for_the_python_that_ships_with_macos():
    ast.parse(HELPER_PATH.read_text(), feature_version=(3, 9))


# ------------------------------------------------------------------ how a command is built: fixed program + one JSON argument
def test_the_command_is_a_static_script_file_plus_one_json_argument():
    cmd = helper.build_command("note_folders", {})
    assert cmd[:3] == ["/usr/bin/osascript", "-l", "JavaScript"] and "-e" not in cmd and len(cmd) == 5
    assert pathlib.Path(cmd[3]).parent == HELPER_PATH.parent / "ops" and cmd[4] == "{}"


def test_every_reminders_operation_runs_the_eventkit_binary_and_never_a_jxa_script():
    for op in helper.EVENTKIT_OPS:
        cmd = helper.build_command(op, {})
        assert cmd == [helper.EVENTKIT_BIN, op, "{}"], op
    assert helper.EVENTKIT_BIN == str(HELPER_PATH.parent / "bin" / "reminders-eventkit")


def test_hostile_text_only_ever_appears_inside_the_json_argument(monkeypatch):
    ops = {"demo": {"title": ("str", True, 100)}}
    monkeypatch.setattr(helper, "OPS", ops)
    monkeypatch.setattr(helper, "OP_FILES", {"demo": "note_folders.js"})
    evil = '"); do shell script "rm -rf ~"; ("'
    cmd = helper.build_command("demo", {"title": evil})
    assert len(cmd) == 5 and json.loads(cmd[4]) == {"title": evil}      # one argv element, parsed as data
    assert all(evil not in part for part in cmd[:4])
    cmd = helper.build_command("reminder_create", {"title": evil})
    assert len(cmd) == 3 and json.loads(cmd[2]) == {"title": evil} and all(evil not in part for part in cmd[:2])


def test_an_operation_name_can_never_select_an_arbitrary_file():
    for bad in ("../../etc/passwd", "reminder_lists.js", "selftest_echo", "", "REMINDER_LISTS", "reminder_lists; ls", "reminders_snapshot"):
        ok, result, error = helper.run_op(bad, {})
        assert ok is False and error == "unknown operation"


def test_operation_scripts_are_static_and_use_no_dangerous_constructs():
    for path in (HELPER_PATH.parent / "ops").glob("*.js"):
        text = path.read_text()
        assert "function run(argv)" in text, path.name
        assert not re.search(r"\$\{|doShellScript|\$\.system|\beval\(|Function\(|NSTask|do shell script", text), path.name


# ------------------------------------------------------------------ running a script (subprocess faked)
def fake_run(monkeypatch, returncode=0, stdout=b"[]", stderr=b"", exc=None, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append((cmd, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    monkeypatch.setattr(helper.subprocess, "run", run)


def test_a_successful_script_result_is_parsed(monkeypatch):
    seen = []
    fake_run(monkeypatch, stdout=b'[{"id":"x","name":"Home"}]', seen=seen)
    assert helper.run_op("reminder_lists", {}, timeout=7) == (True, [{"id": "x", "name": "Home"}], "")
    assert seen[0][1]["timeout"] == 7 and seen[0][1]["stdin"] == subprocess.DEVNULL          # bounded, and no terminal input


def test_failures_become_short_actionable_messages(monkeypatch):
    fake_run(monkeypatch, returncode=1, stderr=b"execution error: Not authorized to send Apple events to Notes. (-1743)")
    ok, _, error = helper.run_op("note_folders", {})
    assert not ok and "Privacy & Security > Automation" in error
    fake_run(monkeypatch, exc=subprocess.TimeoutExpired("reminders-eventkit", 5))
    assert "did not finish within" in helper.run_op("reminder_lists", {}, timeout=5)[2]
    fake_run(monkeypatch, exc=FileNotFoundError())
    assert "only runs on macOS" in helper.run_op("note_folders", {})[2]
    assert "run install.sh again" in helper.run_op("reminder_lists", {})[2]           # no silent fallback to the JXA scripts
    fake_run(monkeypatch, exc=PermissionError())
    assert "run install.sh again" in helper.run_op("reminders_list", {})[2]
    denied = b'Full Access to Reminders is not granted (currently: denied). Enable "iCloud Mac Helper (Reminders)" in System Settings.\n'
    fake_run(monkeypatch, returncode=1, stderr=denied)
    assert helper.run_op("reminder_lists", {})[2].startswith("Full Access to Reminders is not granted (currently: denied)")
    fake_run(monkeypatch, returncode=1, stderr=b"reminder not found (-1728)\n")
    assert "not found" in helper.run_op("reminder_complete", {"id": "x"})[2]
    fake_run(monkeypatch, stdout=b"not json")
    assert "not JSON" in helper.run_op("reminder_lists", {})[2]
    fake_run(monkeypatch, stdout=b"x" * (helper.MAX_OUTPUT + 1))
    assert "too large" in helper.run_op("reminder_lists", {})[2]


def test_selftest_on_a_non_mac_reports_failure_as_json(capsys, monkeypatch):
    monkeypatch.setattr(helper.platform, "system", lambda: "Linux")                  # the same result when the suite runs on a Mac
    assert helper.selftest() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["platform"]["ok"] is False


# ------------------------------------------------------------------ end to end over real TLS with certificate pinning
def test_a_tool_call_is_answered_through_the_whole_chain(live):
    bridge, cfg = live
    assert helper.handle_one(cfg, wait=0) == "idle"                              # helper announces itself
    seen = []
    def runner(op, args, seconds):
        seen.append((op, args))
        return True, [{"id": "x-apple-reminder://1", "name": "Groceries"}], ""
    worker = threading.Thread(target=lambda: seen.append(helper.handle_one(cfg, runner=runner, wait=5)))
    worker.start()
    result = bridge.call("reminder_lists")
    worker.join(5)
    assert result == [{"id": "x-apple-reminder://1", "name": "Groceries"}]
    assert seen[0] == ("reminder_lists", {}) and seen[1] == "done"
    assert bridge.status()["helper"]["version"] == helper.VERSION


def test_an_error_on_the_mac_reaches_the_caller(live):
    bridge, cfg = live
    helper.handle_one(cfg, wait=0)
    worker = threading.Thread(target=lambda: helper.handle_one(cfg, runner=lambda o, a, t: (False, None, "the requested list was not found"), wait=5))
    worker.start()
    with pytest.raises(BridgeError, match="requested list was not found"):
        bridge.call("reminder_lists")
    worker.join(5)


def test_a_wrong_fingerprint_stops_the_helper_before_anything_is_sent(live):
    bridge, cfg = live
    with pytest.raises(helper.HelperError, match="does not match the pinned fingerprint"):
        helper.handle_one({**cfg, "fingerprint": "00" * 32}, wait=0)
    assert bridge.last_seen is None                                              # the server never saw the token or a request


def test_the_fingerprint_may_be_written_with_colons_prefix_or_capitals(live):
    bridge, cfg = live
    fp = cfg["fingerprint"]
    pretty = "SHA256:" + ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper()
    assert helper.handle_one({**cfg, "fingerprint": pretty}, wait=0) == "idle"


def test_a_wrong_token_is_refused_by_the_server(live):
    bridge, cfg = live
    assert helper.handle_one({**cfg, "token": "not-the-token"}, wait=0) == "refused"
    assert bridge.last_seen is None


def test_plain_http_addresses_are_refused_by_the_helper(live):
    bridge, cfg = live
    with pytest.raises(helper.HelperError, match="https"):
        helper.handle_one({**cfg, "server": cfg["server"].replace("https://", "http://")}, wait=0)


def test_config_problems_are_reported_clearly(tmp_path):
    with pytest.raises(helper.HelperError, match="Run install.sh"):
        helper.load_config(str(tmp_path / "missing.json"))
    (tmp_path / "c.json").write_text(json.dumps({"server": "https://x"}))
    with pytest.raises(helper.HelperError, match="missing 'token'"):
        helper.load_config(str(tmp_path / "c.json"))


# ------------------------------------------------------------------ actionable network errors (the macOS Local Network block)
def test_a_no_route_to_host_error_explains_the_macos_local_network_block():
    text = helper.explain(OSError(65, "No route to host"))
    assert "Local Network" in text and "/usr/bin/python3" in text and "No route to host" in text
    assert helper.explain(helper.HelperError("plain problem")) == "plain problem"
    assert "Local Network" not in helper.explain(OSError(111, "Connection refused"))


def test_the_installer_prefers_apples_python_and_still_passes_a_syntax_check():
    script = (HELPER_PATH.parent / "install.sh").read_text()
    assert 'PY=/usr/bin/python3' in script and "xcode-select -p" in script
    assert subprocess.run(["bash", "-n", str(HELPER_PATH.parent / "install.sh")]).returncode == 0


def test_write_selftest_cleans_up_even_when_a_step_fails(monkeypatch, capsys):
    calls, seen_args = [], []

    def fake_run_op(op, args, timeout=60, extra=None):
        calls.append(op)
        seen_args.append((op, dict(args)))
        if op == "reminder_create":
            return True, {"id": "T1", "list_id": "L1", "title": args["title"], "notes": "", "due": None, "priority": 0}, ""
        if op == "reminders_list":
            return True, {"reminders": [{"id": "T1"}]}, ""
        if op == "reminder_update":
            return False, None, "boom"                                               # a failure mid-way must not skip the cleanup
        if op == "note_create":
            return True, {"id": "x-coredata://N1"}, ""
        if op == "note_read":
            raise RuntimeError("crash while reading the note")
        return True, {"id": "x", "title": "t", "completed": True}, ""

    class Done:
        returncode = 0
        stderr = b""

    deleted = []
    monkeypatch.setattr(helper, "run_op", fake_run_op)
    monkeypatch.setattr(helper.subprocess, "run", lambda cmd, **kw: deleted.append(cmd) or Done())
    with pytest.raises(RuntimeError):
        helper.selftest_write()
    assert "reminder_delete" in calls                                                # the temporary reminder was removed anyway
    assert ("reminder_update", {"id": "x-apple-reminder://T1", "notes": "edited through an old-style id"}) in seen_args   # old ids are exercised
    assert deleted and "selftest_note_delete.js" in deleted[0][-2] and "x-coredata://N1" in deleted[0][-1]   # so was the temporary note


# ------------------------------------------------------------------ Reminders through EventKit
EVENTKIT_DIR = HELPER_PATH.parent / "eventkit"


def test_the_server_cannot_smuggle_extra_arguments_and_extra_is_added_only_after_validation(monkeypatch):
    commands = []

    class Done:
        returncode, stdout, stderr = 0, b"{}", b""

    monkeypatch.setattr(helper.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done())
    ok, _, error = helper.run_op("reminder_update", {"id": "a", "hint": {"list_id": "L", "index": 3}})
    assert ok is False and "does not take 'hint'" in error and not commands
    helper.run_op("reminder_update", {"id": "a", "title": "t"}, extra={"note": 1})
    assert json.loads(commands[-1][-1]) == {"id": "a", "title": "t", "note": 1}


def test_the_embedded_info_plist_and_the_installer_agree_on_the_bundle_id_and_usage_strings():
    import plistlib
    info = plistlib.loads((EVENTKIT_DIR / "Info.plist").read_bytes())
    assert info["NSRemindersFullAccessUsageDescription"] and info["NSRemindersUsageDescription"]   # without these macOS shows no prompt at all
    script = (HELPER_PATH.parent / "install.sh").read_text()
    assert 'EK_ID="%s"' % info["CFBundleIdentifier"] in script
    assert "-Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist" in script and '--identifier "$EK_ID"' in script
    assert info["CFBundleName"] in helper.REMINDERS_GRANT


def test_the_selftest_reads_the_permission_state_from_the_binarys_own_wording():
    swift = (EVENTKIT_DIR / "reminders-eventkit.swift").read_text()
    for phrase, status in (("not yet asked", "notDetermined"), ("denied", "denied"), ("device policy", "restricted"), ("write-only", "writeOnly")):
        assert phrase in swift, phrase                                                 # the binary still says it this way
        assert helper._access_status("Full Access to Reminders is not granted (currently: %s)" % phrase) == status
    assert swift.count('fail("Full Access to Reminders is not granted') == 2          # every refusal names the grant
    assert helper._access_status("something else") == "unknown"


def test_old_style_reminder_ids_are_accepted_by_the_binary():
    swift = (EVENTKIT_DIR / "reminders-eventkit.swift").read_text()
    assert 'let jxaIdPrefix = "x-apple-reminder://"' in swift
    assert "calendarItem(withIdentifier: bareId(id))" in swift and '"id": bareId(r.calendarItemIdentifier)' in swift


def test_a_selftest_started_outside_launchd_reruns_itself_through_launchd(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(helper.platform, "system", lambda: "Darwin")
    monkeypatch.delenv(helper.IN_LAUNCHD, raising=False)
    monkeypatch.setattr(helper, "run_in_launchd", lambda args: seen.append(args) or (1, '{"overall": "fail"}\n'))
    monkeypatch.setattr(helper.sys, "argv", ["helper", "--selftest", "--config", "/c.json"])
    assert helper.main() == 1 and seen == [["--selftest", "--config", "/c.json"]]
    assert json.loads(capsys.readouterr().out) == {"overall": "fail"}
    monkeypatch.setenv("ICLOUD_MAC_HELPER_CONFIG", "/env.json")                     # launchd will not pass this environment on
    monkeypatch.setattr(helper.sys, "argv", ["helper", "--selftest-write"])
    helper.main()
    assert seen[-1] == ["--selftest-write", "--config", "/env.json"]


def test_inside_launchd_the_selftest_runs_and_leaves_its_report_in_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.platform, "system", lambda: "Darwin")
    monkeypatch.setenv(helper.IN_LAUNCHD, "1")
    monkeypatch.setattr(helper, "selftest_write", lambda: print('{"overall": "pass"}') or 0)
    out = tmp_path / "result.json"
    monkeypatch.setattr(helper.sys, "argv", ["helper", "--selftest-write", "--agent-out", str(out)])
    assert helper.main() == 0
    assert json.loads(out.read_text()) == {"exit": 0, "stdout": '{"overall": "pass"}\n'}


def test_the_launchd_job_uses_the_agents_python_and_is_always_removed(monkeypatch):
    import plistlib
    calls = []

    class R:
        returncode, stdout, stderr = 0, b"", b""

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == [helper.LAUNCHCTL, "bootstrap"]:
            job = plistlib.loads(pathlib.Path(cmd[3]).read_bytes())
            calls.append(job)
            out = job["ProgramArguments"][job["ProgramArguments"].index("--agent-out") + 1]
            pathlib.Path(out).write_text(json.dumps({"exit": 0, "stdout": "report"}))
        return R()

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setenv("ICLOUD_MAC_HELPER_PYTHON", "/usr/bin/python3")
    assert helper.run_in_launchd(["--selftest"], timeout=5) == (0, "report")
    job = next(c for c in calls if isinstance(c, dict))
    assert job["Label"] == helper.PROBE_LABEL and "KeepAlive" not in job and job["EnvironmentVariables"] == {helper.IN_LAUNCHD: "1"}
    assert job["ProgramArguments"][:3] == ["/usr/bin/python3", str(HELPER_PATH), "--selftest"]
    assert calls[-1][:2] == [helper.LAUNCHCTL, "bootout"] and calls[-1][2].endswith("/" + helper.PROBE_LABEL)


def test_script_errors_are_shown_without_osascript_wrapping():
    text = "execution error: Error: several lists are named 'Work'; pass list_id (from reminders_list_lists) to choose one (-2700)"
    assert helper._friendly(text) == "several lists are named 'Work'; pass list_id (from reminders_list_lists) to choose one"
    assert helper._friendly("script.js: execution error: Error: invalid due date (-2700)") == "invalid due date"
    assert "not found" in helper._friendly("Error: Can't get object. (-1728)")


def test_note_backups_older_than_30_days_go_to_the_trash_and_nothing_else(tmp_path):
    import os
    now = 2_000_000_000
    for name, age_days in (("old-a.html", 31), ("old-b.html", 90), ("fresh.html", 29), ("notes.txt", 400)):
        p = tmp_path / name
        p.write_text("x")
        os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    trashed = []
    moved = helper.prune_note_backups(folder=str(tmp_path), trash=lambda paths: trashed.extend(paths) or True, now=now)
    assert moved == 2 and sorted(os.path.basename(p) for p in trashed) == ["old-a.html", "old-b.html"]
    assert helper.prune_note_backups(folder=str(tmp_path / "missing"), now=now) == 0          # no folder yet: nothing to do
    assert helper.prune_note_backups(folder=str(tmp_path), trash=lambda paths: False, now=now) == 0   # a failed move is not counted
