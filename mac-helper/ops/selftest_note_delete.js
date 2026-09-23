// Used only by the self-test, to remove the note it created. Not part of the operations the server can request.
function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Notes');
  var n = app.notes.byId(a.id);
  var id = n.id();
  app.delete(n);
  return JSON.stringify({ deleted: id });
}
