// Read one note found by id, as plain text. Arguments: id, max_chars (default 30000). Locked notes are reported, never read.
// FNV-1a over the plain text: note_update refuses to change a note whose hash no longer matches.
function contentHash(text) {
  var h = 0x811c9dc5;
  for (var i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0; }
  return ("00000000" + h.toString(16)).slice(-8);
}

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

function run(argv) {
  var a = JSON.parse(argv[0]);
  var app = Application('Notes');
  var n = findNote(app, a.id);
  var folder = null;
  try { folder = n.container().name(); } catch (e) {}
  var c = n.creationDate(), m = n.modificationDate();
  var head = { id: n.id(), title: n.name(), folder: folder, created: c ? c.toISOString() : null, modified: m ? m.toISOString() : null };
  var locked;                                                        // fail closed: unknown counts as locked
  try { locked = n.passwordProtected() === true; } catch (e3) { locked = true; }
  if (locked) { head.locked = true; head.text = ""; head.truncated = false; return JSON.stringify(head); }
  var text = String(n.plaintext() || ""), max = a.max_chars || 30000;
  head.locked = false;
  head.content_hash = contentHash(text);                              // of the whole text, before any truncation
  head.truncated = text.length > max;
  head.text = head.truncated ? text.substring(0, max) : text;
  return JSON.stringify(head);
}
