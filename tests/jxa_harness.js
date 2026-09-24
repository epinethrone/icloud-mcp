// Runs one of mac-helper/ops/*.js against a FAKE JXA object model, so the script logic can be tested off a Mac.
// usage: node jxa_harness.js <script.js> '<json args>' <fixture.json>   ->  prints {"ok":true,"result":"...","state":{...}} or {"ok":false,"error":"..."}
// The fake mirrors how JXA exposes Reminders and Notes: collections are callable, have batch property methods, byId/byName and push;
// items expose properties as getter functions and accept assignment. It checks OUR logic, not Apple's behaviour.
var fs = require('fs'), vm = require('vm');
var scriptPath = process.argv[2], argJson = process.argv[3], fixture = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
var counter = 0;
function nf(msg) { return new Error("Can't get object. (-1728)" + (msg ? " " + msg : "")); }
function d(v) { return v ? new Date(v) : null; }
function def(o, k, v) { Object.defineProperty(o, k, { value: v, configurable: true, enumerable: false, writable: true }); }

function item(data, keys, extra) {
  var o = {};
  keys.forEach(function (k) {
    Object.defineProperty(o, k, { get: function () { return function () { return data[k]; }; }, set: function (v) { data[k] = v; if (o._onSet) o._onSet(); }, enumerable: true });
  });
  def(o, '_data', data);
  if (keys.indexOf('completed') >= 0 || keys.indexOf('plaintext') >= 0) def(o, 'properties', function () {
    var r = {}; keys.forEach(function (k) { r[k] = data[k]; }); return r;
  });
  if (extra) Object.keys(extra).forEach(function (k) { def(o, k, extra[k]); });
  return o;
}
function missing(keys) {
  var o = {};
  keys.forEach(function (k) { Object.defineProperty(o, k, { get: function () { return function () { throw nf(); }; } }); });
  return o;
}
var stats = { scans: 0, indexReads: 0 };
// Whole-collection reads (batch properties, byId, byName) walk every item, which is what makes big lists slow on a real Mac: count them.
// Reading one item BY POSITION does not scan, as measured on a real Mac.
function collection(getItems, keys, extra) {
  var f = function () { stats.scans++; return getItems(); };
  keys.forEach(function (k) { def(f, k, function () { stats.scans++; return getItems().map(function (i) { return i._data[k]; }); }); });
  def(f, 'byId', function (id) { stats.scans++; var m = getItems().filter(function (i) { return i._data.id === id; })[0]; return m || missing(keys); });
  def(f, 'byName', function (name) { stats.scans++; var m = getItems().filter(function (i) { return i._data.name === name; })[0]; return m || missing(keys); });
  if (extra) Object.keys(extra).forEach(function (k) { def(f, k, extra[k]); });
  return new Proxy(f, {
    get: function (target, prop) {
      if (typeof prop === 'string' && /^\d+$/.test(prop)) {
        stats.indexReads++;
        var m = getItems()[+prop];
        return m || missing(keys);
      }
      return target[prop];
    }
  });
}

// ---- Reminders
var RKEYS = ['id', 'name', 'body', 'completed', 'dueDate', 'priority'];
function buildReminders() {
  var fx = fixture.reminders || { lists: [] };
  var lists = fx.lists.map(function (l) {
    var L = { id: l.id || ("L-" + l.name), name: l.name, account: l.account || "iCloud", items: [] };
    L.items = (l.reminders || []).map(function (r) {
      var it = item({ id: r.id, name: r.name, body: r.body || "", completed: !!r.completed, dueDate: d(r.dueDate), priority: r.priority || 0 }, RKEYS, { _list: L });
      if (fx.moveOnWrite) def(it, '_onSet', function () { var i = L.items.indexOf(it); L.items.splice(i, 1); L.items.push(it); });   // like a list that re-sorts after an edit
      return it;
    });
    return L;
  });
  var listObjs = lists.map(function (L) {
    var lo = item({ id: L.id, name: L.name }, ['id', 'name']);
    def(lo, '_L', L);
    def(lo, 'container', function () { return { name: function () { return L.account; } }; });
    lo.reminders = collection(function () { return L.items; }, RKEYS, {
      push: function (r) { r._data.id = r._data.id || ("x-apple-reminder://new-" + (++counter)); L.items.push(r); r._list = L; }
    });
    return lo;
  });
  var app = {};
  app.lists = collection(function () { return listObjs; }, ['id', 'name'], {});
  def(app.lists, 'byName', function (n) { return listObjs.filter(function (x) { return x._data.name === n; })[0] || missing(['id', 'name']); });
  def(app.lists, 'byId', function (id) { return listObjs.filter(function (x) { return x._data.id === id; })[0] || missing(['id', 'name']); });
  var all = function () { var out = []; lists.forEach(function (L) { L.items.forEach(function (i) { out.push(i); }); }); return out; };
  app.reminders = collection(all, RKEYS, {});
  if (fx.topLevelById === false) def(app.reminders, 'byId', function () { return missing(RKEYS); });
  app.defaultList = function () { return listObjs.filter(function (x) { return x._data.name === (fx.defaultList || lists[0].name); })[0]; };
  if (fx.noProperties) allItemsNoProps();
  function allItemsNoProps() { lists.forEach(function (L) { L.items.forEach(function (i) { def(i, 'properties', function () { throw nf('properties'); }); }); }); }
  app.Reminder = function (props) { return item({ id: null, name: props.name, body: props.body || "", completed: false, dueDate: props.dueDate || null, priority: props.priority || 0 }, RKEYS); };
  app['delete'] = function (r) { var L = r._list; L.items.splice(L.items.indexOf(r), 1); };
  app._snapshot = function () {
    return { lists: lists.map(function (L) { return { id: L.id, name: L.name, reminders: L.items.map(function (i) { var x = i._data; return { id: x.id, name: x.name, body: x.body, completed: x.completed, dueDate: x.dueDate ? x.dueDate.toISOString() : null, priority: x.priority }; }) }; }) };
  };
  return app;
}

// ---- Notes
var NKEYS = ['id', 'name', 'plaintext', 'modificationDate', 'creationDate', 'passwordProtected'];
function stripTags(s) { var prev; do { prev = s; s = s.replace(/<[^>]*>/g, ""); } while (s !== prev); return s.replace(/</g, ""); }
function htmlToText(html) { return stripTags(html.replace(/<br>/g, "\n").replace(/<\/(h1|div)>/g, "\n")).replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\n+$/, ""); }
function buildNotes() {
  var fx = fixture.notes || { accounts: [] };
  var folders = [];
  var accounts = fx.accounts.map(function (a) {
    var A = { name: a.name, folders: [] };
    A.folders = (a.folders || []).map(function (f) {
      var F = { id: f.id, name: f.name, items: [], account: A };
      F.items = (f.notes || []).map(function (n) {
        return item({ id: n.id, name: n.name, plaintext: n.plaintext || "", body: "", modificationDate: d(n.modificationDate), creationDate: d(n.creationDate), passwordProtected: !!n.passwordProtected }, NKEYS, { _F: F });
      });
      folders.push(F);
      return F;
    });
    return A;
  });
  function newFolder(name, parentObj, A) {
    var F = { id: "x-coredata://folder/new-" + (++counter), name: name, items: [], account: A, sub: [], parentObj: parentObj };
    folders.push(F);
    return F;
  }
  function folderObj(F) {
    if (F._obj) return F._obj;
    var fo = item({ id: F.id, name: F.name }, ['id', 'name']);
    F.sub = F.sub || [];
    fo.notes = collection(function () { return F.items; }, NKEYS, { push: function (n) { n._data.id = "x-coredata://note/new-" + (++counter); n._F = F; F.items.push(n); } });
    fo.folders = collection(function () { return F.sub.map(folderObj); }, ['id', 'name'], {
      push: function (nf) { var C = newFolder(nf._data.name, fo, F.account); F.sub.push(C); nf._data.id = C.id; C._obj = nf; def(nf, '_Fd', C); nf.notes = folderObj(C).notes; }
    });
    def(fo, 'container', function () { return F.parentObj || accountObj(F.account); });
    def(fo, '_Fd', F);
    F._obj = fo;
    return fo;
  }
  function accountObj(A) {
    if (A._obj) return A._obj;
    var ao = item({ name: A.name }, ['name']);
    ao.folders = collection(function () { return A.folders.map(folderObj); }, ['id', 'name'], {
      push: function (nf) { var C = newFolder(nf._data.name, null, A); A.folders.push(C); nf._data.id = C.id; C._obj = nf; def(nf, '_Fd', C); }
    });
    A._obj = ao;
    return ao;
  }
  var app = {};
  app.accounts = collection(function () { return accounts.map(accountObj); }, ['name']);
  def(app.accounts, 'byName', function (n) { var A = accounts.filter(function (x) { return x.name === n; })[0]; return A ? accountObj(A) : missing(['name']); });
  app.defaultAccount = function () { return accountObj(accounts.filter(function (x) { return x.name === (fx.defaultAccount || accounts[0].name); })[0]); };
  app.Folder = function (props) { return item({ id: null, name: props.name }, ['id', 'name']); };
  app.move = function (n, opts) { var F = n._F, T = opts.to._Fd; F.items.splice(F.items.indexOf(n), 1); T.items.push(n); n._F = T; };
  app.folders = collection(function () { return folders.map(folderObj); }, ['id', 'name']);
  var allNotes = function () { var out = []; folders.forEach(function (F) { F.items.forEach(function (n) { out.push(n); }); }); return out; };
  app.notes = collection(allNotes, NKEYS, {});
  if (fx.topLevelById === false) def(app.notes, 'byId', function () { return missing(NKEYS); });
  allNotes().forEach(function (n) { def(n, 'container', function () { return folderObj(n._F); }); });
  app.Note = function (props) {
    var text = htmlToText(props.body || "");
    var o = item({ id: null, name: text.split("\n")[0], plaintext: text, body: props.body, modificationDate: new Date("2026-09-20T12:00:00Z"), creationDate: new Date("2026-09-20T12:00:00Z"), passwordProtected: false }, NKEYS);
    return o;
  };
  app['delete'] = function (n) { var F = n._F; F.items.splice(F.items.indexOf(n), 1); };
  app._snapshot = function () {
    return { folders: folders.map(function (F) { return { id: F.id, name: F.name, in: F.parentObj ? F.parentObj._Fd.name : F.account.name, notes: F.items.map(function (n) { var x = n._data; return { id: x.id, name: x.name, plaintext: x.plaintext, body: x.body }; }) }; }) };
  };
  return app;
}

var apps = { Reminders: buildReminders(), Notes: buildNotes() };
var ctx = { Application: function (n) { return apps[n]; }, JSON: JSON, Date: Date, Error: Error, isNaN: isNaN, String: String, Math: Math };
vm.createContext(ctx);
try {
  vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), ctx);
  var result = ctx.run([argJson]);
  process.stdout.write(JSON.stringify({ ok: true, result: result, state: { reminders: apps.Reminders._snapshot(), notes: apps.Notes._snapshot() }, stats: stats }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
}
