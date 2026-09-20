// Change a reminder found by id. Arguments: id, and any of title, notes, due (ISO 8601), clear_due (true removes the due date), priority.
// The helper may add an internal `hint` (list id and position) that makes the lookup fast on big lists.
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
  var newDue = a.clear_due === true ? null : (a.due ? parseDue(a.due) : undefined);        // validate BEFORE touching anything
  var wants = (a.title != null) + (a.notes != null) + (a.priority != null) + (a.clear_due === true || !!a.due);
  if (wants === 0) throw new Error("nothing to update");
  var loc = locate(app, a.id, a.hint);
  var cur = props(guard(loc, a.id));
  if (a.title != null) { guard(loc, a.id).name = a.title; cur.title = a.title; }
  if (a.notes != null) { guard(loc, a.id).body = a.notes; cur.notes = a.notes; }
  if (a.priority != null) { guard(loc, a.id).priority = a.priority; cur.priority = a.priority; }
  if (newDue !== undefined) { guard(loc, a.id).dueDate = newDue; cur.due = newDue; }
  return JSON.stringify({ id: cur.id, title: cur.title, notes: cur.notes, completed: cur.completed, due: cur.due ? cur.due.toISOString() : null, priority: cur.priority, position: loc.idx });
}
