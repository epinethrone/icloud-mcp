// Create a Notes folder. Arguments: name, account (account name; the default Notes account if omitted), parent_id (folder id from
// note_folders, to make a subfolder; wins over account). If a folder with that name already exists in the same place, it is returned
// with existed: true instead of creating a duplicate. Recently Deleted can never be created or targeted.
var RECENTLY_DELETED = ["recently deleted", "onlangs verwijderd", "zuletzt gelöscht", "récemment supprimées", "eliminadas recientemente"];
function norm(s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); }

function run(argv) {
  var a = JSON.parse(argv[0]);
  var name = String(a.name || "").replace(/\s+/g, " ").trim();
  if (!name) throw new Error("a folder name is required");
  if (RECENTLY_DELETED.indexOf(norm(name)) >= 0) throw new Error("'" + name + "' is reserved by Notes. Nothing was created.");
  var app = Application('Notes');
  var parent, where;
  if (a.parent_id) {
    parent = app.folders.byId(a.parent_id);
    where = parent.name();                                              // throws "not found" for an unknown folder id
    if (RECENTLY_DELETED.indexOf(norm(where)) >= 0) throw new Error("folders cannot be created inside Recently Deleted. Nothing was created.");
  } else {
    parent = a.account ? app.accounts.byName(a.account) : app.defaultAccount();
    where = parent.name();                                              // throws "not found" for an unknown account
  }
  var existing = parent.folders();
  for (var i = 0; i < existing.length; i++) {
    if (norm(existing[i].name()) === norm(name))
      return JSON.stringify({ id: existing[i].id(), name: existing[i].name(), in: where, existed: true });
  }
  var f = app.Folder({ name: name });
  parent.folders.push(f);
  return JSON.stringify({ id: f.id(), name: f.name(), in: where, existed: false });
}
