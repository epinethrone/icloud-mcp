// Apple Maps for icloud-mac-helper: travel time between two places and place search, through MapKit. Built on the Mac by
// install.sh (never shipped compiled) and ad-hoc signed. It needs no permission: places are addresses, names or coordinates the
// agent gives, never the Mac's own location.
//
// Contract: `maps-cli <op> '<json args>'`. Exit 0 with one JSON value on stdout; nonzero exit with a plain-text message on stderr.
// MapKit answers on the main queue, so the main run loop is pumped while waiting (a semaphore on main would deadlock).
// MKPlacemark and MKMapItem.placemark are deprecated since macOS 26 but still work; their replacements exist only in the macOS 26
// SDK, and using them would stop the helper building on older Macs. The build warnings about them are expected.
import Foundation
import MapKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func printJSON(_ value: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { fail("could not encode the result") }
    FileHandle.standardOutput.write(data)
}

guard CommandLine.arguments.count >= 3 else { fail("usage: maps-cli <op> '<json args>'") }
let op = CommandLine.arguments[1]
guard let argData = CommandLine.arguments[2].data(using: .utf8),
      let args = (try? JSONSerialization.jsonObject(with: argData)) as? [String: Any] else { fail("arguments must be a JSON object") }

func pump(_ seconds: Double, _ done: () -> Bool) -> Bool {
    let end = Date().addingTimeInterval(seconds)
    while !done() && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
    return done()
}

func explain(_ error: Error, _ what: String) -> String {
    if let e = error as? MKError {
        switch e.code {
        case .loadingThrottled: return "Apple Maps is limiting requests right now; try again in a minute"
        case .placemarkNotFound: return "Apple Maps could not find \(what)"
        case .directionsNotFound: return "Apple Maps has no \(what)"
        case .serverFailure: return "Apple Maps did not answer (server failure); try again shortly"
        default: break
        }
    }
    return "Apple Maps: \(error.localizedDescription)"
}

/// A failed directions request: throttling and real outages keep their own message, everything else means no route.
func noRouteOr(_ error: Error?, _ message: String) -> String {
    if let e = error as? MKError, e.code == .loadingThrottled { return explain(e, "") }
    return message
}

// MARK: - Places

let coordinatePattern = try! NSRegularExpression(pattern: "^\\s*(-?\\d{1,2}(?:\\.\\d+)?)\\s*,\\s*(-?\\d{1,3}(?:\\.\\d+)?)\\s*$")

func search(_ query: String, near: MKCoordinateRegion? = nil, limit: Int = 1) -> [MKMapItem] {
    let req = MKLocalSearch.Request()
    req.naturalLanguageQuery = query
    if let near = near { req.region = near }
    var items: [MKMapItem] = [], err: Error? = nil, done = false
    MKLocalSearch(request: req).start { r, e in items = r?.mapItems ?? []; err = e; done = true }
    guard pump(20, { done }) else { fail("Apple Maps did not answer a place search within 20 seconds") }
    if let err = err, items.isEmpty {
        // Apple Maps answers an unknown place with "server failure" as often as with "not found": both mean no result here.
        if let e = err as? MKError, e.code == .placemarkNotFound || e.code == .serverFailure { return [] }
        fail(explain(err, "'\(query)'"))
    }
    return Array(items.prefix(limit))
}

func resolve(_ text: String, _ label: String, near: MKCoordinateRegion? = nil) -> MKMapItem {
    let ns = text as NSString
    if let m = coordinatePattern.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)),
       let lat = Double(ns.substring(with: m.range(at: 1))), let lon = Double(ns.substring(with: m.range(at: 2))),
       abs(lat) <= 90, abs(lon) <= 180 {
        return MKMapItem(placemark: MKPlacemark(coordinate: CLLocationCoordinate2D(latitude: lat, longitude: lon)))
    }
    guard let item = search(text, near: near).first else {
        fail("Apple Maps could not find the \(label) '\(text)'. Check the spelling, or add the street and city.")
    }
    return item
}

func placeJSON(_ item: MKMapItem, query: String? = nil) -> [String: Any] {
    let c = item.placemark.coordinate
    var out: [String: Any] = ["name": item.name ?? "", "latitude": c.latitude, "longitude": c.longitude]
    if let q = query { out["query"] = q }
    if let address = item.placemark.title, !address.isEmpty { out["address"] = address }
    if let cat = item.pointOfInterestCategory { out["category"] = cat.rawValue.replacingOccurrences(of: "MKPOICategory", with: "") }
    if let phone = item.phoneNumber, !phone.isEmpty { out["phone"] = phone }
    if let url = item.url { out["url"] = url.absoluteString }
    return out
}

// MARK: - Times

func parseTime(_ s: String) -> Date {
    let iso = ISO8601DateFormatter()
    for opts: ISO8601DateFormatter.Options in [[.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime]] {
        iso.formatOptions = opts
        if let d = iso.date(from: s) { return d }
    }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone.current
    for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm"] {
        f.dateFormat = format
        if let d = f.date(from: s) { return d }
    }
    fail("not a date-time: '\(s)' (use ISO 8601, e.g. 2026-10-02T09:30)")
}

func isoLocal(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.timeZone = TimeZone.current
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

// MARK: - Operations

switch op {
case "maps_travel_time":
    guard let o = args["origin"] as? String, let d = args["destination"] as? String else { fail("origin and destination are required") }
    let modes: [String: (MKDirectionsTransportType, String)] = [
        "walking": (.walking, "WALKING"), "cycling": (.cycling, "BICYCLE"), "driving": (.automobile, "AUTOMOBILE"), "transit": (.transit, "TRANSIT")]
    let modeName = (args["mode"] as? String) ?? "cycling"
    guard let (transport, routing) = modes[modeName] else { fail("mode must be walking, cycling, driving or transit") }
    let departAt = (args["depart_at"] as? String).map(parseTime)
    let arriveAt = (args["arrive_at"] as? String).map(parseTime)
    if departAt != nil && arriveAt != nil { fail("give depart_at or arrive_at, not both") }
    let from = resolve(o, "origin")
    // The destination is looked up around the origin, so "Domtoren" means the one in the same city, not a namesake far away.
    let to = resolve(d, "destination", near: MKCoordinateRegion(center: from.placemark.coordinate,
                                                                span: MKCoordinateSpan(latitudeDelta: 1.0, longitudeDelta: 1.0)))
    func where_(_ i: MKMapItem) -> String { [i.name, i.placemark.title].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ") }
    let noRoute = { (what: String) -> String in
        "Apple Maps has no \(what) from \(where_(from)) to \(where_(to)). Check these are the places meant; add the street and city if not."
    }
    let req = MKDirections.Request()
    req.source = from
    req.destination = to
    req.transportType = transport
    if let a = arriveAt { req.arrivalDate = a } else { req.departureDate = departAt ?? Date() }
    var out: [String: Any] = ["origin": placeJSON(from, query: o), "destination": placeJSON(to, query: d), "mode": modeName,
                              "travel_routing": routing, "source": "Apple Maps estimate", "computed_at": isoLocal(Date()),
                              "note": "An estimate from Apple Maps for this time of day, not a measured journey."]
    var done = false, err: Error? = nil
    if transport == .transit {                     // public transport: Apple gives a travel time, not a route
        var eta: MKDirections.ETAResponse? = nil
        MKDirections(request: req).calculateETA { r, e in eta = r; err = e; done = true }
        guard pump(25, { done }) else { fail("Apple Maps did not answer within 25 seconds") }
        guard let r = eta else { fail(noRouteOr(err, noRoute("public transport route"))) }
        out["minutes"] = Int((r.expectedTravelTime / 60).rounded(.up))
        out["distance_km"] = (r.distance / 100).rounded() / 10
        out["departure"] = isoLocal(r.expectedDepartureDate)
        out["arrival"] = isoLocal(r.expectedArrivalDate)
        out["route"] = NSNull()
    } else {
        req.requestsAlternateRoutes = (args["alternatives"] as? Bool) == true
        var resp: MKDirections.Response? = nil
        MKDirections(request: req).calculate { r, e in resp = r; err = e; done = true }
        guard pump(25, { done }) else { fail("Apple Maps did not answer within 25 seconds") }
        guard let routes = resp?.routes, let best = routes.first else { fail(noRouteOr(err, noRoute("\(modeName) route"))) }
        let minutes = Int((best.expectedTravelTime / 60).rounded(.up))
        out["minutes"] = minutes
        out["distance_km"] = (best.distance / 100).rounded() / 10
        let dep = arriveAt.map { $0.addingTimeInterval(-best.expectedTravelTime) } ?? (departAt ?? Date())
        out["departure"] = isoLocal(dep)
        out["arrival"] = isoLocal(dep.addingTimeInterval(best.expectedTravelTime))
        out["route"] = ["name": best.name, "has_tolls": best.hasTolls, "has_highways": best.hasHighways]
        if routes.count > 1 {
            out["alternatives"] = routes.dropFirst().map { ["name": $0.name, "minutes": Int(($0.expectedTravelTime / 60).rounded(.up)),
                                                            "distance_km": ($0.distance / 100).rounded() / 10] }
        }
    }
    printJSON(out)

case "maps_search":
    guard let q = args["query"] as? String, !q.isEmpty else { fail("query is required") }
    let limit = max(1, min((args["limit"] as? Int) ?? 10, 20))
    var region: MKCoordinateRegion? = nil
    if let near = args["near"] as? String, !near.isEmpty {
        let center = resolve(near, "place to search near").placemark.coordinate
        region = MKCoordinateRegion(center: center, span: MKCoordinateSpan(latitudeDelta: 0.2, longitudeDelta: 0.2))
    }
    printJSON(["places": search(q, near: region, limit: limit).map { placeJSON($0) }])

default:
    fail("unknown operation \(op)")
}
