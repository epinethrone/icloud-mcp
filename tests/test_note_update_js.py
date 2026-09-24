"""ops/note_update.js run by Apple's JavaScript engine against a fake Notes: no Notes access, so no privacy prompt. macOS only."""
import json
import pathlib
import shutil
import subprocess

import pytest

OPS = pathlib.Path(__file__).parent.parent / "mac-helper" / "ops"
pytestmark = pytest.mark.skipif(not shutil.which("osascript"), reason="JXA needs macOS")

HARNESS = r"""
function fakeNote(o) {
  var n = { _id: o.id || "x-coredata://N/ICNote/p1", _name: o.name || "Shopping", _body: o.body || "<h1>Shopping</h1><div>milk</div>",
            _plain: o.plain !== undefined ? o.plain : "Shopping\nmilk", _locked: !!o.locked, _att: o.att || 0, _folder: o.folder || "Notes" };
  var note = { id: function () { return n._id; }, name: function () { return n._name; }, plaintext: function () { return n._plain; },
               body: function () { return n._body; }, passwordProtected: function () { return n._locked; },
               attachments: function () { return new Array(n._att); }, container: function () { return { name: function () { return n._folder; } }; } };
  Object.defineProperty(note, "body", { get: function () { return function () { return n._body; }; },
    set: function (v) { n._body = v; n._plain = v.replace(/<\/div><div>|<\/h1>/g, "\n").replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"); } });
  return { note: note, state: n };
}
function runCase(c) {
  var f = fakeNote(c.note || {});
  var app = { notes: { byId: function () { return f.note; } }, folders: function () { return []; } };
  var saved = [];
  var env = { home: "/tmp/h", stamp: "20260924T060000", write: function (dir, path, text) { saved.push([path, text]); return c.backupFails ? false : true; } };
  var args = c.args; if (args.expected_hash === "CURRENT") args.expected_hash = contentHash(f.state._plain);
  try { var r = update(app, args, env); return { ok: true, result: r, body: f.state._body, saved: saved }; }
  catch (e) { return { ok: false, error: String(e.message || e), body: f.state._body, saved: saved }; }
}
function run(argv) { return JSON.stringify(JSON.parse(argv[0]).map(runCase)); }
"""


def run_cases(cases):
    src = (OPS / "note_update.js").read_text().replace("function run(argv)", "function realRun(argv)") + HARNESS
    p = subprocess.run(["osascript", "-l", "JavaScript", "-e", src, json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_append_keeps_the_old_text_escapes_the_new_and_backs_up_first():
    (r,) = run_cases([{"args": {"id": "i", "title": "shopping", "expected_hash": "CURRENT", "text": "eggs <b>&\nbread", "mode": "append"}}])
    assert r["ok"], r
    assert r["body"] == "<h1>Shopping</h1><div>milk</div><div>eggs &lt;b&gt;&amp;</div><div>bread</div>"
    assert r["saved"][0][0].startswith("/tmp/h/Library/Application Support/icloud-mac-helper/note-backups/")
    assert r["saved"][0][1] == "<h1>Shopping</h1><div>milk</div>" and r["result"]["backup"] == r["saved"][0][0]


def test_replace_keeps_the_title_heading():
    (r,) = run_cases([{"args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "cheese", "mode": "replace"}}])
    assert r["ok"] and r["body"] == "<h1>Shopping</h1><div>cheese</div>"


@pytest.mark.parametrize("case, message", [
    ({"args": {"id": "i", "title": "Other", "expected_hash": "CURRENT", "text": "x", "mode": "append"}}, "title does not match"),
    ({"args": {"id": "i", "title": "Shopping", "expected_hash": "deadbeef", "text": "x", "mode": "append"}}, "changed since it was read"),
    ({"note": {"locked": True}, "args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "x", "mode": "append"}}, "locked"),
    ({"note": {"att": 2}, "args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "x", "mode": "append"}}, "attachment"),
    ({"note": {"folder": "Recently Deleted"}, "args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "x", "mode": "replace"}}, "Recently Deleted"),
    ({"args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "x", "mode": "prepend"}}, "mode must be"),
    ({"backupFails": True, "args": {"id": "i", "title": "Shopping", "expected_hash": "CURRENT", "text": "x", "mode": "append"}}, "backup"),
])
def test_every_refusal_changes_nothing(case, message):
    (r,) = run_cases([case])
    assert not r["ok"] and message in r["error"] and "Nothing was changed" in r["error"]
    assert r["body"] == "<h1>Shopping</h1><div>milk</div>"


def test_note_read_and_note_update_compute_the_same_hash():
    def hash_in(file):
        src = (OPS / file).read_text().replace("function run(argv)", "function realRun(argv)")
        src += '\nfunction run(argv) { return contentHash("Shopping\\nmilk ü 🥚"); }'
        return subprocess.run(["osascript", "-l", "JavaScript", "-e", src, "[]"], capture_output=True, text=True, timeout=30).stdout.strip()
    a, b = hash_in("note_read.js"), hash_in("note_update.js")
    assert a == b and len(a) == 8
