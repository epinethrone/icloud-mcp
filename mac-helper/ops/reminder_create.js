// Create a reminder. Arguments: title, list (name) or list_id (preferred when names repeat), default list if both omitted, notes, due (ISO 8601), priority (0 none, 1 high, 5 medium, 9 low).
function parseDue(s) {
  if (!s) return null;
  var m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(s));
  if (!m) throw new Error("invalid due date");
  var y = +m[1], mo = +m[2], dd = +m[3];
  var probe = new Date(y, mo - 1, dd, 12, 0, 0);                       // JavaScript silently rolls 2026-02-30 over to March: refuse instead
  if (probe.getFullYear() !== y || probe.getMonth() !== mo - 1 || probe.getDate() !== dd) throw new Error("invalid due date");
  if (m[4] !== undefined && (+m[4] > 23 || +m[5] > 59)) throw new Error("invalid due date");
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(s))) return new Date(y, mo - 1, dd, 9, 0, 0);   // a bare date means 09:00 local time that day
  var d = new Date(String(s).replace(" ", "T"));                                        // no offset = local time; an offset is honoured
  if (isNaN(d.getTime())) throw new Error("invalid due date");
  return d;
}

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Reminders');
  var list;
  if (a.list_id) {
    list = app.lists.byId(a.list_id);
  } else if (a.list) {
    var names = app.lists.name(), ids = app.lists.id(), hits = [];    // list names are NOT unique (two accounts can both have "Groceries")
    for (var i = 0; i < names.length; i++) if (names[i] === a.list) hits.push(ids[i]);
    if (hits.length === 0) throw new Error("list not found (-1728): " + a.list);
    if (hits.length > 1) throw new Error("several lists are named '" + a.list + "'; pass list_id (from reminders_list_lists) to choose one");
    list = app.lists.byId(hits[0]);
  } else {
    list = app.defaultList();
  }
  var listName = list.name(), listId = list.id();                     // fails here, before anything is created, if the list does not exist
  var props = { name: a.title };
  if (a.notes) props.body = a.notes;
  if (a.priority) props.priority = a.priority;
  var due = parseDue(a.due);
  if (due) props.dueDate = due;
  var r = app.Reminder(props);
  list.reminders.push(r);
  // Only the id is read back: every read of a new reminder can cost a scan of a big list, and everything else is what we just set.
  return JSON.stringify({ id: r.id(), title: a.title, notes: a.notes || "", list: listName, list_id: listId, due: due ? due.toISOString() : null, priority: a.priority || 0 });
}
