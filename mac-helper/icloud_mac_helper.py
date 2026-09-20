#!/usr/bin/env python3
"""icloud-mac-helper: lets the icloud-mcp server use Reminders and Notes on this Mac.

It connects OUT to the server's private HTTPS port and asks for work. It opens no listening port. It performs only the fixed
operations in OPS, each backed by a static script in ops/ that receives its arguments as JSON in argv (never as script text).
The server is identified by a pinned certificate fingerprint and authenticated with a bearer token.

Portions of the ops/ scripts are adapted from MrGo2/icloud-mcp (MIT), see THIRD_PARTY_NOTICES.md.
Only the Python standard library is used, so it runs on the python3 that ships with the macOS command line tools.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
OPS_DIR = os.path.join(HERE, "ops")
DEFAULT_CONFIG = os.path.expanduser("~/.config/icloud-mac-helper/config.json")
MAX_OUTPUT = 2 * 1024 * 1024        # largest script result accepted
MAX_RESPONSE = 1024 * 1024          # largest server response accepted

# The fixed operations, identical to the server's table (tests keep them in sync): name -> {argument: (type, required, max)}.
OPS = {
    # Reminders
    "reminder_lists": {},
    "reminders_list": {"list": ("str", False, 200), "list_id": ("str", False, 200), "query": ("str", False, 200), "refresh": ("bool", False, 0),
                       "limit": ("int", False, 200)},
    "reminder_create": {"title": ("str", True, 500), "list": ("str", False, 200), "list_id": ("str", False, 200), "notes": ("str", False, 20000), "due": ("iso", False, 40),
                        "priority": ("int", False, 9)},
    "reminder_update": {"id": ("str", True, 500), "title": ("str", False, 500), "notes": ("str", False, 20000), "due": ("iso", False, 40),
                        "clear_due": ("bool", False, 0), "priority": ("int", False, 9)},
    "reminder_complete": {"id": ("str", True, 500), "completed": ("bool", False, 0)},
    "reminder_delete": {"id": ("str", True, 500)},
    # Notes
    "note_folders": {},
    "notes_list": {"folder": ("str", False, 200), "query": ("str", False, 200), "search_body": ("bool", False, 0), "limit": ("int", False, 100)},
    "note_read": {"id": ("str", True, 500), "max_chars": ("int", False, 100000)},
    "note_create": {"title": ("str", True, 500), "body": ("str", False, 100000), "folder": ("str", False, 200)},
}
OP_FILES = {
    "reminder_lists": "reminder_lists.js", "reminder_create": "reminder_create.js",            # reminders_list is answered from the cache, see ReminderCache
    "reminder_update": "reminder_update.js", "reminder_complete": "reminder_complete.js", "reminder_delete": "reminder_delete.js",
    "note_folders": "note_folders.js", "notes_list": "notes_list.js", "note_read": "note_read.js", "note_create": "note_create.js",
}

# Scripts the helper runs for its own cache; the server can never ask for these.
INTERNAL_FILES = {"reminders_snapshot": "reminders_snapshot.js"}
CACHE_TTL = 300       # seconds a big list's cached reminders are served before a background refresh is started
CHEAP_SCAN = 3.0      # a list that scans faster than this (seconds) is simply re-read live, at most every LIVE_MAX_AGE seconds
LIVE_MAX_AGE = 10
LISTS_TTL = 60        # how long the list of Reminders lists is reused
REFRESH_DEBOUNCE = 2.0  # wait this long after a write before re-reading its list, so a burst of writes causes one re-read

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def _iso_ok(value):
    """A well-formed ISO 8601 date or date-time that is also a REAL date and time (identical to the server's check)."""
    m = _ISO.match(value)
    if not m:
        return False
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    if m.group(4) is not None and not (int(m.group(4)) <= 23 and int(m.group(5)) <= 59 and (m.group(6) is None or int(m.group(6)) <= 59)):
        return False
    off = m.group(7)
    return not (off and off != "Z" and (int(off[1:3]) > 23 or int(off[-2:]) > 59))


class HelperError(Exception):
    pass


def validate_args(op, args):
    spec = OPS.get(op)
    if spec is None:
        raise HelperError("unknown operation %r" % (op,))
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise HelperError("arguments must be an object")
    clean = {}
    for key, value in args.items():
        if key not in spec:
            raise HelperError("operation %s does not take %r" % (op, key))
        kind, _required, limit = spec[key]
        if value is None:
            clean[key] = None
        elif kind == "str":
            if not isinstance(value, str) or len(value) > limit:
                raise HelperError("%s must be text of at most %d characters" % (key, limit))
            clean[key] = value
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise HelperError("%s must be a whole number between 0 and %d" % (key, limit))
            clean[key] = value
        elif kind == "bool":
            if not isinstance(value, bool):
                raise HelperError("%s must be true or false" % key)
            clean[key] = value
        elif kind == "iso":
            if not isinstance(value, str) or len(value) > 40 or not _iso_ok(value):
                raise HelperError("%s must be an ISO 8601 date or date-time" % key)
            clean[key] = value
        else:
            raise HelperError("unsupported argument type %s" % kind)
    missing = [k for k, (_t, required, _m) in spec.items() if required and clean.get(k) is None]
    if missing:
        raise HelperError("operation %s needs: %s" % (op, ", ".join(missing)))
    return clean


def _friendly(stderr):
    text = stderr.strip()
    if "-1743" in text or "Not authorized" in text:
        return ("macOS has not allowed this helper to control the app. Open System Settings > Privacy & Security > Automation and "
                "enable it for the helper (python3 / osascript), then try again.")
    if "-1728" in text or "-1719" in text:
        return "the requested list, folder or item was not found"
    # Our own scripts raise readable errors ("several lists are named ...", "invalid due date"): show that text without osascript's wrapping.
    last = text.splitlines()[-1] if text else ""
    m = re.search(r"execution error: (?:Error: )?(.+?)(?: \(-?\d+\))?\s*$", last)
    return (m.group(1) if m else text)[:300] or "the script failed"


def explain(e):
    """Turn a low-level network error into something a person can act on."""
    if isinstance(e, OSError) and getattr(e, "errno", None) in (65, 113):        # EHOSTUNREACH on macOS / Linux
        return ("%s. On macOS this usually means the local network is blocked for this program (macOS does that to third-party Python). "
                "Use Apple's own /usr/bin/python3 (install it with: xcode-select --install), or allow it in System Settings > Privacy & "
                "Security > Local Network. Also check the server address and that this Mac is on the home network or VPN." % e)
    return str(e)


def build_command(op, args):
    """The exact command line: a static script file plus ONE JSON argument. No -e, no generated source."""
    return ["osascript", "-l", "JavaScript", os.path.join(OPS_DIR, OP_FILES.get(op) or INTERNAL_FILES[op]), json.dumps(args, separators=(",", ":"))]


def run_op(op, args, timeout=60, extra=None, internal=False):
    """Run one operation. Returns (ok, result, error). The child is killed if it exceeds the timeout.
    `extra` is added AFTER validation and only by the helper itself (the position hint); `internal` runs one of the helper's own scripts."""
    if internal:
        if op not in INTERNAL_FILES:
            return False, None, "unknown operation"
        clean = dict(args)
    else:
        if op not in OPS or op not in OP_FILES:
            return False, None, "unknown operation"
        try:
            clean = validate_args(op, args)
        except HelperError as e:
            return False, None, str(e)
    if extra:
        clean = dict(clean, **extra)
    try:
        proc = subprocess.run(build_command(op, clean), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, "the script did not finish within %ds and was stopped" % timeout
    except FileNotFoundError:
        return False, None, "osascript was not found (this helper only runs on macOS)"
    if proc.returncode != 0:
        return False, None, _friendly(proc.stderr.decode("utf-8", "replace"))
    if len(proc.stdout) > MAX_OUTPUT:
        return False, None, "the result was too large"
    try:
        return True, json.loads(proc.stdout.decode("utf-8")), ""
    except ValueError:
        return False, None, "the script returned something that is not JSON"


# ---------------------------------------------------------------------------
# Reminders cache
#
# Measured on a real Mac: Reminders scans a WHOLE list for almost every request (about 15 ms per item, so 16 s for a list holding a thousand
# completed reminders), even to return four open ones. Reading one item by POSITION is instant. So this cache
#   * reads each list once (one scan for the completed flags, then only the open reminders by position) and keeps only the ACTIVE reminders,
#   * re-reads small lists live (they take milliseconds) and big lists in the background, and says how old cached data is,
#   * remembers each reminder's position so edits go straight to it (the scripts verify the id first and fall back to a scan if it moved).
# Only one Reminders script runs at a time (Reminders handles requests one by one anyway, and it keeps the queue fair).
# ---------------------------------------------------------------------------
class ReminderCache(object):
    def __init__(self, call=None, clock=time.time, debounce=None, background=True):
        self._call_fn = call or (lambda op, args, timeout, extra=None, internal=False: run_op(op, args, timeout, extra, internal))
        self._clock = clock
        self._debounce = REFRESH_DEBOUNCE if debounce is None else debounce
        self._background = background
        self._state = threading.RLock()       # guards the dictionaries below
        self._mac = threading.Lock()          # one Reminders script at a time
        self._lists = []
        self._lists_at = 0.0
        self._snaps = {}                      # list id -> {"items", "count", "at", "took", "name", "account", "dirty"}
        self._bg = set()                      # list ids being re-read in the background

    # -- running scripts (serialised) ------------------------------------------------------------------------------------------------
    def _locked(self, wait):
        if not self._mac.acquire(timeout=max(0.1, wait)):
            raise HelperError("Reminders is busy re-reading a large list; try again in a few seconds")

    def _script(self, op, args, timeout, extra=None, internal=False, wait=30):
        started = self._clock()
        self._locked(wait)
        try:
            left = max(5, int(timeout - (self._clock() - started)))
            return self._call_fn(op, args, left, extra, internal)
        finally:
            self._mac.release()

    # -- lists -----------------------------------------------------------------------------------------------------------------------
    def lists(self, force=False):
        with self._state:
            if self._lists and not force and self._clock() - self._lists_at < LISTS_TTL:
                return list(self._lists)
        ok, result, error = self._script("reminder_lists", {}, 30)
        if not ok:
            with self._state:
                if self._lists:
                    return list(self._lists)                # a stale list of lists beats none
            raise HelperError(error)
        with self._state:
            self._lists, self._lists_at = result, self._clock()
            return list(result)

    # -- snapshots -------------------------------------------------------------------------------------------------------------------
    def refresh(self, list_id, min_age=None, timeout=60, wait=30):
        """Re-read one list. With min_age, does nothing if a snapshot that fresh (and not dirty) already exists (another caller may have
        just made it while this one waited for its turn)."""
        started = self._clock()
        self._locked(wait)
        try:
            with self._state:
                snap = self._snaps.get(list_id)
            if min_age is not None and snap is not None and not snap.get("dirty") and self._clock() - snap["at"] < min_age:
                return snap
            t0 = self._clock()
            ok, result, error = self._call_fn("reminders_snapshot", {"list_id": list_id}, max(5, int(timeout - (t0 - started))), None, True)
            if not ok:
                raise HelperError(error)
            snap = {"items": result["items"], "count": result["count"], "at": self._clock(), "took": self._clock() - t0,
                    "name": result["list"], "account": result.get("account"), "dirty": False}
            with self._state:
                self._snaps[list_id] = snap
            return snap
        finally:
            self._mac.release()

    def _kick(self, list_id, delay=0.0):
        """Re-read a list in the background (at most one at a time per list)."""
        if not self._background:
            return
        with self._state:
            if list_id in self._bg:
                return
            self._bg.add(list_id)

        def work():
            try:
                if delay:
                    time.sleep(delay)
                self.refresh(list_id, timeout=120, wait=120)
            except HelperError as e:
                print("reminders: background refresh failed: %s" % e, file=sys.stderr, flush=True)
            finally:
                with self._state:
                    self._bg.discard(list_id)
        threading.Thread(target=work, daemon=True).start()

    def prewarm(self):
        """Read every list once at start-up, in the background, so the first request is usually instant."""
        def work():
            try:
                for lst in self.lists():
                    self.refresh(lst["id"], min_age=CACHE_TTL, timeout=120, wait=300)
            except HelperError as e:
                print("reminders: could not pre-load: %s" % e, file=sys.stderr, flush=True)
        threading.Thread(target=work, daemon=True).start()

    # -- reading ---------------------------------------------------------------------------------------------------------------------
    def read(self, args, budget=45):
        want_id, want_name = args.get("list_id"), args.get("list")
        lists = self.lists()
        if want_id:
            targets = [x for x in lists if x["id"] == want_id]
        elif want_name:
            targets = [x for x in lists if x["name"] == want_name]
        else:
            targets = lists
        if (want_id or want_name) and not targets:
            raise HelperError("list not found: use reminders_lists to see the names and ids")
        force = args.get("refresh") is True
        if force and not (want_id or want_name):
            raise HelperError("refresh=true needs list or list_id (re-reading every list can take a minute or more)")
        deadline = self._clock() + budget
        items, cached = [], []
        for lst in targets:
            with self._state:
                snap = self._snaps.get(lst["id"])
            age = None if snap is None else self._clock() - snap["at"]
            if snap is None or force:
                sync, background = True, False
            elif snap["took"] <= CHEAP_SCAN:
                sync, background = (age > LIVE_MAX_AGE or snap.get("dirty")), False
            else:
                sync, background = False, (age > CACHE_TTL or snap.get("dirty"))
            if sync:
                left = deadline - self._clock()
                if left <= 1:
                    raise HelperError("still loading your reminders (a large list takes a while the first time); try again in a minute")
                try:
                    snap = self.refresh(lst["id"], min_age=None if force else LIVE_MAX_AGE, timeout=left, wait=left)
                except HelperError as e:
                    if snap is None:
                        raise
                    print("reminders: using cached data for a list that could not be re-read: %s" % e, file=sys.stderr, flush=True)
                age = self._clock() - snap["at"]
            elif background:
                self._kick(lst["id"])
            if not sync and age is not None and age > 15:
                cached.append({"list": lst["name"], "list_id": lst["id"], "age_seconds": int(age), "refreshing": bool(background)})
            for it in snap["items"]:
                items.append({"id": it["id"], "title": it["title"], "notes": it["notes"], "due": it["due"], "priority": it["priority"],
                              "list": lst["name"], "list_id": lst["id"], "account": lst.get("account")})
        query = (args.get("query") or "").lower()
        if query:
            items = [i for i in items if query in i["title"].lower() or query in (i["notes"] or "").lower()]
        items.sort(key=lambda i: (i["due"] is None, i["due"] or ""))
        out = {"reminders": items[: args.get("limit") or 50]}
        if cached:
            out["cached"] = cached
        return out

    # -- writing ---------------------------------------------------------------------------------------------------------------------
    def _find(self, rid):
        with self._state:
            for list_id, snap in self._snaps.items():
                for it in snap["items"]:
                    if it["id"] == rid:
                        return list_id, snap, it
        return None, None, None

    def write(self, op, args, seconds):
        try:
            clean = validate_args(op, args)
        except HelperError as e:
            return False, None, str(e)
        extra, list_id = None, None
        if op != "reminder_create":
            list_id, _snap, it = self._find(clean["id"])
            if it is not None and it.get("position") is not None:
                extra = {"hint": {"list_id": list_id, "index": it["position"]}}
            elif list_id:
                extra = {"hint": {"list_id": list_id}}
        try:
            ok, result, error = self._script(op, clean, seconds, extra, wait=max(5, seconds / 2.0))
        except HelperError as e:
            return False, None, str(e)
        if ok and isinstance(result, dict):
            position = result.pop("position", None)
            self._patch(op, clean, result, list_id, position)
        return ok, result, error

    def _patch(self, op, clean, result, list_id, position):
        """Keep the cache truthful after our own write, then re-read the affected list shortly."""
        with self._state:
            snap = self._snaps.get(list_id) if list_id else None
            if op == "reminder_update" and snap is not None:
                for it in snap["items"]:
                    if it["id"] == clean["id"]:
                        it.update(title=result["title"], notes=result["notes"], due=result["due"], priority=result["priority"])
                        if position is not None:
                            it["position"] = position
            elif op == "reminder_complete":
                if snap is not None and result.get("completed"):
                    snap["items"] = [it for it in snap["items"] if it["id"] != clean["id"]]
                elif not result.get("completed"):
                    for other in self._snaps.values():          # re-opened: it is not in any snapshot, so every list may have changed
                        other["dirty"] = True
            elif op == "reminder_delete" and snap is not None:
                gone = next((it.get("position") for it in snap["items"] if it["id"] == clean["id"]), None)
                snap["items"] = [it for it in snap["items"] if it["id"] != clean["id"]]
                snap["count"] = max(0, snap["count"] - 1)
                if gone is not None:                            # later positions moved up by one
                    for it in snap["items"]:
                        if it.get("position") is not None and it["position"] > gone:
                            it["position"] -= 1
            elif op == "reminder_create":
                snap = self._snaps.get(result.get("list_id"))
                if snap is not None:
                    snap["items"].append({"id": result["id"], "title": result["title"], "notes": result.get("notes") or "", "due": result["due"],
                                          "priority": result.get("priority") or 0, "position": None})
                    snap["count"] += 1
                list_id = result.get("list_id")
            if snap is not None:
                snap["dirty"] = True
        if list_id:
            self._kick(list_id, self._debounce)

    # -- entry point used by the poll loop ---------------------------------------------------------------------------------------------
    def dispatch(self, op, args, seconds):
        if op == "reminders_list":
            try:
                return True, self.read(validate_args(op, args)), ""
            except HelperError as e:
                return False, None, str(e)
        if op in ("reminder_create", "reminder_update", "reminder_complete", "reminder_delete"):
            return self.write(op, args, seconds)
        if op == "reminder_lists":
            try:
                return True, self.lists(force=True), ""
            except HelperError as e:
                return False, None, str(e)
        return run_op(op, args, seconds)


# ---------------------------------------------------------------------------
# Talking to the server (TLS with a pinned certificate fingerprint + bearer token)
# ---------------------------------------------------------------------------
def load_config(path=None):
    path = path or os.environ.get("ICLOUD_MAC_HELPER_CONFIG") or DEFAULT_CONFIG
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise HelperError("cannot read the config file %s (%s). Run install.sh first." % (path, e))
    for key in ("server", "token", "fingerprint"):
        if not cfg.get(key):
            raise HelperError("the config file is missing %r" % key)
    try:
        if os.stat(path).st_mode & 0o077:
            print("warning: %s is readable by other users; run: chmod 600 %s" % (path, path), file=sys.stderr)
    except OSError:
        pass
    return cfg


def _norm_fp(value):
    return re.sub(r"[^0-9a-f]", "", value.lower().replace("sha256:", ""))


def _connect(cfg, timeout):
    parts = urlsplit(cfg["server"])
    if parts.scheme != "https" or not parts.hostname:
        raise HelperError("the server address must start with https://")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                                   # identity is checked by fingerprint below, not by a CA
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=ctx)
    conn.connect()
    seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
    if not hmac.compare_digest(seen, _norm_fp(cfg["fingerprint"])):
        conn.close()
        raise HelperError("the server's certificate does not match the pinned fingerprint. Nothing was sent. If you reinstalled the "
                          "server, copy its new fingerprint into the config.")
    return conn


def request(cfg, method, path, payload=None, timeout=40):
    conn = _connect(cfg, timeout)
    try:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json", "User-Agent": "icloud-mac-helper/" + VERSION}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise HelperError("the server response was too large")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            data = None
        return resp.status, data
    finally:
        conn.close()


def handle_one(cfg, runner=run_op, wait=25):
    """One poll cycle. Returns 'idle', 'done' or 'refused'."""
    meta = {"wait": wait, "host": socket.gethostname(), "version": VERSION, "os": "macOS " + platform.mac_ver()[0]}
    status, job = request(cfg, "POST", "/bridge/poll", meta, timeout=wait + 15)
    if status == 204:
        return "idle"
    if status in (401, 429):
        return "refused"
    if status != 200 or not isinstance(job, dict) or "id" not in job:
        raise HelperError("unexpected answer from the server (HTTP %s)" % status)
    ok, result, error = runner(str(job.get("op", "")), job.get("args") or {}, max(1, min(int(job.get("seconds", 60)), 120)))
    request(cfg, "POST", "/bridge/result", {"job": job["id"], "ok": ok, "result": result, "error": error})
    return "done"


def run_forever(cfg_path=None):
    backoff = 2
    cache = ReminderCache()
    cache.prewarm()
    while True:
        try:
            cfg = load_config(cfg_path)
            outcome = handle_one(cfg, runner=cache.dispatch)
            backoff = 2
            if outcome == "refused":
                print("the server refused the token; retrying in 60s", file=sys.stderr, flush=True)
                time.sleep(60)
        except (HelperError, OSError, ssl.SSLError, http.client.HTTPException) as e:
            print("cannot reach the server: %s (retrying in %ds)" % (explain(e), backoff), file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Self-test: prints ONE JSON document with pass/fail, counts and timings. No secrets and no reminder or note content.
# ---------------------------------------------------------------------------
HOSTILE = [
    'plain', 'quote " and \\ backslash', "single ' quote", "line1\nline2\ttab", "unicode: café 日本語 \U0001F642",
    '"); do shell script "touch {marker}"; ("', "'); $.system('touch {marker}'); ('", "${1+1} `date` $(date)", "\\u0022; throw 1; \\u0022",
]


def selftest(cfg_path=None):
    report = {"helper_version": VERSION, "python": sys.version.split()[0], "macos": platform.mac_ver()[0], "checks": {}}
    checks = report["checks"]
    osa = shutil.which("osascript")
    checks["platform"] = {"ok": platform.system() == "Darwin" and bool(osa), "osascript": bool(osa)}
    if not checks["platform"]["ok"]:
        print(json.dumps(report, indent=2))
        return 1

    marker = os.path.join(tempfile.gettempdir(), "icloud-helper-injection-marker-%d" % os.getpid())
    payload = [s.replace("{marker}", marker) for s in HOSTILE]
    try:
        proc = subprocess.run(["osascript", "-l", "JavaScript", os.path.join(OPS_DIR, "selftest_echo.js"), json.dumps(payload)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        echoed = json.loads(proc.stdout.decode("utf-8")) if proc.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError):
        echoed = None
    checks["argument_passing"] = {"ok": echoed == payload and not os.path.exists(marker), "round_trip_exact": echoed == payload,
                                  "injection_marker_created": os.path.exists(marker), "cases": len(payload)}
    if os.path.exists(marker):
        os.unlink(marker)

    t0 = time.time()
    ok, result, error = run_op("reminder_lists", {}, timeout=60)
    checks["reminders_access"] = {"ok": ok, "seconds": round(time.time() - t0, 2)}
    if ok:
        checks["reminders_access"]["lists"] = len(result)
    else:
        checks["reminders_access"]["error"] = error

    # Reading reminders: time one scan of every list. Reminders scans a whole list per request, so big lists are slow (about 15 ms per item).
    # Only counts and timings are reported, never names or content.
    t0 = time.time()
    try:
        cache = ReminderCache(background=False)
        details = []
        for n, lst in enumerate(cache.lists(), 1):
            snap = cache.refresh(lst["id"], timeout=180, wait=180)
            details.append({"list": n, "seconds": round(snap["took"], 1), "open": len(snap["items"]), "total": snap["count"]})
        checks["reminders_read"] = {"ok": True, "seconds": round(time.time() - t0, 1), "slowest_list_seconds": max([d["seconds"] for d in details] or [0]), "lists": details}
    except HelperError as e:
        checks["reminders_read"] = {"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)}

    # Notes is optional: it only matters if the server has ENABLE_NOTES on, so a refusal here is reported but does not fail the self-test.
    t0 = time.time()
    ok, result, error = run_op("note_folders", {}, timeout=60)
    checks["notes_access"] = {"ok": True, "available": ok, "seconds": round(time.time() - t0, 2)}
    if ok:
        checks["notes_access"]["folders"] = len(result)
    else:
        checks["notes_access"]["note"] = "Notes is not available yet (%s). Only needed if you enable Reminders AND Notes on the server." % error

    try:
        cfg = load_config(cfg_path)
        t0 = time.time()
        status, data = request(cfg, "GET", "/bridge/ping", timeout=15)
        checks["server"] = {"ok": status == 200, "http_status": status, "seconds": round(time.time() - t0, 2)}
        if status == 401:
            checks["server"]["error"] = "the server rejected the token"
    except (HelperError, OSError, ssl.SSLError, http.client.HTTPException) as e:
        checks["server"] = {"ok": False, "error": explain(e)}
    report["overall"] = "pass" if all(c["ok"] for c in checks.values()) else "fail"
    print(json.dumps(report, indent=2))
    return 0 if report["overall"] == "pass" else 1


def selftest_write():
    """Round-trips a temporary reminder and a temporary note through the real scripts, then removes them. Touches your data, so it is opt-in."""
    stamp = "icloud-mac-helper selftest %d" % int(time.time())
    steps = []
    rid = nid = None
    cache = ReminderCache(background=False)

    def step(name, ok, detail=None):
        steps.append({"step": name, "ok": bool(ok), **({"detail": detail} if detail else {})})
        return ok

    try:
        ok, r, e = cache.dispatch("reminder_create", {"title": stamp, "notes": "temporary, safe to delete", "due": "2099-01-01"}, 120)
        if step("reminder_create", ok, None if ok else e):
            rid = r["id"]
            ok, r2, e = cache.dispatch("reminders_list", {"list_id": r["list_id"], "refresh": True, "query": stamp}, 120)
            step("reminders_list finds it", ok and any(x["id"] == rid for x in r2["reminders"]), None if ok else e)
            ok, r2, e = cache.dispatch("reminder_update", {"id": rid, "title": stamp + " (edited)", "priority": 1}, 120)
            step("reminder_update", ok, None if ok else e)
            ok, r2, e = cache.dispatch("reminder_complete", {"id": rid}, 120)
            step("reminder_complete", ok, None if ok else e)
    finally:
        if rid:
            ok, r2, e = cache.dispatch("reminder_delete", {"id": rid}, 120)
            step("reminder_delete (cleanup)", ok, None if ok else e)

    try:
        ok, r, e = run_op("note_create", {"title": stamp, "body": "temporary, safe to delete <b>& \"quotes\"</b>"}, timeout=60)
        if step("note_create", ok, None if ok else e):
            nid = r["id"]
            ok, r, e = run_op("note_read", {"id": nid}, timeout=60)
            step("note_read round trip", ok and stamp in json.dumps(r), None if ok else e)
    finally:
        if nid:
            try:
                proc = subprocess.run(["osascript", "-l", "JavaScript", os.path.join(OPS_DIR, "selftest_note_delete.js"), json.dumps({"id": nid})],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                step("note delete (cleanup)", proc.returncode == 0, None if proc.returncode == 0 else proc.stderr.decode("utf-8", "replace")[:200])
            except subprocess.TimeoutExpired:
                step("note delete (cleanup)", False, "timed out; delete the note called '%s' by hand" % stamp)
    overall = "pass" if all(s["ok"] for s in steps) else "fail"
    print(json.dumps({"helper_version": VERSION, "steps": steps, "overall": overall}, indent=2))
    return 0 if overall == "pass" else 1


def main():
    ap = argparse.ArgumentParser(description="icloud-mac-helper")
    ap.add_argument("--config", help="path to the config file")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", action="store_true", help="run until stopped (what the LaunchAgent does)")
    g.add_argument("--selftest", action="store_true", help="check this Mac and the connection, print a JSON report")
    g.add_argument("--selftest-write", action="store_true", help="also create, edit and delete a temporary reminder and note (touches your data)")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        print(VERSION)
        return 0
    if a.selftest:
        return selftest(a.config)
    if a.selftest_write:
        return selftest_write()
    if a.run:
        run_forever(a.config)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
