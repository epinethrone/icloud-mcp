// Move one note to Recently Deleted. Arguments: id, title (the note's current title, which must match: a guard against a stale or wrong id).
// Notes' own delete moves an iCloud note to Recently Deleted, where the user can recover it for about 30 days. This script never deletes
// a note that is ALREADY in Recently Deleted (that would be permanent) and never touches a locked note (its contents cannot be checked).
var RECENTLY_DELETED = ["recently deleted", "onlangs verwijderd", "zuletzt gelöscht", "récemment supprimées", "eliminadas recientemente"];

function findNote(app, id) {
  var n = app.notes.byId(id);
  try { n.id(); return n; } catch (e) {}
  var folders = app.folders();                                       // fall back to a per-folder lookup
  for (var i = 0; i < folders.length; i++) {
    var c = folders[i].notes.byId(id);
    try { c.id(); return c; } catch (e2) {}
  }
  throw new Error("note not found (-1728)");
}

function norm(s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); }

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Notes');
  var n = findNote(app, a.id);
  var id = n.id(), title = n.name(), folder = null;
  try { folder = n.container().name(); } catch (e) {}
  if (norm(title) !== norm(a.title))
    throw new Error("title does not match: this id belongs to a note titled '" + title + "'. Nothing was deleted.");
  if (folder !== null && RECENTLY_DELETED.indexOf(norm(folder)) >= 0)
    throw new Error("this note is already in Recently Deleted; removing it from there is permanent, so it is left to the user. Nothing was deleted.");
  var locked;                                                        // fail closed: unknown counts as locked
  try { locked = n.passwordProtected() === true; } catch (e3) { locked = true; }
  if (locked) throw new Error("this note is locked; locked notes are never deleted. Nothing was deleted.");
  app.delete(n);
  return JSON.stringify({ deleted: id, title: title, folder: folder, recoverable: "moved to Recently Deleted in Notes, recoverable for about 30 days" });
}
