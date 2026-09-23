// List Notes folders (id, name, account) without counting notes, which is slow on large libraries.
function run(argv) {
  JSON.parse(argv[0] || "{}");
  var app = Application('Notes');
  var accounts = app.accounts();
  var out = [];
  for (var i = 0; i < accounts.length; i++) {
    var acc = accounts[i], accName = acc.name();
    var folders = acc.folders;
    var ids = folders.id(), names = folders.name();
    for (var j = 0; j < ids.length; j++) out.push({ id: ids[j], name: names[j], account: accName });
  }
  return JSON.stringify(out);
}
