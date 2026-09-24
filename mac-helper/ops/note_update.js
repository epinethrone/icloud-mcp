// Change the text of one note: append plain text to it, or replace its text. Arguments: id, title (the note's current title),
// expected_hash (content_hash from note_read, so a note that changed since it was read is never overwritten), text, mode
// ("append" or "replace"). Refuses locked notes, notes with attachments (rewriting their body could drop the attachments) and notes
// in Recently Deleted. Before any change the old HTML is saved to ~/Library/Application Support/icloud-mac-helper/note-backups/.
// Text is escaped, never trusted as markup. "append" keeps the existing formatting; "replace" keeps the title as the heading and
// turns the rest into the new plain text.
var RECENTLY_DELETED = ["recently deleted", "onlangs verwijderd", "zuletzt gelöscht", "récemment supprimées", "eliminadas recientemente"];
function norm(s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); }
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

// FNV-1a over the plain text: the same function note_read uses for content_hash.
function contentHash(text) {
  var h = 0x811c9dc5;
  for (var i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0; }
  return ("00000000" + h.toString(16)).slice(-8);
}

function textHtml(text) { return "<div>" + String(text).split("\n").map(function (l) { return l ? esc(l) : "<br>"; }).join("</div><div>") + "</div>"; }

function findNote(app, id) {
  var n = app.notes.byId(id);
  try { n.id(); return n; } catch (e) {}
  var folders = app.folders();
  for (var i = 0; i < folders.length; i++) {
    var c = folders[i].notes.byId(id);
    try { c.id(); return c; } catch (e2) {}
  }
  throw new Error("note not found (-1728)");
}

function backup(id, html, env) {
  var dir = env.home + "/Library/Application Support/icloud-mac-helper/note-backups";
  var name = dir + "/" + String(id).replace(/[^A-Za-z0-9]+/g, "_").slice(-60) + "-" + env.stamp + ".html";
  return env.write(dir, name, html) ? name : null;
}

function update(app, a, env) {
  if (a.mode !== "append" && a.mode !== "replace") throw new Error("mode must be 'append' or 'replace'. Nothing was changed.");
  if (!a.text) throw new Error("text is empty. Nothing was changed.");
  var n = findNote(app, a.id);
  var title = n.name();
  if (norm(title) !== norm(a.title)) throw new Error("title does not match: this id belongs to a note titled '" + title + "'. Nothing was changed.");
  var locked = false;
  try { locked = n.passwordProtected() === true; } catch (e) {}
  if (locked) throw new Error("the note is locked, so it is never changed. Nothing was changed.");
  var folder = null;
  try { folder = n.container().name(); } catch (e2) {}
  if (folder !== null && RECENTLY_DELETED.indexOf(norm(folder)) >= 0) throw new Error("the note is in Recently Deleted. Nothing was changed.");
  var attachments = 0;
  try { attachments = n.attachments().length; } catch (e3) {}
  if (attachments > 0) throw new Error("the note has " + attachments + " attachment(s); rewriting it could lose them, so it is not changed. Nothing was changed.");
  var current = contentHash(String(n.plaintext() || ""));
  if (current !== a.expected_hash) throw new Error("the note changed since it was read (content_hash " + a.expected_hash + " is now " + current + "). Read it again first. Nothing was changed.");
  var old = String(n.body() || "");
  var saved = backup(n.id(), old, env);
  if (!saved) throw new Error("could not save a backup of the note first. Nothing was changed.");
  var html = a.mode === "append" ? old + textHtml(a.text) : "<h1>" + esc(title) + "</h1>" + textHtml(a.text);
  n.body = html;
  var after = String(n.plaintext() || "");
  return { id: n.id(), title: n.name(), folder: folder, mode: a.mode, content_hash: contentHash(after), backup: saved,
           chars: after.length };
}

function run(argv) {
  var a = JSON.parse(argv[0]);
  ObjC.import("Foundation");
  var env = {
    home: ObjC.unwrap($.NSHomeDirectory()),
    stamp: new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, ""),
    write: function (dir, path, text) {
      $.NSFileManager.defaultManager.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(dir, true, $({ NSFilePosixPermissions: 448 }), null);
      return $.NSString.alloc.initWithUTF8String(text).writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null);
    }
  };
  return JSON.stringify(update(Application('Notes'), a, env));
}
