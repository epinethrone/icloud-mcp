// Delete a reminder found by id. Argument: id. Reminders has no trash reachable from scripts, so this is permanent.
// The helper may add an internal `hint` (list id and position); the id is verified again right before the delete.
// ---- shared: finding a reminder without scanning big lists (kept identical in every script that uses it; a test checks that)
// Reminders scans a whole list for every lookup BY ID (about 15 ms per item, so 15 s or more on a list with a thousand completed reminders),
// but reading BY POSITION is instant. So the helper passes a hint (list id and position) that is always verified against the id before use.
function props(r) {
  var p = null;
  try { p = r.properties(); } catch (e) {}
  if (p && p.id !== undefined) {
    return { id: p.id, title: p.name || "", notes: p.body || "", completed: !!p.completed, due: p.dueDate ? new Date(p.dueDate) : null, priority: p.priority || 0 };
  }
  var d = r.dueDate();
  return { id: r.id(), title: r.name() || "", notes: r.body() || "", completed: !!r.completed(), due: d ? new Date(d) : null, priority: r.priority() || 0 };
}
function locate(app, id, hint) {
  var lists = app.lists, hinted = null, i;
  if (hint && hint.list_id) {
    try { var l = lists.byId(hint.list_id); l.id(); hinted = hint.list_id; var rs = l.reminders;
      if (typeof hint.index === "number") { try { if (rs[hint.index].id() === id) return { rs: rs, idx: hint.index }; } catch (e2) {} }   // fast path
      var j = rs.id().indexOf(id);                                   // the position moved: scan this one list only
      if (j >= 0) return { rs: rs, idx: j };
    } catch (e3) {}
  }
  var ids = lists.id(), all = lists();
  for (i = 0; i < all.length; i++) {
    if (ids[i] === hinted) continue;
    var rs2 = all[i].reminders, k = rs2.id().indexOf(id);
    if (k >= 0) return { rs: rs2, idx: k };
  }
  throw new Error("reminder not found (-1728)");
}
function guard(loc, id) {                                          // re-check right before every read or write: a position can shift
  var r = loc.rs[loc.idx];
  if (r.id() !== id) throw new Error("the reminder moved while it was being changed; read the list again and retry");
  return r;
}
// ---- end shared

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Reminders');
  var loc = locate(app, a.id, a.hint);
  var cur = props(guard(loc, a.id));
  app.delete(guard(loc, a.id));
  return JSON.stringify({ deleted: cur.id, title: cur.title, position: loc.idx });
}
