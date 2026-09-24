// Move one note to another folder. Arguments: id, title (the note's current title, which must match: a guard against a stale or wrong id),
// and the destination as folder_id (from note_folders, preferred) or folder (a name, refused if several folders share it).
// Never moves a note INTO Recently Deleted (that is a delete, which has its own operation). Locked notes can be moved: nothing is read.
var RECENTLY_DELETED = ["recently deleted", "onlangs verwijderd", "zuletzt gelöscht", "récemment supprimées", "eliminadas recientemente"];
function norm(s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); }

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

function findFolder(app, a) {
  if (a.folder_id) { var f = app.folders.byId(a.folder_id); f.name(); return f; }   // throws "not found" for an unknown id
  if (!a.folder) throw new Error("give the destination as folder_id (from notes_list_folders) or folder (a name). Nothing was moved.");
  var all = app.folders(), hits = [];
  for (var i = 0; i < all.length; i++) if (norm(all[i].name()) === norm(a.folder)) hits.push(all[i]);
  if (hits.length === 0) throw new Error("folder '" + a.folder + "' not found (-1728). Create it first. Nothing was moved.");
  if (hits.length > 1) throw new Error(hits.length + " folders are called '" + a.folder + "'; pass folder_id from notes_list_folders instead. Nothing was moved.");
  return hits[0];
}

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Notes');
  var n = findNote(app, a.id);
  var title = n.name(), from = null;
  try { from = n.container().name(); } catch (e) {}
  if (norm(title) !== norm(a.title))
    throw new Error("title does not match: this id belongs to a note titled '" + title + "'. Nothing was moved.");
  var dest = findFolder(app, a), to = dest.name();
  if (RECENTLY_DELETED.indexOf(norm(to)) >= 0)
    throw new Error("moving a note into Recently Deleted is a delete; use notes_delete for that. Nothing was moved.");
  if (from !== null && norm(from) === norm(to) && !a.folder_id) return JSON.stringify({ id: n.id(), title: title, from: from, to: to, moved: false, note: "already in that folder" });
  app.move(n, { to: dest });
  var id = null;
  try { id = n.id(); } catch (e2) {}                                  // moving between accounts can give the note a new id
  return JSON.stringify({ id: id, title: title, from: from, to: to, moved: true });
}
