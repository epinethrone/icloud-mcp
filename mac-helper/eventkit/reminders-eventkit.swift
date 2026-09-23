// The six Reminders operations of icloud-mac-helper, through EventKit. Built on the Mac by install.sh (never shipped compiled), with
// Info.plist embedded in __TEXT,__info_plist and an ad-hoc signature carrying the same bundle id: without that usage string macOS 27
// shows no permission prompt at all and the request silently never succeeds.
//
// Contract: `reminders-eventkit <op> '<json args>'`. Exit 0 with one JSON value on stdout; nonzero exit with a plain-text message on
// stderr. Args arrive already type/length-validated by the Python helper's validate_args(); this binary still enforces the op-specific
// business rules (ambiguous list name, not found, impossible due date) the JS scripts enforced. One process per operation: measured
// fixed cost is ~21 ms under the LaunchAgent, which a long-lived process would save but a network round trip hides.
//
// IDENTIFIERS: EKReminder.calendarItemIdentifier is the JXA/AppleScript id without its "x-apple-reminder://" prefix (verified on the Mac
// for all 50 open reminders across 11 lists; list ids are identical in both). Ids are returned bare and accepted in either form, so an
// id an agent kept from the JXA era still resolves.

import EventKit
import Foundation

// MARK: - Output helpers

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func printJSON(_ value: Any) {
    guard JSONSerialization.isValidJSONObject(value),
          let data = try? JSONSerialization.data(withJSONObject: value, options: []) else {
        fail("internal error: the result could not be encoded as JSON")   // v1 wrote an empty body here, which surfaced as the useless "the script returned something that is not JSON"
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func jsonOrNull(_ s: String?) -> Any { s ?? NSNull() }   // a bare Swift nil inside [String: Any] is NOT valid JSON input and made v1 emit nothing

guard CommandLine.arguments.count >= 3 else { fail("usage: reminders-eventkit <op> '<json args>'") }
let op = CommandLine.arguments[1]
guard let argsData = CommandLine.arguments[2].data(using: .utf8),
      let args = (try? JSONSerialization.jsonObject(with: argsData)) as? [String: Any] else {
    fail("arguments must be a JSON object")
}

// MARK: - Access
//
// EventKit needs "Full Access to Reminders", which is a DIFFERENT grant from the Automation grant the JXA path uses; the installer and
// self-test both need a new check. The failure this guards against: with no grant, calendars(for:) returns [] and fetches return [] with
// no error at all, so a denied helper looks exactly like an empty Reminders. Every op therefore checks status explicitly, including
// reminder_lists, which v1 did not check at all.

// Switched on rawValue on purpose: .authorized is deprecated on macOS 14+ while .fullAccess / .writeOnly do not exist below it, so
// naming both in one switch either warns or fails to build depending on the SDK. The raw values are stable API.
func authorizationDescription(_ status: EKAuthorizationStatus) -> String {
    switch status.rawValue {
    case 0: return "not yet asked"
    case 1: return "blocked by a device policy (Screen Time or MDM)"
    case 2: return "denied"
    case 3: return "granted"
    case 4: return "write-only, which is not enough to read reminders"
    default: return "unknown (\(status.rawValue))"
    }
}

func hasFullAccess(_ status: EKAuthorizationStatus) -> Bool { status.rawValue == 3 }

let store = EKEventStore()

@discardableResult
func ensureAccess() -> EKAuthorizationStatus {
    var status = EKEventStore.authorizationStatus(for: .reminder)
    if status == .notDetermined {
        let sem = DispatchSemaphore(value: 0)
        var requestError: Error?
        guard #available(macOS 14.0, *) else { fail("this helper needs macOS 14 or newer for EventKit reminder access") }
        store.requestFullAccessToReminders { _, err in requestError = err; sem.signal() }
        _ = sem.wait(timeout: .now() + 30)                   // a prompt nobody clicks must not hang the helper forever
        status = EKEventStore.authorizationStatus(for: .reminder)
        if status == .notDetermined {
            // Two causes look identical from here: a prompt nobody answered, or NO prompt at all because the binary was built without its
            // embedded usage string (macOS 27 then never shows one and never calls back). Say both rather than guess.
            fail("Full Access to Reminders is not granted yet (not yet asked). Either the macOS prompt went unanswered, or none was shown "
                 + "because this binary lacks its embedded Reminders usage string; re-run install.sh"
                 + (requestError.map { " (\($0.localizedDescription))" } ?? "."))
        }
    }
    guard hasFullAccess(status) else {
        fail("Full Access to Reminders is not granted (currently: \(authorizationDescription(status))). Enable \"iCloud Mac Helper "
             + "(Reminders)\" in System Settings > Privacy & Security > Reminders, then try again.")
    }
    return status
}

// MARK: - Due dates
//
// THE OFFSET DECISION, written down because this is the bit that goes wrong quietly.
//
// EventKit stores a reminder's due date as DateComponents, not as an instant. The JXA path stores an absolute Date, which Reminders then
// DISPLAYS in whatever zone the Mac is in. So the only way to keep behaviour identical across the cutover is a two-step:
//   1. resolve the input to one absolute instant, honouring an explicit offset (or Z) when the string carries one, and treating a bare
//      local date-time as local, exactly as `new Date("...")` does in the JS scripts;
//   2. express that instant as wall-clock components in the Mac's CURRENT zone, and pin comps.timeZone to that zone.
//
// Why local components and not the source offset's components: Reminders shows a due date at the pinned wall time, so pinning
// 2026-03-01T09:00+05:00 as 09:00 in a +05:00 zone would display 05:00 in Amsterdam, while the JXA path displays 05:00 too but stores the
// same instant. Converting to local components keeps BOTH the displayed time and the instant identical to today's behaviour.
// Why pinned rather than floating (comps.timeZone = nil): a floating due date follows the device across zones, which JXA's absolute Date
// does not. Pinning matches current behaviour. A bare date is pinned too, for the same reason, even though floating would arguably be
// nicer for "sometime on the 3rd" - that would be a behaviour change and belongs in its own decision, not in a port.
//
// Rules preserved from parseDue() in reminder_create.js / reminder_update.js: a bare date means 09:00 local; an impossible calendar date
// (2026-02-30) is refused rather than rolled over; hour > 23 or minute > 59 is refused. Seconds and fractional seconds are accepted and
// ignored down to the second, which validate_args()'s _ISO regex already permits and v1's prefix-match silently dropped along with the
// whole offset.

let localZone = TimeZone.current
let localCalendar: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone.current
    return c
}()

let dueBare = try! NSRegularExpression(pattern: "^(\\d{4})-(\\d{2})-(\\d{2})$")
let dueFull = try! NSRegularExpression(
    pattern: "^(\\d{4})-(\\d{2})-(\\d{2})[T ](\\d{2}):(\\d{2})(?::(\\d{2})(?:\\.\\d+)?)?(Z|[+-]\\d{2}:?\\d{2})?$")

func intAt(_ s: NSString, _ m: NSTextCheckingResult, _ i: Int) -> Int? {
    let r = m.range(at: i)
    return r.location == NSNotFound ? nil : Int(s.substring(with: r))
}

func realCalendarDate(_ y: Int, _ mo: Int, _ d: Int) -> Bool {
    var probe = DateComponents(); probe.year = y; probe.month = mo; probe.day = d; probe.hour = 12
    guard let date = localCalendar.date(from: probe) else { return false }
    let back = localCalendar.dateComponents([.year, .month, .day], from: date)
    return back.year == y && back.month == mo && back.day == d     // refuse a roll-over the way the JS probe does
}

func zoneFor(offset: String) -> TimeZone? {
    if offset == "Z" { return TimeZone(secondsFromGMT: 0) }
    let sign = offset.hasPrefix("-") ? -1 : 1
    let digits = offset.dropFirst().replacingOccurrences(of: ":", with: "")
    guard digits.count == 4, let hh = Int(digits.prefix(2)), let mm = Int(digits.suffix(2)), hh <= 23, mm <= 59 else { return nil }
    return TimeZone(secondsFromGMT: sign * (hh * 3600 + mm * 60))
}

/// Resolves an input string to the absolute instant it names.
func dueInstant(_ s: String) -> Date {
    let ns = s as NSString
    let range = NSRange(location: 0, length: ns.length)

    if let m = dueBare.firstMatch(in: s, range: range) {
        guard let y = intAt(ns, m, 1), let mo = intAt(ns, m, 2), let d = intAt(ns, m, 3), realCalendarDate(y, mo, d) else { fail("invalid due date") }
        var c = DateComponents(); c.year = y; c.month = mo; c.day = d; c.hour = 9; c.minute = 0; c.second = 0
        c.timeZone = localZone
        guard let date = localCalendar.date(from: c) else { fail("invalid due date") }
        return date
    }

    guard let m = dueFull.firstMatch(in: s, range: range),
          let y = intAt(ns, m, 1), let mo = intAt(ns, m, 2), let d = intAt(ns, m, 3),
          let h = intAt(ns, m, 4), let mi = intAt(ns, m, 5) else { fail("invalid due date") }
    let sec = intAt(ns, m, 6) ?? 0
    guard h <= 23, mi <= 59, sec <= 59, realCalendarDate(y, mo, d) else { fail("invalid due date") }

    var c = DateComponents(); c.year = y; c.month = mo; c.day = d; c.hour = h; c.minute = mi; c.second = sec

    let offsetRange = m.range(at: 7)
    if offsetRange.location != NSNotFound {
        guard let zone = zoneFor(offset: ns.substring(with: offsetRange)) else { fail("invalid due date") }
        var srcCalendar = Calendar(identifier: .gregorian)
        srcCalendar.timeZone = zone
        c.timeZone = zone
        guard let date = srcCalendar.date(from: c) else { fail("invalid due date") }
        return date                                          // an explicit offset names one instant, wherever this Mac happens to be
    }

    c.timeZone = localZone
    guard let date = localCalendar.date(from: c) else { fail("invalid due date") }
    return date                                              // no offset means local time, matching `new Date("2026-03-01T09:00")`
}

/// The components actually stored on the reminder: local wall clock, pinned to this Mac's zone.
func dueComponents(_ s: String) -> DateComponents {
    var comps = localCalendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: dueInstant(s))
    comps.timeZone = localZone
    return comps
}

/// The inverse, for output. Honours a pinned zone if the reminder carries one (it may have been written on another device).
func dueISO(_ comps: DateComponents?) -> String? {
    guard let comps = comps else { return nil }
    var cal = Calendar(identifier: .gregorian)
    cal.timeZone = comps.timeZone ?? localZone
    guard let date = cal.date(from: comps) else { return nil }
    let f = ISO8601DateFormatter()
    f.timeZone = TimeZone(secondsFromGMT: 0)                 // always UTC "Z", matching Date.toISOString() in the JS scripts. The Python
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]   // cache sorts these strings lexicographically, so one fixed shape matters.
    return f.string(from: date)
}

// MARK: - Lists and reminders

func allLists() -> [EKCalendar] { store.calendars(for: .reminder) }

func findList(id: String?, name: String?) -> EKCalendar {
    let lists = allLists()
    if let id = id {
        guard let l = lists.first(where: { $0.calendarIdentifier == id }) else { fail("list not found (-1728)") }
        return l
    }
    if let name = name {
        let hits = lists.filter { $0.title == name }
        if hits.isEmpty { fail("list not found (-1728): \(name)") }
        if hits.count > 1 { fail("several lists are named '\(name)'; pass list_id (from reminders_lists) to choose one") }
        return hits[0]
    }
    guard let d = store.defaultCalendarForNewReminders() ?? lists.first else { fail("no Reminders list is available") }
    return d
}

/// JXA ids are "x-apple-reminder://<calendarItemIdentifier>"; everything here uses the bare form, and accepts either.
let jxaIdPrefix = "x-apple-reminder://"
func bareId(_ id: String) -> String { id.hasPrefix(jxaIdPrefix) ? String(id.dropFirst(jxaIdPrefix.count)) : id }

func reminderJSON(_ r: EKReminder) -> [String: Any] {
    return ["id": bareId(r.calendarItemIdentifier), "title": r.title ?? "", "notes": r.notes ?? "", "completed": r.isCompleted,
            "due": jsonOrNull(dueISO(r.dueDateComponents)), "priority": r.priority,
            "list": r.calendar.title, "list_id": r.calendar.calendarIdentifier, "account": r.calendar.source.title]
}

func fetchIncomplete(in lists: [EKCalendar]) -> [EKReminder] {
    let predicate = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: lists)
    let sem = DispatchSemaphore(value: 0)
    var out: [EKReminder] = []
    var answered = false
    store.fetchReminders(matching: predicate) { r in out = r ?? []; answered = true; sem.signal() }
    if sem.wait(timeout: .now() + 55) == .timedOut || !answered {
        fail("Reminders did not answer within 55s")          // silence here used to be indistinguishable from an empty list
    }
    return out
}

func findReminder(id: String) -> EKReminder {
    guard let item = store.calendarItem(withIdentifier: bareId(id)) as? EKReminder else { fail("reminder not found (-1728)") }
    return item
}

func saveOrFail(_ r: EKReminder) {
    do { try store.save(r, commit: true) } catch { fail("could not save: \(error.localizedDescription)") }
}

// MARK: - Dispatch

switch op {
case "reminder_lists":
    ensureAccess()                                           // v1 omitted this, so a denied helper returned [] and looked like an empty Reminders
    let lists = allLists()
    if lists.isEmpty { fail("Reminders reported no lists at all, which usually means the account has not finished loading; try again shortly") }
    printJSON(lists.map { ["id": $0.calendarIdentifier, "name": $0.title, "account": $0.source.title] })

case "reminders_list":
    ensureAccess()
    let listId = args["list_id"] as? String
    let listName = args["list"] as? String
    let lists: [EKCalendar] = (listId != nil || listName != nil) ? [findList(id: listId, name: listName)] : allLists()
    if lists.isEmpty { fail("Reminders reported no lists at all, which usually means the account has not finished loading; try again shortly") }
    var items = fetchIncomplete(in: lists)                   // active reminders only, matching the current tool contract
    if let q = (args["query"] as? String)?.lowercased(), !q.isEmpty {
        items = items.filter { ($0.title ?? "").lowercased().contains(q) || ($0.notes ?? "").lowercased().contains(q) }
    }
    let keyed = items.map { (r: $0, due: dueISO($0.dueDateComponents)) }
        .sorted { a, b in
            if a.due == nil && b.due == nil { return false }
            if a.due == nil { return false }
            if b.due == nil { return true }
            return a.due! < b.due!                           // same "undated last, then ascending" order the cache produces
        }
    let limit = max(1, (args["limit"] as? Int) ?? 50)
    printJSON(["reminders": keyed.prefix(limit).map { reminderJSON($0.r) }])   // no "cached" key: every read is live

case "reminder_create":
    ensureAccess()
    guard let title = args["title"] as? String, !title.isEmpty else { fail("title is required") }
    let comps = (args["due"] as? String).map(dueComponents)  // validate the date BEFORE creating anything, as the JS script does
    let list = findList(id: args["list_id"] as? String, name: args["list"] as? String)
    let r = EKReminder(eventStore: store)
    r.title = title
    r.calendar = list
    if let notes = args["notes"] as? String { r.notes = notes }
    if let priority = args["priority"] as? Int { r.priority = priority }
    if let comps = comps { r.dueDateComponents = comps }
    saveOrFail(r)
    printJSON(reminderJSON(r))

case "reminder_update":
    ensureAccess()
    guard let id = args["id"] as? String else { fail("id is required") }
    let clearDue = (args["clear_due"] as? Bool) == true
    let comps = clearDue ? nil : (args["due"] as? String).map(dueComponents)   // validate BEFORE touching anything: v1 mutated title and
    let touchesDue = clearDue || comps != nil                                  // notes first and could leave a half-applied edit behind
    guard args["title"] as? String != nil || args["notes"] as? String != nil || args["priority"] as? Int != nil || touchesDue else {
        fail("nothing to update")
    }
    let r = findReminder(id: id)
    if let title = args["title"] as? String { r.title = title }
    if let notes = args["notes"] as? String { r.notes = notes }
    if let priority = args["priority"] as? Int { r.priority = priority }
    if touchesDue { r.dueDateComponents = comps }
    saveOrFail(r)
    printJSON(reminderJSON(r))

case "reminder_complete":
    ensureAccess()
    guard let id = args["id"] as? String else { fail("id is required") }
    let r = findReminder(id: id)
    r.isCompleted = (args["completed"] as? Bool) ?? true
    saveOrFail(r)
    printJSON(["id": bareId(r.calendarItemIdentifier), "title": r.title ?? "", "completed": r.isCompleted])

case "reminder_delete":
    ensureAccess()
    guard let id = args["id"] as? String else { fail("id is required") }
    let r = findReminder(id: id)
    let title = r.title ?? ""
    do { try store.remove(r, commit: true) } catch { fail("could not delete: \(error.localizedDescription)") }
    printJSON(["deleted": bareId(id), "title": title])

default:
    fail("unknown operation \(op)")
}
