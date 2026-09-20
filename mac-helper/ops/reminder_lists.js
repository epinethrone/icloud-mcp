// List the Reminders lists (id, name and account). Adapted from the reminders list script in MrGo2/icloud-mcp (MIT), see THIRD_PARTY_NOTICES.md.
// Arguments arrive as JSON in argv[0]; this operation takes none. Nothing is ever interpolated into script text.
function run(argv) {
  JSON.parse(argv[0] || "{}");                       // a malformed call fails loudly instead of being ignored
  var app = Application('Reminders');
  var lists = app.lists;
  // Names and ids in two batch calls. Counting the reminders in each list is deliberately not done: Reminders scans the whole list for it.
  var names = lists.name();
  var ids = lists.id();
  var out = [];
  for (var i = 0; i < names.length; i++) {
    var account = null;
    try { account = lists[i].container().name(); } catch (e) {}
    out.push({ id: ids[i], name: names[i], account: account });
  }
  return JSON.stringify(out);
}
