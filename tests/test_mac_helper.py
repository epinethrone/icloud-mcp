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
    assert set(helper.OP_FILES) | {"reminders_list"} == set(helper.OPS)          # reminders_list is answered from the cache, not by a script
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


# ------------------------------------------------------------------ how a command is built: static script + one JSON argument
def test_the_command_is_a_static_script_file_plus_one_json_argument():
    cmd = helper.build_command("reminder_lists", {})
    assert cmd[:3] == ["osascript", "-l", "JavaScript"] and "-e" not in cmd and len(cmd) == 5
    assert pathlib.Path(cmd[3]).parent == HELPER_PATH.parent / "ops" and cmd[4] == "{}"


def test_hostile_text_only_ever_appears_inside_the_json_argument(monkeypatch):
    ops = {"demo": {"title": ("str", True, 100)}}
    monkeypatch.setattr(helper, "OPS", ops)
    monkeypatch.setattr(helper, "OP_FILES", {"demo": "reminder_lists.js"})
    evil = '"); do shell script "rm -rf ~"; ("'
    cmd = helper.build_command("demo", {"title": evil})
    assert len(cmd) == 5 and json.loads(cmd[4]) == {"title": evil}      # one argv element, parsed as data
    assert all(evil not in part for part in cmd[:4])


def test_an_operation_name_can_never_select_an_arbitrary_file():
    for bad in ("../../etc/passwd", "reminder_lists.js", "selftest_echo", "", "REMINDER_LISTS", "reminder_lists; ls"):
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
    fake_run(monkeypatch, returncode=1, stderr=b"execution error: Not authorized to send Apple events to Reminders. (-1743)")
    ok, _, error = helper.run_op("reminder_lists", {})
    assert not ok and "Privacy & Security > Automation" in error
    fake_run(monkeypatch, exc=subprocess.TimeoutExpired("osascript", 5))
    assert "did not finish within" in helper.run_op("reminder_lists", {}, timeout=5)[2]
    fake_run(monkeypatch, exc=FileNotFoundError())
    assert "only runs on macOS" in helper.run_op("reminder_lists", {})[2]
    fake_run(monkeypatch, stdout=b"not json")
    assert "not JSON" in helper.run_op("reminder_lists", {})[2]
    fake_run(monkeypatch, stdout=b"x" * (helper.MAX_OUTPUT + 1))
    assert "too large" in helper.run_op("reminder_lists", {})[2]


def test_selftest_on_a_non_mac_reports_failure_as_json(capsys):
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
    calls = []

    def fake_run_op(op, args, timeout=60, extra=None, internal=False):
        calls.append(op)
        if op == "reminder_create":
            return True, {"id": "x-apple-reminder://T1", "list_id": "L1", "title": args["title"], "notes": "", "due": None, "priority": 0}, ""
        if op == "reminder_lists":
            return True, [{"id": "L1", "name": "Home", "account": "iCloud"}], ""
        if op == "reminders_snapshot":
            return True, {"list_id": "L1", "list": "Home", "account": "iCloud", "count": 1,
                          "items": [{"id": "x-apple-reminder://T1", "title": "t", "notes": "", "due": None, "priority": 0, "position": 0}]}, ""
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
    assert deleted and "selftest_note_delete.js" in deleted[0][-2] and "x-coredata://N1" in deleted[0][-1]   # so was the temporary note


# ------------------------------------------------------------------ the Reminders cache
class FakeMac:
    """A fake Reminders behind the helper's call interface. Each snapshot costs simulated seconds, like a big list on a real Mac."""

    def __init__(self):
        self.now = 1000.0
        self.lists = [{"id": "L-small", "name": "Small", "account": "iCloud"}, {"id": "L-big", "name": "Big", "account": "iCloud"},
                      {"id": "L-big2", "name": "Big", "account": "Exchange"}]
        self.cost = {"L-small": 0.2, "L-big": 16.0, "L-big2": 0.3}
        self.items = {
            "L-small": [{"id": "s1", "title": "Milk", "notes": "", "due": "2026-09-22T10:00:00.000Z", "priority": 0, "position": 0}],
            "L-big": [{"id": "b1", "title": "Printer paper", "notes": "A4, 500 sheets", "due": None, "priority": 0, "position": 0},
                      {"id": "b2", "title": "Batteries", "notes": "", "due": "2026-09-21T09:00:00.000Z", "priority": 1, "position": 900},
                      {"id": "b3", "title": "Light bulbs", "notes": "", "due": None, "priority": 0, "position": 1002}],
            "L-big2": []}
        self.calls = []

    def clock(self):
        return self.now

    def call(self, op, args, timeout, extra=None, internal=False):
        self.calls.append((op, dict(args), extra))
        if op == "reminder_lists":
            return True, [dict(x) for x in self.lists], ""
        if op == "reminders_snapshot":
            lid = args["list_id"]
            self.now += self.cost[lid]
            return True, {"list_id": lid, "list": next(x["name"] for x in self.lists if x["id"] == lid), "account": "iCloud",
                          "count": 1000 + len(self.items[lid]), "items": [dict(i) for i in self.items[lid]]}, ""
        if op == "reminder_update":
            return True, {"id": args["id"], "title": args.get("title", "t"), "notes": args.get("notes", ""), "completed": False, "due": None,
                          "priority": args.get("priority", 0), "position": 900}, ""
        if op == "reminder_complete":
            return True, {"id": args["id"], "title": "t", "completed": args.get("completed", True), "position": 900}, ""
        if op == "reminder_delete":
            return True, {"deleted": args["id"], "title": "t", "position": 0}, ""
        if op == "reminder_create":
            return True, {"id": "new1", "title": args["title"], "notes": "", "list": "Big", "list_id": args.get("list_id", "L-big"), "due": None, "priority": 0}, ""
        return False, None, "unexpected " + op

    def snapshots(self):
        return [c for c in self.calls if c[0] == "reminders_snapshot"]


@pytest.fixture
def mac():
    m = FakeMac()
    m.cache = helper.ReminderCache(call=m.call, clock=m.clock, debounce=0, background=False)
    m.kicked = []
    m.cache._kick = lambda list_id, delay=0.0: m.kicked.append(list_id)
    return m


def read(mac, **args):
    return mac.cache.read(helper.validate_args("reminders_list", args))


def test_first_read_scans_each_list_once_and_returns_only_active_reminders_soonest_first(mac):
    out = read(mac)
    assert [r["id"] for r in out["reminders"]] == ["b2", "s1", "b1", "b3"]                # soonest due first, undated last
    assert {r["list_id"] for r in out["reminders"]} == {"L-small", "L-big"} and "position" not in out["reminders"][0]
    assert len(mac.snapshots()) == 3 and "cached" not in out


def test_a_big_list_is_served_from_the_cache_and_a_small_one_is_re_read_live(mac):
    read(mac)
    mac.calls.clear(); mac.now += 30                                                        # 30 s later: small list is old, the big one is fine
    out = read(mac)
    assert [c[1]["list_id"] for c in mac.snapshots()] == ["L-small", "L-big2"]              # only the cheap lists were scanned again
    assert [(c["list_id"], c["refreshing"]) for c in out["cached"]] == [("L-big", False)]        # the big one is labelled with its age
    assert 25 <= out["cached"][0]["age_seconds"] <= 60
    assert mac.kicked == []                                                                # not old enough for a background refresh


def test_an_old_big_list_is_still_served_at_once_and_refreshed_in_the_background(mac):
    read(mac)
    mac.calls.clear(); mac.now += 400
    out = read(mac, list="Big")
    assert mac.kicked.count("L-big") == 1 and not [c for c in mac.snapshots() if c[1]["list_id"] == "L-big"]     # served stale, refresh queued
    assert any(c["list_id"] == "L-big" and c["refreshing"] for c in out["cached"])


def test_filters_limit_and_list_selection(mac):
    read(mac)
    assert [r["id"] for r in read(mac, query="A4")["reminders"]] == ["b1"]           # matches notes, case-insensitively
    assert [r["id"] for r in read(mac, list_id="L-small")["reminders"]] == ["s1"]
    assert len(read(mac, limit=2)["reminders"]) == 2
    assert {r["account"] for r in read(mac, list="Big")["reminders"]} <= {"iCloud", "Exchange"}   # two lists named Big are BOTH searched by name
    with pytest.raises(helper.HelperError, match="not found"):
        read(mac, list="Nope")
    with pytest.raises(helper.HelperError, match="needs list"):
        read(mac, refresh=True)


def test_refresh_rereads_the_named_list_even_when_cached(mac):
    read(mac); mac.calls.clear()
    read(mac, list_id="L-big", refresh=True)
    assert [c[1]["list_id"] for c in mac.snapshots()] == ["L-big"]


def test_reads_time_out_with_a_clear_message_instead_of_hanging(mac):
    mac.cost["L-big"] = 100.0
    with pytest.raises(helper.HelperError, match="still loading"):
        mac.cache.read({}, budget=30)


def test_edits_use_the_remembered_position_and_never_send_a_hint_the_server_supplied(mac):
    read(mac); mac.calls.clear()
    ok, result, _ = mac.cache.write("reminder_update", {"id": "b2", "title": "Batteries AA"}, 60)
    assert ok and mac.calls[0][2] == {"hint": {"list_id": "L-big", "index": 900}}
    assert "position" not in result                                                        # internal detail, never shown to the agent
    ok, _, _ = mac.cache.write("reminder_complete", {"id": "unknown"}, 60)
    assert mac.calls[1][2] is None                                                         # not cached: no hint, the script scans
    ok, _, error = mac.cache.write("reminder_update", {"id": "b2", "title": "x", "hint": {"list_id": "L-small", "index": 0}}, 60)
    assert ok is False and "does not take 'hint'" in error


def test_writes_keep_the_cache_truthful(mac):
    read(mac)
    mac.cache.write("reminder_update", {"id": "b1", "title": "Renamed", "priority": 5}, 60)
    assert next(r for r in read(mac, list_id="L-big")["reminders"] if r["id"] == "b1")["title"] == "Renamed"
    mac.cache.write("reminder_complete", {"id": "b2"}, 60)
    assert "b2" not in [r["id"] for r in read(mac, list_id="L-big")["reminders"]]           # done: gone from the active list at once
    mac.cache.write("reminder_delete", {"id": "b1"}, 60)                                     # was at position 0: everything after shifts up by one
    positions = {i["id"]: i["position"] for i in mac.cache._snaps["L-big"]["items"]}
    assert positions == {"b3": 1001}
    ok, created, _ = mac.cache.write("reminder_create", {"title": "Fresh", "list_id": "L-big"}, 60)
    assert created["id"] == "new1" and "new1" in [r["id"] for r in read(mac, list_id="L-big")["reminders"]]
    assert set(mac.kicked) == {"L-big"}                                                    # every write schedules a re-read of that list to refresh positions


def test_reopening_marks_every_list_dirty(mac):
    read(mac)
    mac.cache.write("reminder_complete", {"id": "nobody", "completed": False}, 60)
    assert all(s["dirty"] for s in mac.cache._snaps.values())


def test_a_busy_mac_gives_a_clear_error_not_a_hang(mac):
    with mac.cache._mac:
        with pytest.raises(helper.HelperError, match="busy"):
            mac.cache._locked(0.1)


def test_dispatch_routes_operations(mac, monkeypatch):
    seen = []
    monkeypatch.setattr(helper, "run_op", lambda op, args, timeout=60, extra=None, internal=False: seen.append(op) or (True, {"note": 1}, ""))
    assert mac.cache.dispatch("reminders_list", {"list_id": "L-small"}, 60)[1]["reminders"][0]["id"] == "s1"
    assert mac.cache.dispatch("reminders_list", {"bogus": 1}, 60)[0] is False                # validation applies here too
    assert mac.cache.dispatch("note_folders", {}, 60) == (True, {"note": 1}, "") and seen == ["note_folders"]


def test_the_position_hint_is_added_after_validation_and_internal_scripts_are_not_remote_operations(monkeypatch):
    commands = []

    class Done:
        returncode, stdout, stderr = 0, b"{}", b""

    monkeypatch.setattr(helper.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done())
    helper.run_op("reminder_update", {"id": "a", "title": "t"}, extra={"hint": {"list_id": "L", "index": 3}})
    assert json.loads(commands[-1][-1])["hint"] == {"list_id": "L", "index": 3}
    assert helper.run_op("reminder_update", {"id": "a", "hint": {"index": 3}})[0] is False   # the server cannot smuggle one in
    assert helper.run_op("reminders_snapshot", {"list_id": "L"}, internal=False)[0] is False  # not in the fixed remote table
    assert helper.run_op("reminders_snapshot", {"list_id": "L"}, internal=True)[0] is True
    assert helper.run_op("reminder_update", {"id": "a"}, internal=True)[0] is False


def test_script_errors_are_shown_without_osascript_wrapping():
    text = "execution error: Error: several lists are named 'Work'; pass list_id (from reminders_lists) to choose one (-2700)"
    assert helper._friendly(text) == "several lists are named 'Work'; pass list_id (from reminders_lists) to choose one"
    assert helper._friendly("script.js: execution error: Error: invalid due date (-2700)") == "invalid due date"
    assert "not found" in helper._friendly("Error: Can't get object. (-1728)")
