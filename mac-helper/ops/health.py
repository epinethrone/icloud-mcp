"""Apple Health for the Mac helper. Run as: python3 -I health.py <operation> '<json arguments>'. Prints one JSON document.

A Mac cannot read HealthKit, so the data comes from the owner's iPhone: the "Health Export" shortcut writes a text file to iCloud Drive
(Shortcuts/Health/health-<yyyyMMdd-HHmmss>.txt, in the Shortcuts app's own iCloud folder, NOT the Drive folder the drive_* operations
work in). Every operation first takes in new or changed files into a private SQLite store on this Mac, then answers from the store with
daily figures: nothing raw leaves the Mac except one day's detail when asked for.

File format (one section per Health type; every column one value per line, in the same order as the start dates):
    #health v1 generated=<ISO 8601> input=<the automation's input>
    ##type=<Health type name>[ grouped=hour|day]
    ##start / ##end / ##value / ##unit / ##source
A column whose line count differs from the start dates (Shortcuts leaves Source empty for grouped types and Sleep) is dropped, never
zipped out of line.

Each file is a snapshot of the window it covers (its oldest sample to the moment it was made). For every type, the NEWEST snapshot wins
inside its window and older files only count outside it: hourly totals, revised sleep stages and overlapping exports are never added
up twice. A type missing from a file means "no information", not "nothing happened".

health_refresh asks the iPhone for a new export by running a command set up on this Mac only (health-refresh.json next to the helper's
config), never one sent by the server, and at most once per min_minutes.
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

SUPPORT = os.path.expanduser("~/Library/Application Support/icloud-mac-helper")
FOLDER = os.environ.get("ICLOUD_HEALTH_DIR") or os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health")
DB = os.environ.get("ICLOUD_HEALTH_DB") or os.path.join(SUPPORT, "health.sqlite")
REFRESH_CONFIG = os.environ.get("ICLOUD_HEALTH_REFRESH") or os.path.join(SUPPORT, "health-refresh.json")
REFRESH_STATE = os.path.join(os.path.dirname(DB), "health-refresh.state")
SF_DATALESS = 0x40000000
FILE_NAME = re.compile(r"^health-\d{8}-\d{6}\.txt$")
MAX_DAYS = 92                   # longest range one summary covers
STABLE_SECONDS = 6              # a refreshed file counts as complete once it stopped growing for this long

# Health type names as the shortcut writes them -> (metric key, kind). Kinds: "total" (summed per day: hourly totals), "daily" (one
# value per day; 0 means no reading), "readings" (individual samples, heart rate), "sleep" (stage intervals).
TYPES = {
    "Steps": ("steps", "total"), "Active Calories": ("active_energy", "total"),
    "Walking + Running Distance": ("distance", "total"), "Exercise Minutes": ("exercise_minutes", "total"),
    "Flights Climbed": ("flights_climbed", "total"), "Time In Daylight": ("daylight_minutes", "total"),
    "Resting Heart Rate": ("resting_heart_rate", "daily"), "Heart Rate Variability": ("hrv", "daily"),
    "Walking Heart Rate Average": ("walking_heart_rate", "daily"), "Respiratory Rate": ("respiratory_rate", "daily"),
    "Blood Oxygen": ("blood_oxygen", "daily"), "Weight": ("weight", "daily"), "Body Fat Percentage": ("body_fat", "daily"),
    "Walking Steadiness": ("walking_steadiness", "daily"),
    "Heart Rate": ("heart_rate", "readings"), "Sleep": ("sleep", "sleep"),
}
KIND = dict(TYPES.values())
ASLEEP = {"core", "deep", "rem", "asleep", "asleep unspecified", "unspecified"}
SESSION_GAP = 2 * 3600          # sleep intervals further apart than this are separate sleeps


class HealthError(Exception):
    pass


# ------------------------------------------------------------------ time
def parse_ts(s):
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def day_of(iso):
    return iso[:10]


def hour_of(iso):
    """The local clock hour a timestamp falls in, as written (grouped starts are not always on the hour: 22:08:02 is the 22:00 hour)."""
    return iso[:13]


def union_seconds(intervals):
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


# ------------------------------------------------------------------ files
def parse_file(text):
    """(generated ISO, [section]) from one export. A section: {label, grouped, rows: [(start, end, value, unit, source)]}."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#health v1 "):
        raise HealthError("not a Health export")
    m = re.search(r"generated=(\S+)", lines[0])
    generated = parse_ts(m.group(1)) if m else None
    if generated is None:
        raise HealthError("the export has no generation time")
    sections, cur, col = [], None, None
    for line in lines[1:]:
        if line.startswith("##type="):
            head = line[len("##type="):]
            gm = re.search(r" grouped=(hour|day)$", head)
            cur = {"label": head[:gm.start()] if gm else head, "grouped": gm.group(1) if gm else None, "cols": {}}
            sections.append(cur)
            col = None
        elif cur is not None and line in ("##start", "##end", "##value", "##unit", "##source"):
            col = line[2:]
            cur["cols"][col] = []
        elif cur is not None and col is not None and line != "":
            cur["cols"][col].append(line)
    out = []
    for sec in sections:
        c = sec["cols"]
        starts, ends = c.get("start", []), c.get("end", [])
        n = len(starts)
        if n == 0 or len(ends) != n:
            continue                                  # a half-written section: no information
        def column(name):
            vals = c.get(name, [])
            return vals if len(vals) == n else [None] * n
        rows = list(zip(starts, ends, column("value"), column("unit"), column("source")))
        out.append({"label": sec["label"], "grouped": sec["grouped"], "rows": rows})
    return generated.isoformat(), out


def offloaded(path):
    try:
        return bool(getattr(os.lstat(path), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


# ------------------------------------------------------------------ store
def connect():
    folder = os.path.dirname(DB)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    new = not os.path.exists(DB)
    con = sqlite3.connect(DB, timeout=20)
    if new:
        os.chmod(DB, 0o600)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, size INTEGER, mtime REAL, generated TEXT, generated_ts REAL);
        CREATE TABLE IF NOT EXISTS sections (file TEXT, metric TEXT, label TEXT, grouped TEXT, generated_ts REAL,
                                             window_start REAL, window_end REAL, rows INTEGER);
        CREATE TABLE IF NOT EXISTS samples (file TEXT, metric TEXT, start TEXT, end TEXT, start_ts REAL, end_ts REAL, day TEXT,
                                            value REAL, text TEXT, unit TEXT, source TEXT);
        CREATE INDEX IF NOT EXISTS samples_metric_day ON samples (metric, day);
        CREATE INDEX IF NOT EXISTS samples_file ON samples (file, metric);
        CREATE INDEX IF NOT EXISTS sections_metric ON sections (metric, generated_ts);
    """)
    return con


def metric_for(label):
    if label in TYPES:
        return TYPES[label]
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "unknown", "daily"


def store_file(con, name, text, size, mtime):
    generated, sections = parse_file(text)
    gen_ts = parse_ts(generated).timestamp()
    con.execute("DELETE FROM samples WHERE file = ?", (name,))
    con.execute("DELETE FROM sections WHERE file = ?", (name,))
    for sec in sections:
        metric, _kind = metric_for(sec["label"])
        rows = []
        for start, end, value, unit, source in sec["rows"]:
            s, e = parse_ts(start), parse_ts(end)
            if s is None or e is None:
                continue
            num = None
            try:
                num = float(value) if value not in (None, "") else None
            except ValueError:
                pass
            rows.append((name, metric, start, end, s.timestamp(), e.timestamp(), day_of(start), num,
                         None if num is not None else value, unit or None, source or None))
        if not rows:
            continue
        con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?,?)",
                    (name, metric, sec["label"], sec["grouped"], gen_ts, min(r[4] for r in rows), max(gen_ts, max(r[5] for r in rows)),
                     len(rows)))
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (name, size, mtime, generated, gen_ts))


def ingest(con, download=True):
    """Take in every new or changed export. Returns {added, updated, waiting} (waiting: files still only in iCloud)."""
    added = updated = waiting = 0
    if not os.path.isdir(FOLDER):
        return {"added": 0, "updated": 0, "waiting": 0, "folder_missing": True}
    known = {n: (sz, mt) for n, sz, mt in con.execute("SELECT name, size, mtime FROM files")}
    for name in sorted(os.listdir(FOLDER)):
        if not FILE_NAME.match(name):
            continue
        path = os.path.join(FOLDER, name)
        if offloaded(path):
            if download:
                try:
                    subprocess.run(["/usr/bin/brctl", "download", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            waiting += 1
            continue
        st = os.stat(path)
        if known.get(name) == (st.st_size, st.st_mtime):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(50 * 1024 * 1024)
        try:
            store_file(con, name, text, st.st_size, st.st_mtime)
        except HealthError:
            continue
        if name in known:
            updated += 1
        else:
            added += 1
    con.commit()
    return {"added": added, "updated": updated, "waiting": waiting}


def effective(con, metric, first_day, last_day):
    """The samples that count for a metric between two days: newest snapshot first, each one only outside the windows of newer ones."""
    covered, out = [], []
    secs = con.execute("SELECT file, window_start, window_end FROM sections WHERE metric = ? ORDER BY generated_ts DESC", (metric,)).fetchall()
    for file, ws, we in secs:
        rows = con.execute("SELECT start, end, start_ts, end_ts, value, text, unit, source FROM samples "
                           "WHERE file = ? AND metric = ? AND day BETWEEN ? AND ?", (file, metric, first_day, last_day)).fetchall()
        for r in rows:
            if not any(a <= r[2] <= b for a, b in covered):
                out.append(r)
        covered.append((ws, we))
    return out


# ------------------------------------------------------------------ the full history (Health app > profile > Export All Health Data)
HK = {
    "HKQuantityTypeIdentifierStepCount": "steps", "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance", "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_minutes",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights_climbed", "HKQuantityTypeIdentifierTimeInDaylight": "daylight_minutes",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_heart_rate", "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
    "HKQuantityTypeIdentifierOxygenSaturation": "blood_oxygen", "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat", "HKQuantityTypeIdentifierAppleWalkingSteadiness": "walking_steadiness",
    "HKQuantityTypeIdentifierHeartRate": "heart_rate", "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
SLEEP_STAGE = {"HKCategoryValueSleepAnalysisAsleepCore": "Core", "HKCategoryValueSleepAnalysisAsleepDeep": "Deep",
               "HKCategoryValueSleepAnalysisAsleepREM": "REM", "HKCategoryValueSleepAnalysisAsleepUnspecified": "Asleep",
               "HKCategoryValueSleepAnalysisAsleep": "Asleep", "HKCategoryValueSleepAnalysisAwake": "Awake",
               "HKCategoryValueSleepAnalysisInBed": "In Bed"}
IMPORT_NAME = "export.zip"


def _hk_iso(s):
    """'2026-09-25 10:00:00 +0200' -> '2026-09-25T10:00:00+02:00'."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{2})(\d{2})$", (s or "").strip())
    return "%sT%s%s:%s" % m.groups() if m else None


def import_export(path, out=sys.stderr):
    """Take in Apple Health's own export (the zip, or export.xml): years of history, read as a stream.

    Health keeps every source's samples, so the iPhone and the Watch both count the same steps. Like the Health app, totals are not
    added across sources: per hour, each source's total is taken and the largest one counts. Daily types become one mean per day,
    heart rate stays as readings, and sleep keeps the sources that record stages (the Watch) when there are any. The result is one
    snapshot named export.zip covering everything up to the export date; the iPhone's newer two-day exports win inside their own
    windows, as usual."""
    import xml.etree.ElementTree as ET
    import zipfile
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        inner = next((n for n in z.namelist() if n.endswith("/export.xml") or n == "export.xml"), None)
        if inner is None:
            raise HealthError("no export.xml in %s" % path)
        stream = z.open(inner)
    else:
        stream = open(path, "rb")
    totals, offsets, daily, hr, sleep, units = {}, {}, {}, [], [], {}
    generated, count, root = None, 0, None
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            if root is None:
                root = el
            continue
        tag = el.tag
        if tag == "ExportDate":
            generated = _hk_iso(el.get("value"))
        elif tag == "Record":
            metric = HK.get(el.get("type"))
            start, end = _hk_iso(el.get("startDate")), _hk_iso(el.get("endDate"))
            if metric and start and end:
                count += 1
                src = el.get("sourceName") or ""
                kind = KIND[metric]
                if kind == "sleep":
                    stage = SLEEP_STAGE.get(el.get("value"))
                    if stage:
                        sleep.append((start, end, stage, src))
                else:
                    try:
                        v = float(el.get("value"))
                    except (TypeError, ValueError):
                        v = None
                    if v is not None:
                        unit = el.get("unit") or ""
                        if metric in ("blood_oxygen", "body_fat") and unit == "%" and v <= 1:
                            v *= 100                                  # stored as a fraction
                        units.setdefault(metric, unit)
                        if kind == "total":
                            k = (metric, hour_of(start), src)
                            totals[k] = totals.get(k, 0.0) + v
                            offsets[k[:2]] = start[19:]
                        elif kind == "daily":
                            daily.setdefault((metric, day_of(start)), []).append(v)
                        else:
                            hr.append((start, end, v, src))
                if count % 500000 == 0:
                    print("read %d samples" % count, file=out)
            el.clear()
            if root is not None and count % 20000 == 0:
                root.clear()                                         # drop the finished records: the file can be gigabytes
        elif tag in ("Workout", "ActivitySummary", "Correlation", "ClinicalRecord"):
            el.clear()
    if generated is None:
        raise HealthError("this does not look like an Apple Health export (no ExportDate)")
    rows = []
    best = {}
    for (metric, hour, _src), v in totals.items():
        if v > best.get((metric, hour), -1):
            best[(metric, hour)] = v
    for (metric, hour), v in best.items():
        off = offsets[(metric, hour)]
        rows.append((metric, hour + ":00:00" + off, hour + ":59:59" + off, v, None, units.get(metric), None))
    for (metric, day), vals in daily.items():
        end = min(day + "T23:59:59", generated[:19]) + generated[19:]   # the export day ends at the export, not at midnight
        rows.append((metric, day + "T00:00:00" + generated[19:], end, sum(vals) / len(vals), None, units.get(metric), None))
    seen = set()
    for start, end, v, src in hr:
        if (start, v) not in seen:                                   # the same reading synced from two devices
            seen.add((start, v))
            rows.append(("heart_rate", start, end, v, None, units.get("heart_rate"), src))
    staged = {src for _s, _e, stage, src in sleep if stage in ("Core", "Deep", "REM")}
    for start, end, stage, src in sleep:
        if not staged or src in staged:
            rows.append(("sleep", start, end, None, stage, None, src))
    con = connect()
    gen_ts = parse_ts(generated).timestamp()
    con.execute("DELETE FROM samples WHERE file = ?", (IMPORT_NAME,))
    con.execute("DELETE FROM sections WHERE file = ?", (IMPORT_NAME,))
    by_metric = {}
    batch = []
    for metric, start, end, v, text, unit, src in rows:
        s_ts, e_ts = parse_ts(start).timestamp(), parse_ts(end).timestamp()
        batch.append((IMPORT_NAME, metric, start, end, s_ts, e_ts, day_of(start), v, text, unit, src))
        lo, hi, n = by_metric.get(metric, (s_ts, e_ts, 0))
        by_metric[metric] = (min(lo, s_ts), max(hi, e_ts), n + 1)
        if len(batch) >= 50000:
            con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
    if batch:
        con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
    for metric, (lo, hi, n) in by_metric.items():
        con.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?,?)", (IMPORT_NAME, metric, "Apple Health export", None, gen_ts, lo,
                                                                      max(hi, gen_ts), n))
    st = os.stat(path)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (IMPORT_NAME, st.st_size, st.st_mtime, generated, gen_ts))
    con.commit()
    first = con.execute("SELECT MIN(day) FROM samples WHERE file = ?", (IMPORT_NAME,)).fetchone()[0]
    return {"imported": IMPORT_NAME, "export_date": generated, "samples_read": count, "stored": len(rows), "history_from": first,
            "metrics": {m: v[2] for m, v in sorted(by_metric.items())}}


# ------------------------------------------------------------------ figures
def _round(x, places=1):
    return None if x is None else round(x, places)


def _unit(rows):
    for r in rows:
        if r[6]:
            return r[6]
    return None


def daily_totals(rows):
    """{day: {total, hours_recorded}}: hours_recorded counts hours with a non-zero value (0 is also what an hour with no data shows)."""
    days = {}
    for start, _e, _s, _e2, value, _t, _u, _src in rows:
        if value is None:
            continue
        d = days.setdefault(day_of(start), {"total": 0.0, "hours": set()})
        d["total"] += value
        if value > 0:
            d["hours"].add(hour_of(start))
    return {k: {"total": _round(v["total"], 2) if v["hours"] else None, "hours_recorded": len(v["hours"])} for k, v in days.items()}


def daily_values(rows):
    days = {}
    for start, _e, _s, _e2, value, _t, _u, _src in rows:
        if value is not None and value > 0:
            days.setdefault(day_of(start), []).append(value)
    return {k: _round(sum(v) / len(v), 2) for k, v in days.items()}


def daily_readings(rows):
    days = {}
    for start, _e, s_ts, _e2, value, _t, _u, _src in rows:
        if value is not None and value > 0:
            days.setdefault(day_of(start), []).append((s_ts, start, value))
    out = {}
    for k, v in days.items():
        v.sort()
        vals = [x[2] for x in v]
        out[k] = {"min": _round(min(vals)), "avg": _round(sum(vals) / len(vals)), "max": _round(max(vals)), "readings": len(vals),
                  "from": v[0][1][11:16], "to": v[-1][1][11:16]}
    return out


def sleep_sessions(rows):
    """Sleeps, each dated by the day it ended: start, end, asleep and awake minutes, minutes per stage. Overlapping intervals (two
    sources, or a revised export) are merged, never added."""
    iv = []
    for start, end, s_ts, e_ts, _v, text, _u, _src in rows:
        if e_ts > s_ts:
            iv.append((s_ts, e_ts, start, end, (text or "").strip().lower()))
    iv.sort()
    groups, cur, cur_end = [], [], None
    for x in iv:
        if cur and x[0] > cur_end + SESSION_GAP:
            groups.append(cur)
            cur, cur_end = [], None
        cur.append(x)
        cur_end = x[1] if cur_end is None else max(cur_end, x[1])
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        asleep = [(a, b) for a, b, _s, _e, st in g if st in ASLEEP]
        if not asleep:
            continue
        stages = {}
        for a, b, _s, _e, st in g:
            stages.setdefault(st or "unknown", []).append((a, b))
        first = min(g, key=lambda x: x[0])
        last = max(g, key=lambda x: x[1])
        out.append({"date": day_of(last[3]), "start": first[2][:16], "end": last[3][:16],
                    "asleep_minutes": int(round(union_seconds(asleep) / 60)),
                    "awake_minutes": int(round(union_seconds(stages.get("awake", [])) / 60)),
                    "stages_minutes": {k: int(round(union_seconds(v) / 60)) for k, v in sorted(stages.items()) if k != "awake"}})
    return out


# ------------------------------------------------------------------ operations
def _dates(a):
    try:
        first = datetime.date.fromisoformat(str(a.get("start") or "")[:10])
        last = datetime.date.fromisoformat(str(a.get("end") or a.get("start") or "")[:10])
    except ValueError:
        raise HealthError("start and end must be dates (YYYY-MM-DD)")
    if last < first:
        raise HealthError("end is before start")
    if (last - first).days + 1 > MAX_DAYS:
        raise HealthError("at most %d days at a time" % MAX_DAYS)
    return first.isoformat(), last.isoformat()


def freshness(con):
    # an export with no sections (Health was locked, or access not granted yet) is not fresh data
    row = con.execute("SELECT generated, generated_ts FROM files WHERE name IN (SELECT file FROM sections) "
                      "ORDER BY generated_ts DESC LIMIT 1").fetchone()
    latest = {}
    for metric, end in con.execute("SELECT metric, MAX(end) FROM samples WHERE value IS NULL OR value > 0 GROUP BY metric"):
        latest[metric] = end[:16]
    first = con.execute("SELECT MIN(day) FROM samples").fetchone()[0]
    return {"latest_export": row[0][:16] if row else None,
            "latest_export_age_minutes": int((time.time() - row[1]) / 60) if row else None,
            "history_from": first, "latest_reading": latest}


def op_summary(a):
    first, last = _dates(a)
    con = connect()
    got = ingest(con)
    days, units = {}, {}
    metrics = [m for (m,) in con.execute("SELECT DISTINCT metric FROM sections")]
    for metric in metrics:
        kind = KIND.get(metric, "daily")
        if kind == "sleep":
            continue
        rows = effective(con, metric, first, last)
        if not rows:
            continue
        per_day = {"total": daily_totals, "daily": daily_values, "readings": daily_readings}[kind](rows)
        u = _unit(rows)
        if u:
            units[metric] = u
        for d, v in per_day.items():
            if v is not None and not (isinstance(v, dict) and v.get("total", 0) is None):
                days.setdefault(d, {})[metric] = v
    # a sleep that ended on `first` started the evening before, so look one day back
    prev = (datetime.date.fromisoformat(first) - datetime.timedelta(days=1)).isoformat()
    sleep = [s for s in sleep_sessions(effective(con, "sleep", prev, last)) if first <= s["date"] <= last]
    return {"start": first, "end": last, "days": [dict(date=d, **days[d]) for d in sorted(days)], "sleep": sleep, "units": units,
            "freshness": freshness(con), "new_exports": got["added"] + got["updated"],
            **({"exports_still_downloading": got["waiting"]} if got.get("waiting") else {})}


def op_day(a):
    first, _last = _dates({"start": a.get("date")})
    metric = str(a.get("metric") or "")
    con = connect()
    ingest(con)
    kind = KIND.get(metric)
    if kind is None:
        known = sorted({m for (m,) in con.execute("SELECT DISTINCT metric FROM sections")})
        raise HealthError("unknown metric %r; known: %s" % (metric, ", ".join(known)))
    if kind == "sleep":
        prev = (datetime.date.fromisoformat(first) - datetime.timedelta(days=1)).isoformat()
        rows = sorted(effective(con, "sleep", prev, first), key=lambda r: r[2])
        sessions = [s for s in sleep_sessions(rows) if s["date"] == first]
        lo = min((s["start"] for s in sessions), default=None)
        stages = [{"stage": r[5], "start": r[0][11:16], "end": r[1][11:16]} for r in rows
                  if sessions and r[0][:16] >= lo and r[1][:10] <= first]
        return {"date": first, "metric": metric, "sleep": sessions, "stages": stages}
    rows = sorted(effective(con, metric, first, first), key=lambda r: r[2])
    unit = _unit(rows)
    if kind == "total":
        hours = {}
        for r in rows:
            if r[4] is not None:
                hours[hour_of(r[0])[11:13] + ":00"] = _round(hours.get(hour_of(r[0])[11:13] + ":00", 0) + r[4], 2)
        return {"date": first, "metric": metric, "unit": unit, "hours": hours, **daily_totals(rows).get(first, {"total": None})}
    if kind == "readings":
        pts = [(r[0][11:16], r[4]) for r in rows if r[4] is not None and r[4] > 0]
        step = max(1, len(pts) // 300)                   # at most ~300 points: an agent needs the shape, not every beat
        return {"date": first, "metric": metric, "unit": unit, **daily_readings(rows).get(first, {}),
                "readings_shown": [{"time": t, "value": v} for t, v in pts[::step]]}
    return {"date": first, "metric": metric, "unit": unit, "value": daily_values(rows).get(first)}


def op_status(a):
    con = connect()
    got = ingest(con)
    files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {"exports": files, "refresh_set_up": os.path.exists(REFRESH_CONFIG), **freshness(con),
            **({"folder_missing": True} if got.get("folder_missing") else {})}


def _newest_file():
    try:
        names = sorted(n for n in os.listdir(FOLDER) if FILE_NAME.match(n))
    except OSError:
        return None
    return os.path.join(FOLDER, names[-1]) if names else None


def op_refresh(a):
    budget = max(5, int(a.get("budget") or 40))
    deadline = time.time() + budget - 3
    con = connect()
    ingest(con)
    fresh = freshness(con)
    age = fresh["latest_export_age_minutes"]
    if age is not None and age < 10:
        return {"refreshed": False, "reason": "the latest export is only %d minutes old" % age, "freshness": fresh}
    try:
        with open(REFRESH_CONFIG) as f:
            cfg = json.load(f)
        command = cfg["command"]
        if not (isinstance(command, list) and command and all(isinstance(x, str) for x in command)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return {"refreshed": False, "reason": "refreshing is not set up on the Mac (no valid %s)" % os.path.basename(REFRESH_CONFIG),
                "freshness": fresh}
    min_minutes = max(1, int(cfg.get("min_minutes", 10)))
    try:
        last = float(open(REFRESH_STATE).read().strip())
    except (OSError, ValueError):
        last = 0.0
    asked = time.time()
    if asked - last < min_minutes * 60:
        asked = last                                  # asked recently already: wait for that answer instead of asking again
    else:
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"refreshed": False, "reason": "the refresh command failed: %s" % e, "freshness": fresh}
        if proc.returncode != 0:
            return {"refreshed": False, "reason": "the refresh command failed: " + proc.stderr.decode("utf-8", "replace").strip()[:200],
                    "freshness": fresh}
        with open(REFRESH_STATE, "w") as f:
            f.write(str(asked))
    seen = None
    while time.time() < deadline:
        path = _newest_file()
        if path and os.path.getmtime(path) >= asked - 2 and not offloaded(path):
            size = os.path.getsize(path)
            if seen and seen[0] == path and seen[1] == size and time.time() - seen[2] >= STABLE_SECONDS:
                ingest(con, download=False)
                return {"refreshed": True, "freshness": freshness(con)}
            if not seen or seen[0] != path or seen[1] != size:
                seen = (path, size, time.time())
        time.sleep(1)
    ingest(con)
    return {"refreshed": False, "reason": "the iPhone did not send new data in time (it may be locked or offline); the figures are "
            "from the latest export", "freshness": freshness(con)}


OPS = {"health_summary": op_summary, "health_day": op_day, "health_status": op_status, "health_refresh": op_refresh}


def main(argv):
    if len(argv) == 3 and argv[1] == "import":            # run by hand on the Mac only: the server has no way to ask for it
        try:
            print(json.dumps(import_export(argv[2]), ensure_ascii=False))
        except (HealthError, OSError) as e:
            print("import failed: %s" % e, file=sys.stderr)
            return 1
        return 0
    if len(argv) != 3 or argv[1] not in OPS:
        print("usage: health.py <%s> '<json>'" % "|".join(sorted(OPS)), file=sys.stderr)
        return 2
    try:
        result = OPS[argv[1]](json.loads(argv[2]))
    except HealthError as e:
        print("execution error: Error: %s" % e, file=sys.stderr)
        return 1
    except PermissionError:
        print("execution error: Error: macOS has not given the helper access to the Shortcuts folder in iCloud Drive (System Settings > "
              "Privacy & Security > Full Disk Access or Files and Folders, for python3)", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
