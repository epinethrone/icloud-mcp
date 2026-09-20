// Create a note. Arguments: title, body (plain text), folder (name; "Notes" if omitted). Notes stores HTML, so text is escaped, never trusted as markup.
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Notes');
  var folder = app.folders.byName(a.folder || "Notes");
  var folderName = folder.name();                                     // throws "not found" for an unknown folder
  var html = "<h1>" + esc(a.title) + "</h1>";
  if (a.body) html += "<div>" + String(a.body).split("\n").map(esc).join("<br>") + "</div>";
  var n = app.Note({ body: html });
  folder.notes.push(n);
  return JSON.stringify({ id: n.id(), title: n.name(), folder: folderName });
}
