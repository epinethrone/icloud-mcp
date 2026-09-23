"""The real operation scripts (mac-helper/ops/*.js) run against a fake JXA object model in Node.
This checks OUR logic (filtering, sorting, limits, null-vs-missing, dates, escaping, hostile input). Apple's real behaviour can only be
confirmed on a Mac, by the helper's self-test."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parent
OPS = ROOT.parent / "mac-helper" / "ops"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed for the JXA harness")

FIXTURE = {
    "reminders": {"defaultList": "Home", "lists": [
        {"name": "Home", "reminders": [
            {"id": "r1", "name": "Buy milk", "body": "semi-skimmed", "completed": False, "dueDate": "2026-09-22T10:00:00Z", "priority": 5},
            {"id": "r2", "name": "Call the plumber", "body": "", "completed": False, "dueDate": None, "priority": 0},
            {"id": "r3", "name": "Old thing", "body": "milk again", "completed": True, "dueDate": "2026-09-01T08:00:00Z", "priority": 0},
            {"id": "r4", "name": "Pay rent", "body": "", "completed": False, "dueDate": "2026-09-21T09:00:00Z", "priority": 1}]},
        {"name": "Work", "reminders": [{"id": "w1", "name": "Send report", "body": "Q3 numbers", "completed": False, "dueDate": "2026-09-23T15:00:00Z", "priority": 0}]}]},
    "notes": {"accounts": [
        {"name": "iCloud", "folders": [
            {"id": "f1", "name": "Notes", "notes": [
                {"id": "n1", "name": "Shopping ideas", "plaintext": "Shopping ideas\nwine, cheese", "modificationDate": "2026-09-10T10:00:00Z", "creationDate": "2026-09-01T10:00:00Z"},
                {"id": "n2", "name": "Trip plan", "plaintext": "Trip plan\nbook the ferry for Friday", "modificationDate": "2026-09-18T10:00:00Z", "creationDate": "2026-08-01T10:00:00Z"},
                {"id": "n3", "name": "Secret", "plaintext": "hidden", "modificationDate": "2026-09-05T10:00:00Z", "creationDate": "2026-09-05T10:00:00Z", "passwordProtected": True}]},
            {"id": "f2", "name": "Work", "notes": [
                {"id": "n4", "name": "Meeting notes", "plaintext": "Meeting notes\nlong " + "x" * 200, "modificationDate": "2026-09-19T10:00:00Z", "creationDate": "2026-09-19T09:00:00Z"}]}]},
        {"name": "On My Mac", "folders": [{"id": "f3", "name": "Scratch", "notes": []}]}]},
}


def run(script, args=None, fixture=None, tmp_path=None):
    fx = json.loads(json.dumps(FIXTURE if fixture is None else fixture))
    path = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"jxa-fixture-{os.getpid()}-{abs(hash(script + json.dumps(args)))}.json"
    path.write_text(json.dumps(fx))
    try:
        out = subprocess.run(["node", str(ROOT / "jxa_harness.js"), str(OPS / f"{script}.js"), json.dumps(args if args is not None else {}), str(path)],
                             capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    finally:
        path.unlink(missing_ok=True)
    assert out.stdout, out.stderr
    return json.loads(out.stdout)


def ok(script, args=None, fixture=None):
    r = run(script, args, fixture)
    assert r["ok"], r
    return json.loads(r["result"]), r["state"]


def fails(script, args, match, fixture=None):
    r = run(script, args, fixture)
    assert r["ok"] is False and match in r["error"], r
    return r


# ------------------------------------------------------------------ reminders
def run_full(script, args=None, fixture=None):
    r = run(script, args, fixture)
    assert r["ok"], r
    return json.loads(r["result"]), r["state"], r["stats"]


def big_fixture(completed=1000, open_positions=(0, 3, 999)):
    """A list shaped like a long-used shopping list: about a thousand completed reminders and a handful of open ones."""
    rems = []
    for i in range(completed + len(open_positions)):
        is_open = i in open_positions
        rems.append({"id": f"g{i}", "name": f"open {i}" if is_open else f"done {i}", "body": "", "completed": not is_open, "dueDate": None, "priority": 0})
    fx = json.loads(json.dumps(FIXTURE))
    fx["reminders"]["lists"].append({"id": "L-big", "name": "Groceries", "account": "iCloud", "reminders": rems})
    return fx


def test_reminder_lists_include_id_and_account():
    result, _ = ok("reminder_lists", {})
    assert [(x["name"], x["account"]) for x in result] == [("Home", "iCloud"), ("Work", "iCloud")]
    assert all(x["id"] for x in result)


def test_snapshot_returns_only_open_reminders_with_their_positions():
    snap, _, _ = run_full("reminders_snapshot", {"list_id": "L-Home"})
    assert snap["list"] == "Home" and snap["account"] == "iCloud" and snap["count"] == 4
    assert [(i["id"], i["position"]) for i in snap["items"]] == [("r1", 0), ("r2", 1), ("r4", 3)]              # r3 is completed and never read
    assert next(i for i in snap["items"] if i["id"] == "r1") == {"id": "r1", "title": "Buy milk", "notes": "semi-skimmed", "due": "2026-09-22T10:00:00.000Z", "priority": 5, "position": 0}
    assert next(i for i in snap["items"] if i["id"] == "r2")["due"] is None


def test_snapshot_cost_does_not_grow_with_the_number_of_completed_reminders():
    """The whole point: on a real Mac a 1000-item list took about 16 s per read. One scan (the completed flags) plus a few positional reads is the floor."""
    snap, _, stats = run_full("reminders_snapshot", {"list_id": "L-big"}, big_fixture())
    assert [i["id"] for i in snap["items"]] == ["g0", "g3", "g999"] and [i["position"] for i in snap["items"]] == [0, 3, 999]
    assert snap["count"] == 1003
    assert stats["scans"] <= 3 and stats["indexReads"] <= 10, stats                                             # not one scan per property


def test_snapshot_works_when_properties_is_unavailable_and_rejects_an_unknown_list():
    fx = json.loads(json.dumps(FIXTURE)); fx["reminders"]["noProperties"] = True
    snap, _, _ = run_full("reminders_snapshot", {"list_id": "L-Home"}, fx)
    assert [i["title"] for i in snap["items"]] == ["Buy milk", "Call the plumber", "Pay rent"]
    fails("reminders_snapshot", {"list_id": "L-nope"}, "-1728")


def test_create_defaults_to_the_default_list_and_stores_hostile_text_literally():
    evil = '"); Application("Reminders").delete(x); (\'\n${1+1} </h1> \\'
    created, state = ok("reminder_create", {"title": evil, "notes": evil, "priority": 9})
    assert created["list"] == "Home" and created["list_id"] == "L-Home" and created["id"].startswith("x-apple-reminder://")
    stored = [r for l in state["reminders"]["lists"] if l["name"] == "Home" for r in l["reminders"] if r["id"] == created["id"]][0]
    assert stored["name"] == evil and stored["body"] == evil and stored["priority"] == 9
    assert len(state["reminders"]["lists"][0]["reminders"]) == 5                                                 # one added, nothing deleted


def test_create_in_a_named_list_with_due_dates():
    _, state = ok("reminder_create", {"title": "A", "list": "Work", "due": "2026-09-30"})
    work = [l for l in state["reminders"]["lists"] if l["name"] == "Work"][0]["reminders"]
    assert work[-1]["dueDate"] == "2026-09-30T09:00:00.000Z"                                                     # a bare date means 09:00 local (UTC in this test)
    assert ok("reminder_create", {"title": "B", "due": "2026-09-30T15:30:00+02:00"})[0]["due"] == "2026-09-30T13:30:00.000Z"
    assert ok("reminder_create", {"title": "C", "due": "2026-09-30T15:30:00"})[0]["due"] == "2026-09-30T15:30:00.000Z"
    for bad in ("2026-13-45", "2026-02-30", "2026-09-31", "2026-09-21T25:00:00", "2026-09-21T10:61:00", "garbage"):
        fails("reminder_create", {"title": "D", "due": bad}, "invalid due date")
        fails("reminder_update", {"id": "r1", "due": bad}, "invalid due date")
    assert ok("reminder_create", {"title": "leap", "due": "2028-02-29"})[0]["due"] == "2028-02-29T09:00:00.000Z"          # a real leap day is fine
    fails("reminder_create", {"title": "not leap", "due": "2026-02-29"}, "invalid due date")
    fails("reminder_create", {"title": "E", "list": "Nope"}, "-1728")


def test_duplicate_list_names_are_an_error_until_a_list_id_is_given():
    fx = json.loads(json.dumps(FIXTURE))
    fx["reminders"]["lists"].append({"id": "L-work2", "name": "Work", "account": "Exchange", "reminders": []})
    fails("reminder_create", {"title": "x", "list": "Work"}, "several lists are named 'Work'", fx)
    created, state = ok("reminder_create", {"title": "x", "list_id": "L-work2"}, fx)
    assert created["list_id"] == "L-work2" and created["list"] == "Work"
    second = [l for l in state["reminders"]["lists"] if l["id"] == "L-work2"][0]["reminders"]
    assert [r["name"] for r in second] == ["x"]                                                                # went to the chosen one, not the first
    fails("reminder_create", {"title": "x", "list_id": "L-nope"}, "-1728", fx)


def test_update_changes_only_what_is_given_and_clear_due_removes_the_date():
    _, state = ok("reminder_update", {"id": "r1", "title": "Buy oat milk"})
    r1 = state["reminders"]["lists"][0]["reminders"][0]
    assert (r1["name"], r1["body"], r1["dueDate"], r1["priority"]) == ("Buy oat milk", "semi-skimmed", "2026-09-22T10:00:00.000Z", 5)
    _, state = ok("reminder_update", {"id": "r1", "clear_due": True})
    assert state["reminders"]["lists"][0]["reminders"][0]["dueDate"] is None
    result, state = ok("reminder_update", {"id": "r1", "due": "2026-10-01T08:00:00Z", "notes": "", "priority": 0})
    r1 = state["reminders"]["lists"][0]["reminders"][0]
    assert (r1["dueDate"], r1["body"], r1["priority"]) == ("2026-10-01T08:00:00.000Z", "", 0)                   # '' clears the notes, 0 clears the priority
    assert result["due"] == "2026-10-01T08:00:00.000Z" and result["notes"] == "" and result["title"] == "Buy milk"
    fails("reminder_update", {"id": "r1"}, "nothing to update")
    fails("reminder_update", {"id": "missing", "title": "x"}, "not found")


def test_a_correct_hint_reaches_the_reminder_without_scanning_the_big_list():
    fx = big_fixture()
    result, state, stats = run_full("reminder_update", {"id": "g999", "title": "renamed", "hint": {"list_id": "L-big", "index": 999}}, fx)
    assert result["title"] == "renamed" and result["position"] == 999
    assert stats["scans"] <= 2, stats                                                                          # the list lookup only, never the 1000 reminders
    big = [l for l in state["reminders"]["lists"] if l["id"] == "L-big"][0]["reminders"]
    assert big[999]["name"] == "renamed" and big[998]["name"] == "done 998"


def test_a_wrong_hint_is_never_trusted_and_falls_back_to_the_right_reminder():
    fx = big_fixture()
    for hint in ({"list_id": "L-big", "index": 5}, {"list_id": "L-big", "index": 5000}, {"list_id": "L-gone", "index": 0}, {"list_id": "L-Home", "index": 999}, {}):
        result, state, _ = run_full("reminder_update", {"id": "g3", "title": "right one", "hint": hint}, fx)
        assert result["id"] == "g3" and result["position"] == 3
        names = [r["name"] for l in state["reminders"]["lists"] for r in l["reminders"] if r["name"] == "right one"]
        assert names == ["right one"]                                                                          # exactly one changed, and it is the right one
    fails("reminder_update", {"id": "g3", "title": "x", "hint": {"list_id": "L-big", "index": 3}}, "not found", FIXTURE)   # unknown id: error, nothing edited


def test_an_edit_stops_if_the_reminder_moves_part_way_through():
    fx = json.loads(json.dumps(FIXTURE)); fx["reminders"]["moveOnWrite"] = True
    r = run("reminder_update", {"id": "r1", "title": "a", "notes": "b"}, fx)                                      # the first write moves r1 to the end of its list
    assert r["ok"] is False and "moved while it was being changed" in r["error"]
    result, state, _ = run_full("reminder_complete", {"id": "r1", "hint": {"list_id": "L-Home", "index": 0}}, fx)   # one write only: fine
    assert result["completed"] is True and result["title"] == "Buy milk"                                        # title read BEFORE the item moved


def test_complete_and_uncomplete_and_delete():
    assert ok("reminder_complete", {"id": "r2"})[0]["completed"] is True
    assert ok("reminder_complete", {"id": "r3", "completed": False})[0]["completed"] is False
    deleted, state = ok("reminder_delete", {"id": "r2"})
    assert deleted["deleted"] == "r2" and deleted["title"] == "Call the plumber"
    assert "r2" not in [r["id"] for l in state["reminders"]["lists"] for r in l["reminders"]]
    fails("reminder_delete", {"id": "no-such-reminder"}, "not found")


def test_delete_with_a_hint_removes_only_the_named_reminder():
    fx = big_fixture()
    deleted, state, stats = run_full("reminder_delete", {"id": "g999", "hint": {"list_id": "L-big", "index": 999}}, fx)
    assert deleted == {"deleted": "g999", "title": "open 999", "position": 999} and stats["scans"] <= 2
    big = [l for l in state["reminders"]["lists"] if l["id"] == "L-big"][0]["reminders"]
    assert len(big) == 1002 and "g999" not in [r["id"] for r in big]
    result = run("reminder_delete", {"id": "g0", "hint": {"list_id": "L-big", "index": 999}}, big_fixture())    # the hint points at another reminder
    assert result["ok"]                                                                                        # falls back, deletes g0 (never g999)
    big = [l for l in result["state"]["reminders"]["lists"] if l["id"] == "L-big"][0]["reminders"]
    assert "g0" not in [r["id"] for r in big] and "g999" in [r["id"] for r in big]


def test_the_shared_lookup_code_is_identical_in_every_script_that_uses_it():
    import re
    blocks = {}
    for f in OPS.glob("*.js"):
        m = re.search(r"// ---- shared:.*?// ---- end shared", f.read_text(), re.S)
        if m:
            blocks[f.name] = m.group(0)
    assert set(blocks) == {"reminder_update.js", "reminder_complete.js", "reminder_delete.js", "reminders_snapshot.js"}
    assert len(set(blocks.values())) == 1


# ------------------------------------------------------------------ notes
def test_note_folders_span_accounts():
    result, _ = ok("note_folders")
    assert [(f["name"], f["account"]) for f in result] == [("Notes", "iCloud"), ("Work", "iCloud"), ("Scratch", "On My Mac")]


def test_note_listing_is_newest_first_and_can_filter():
    assert [n["title"] for n in ok("notes_list")[0]] == ["Meeting notes", "Trip plan", "Shopping ideas", "Secret"]
    assert [n["title"] for n in ok("notes_list", {"folder": "Work"})[0]] == ["Meeting notes"]
    assert [n["title"] for n in ok("notes_list", {"query": "TRIP"})[0]] == ["Trip plan"]
    assert len(ok("notes_list", {"limit": 2})[0]) == 2
    fails("notes_list", {"folder": "Nope"}, "-1728")


def test_body_search_is_opt_in_and_returns_a_snippet():
    assert ok("notes_list", {"query": "ferry"})[0] == []                                                        # titles only by default
    found = ok("notes_list", {"query": "ferry", "search_body": True})[0]
    assert [n["title"] for n in found] == ["Trip plan"] and "ferry" in found[0]["snippet"]
    assert all(len(n["snippet"]) <= 200 for n in ok("notes_list", {"query": "note", "search_body": True})[0] if "snippet" in n)


def test_reading_a_note_returns_plain_text_truncates_and_never_reads_locked_notes():
    note = ok("note_read", {"id": "n2"})[0]
    assert note["text"].startswith("Trip plan") and note["folder"] == "Notes" and note["locked"] is False and note["truncated"] is False
    short = ok("note_read", {"id": "n4", "max_chars": 20})[0]
    assert len(short["text"]) == 20 and short["truncated"] is True
    locked = ok("note_read", {"id": "n3"})[0]
    assert locked["locked"] is True and locked["text"] == "" and "hidden" not in json.dumps(locked)
    fails("note_read", {"id": "nope"}, "not found")


def test_creating_a_note_escapes_html_and_keeps_line_breaks():
    evil = "<script>alert(1)</script> & </h1><h1>x"
    created, state = ok("note_create", {"title": evil, "body": "line one\nline <two> & three"})
    assert created["folder"] == "Notes"
    stored = [n for f in state["notes"]["folders"] if f["name"] == "Notes" for n in f["notes"] if n["id"] == created["id"]][0]
    assert "<script>" not in stored["body"] and "&lt;script&gt;" in stored["body"] and "line one<br>line &lt;two&gt; &amp; three" in stored["body"]
    assert stored["body"].startswith("<h1>") and stored["body"].count("<h1>") == 1
    ok("note_create", {"title": "In Work", "folder": "Work"})
    fails("note_create", {"title": "x", "folder": "Nope"}, "-1728")


def test_the_selftest_only_note_delete_removes_exactly_one_note():
    _, state = ok("selftest_note_delete", {"id": "n1"})
    assert [n["id"] for f in state["notes"]["folders"] for n in f["notes"]] == ["n2", "n3", "n4"]


def _note_ids(state):
    return [n["id"] for f in state["notes"]["folders"] for n in f["notes"]]


def test_note_delete_needs_the_matching_title_and_removes_exactly_one_note():
    deleted, state = ok("note_delete", {"id": "n1", "title": "  shopping   IDEAS "})           # whitespace and case do not matter
    assert deleted["deleted"] == "n1" and deleted["title"] == "Shopping ideas" and "30 days" in deleted["recoverable"]
    assert _note_ids(state) == ["n2", "n3", "n4"]
    r = fails("note_delete", {"id": "n2", "title": "Shopping ideas"}, "title does not match")  # right title, wrong id
    assert "Trip plan" in r["error"]
    fails("note_delete", {"id": "nope", "title": "x"}, "not found")


def test_note_delete_never_touches_locked_notes_or_recently_deleted():
    fails("note_delete", {"id": "n3", "title": "Secret"}, "locked")
    fx = json.loads(json.dumps(FIXTURE))
    fx["notes"]["accounts"][0]["folders"].append({"id": "f9", "name": "Recently Deleted", "notes": [
        {"id": "d1", "name": "Gone soon", "plaintext": "Gone soon", "modificationDate": "2026-09-01T10:00:00Z", "creationDate": "2026-09-01T10:00:00Z"}]})
    r = fails("note_delete", {"id": "d1", "title": "Gone soon"}, "already in Recently Deleted", fixture=fx)
    fx["notes"]["accounts"][0]["folders"][-1]["name"] = "Onlangs verwijderd"                     # Dutch system language
    fails("note_delete", {"id": "d1", "title": "Gone soon"}, "already in Recently Deleted", fixture=fx)


def _folders(state):
    return {f["name"]: f for f in state["notes"]["folders"]}


def test_creating_a_folder_goes_to_the_default_account_and_never_duplicates():
    made, state = ok("note_folder_create", {"name": "  Recipes  "})
    assert made["name"] == "Recipes" and made["in"] == "iCloud" and made["existed"] is False and _folders(state)["Recipes"]["in"] == "iCloud"
    again, state = ok("note_folder_create", {"name": "work"})                                   # same name, other case: existing one returned
    assert again["existed"] is True and again["id"] == "f2" and [f["name"] for f in state["notes"]["folders"]].count("Work") == 1
    other, _ = ok("note_folder_create", {"name": "Drafts", "account": "On My Mac"})
    assert other["in"] == "On My Mac"
    fails("note_folder_create", {"name": "Drafts", "account": "Nope"}, "-1728")
    fails("note_folder_create", {"name": "Recently Deleted"}, "reserved")
    fails("note_folder_create", {"name": "   "}, "name is required")


def test_creating_a_subfolder_inside_an_existing_folder():
    sub, state = ok("note_folder_create", {"name": "2026", "parent_id": "f2"})
    assert sub["in"] == "Work" and _folders(state)["2026"]["in"] == "Work"
    fails("note_folder_create", {"name": "x", "parent_id": "nope"}, "-1728")


def test_moving_a_note_needs_the_matching_title_and_a_clear_destination():
    moved, state = ok("note_move", {"id": "n1", "title": "shopping ideas", "folder": "Work"})
    assert moved["moved"] is True and moved["from"] == "Notes" and moved["to"] == "Work"
    assert "n1" in [n["id"] for n in _folders(state)["Work"]["notes"]] and "n1" not in [n["id"] for n in _folders(state)["Notes"]["notes"]]
    by_id, _ = ok("note_move", {"id": "n2", "title": "Trip plan", "folder_id": "f3"})
    assert by_id["to"] == "Scratch"
    r = fails("note_move", {"id": "n2", "title": "Wrong", "folder": "Work"}, "title does not match")
    assert "Trip plan" in r["error"]
    fails("note_move", {"id": "n2", "title": "Trip plan"}, "folder_id")                           # no destination at all
    fails("note_move", {"id": "n2", "title": "Trip plan", "folder": "Nope"}, "not found")
    locked, _ = ok("note_move", {"id": "n3", "title": "Secret", "folder": "Work"})                # moving reads nothing, so locked is fine
    assert locked["moved"] is True
    same, _ = ok("note_move", {"id": "n4", "title": "Meeting notes", "folder": "Work"})
    assert same["moved"] is False


def test_moving_never_targets_recently_deleted_and_refuses_ambiguous_names():
    fx = json.loads(json.dumps(FIXTURE))
    fx["notes"]["accounts"][0]["folders"].append({"id": "f9", "name": "Recently Deleted", "notes": []})
    fx["notes"]["accounts"][1]["folders"].append({"id": "f8", "name": "Work", "notes": []})         # a second "Work", on another account
    fails("note_move", {"id": "n1", "title": "Shopping ideas", "folder": "Recently Deleted"}, "use notes_delete", fixture=fx)
    fails("note_move", {"id": "n1", "title": "Shopping ideas", "folder_id": "f9"}, "use notes_delete", fixture=fx)
    fails("note_move", {"id": "n1", "title": "Shopping ideas", "folder": "Work"}, "pass folder_id", fixture=fx)
    ok("note_move", {"id": "n1", "title": "Shopping ideas", "folder_id": "f8"}, fixture=fx)
