"""iMessage for icloud-mac-helper: read and search the owner's own Messages history (read-only), and send through Messages.

Run by the helper as `python3 -I imessage.py <op> '<json args>'` with Apple's Python 3.9 and the standard library only. It opens one
file, ~/Library/Messages/chat.db of the macOS user running it (the owner's), read-only (mode=ro, query_only), and never any other
user's database: the owner's separate `claude` user and its bots are never touched. Full Disk Access for the helper's Python is
what lets it read the file; without it the answer says so.

Since macOS 13 most message text is not in the `text` column but inside `attributedBody`, an NSAttributedString typedstream;
decode_attributed_body() takes the string out of it (checked on 97,702 real rows with no failures). Tapbacks are folded into the
message they react to; retracted messages, system items and messages marked as spam are left out. Sending (imessage_send) goes
through Messages with a static script, iMessage only, never to a handle in this Mac's imessage-never-send.txt, and is confirmed by
the new row Messages writes.
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import unicodedata

DB = os.path.expanduser("~/Library/Messages/chat.db")
if os.environ.get("ICLOUD_MAC_HELPER_TESTING") == "1" and os.environ.get("ICLOUD_IMESSAGE_DB"):
    DB = os.environ["ICLOUD_IMESSAGE_DB"]                            # tests only: a fixture database
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
REACTIONS = {2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh", 2004: "emphasize", 2005: "question", 2006: "emoji"}
MAX_SCAN = 1000000                                                   # the whole history: decoding is fast (about 0.5 s per 100,000 messages)


class Fail(Exception):
    pass


def fail(message):
    raise Fail(message)


# ---------------------------------------------------------------------------------------------------- decoding
def decode_attributed_body(blob):
    """The message text inside an NSAttributedString typedstream (chat.db attributedBody), or None."""
    if not blob:
        return None
    blob = bytes(blob)
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i + 8)
    if j < 0 or j > i + 16 or j + 1 >= len(blob):
        return None
    k = j + 1
    n = blob[k]
    if n == 0x81:
        length, start = int.from_bytes(blob[k + 1:k + 3], "little"), k + 3
    elif n == 0x82:
        length, start = int.from_bytes(blob[k + 1:k + 5], "little"), k + 5
    elif n == 0x83:
        length, start = int.from_bytes(blob[k + 1:k + 9], "little"), k + 9
    else:
        length, start = n, k + 1
    raw = blob[start:start + length]
    if len(raw) != length:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def text_of(text, body):
    t = text if text else (decode_attributed_body(body) or "")
    return t.replace("￼", "[attachment]").strip()


def when(n):
    """Apple time (nanoseconds since 2001-01-01 UTC; seconds in very old rows) as local ISO 8601, or None."""
    if not n:
        return None
    seconds = n / 1e9 if abs(n) > 1e11 else float(n)
    return (APPLE_EPOCH + dt.timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def apple_ns(iso):
    """ISO 8601 (a date means its start, local time) as Apple nanoseconds."""
    text = iso.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(text)
    except ValueError:
        fail("not a date: '%s' (use ISO 8601, e.g. 2026-09-01 or 2026-09-01T09:00)" % iso)
    if d.tzinfo is None:
        d = d.astimezone()                                           # a local date-time
    return int((d - APPLE_EPOCH).total_seconds() * 1e9)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def split_list(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------------------------------- the database
def connect():
    if not os.path.exists(DB):
        fail("there is no Messages database for this Mac user (Messages has not been used here)")
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        db.execute("PRAGMA query_only = 1")
        db.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.OperationalError as e:
        text = str(e).lower()
        if "unable to open" in text or "authorization" in text or "not authorized" in text:
            fail("the helper cannot read Messages: give Full Disk Access to the Python the helper runs with (the same grant iCloud "
                 "Drive uses), in System Settings > Privacy & Security > Full Disk Access")
        if "locked" in text or "busy" in text:
            fail("Messages is busy writing; try again in a moment")
        fail("could not open the Messages database: %s" % e)
    return db


def chat_groups(db, exclude):
    """Conversations by chat_identifier: the SMS and iMessage rows of one person are one conversation."""
    groups = {}
    for rowid, guid, ident, name, service, style, archived in db.execute(
            "SELECT ROWID, guid, chat_identifier, display_name, service_name, style, is_archived FROM chat"):
        if not ident or ident in exclude:
            continue
        g = groups.setdefault(ident, {"chat_id": ident, "rowids": [], "guids": {}, "row_service": {}, "name": "", "group": False, "services": set(),
                                      "participants": set(), "archived": True, "last": 0, "unread": 0})
        g["rowids"].append(rowid)
        g["guids"][rowid] = guid
        g["row_service"][rowid] = service or ""
        g["name"] = g["name"] or (name or "")
        g["group"] = g["group"] or style == 43
        if service:
            g["services"].add(service)
        g["archived"] = g["archived"] and bool(archived)
    by_rowid = {r: g for g in groups.values() for r in g["rowids"]}
    for chat_id, last in db.execute("SELECT chat_id, max(message_date) FROM chat_message_join GROUP BY chat_id"):
        if chat_id in by_rowid and last:
            by_rowid[chat_id]["last"] = max(by_rowid[chat_id]["last"], last)
    for chat_id, handle in db.execute("SELECT j.chat_id, h.id FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id"):
        if chat_id in by_rowid and handle:
            by_rowid[chat_id]["participants"].add(handle)
    for chat_id, n in db.execute("SELECT j.chat_id, count(*) FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
                                 "WHERE m.is_from_me = 0 AND m.is_read = 0 AND m.item_type = 0 AND m.associated_message_type = 0 "
                                 "GROUP BY j.chat_id"):
        if chat_id in by_rowid:
            by_rowid[chat_id]["unread"] += n
    return groups


def placeholders(n):
    return ",".join("?" * n)


MESSAGE_COLUMNS = ("m.ROWID, m.guid, m.text, m.attributedBody, m.date, m.date_delivered, m.date_read, m.is_from_me, h.id, m.service, "
                   "m.date_edited, m.cache_has_attachments, m.reply_to_guid, m.thread_originator_guid, m.associated_message_type, "
                   "m.associated_message_guid, m.associated_message_emoji, m.item_type, m.is_spam, m.date_retracted, m.error")


def message_rows(db, where, params, order="m.ROWID DESC", limit=None):
    sql = ("SELECT %s, j.chat_id FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
           "LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE %s ORDER BY %s" % (MESSAGE_COLUMNS, where, order))
    if limit:
        sql += " LIMIT %d" % int(limit)
    return db.execute(sql, params)


def message_json(row):
    (rowid, guid, text, body, date, delivered, read, from_me, handle, service, edited, has_att, reply_to, thread, _t, _g, _e,
     _item, _spam, _retracted, error) = row[:21]
    m = {"id": guid, "rowid": rowid, "at": when(date), "from_me": bool(from_me), "sender": None if from_me else handle,
         "text": text_of(text, body), "service": service}
    if from_me:
        if delivered:
            m["delivered_at"] = when(delivered)
        if read:
            m["read_at"] = when(read)
        if error:
            m["error"] = error
    if edited:
        m["edited"] = True
    if thread:                                                       # an inline reply (reply_to_guid is set on ordinary messages too)
        m["reply_to"] = thread
    if has_att:
        m["_has_attachments"] = True
    return m


def attachments_for(db, rowids):
    out = {}
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        for mid, name, mime, size in db.execute(
                "SELECT j.message_id, a.transfer_name, a.mime_type, a.total_bytes FROM message_attachment_join j "
                "JOIN attachment a ON a.ROWID = j.attachment_id WHERE j.message_id IN (%s)" % placeholders(len(chunk)), chunk):
            out.setdefault(mid, []).append({"name": name or "", "type": mime or "", "bytes": size or 0})
    return out


def is_plain_message(row):
    return row[14] == 0 and row[17] == 0 and not row[18] and not row[19]      # not a tapback, a system item, spam or retracted


# ---------------------------------------------------------------------------------------------------- operations
def op_chats(a):
    db = connect()
    groups = chat_groups(db, set(split_list(a.get("exclude"))))
    since = apple_ns(a["since"]) if a.get("since") else None
    chats = [g for g in groups.values() if g["last"] and (since is None or g["last"] >= since)
             and (a.get("include_archived") or not g["archived"])]
    chats.sort(key=lambda g: -g["last"])
    chats = chats[:max(1, min(int(a.get("limit") or 50), 1000))]
    out = []
    for g in chats:
        latest = next((r for r in message_rows(db, "j.chat_id IN (%s)" % placeholders(len(g["rowids"])), g["rowids"], limit=20)
                       if is_plain_message(r)), None)
        out.append({"chat_id": g["chat_id"], "name": g["name"], "group": g["group"], "services": sorted(g["services"]),
                    "participants": sorted(g["participants"]), "last_message_at": when(g["last"]),
                    "last_text": text_of(latest[2], latest[3])[:120] if latest else "", "last_from_me": bool(latest[7]) if latest else None,
                    "unread": g["unread"], "archived": g["archived"]})
    return {"chats": out}


def op_read(a):
    db = connect()
    groups = chat_groups(db, set(split_list(a.get("exclude"))))
    g = groups.get(a.get("chat_id") or "")
    if g is None:
        fail("no conversation with chat_id '%s' (take it from imessage_list_chats)" % a.get("chat_id"))
    limit = max(1, min(int(a.get("limit") or 50), 500))
    where = ["j.chat_id IN (%s)" % placeholders(len(g["rowids"]))]
    params = list(g["rowids"])
    if a.get("before_id"):
        where.append("m.ROWID < ?")
        params.append(int(a["before_id"]))
    if a.get("since"):
        where.append("m.date >= ?")
        params.append(apple_ns(a["since"]))
    messages, reactions, by_guid, older = [], {}, {}, False
    for row in message_rows(db, " AND ".join(where), params, limit=limit * 4 + 50):
        kind = row[14]
        if 2000 <= kind <= 2006 or 3000 <= kind <= 3006:
            target = (row[15] or "").split("/", 1)[-1]
            name = REACTIONS.get(kind if kind < 3000 else kind - 1000, "reaction")
            if kind == 2006 and row[16]:
                name = row[16]
            r = reactions.setdefault(target, {})
            r[name] = r.get(name, 0) + (1 if kind < 3000 else -1)
            continue
        if not is_plain_message(row):
            continue
        if len(messages) == limit:
            older = True
            break
        m = message_json(row)
        messages.append(m)
        by_guid[m["id"]] = m
    atts = attachments_for(db, [m["rowid"] for m in messages if m.pop("_has_attachments", False)])
    for m in messages:
        if m["rowid"] in atts:
            m["attachments"] = atts[m["rowid"]]
        got = {k: v for k, v in reactions.get(m["id"], {}).items() if v > 0}
        if got:
            m["reactions"] = got
    messages.reverse()                                               # oldest first, as a conversation reads
    chat = {"chat_id": g["chat_id"], "name": g["name"], "group": g["group"], "participants": sorted(g["participants"]),
            "services": sorted(g["services"])}
    out = {"chat": chat, "messages": messages, "complete": not older}
    if older and messages:
        out["older_before_id"] = messages[0]["rowid"]
    return out


def op_search(a):
    db = connect()
    exclude = set(split_list(a.get("exclude")))
    groups = chat_groups(db, exclude)
    chat_of = {r: g for g in groups.values() for r in g["rowids"]}
    q = norm(a.get("query") or "")
    if not q:
        fail("query is required")
    where, params = ["1 = 1"], []
    if a.get("chat_id"):
        g = groups.get(a["chat_id"])
        if g is None:
            fail("no conversation with chat_id '%s'" % a["chat_id"])
        where.append("j.chat_id IN (%s)" % placeholders(len(g["rowids"])))
        params += g["rowids"]
    if a.get("since"):
        where.append("m.date >= ?")
        params.append(apple_ns(a["since"]))
    if a.get("before"):
        where.append("m.date < ?")
        params.append(apple_ns(a["before"]))
    limit = max(1, min(int(a.get("limit") or 20), 200))
    deadline = time.monotonic() + max(1, int(a.get("budget") or 40))
    matches, scanned, complete = [], 0, True
    for row in message_rows(db, " AND ".join(where), params):
        if row[21] not in chat_of or not is_plain_message(row):
            continue
        scanned += 1
        if scanned > MAX_SCAN or time.monotonic() > deadline:
            complete = False
            break
        if q in norm(text_of(row[2], row[3])):
            m = message_json(row)
            m.pop("_has_attachments", None)
            g = chat_of[row[21]]
            m.update(chat_id=g["chat_id"], chat_name=g["name"], group=g["group"])
            matches.append(m)
            if len(matches) == limit:
                complete = False
                break
    return {"matches": matches, "scanned": scanned, "complete": complete}


OSASCRIPT = "/usr/bin/osascript"
if os.environ.get("ICLOUD_MAC_HELPER_TESTING") == "1" and os.environ.get("ICLOUD_IMESSAGE_OSASCRIPT"):
    OSASCRIPT = os.environ["ICLOUD_IMESSAGE_OSASCRIPT"]              # tests only: a fake that writes to the fixture database
SEND_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imessage_send.js")
NEVER_SEND_FILE = os.path.expanduser("~/Library/Application Support/icloud-mac-helper/imessage-never-send.txt")
CONFIRM_SECONDS = 8.0
if os.environ.get("ICLOUD_MAC_HELPER_TESTING") == "1":                  # tests only
    NEVER_SEND_FILE = os.environ.get("ICLOUD_IMESSAGE_NEVER_SEND_FILE", NEVER_SEND_FILE)
    CONFIRM_SECONDS = float(os.environ.get("ICLOUD_IMESSAGE_CONFIRM_SECONDS", CONFIRM_SECONDS))


def never_send_here():
    """This Mac's own list of handles nothing is ever sent to (one per line; # starts a comment). It holds even if the server's
    settings do not, so the Mac never messages, say, the owner's own assistant, whatever the server asks."""
    try:
        with open(NEVER_SEND_FILE) as f:
            return {line.split("#", 1)[0].strip().lower() for line in f if line.split("#", 1)[0].strip()}
    except OSError:
        return set()


def op_send(a):
    import subprocess
    text = a.get("text") or ""
    if not text.strip():
        fail("text is empty")
    chat_id, handle = a.get("chat_id"), a.get("handle")
    if bool(chat_id) == bool(handle):
        fail("give exactly one of chat_id or handle")
    db = connect()
    groups = chat_groups(db, set())
    target = chat_id or handle
    blocked = never_send_here()
    g = groups.get(target)
    people = set(p.lower() for p in (g["participants"] if g else [])) | {target.lower()}
    if people & blocked:
        fail("this Mac never sends to %s (imessage-never-send.txt). Nothing was sent." % ", ".join(sorted(people & blocked)))
    if chat_id and g is None:
        fail("no conversation with chat_id '%s'. Nothing was sent." % chat_id)
    payload = {"text": text}
    if g is not None:
        imessage_rows = [r for r in g["rowids"] if g["row_service"][r] == "iMessage"]
        if not imessage_rows:
            fail("that conversation exists only as SMS; only iMessage is sent from here. Nothing was sent.")
        last = dict(db.execute("SELECT chat_id, max(message_date) FROM chat_message_join WHERE chat_id IN (%s) GROUP BY chat_id"
                               % placeholders(len(imessage_rows)), imessage_rows).fetchall())
        payload["guid"] = g["guids"][max(imessage_rows, key=lambda r: last.get(r) or 0)]
    else:
        payload["handle"] = handle
    before = db.execute("SELECT coalesce(max(ROWID), 0) FROM message").fetchone()[0]
    db.close()
    try:
        p = subprocess.run([OSASCRIPT, "-l", "JavaScript", SEND_SCRIPT, json.dumps(payload)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("Messages did not answer within 30 seconds; check Messages on the Mac before sending again (it may have gone out)")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        if "-1743" in err or "not allowed" in err.lower():
            fail("the helper is not allowed to control Messages: allow it in System Settings > Privacy & Security > Automation. Nothing was sent.")
        if "-1728" in err or "Can't get" in err:
            fail("Messages does not know that conversation or handle (no iMessage). Nothing was sent.")
        fail("Messages refused the message: %s" % err.strip()[:200])
    deadline = time.monotonic() + CONFIRM_SECONDS
    while time.monotonic() < deadline:
        db = connect()
        row = db.execute("SELECT m.guid, m.is_sent, m.is_delivered, m.error, m.text, m.attributedBody FROM message m "
                         "JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID = j.chat_id "
                         "WHERE m.ROWID > ? AND m.is_from_me = 1 AND c.chat_identifier = ? ORDER BY m.ROWID DESC LIMIT 5",
                         (before, target)).fetchall()
        db.close()
        mine = next((r for r in row if text_of(r[4], r[5]) == text.replace("\ufffc", "[attachment]").strip()), None)
        if mine and (mine[1] or mine[2] or mine[3]):
            if mine[3]:
                return {"status": "failed", "message_id": mine[0], "error": mine[3],
                        "note": "Messages could not deliver it (error %d); it shows as not delivered on the Mac." % mine[3]}
            return {"status": "sent", "message_id": mine[0], "delivered": bool(mine[2])}
        time.sleep(0.5)
    return {"status": "unconfirmed", "note": "Messages accepted it but did not confirm within 8 seconds: check the conversation before "
                                             "sending again."}


OPS = {"imessage_chats": op_chats, "imessage_read": op_read, "imessage_search": op_search, "imessage_send": op_send}


def main(argv):
    if len(argv) != 3 or argv[1] not in OPS:
        sys.stderr.write("usage: imessage.py <%s> '<json args>'\n" % "|".join(sorted(OPS)))
        return 2
    try:
        print(json.dumps(OPS[argv[1]](json.loads(argv[2])), ensure_ascii=False))
        return 0
    except Fail as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    except sqlite3.Error as e:
        sys.stderr.write("the Messages database could not be read: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
