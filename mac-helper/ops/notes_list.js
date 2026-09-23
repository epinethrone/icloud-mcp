// List or search notes, newest first. Arguments: folder (name), query (text), search_body (bool: also search the note text; slower), limit.
// Adapted in part from the notes scripts in MrGo2/icloud-mcp (MIT), see THIRD_PARTY_NOTICES.md.
function run(argv) {
  var a = JSON.parse(argv[0] || "{}");
  var app = Application('Notes');
  var limit = a.limit || 25;
  var query = a.query ? String(a.query).toLowerCase() : null;
  var folders = a.folder ? [app.folders.byName(a.folder)] : app.folders();
  var out = [];
  for (var i = 0; i < folders.length; i++) {
    var folder = folders[i];
    var folderName = folder.name();                   // throws "not found" for an unknown folder name
    var notes = folder.notes;
    var ids = notes.id(), names = notes.name(), mods = notes.modificationDate(), cres = notes.creationDate();
    var texts = (query && a.search_body === true) ? notes.plaintext() : null;
    for (var j = 0; j < ids.length; j++) {
      var title = names[j] || "";
      var snippet = null;
      if (query) {
        var inTitle = title.toLowerCase().indexOf(query) !== -1;
        var inBody = texts ? String(texts[j] || "").toLowerCase().indexOf(query) !== -1 : false;
        if (!inTitle && !inBody) continue;
        if (texts) snippet = String(texts[j] || "").substring(0, 200);
      }
      var row = { id: ids[j], title: title, folder: folderName, created: cres[j] ? cres[j].toISOString() : null, modified: mods[j] ? mods[j].toISOString() : null };
      if (snippet !== null) row.snippet = snippet;
      out.push(row);
    }
  }
  out.sort(function (x, y) { return new Date(y.modified || 0) - new Date(x.modified || 0); });
  return JSON.stringify(out.slice(0, limit));
}
