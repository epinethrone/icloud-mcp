import Foundation
import Observation
import ServiceManagement

/// The one summary the menu bar icon and the popover header show.
enum Overall: Equatable {
    case notConfigured, starting, stopped, unreachable, paused, issues(Int), running

    var title: String {
        switch self {
        case .notConfigured: "Not set up"
        case .starting: "Starting"
        case .stopped: "Stopped"
        case .unreachable: "Not responding"
        case .paused: "Paused"
        case .issues(let n): n == 1 ? "1 issue" : "\(n) issues"
        case .running: "Running"
        }
    }

    /// Template symbols that differ in shape, so state never depends on colour alone.
    var symbol: String {
        switch self {
        case .notConfigured: "icloud.dashed"
        case .starting: "icloud"
        case .stopped: "icloud.slash"
        case .unreachable, .issues: "exclamationmark.icloud"
        case .paused: "pause.circle"
        case .running: "icloud"
        }
    }
}

struct Issue: Identifiable, Equatable {
    let id: String
    let title: String
    let detail: String?
}

@MainActor
@Observable
final class Controller {
    // Settings, kept in UserDefaults (the app writes no files of its own).
    var dataFolder: String { didSet { defaults.set(dataFolder, forKey: "dataFolder"); refreshSoon() } }
    var adminPort: Int { didSet { defaults.set(adminPort, forKey: "adminPort"); refreshSoon() } }
    var labels: [ServiceJob.Role: String] {
        didSet { defaults.set(Dictionary(uniqueKeysWithValues: labels.map { ($0.key.rawValue, $0.value) }), forKey: "labels") }
    }

    private(set) var discovered: [ServiceJob] = []
    private(set) var states: [ServiceJob.Role: JobState] = [:]
    private(set) var status: ServerStatus?
    private(set) var statusError: AdminError?
    private(set) var health: HealthReport?
    private(set) var healthCheckedAt: Date?
    private(set) var busy: String?
    var lastError: String?
    var popoverVisible = false { didSet { if popoverVisible { refreshSoon(withHealth: true) } } }

    private let defaults = UserDefaults.standard
    private var loop: Task<Void, Never>?
    private var restartedAt: Date?

    init() {
        dataFolder = defaults.string(forKey: "dataFolder") ?? ""
        adminPort = defaults.object(forKey: "adminPort") as? Int ?? 8002
        let saved = defaults.dictionary(forKey: "labels") as? [String: String] ?? [:]
        labels = Dictionary(uniqueKeysWithValues: saved.compactMap { k, v in ServiceJob.Role(rawValue: k).map { ($0, v) } })
        rediscover()
        if dataFolder.isEmpty, let guess = Services.guessDataFolders(server: job(.server)).first { dataFolder = guess }
        loop = Task { [weak self] in await self?.run() }
    }

    // MARK: - Jobs

    func rediscover() {
        discovered = Services.discover()
        for role in ServiceJob.Role.allCases where labels[role] == nil || !discovered.contains(where: { $0.label == labels[role] }) {
            if let found = Services.best(for: role, in: discovered) { labels[role] = found.label }
            else { labels[role] = nil }
        }
    }

    func job(_ role: ServiceJob.Role) -> ServiceJob? {
        labels[role].flatMap { label in discovered.first { $0.label == label } }
    }

    var roles: [ServiceJob.Role] { ServiceJob.Role.allCases.filter { job($0) != nil } }

    // MARK: - Summary

    var client: AdminClient { AdminClient(port: adminPort, dataFolder: dataFolder) }

    var issues: [Issue] {
        var out: [Issue] = []
        for role in roles where states[role] == .notRunning {
            out.append(Issue(id: "job-\(role)", title: "\(role.title) is not running", detail: "launchd is trying to start it again."))
        }
        if let helper = status?.helper, !helper.online, job(.helper) != nil || status?.tools.byArea.keys.contains("reminders") == true {
            out.append(Issue(id: "helper", title: "Mac helper is offline", detail: "Reminders, Notes and other Mac features are unavailable."))
        }
        // The helper's line in the health check duplicates the live status above, which is fresher.
        for (area, result) in (health?.areas ?? [:]).sorted(by: { $0.key < $1.key }) where !result.ok && area != "mac_helper" {
            out.append(Issue(id: "area-\(area)", title: "\(Self.areaName(area)) is not working", detail: Self.explain(result.error)))
        }
        return out
    }

    var overall: Overall {
        if dataFolder.isEmpty && job(.server) == nil { return .notConfigured }
        if states[.server] == .stopped { return .stopped }
        if status == nil {
            if let restartedAt, Date().timeIntervalSince(restartedAt) < 30 { return .starting }
            return statusError == nil ? .starting : (statusError == .notConfigured ? .notConfigured : .unreachable)
        }
        if status?.paused == true { return .paused }
        let n = issues.count
        return n > 0 ? .issues(n) : .running
    }

    var summaryLine: String {
        switch overall {
        case .running, .issues, .paused:
            guard let status else { return overall.title }
            let tools = status.tools.total == 1 ? "1 tool" : "\(status.tools.total) tools"
            return "\(overall.title) · \(tools)"
        default:
            return overall.title
        }
    }

    /// The server's health messages are written for agents; this turns them into one plain sentence for a person.
    static func explain(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        let text = raw.lowercased()
        // Network first: "connection/login failed: Connection refused" is a network problem, not a wrong password.
        if ["connection refused", "connectionerror", "could not connect", "timed out", "timeout", "name or service not known",
            "nodename nor servname", "network is unreachable"].contains(where: text.contains) {
            return "Could not reach iCloud. Check the internet connection."
        }
        if ["authentication failed", "authenticationfailed", "login failed", "invalid credentials", "401"].contains(where: text.contains) {
            return "iCloud refused the sign-in. The app-specific password may have been revoked; replace it in Settings."
        }
        var message = raw
        if let colon = message.firstIndex(of: ":"), message[..<colon].hasSuffix("Error") {        // "CalendarError: ..." -> "..."
            message = String(message[message.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        let sentences = message.components(separatedBy: ". ").filter { !$0.contains("icloud_check_health") }
        message = sentences.joined(separator: ". ")
        return message.count > 140 ? String(message.prefix(139)) + "…" : message
    }

    static func areaName(_ key: String) -> String {
        ["mail": "Mail", "calendar": "Calendar", "contacts": "Contacts", "reminders": "Reminders", "notes": "Notes",
         "drive": "iCloud Drive", "maps": "Maps", "imessage": "Messages", "mac": "Mac helper", "helper": "Mac helper",
         "shortcuts": "Shortcuts"][key] ?? key.capitalized
    }

    // MARK: - Refreshing

    private var wakeRequested = false
    private var wantHealth = true

    private func refreshSoon(withHealth: Bool = false) {
        if withHealth { wantHealth = true }
        wakeRequested = true
    }

    /// Polls every 5 seconds while the popover is open and every minute otherwise; a change of settings or opening the
    /// popover wakes it at once.
    private func run() async {
        while !Task.isCancelled {
            wakeRequested = false
            await refresh()
            let until = Date().addingTimeInterval(popoverVisible ? 5 : 60)
            while Date() < until && !wakeRequested && !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    func refresh() async {
        for role in roles {
            if let job = job(role) { states[role] = await Services.state(of: job) }
        }
        do {
            status = try await client.status()
            statusError = nil
        } catch let error as AdminError {
            status = nil
            statusError = error
        } catch {
            status = nil
            statusError = .unreachable
        }
        // The health check signs in to iCloud, so it runs when the popover opens (at most every 3 minutes) and otherwise
        // every 15 minutes, never on every poll.
        let age = healthCheckedAt.map { Date().timeIntervalSince($0) } ?? .infinity
        if status != nil, (wantHealth && age > 180) || age > 900 {
            wantHealth = false
            healthCheckedAt = Date()
            health = try? await client.health()
        }
    }

    // MARK: - Actions

    private func perform(_ label: String, _ work: @escaping () async throws -> Void) {
        guard busy == nil else { return }
        busy = label
        lastError = nil
        Task {
            do { try await work() } catch { lastError = error.localizedDescription }
            busy = nil
            await refresh()
        }
    }

    func setPaused(_ paused: Bool) {
        perform(paused ? "Pausing" : "Resuming") { try await self.client.setPaused(paused) }
    }

    func restart(_ role: ServiceJob.Role) {
        perform("Restarting \(role.title.lowercased())") {
            if role == .server { self.restartedAt = Date() }
            if let job = self.job(role) {
                guard await Services.restart(job) else { throw AdminError.refused("\(role.title) could not be restarted.") }
            } else if role == .server {
                try await self.client.restart()
            }
            if role == .server { await self.waitForServer() }
        }
    }

    func restartAll() {
        perform("Restarting") {
            self.restartedAt = Date()
            for role in [ServiceJob.Role.helper, .server, .tunnel] {     // the helper first: the server expects its version
                if let job = self.job(role) { _ = await Services.restart(job) }
            }
            await self.waitForServer()
        }
    }

    func stopAll() {
        perform("Stopping") {
            for role in [ServiceJob.Role.tunnel, .server, .helper] {
                if let job = self.job(role) { _ = await Services.stop(job) }
            }
        }
    }

    func startAll() {
        perform("Starting") {
            self.restartedAt = Date()
            for role in [ServiceJob.Role.helper, .server, .tunnel] {
                if let job = self.job(role) { _ = await Services.start(job) }
            }
            await self.waitForServer()
        }
    }

    /// Waits up to 30 seconds for the admin API to answer again after a restart.
    private func waitForServer() async {
        for _ in 0..<30 {
            try? await Task.sleep(for: .seconds(1))
            if (try? await client.status()) != nil { return }
        }
    }

    func signOutAll() async throws -> Int {
        let n = try await client.signOutAll()
        await refresh()
        return n
    }

    /// Saves the passcode, optionally signs every app out, and restarts the server so the new passcode applies.
    func changePasscode(_ passcode: String, signOut: Bool) async throws {
        try await client.setOwnerPasscode(passcode)
        if signOut { _ = try await client.signOutAll() }
        try await restartServerAndWait()
    }

    /// Tests the new app-specific password against iCloud (the server refuses it otherwise), saves it, restarts, and runs the
    /// health check so the result shows at once.
    func changeAppPassword(_ password: String) async throws -> HealthReport? {
        try await client.setAppPassword(password)
        try await restartServerAndWait()
        healthCheckedAt = Date()
        health = try? await client.health()
        return health
    }

    private func restartServerAndWait() async throws {
        restartedAt = Date()
        if let job = job(.server) {
            guard await Services.restart(job) else { throw AdminError.refused("Saved, but the server could not be restarted.") }
        } else {
            try await client.restart()
        }
        await waitForServer()
        await refresh()
    }

    // MARK: - Open at login

    var opensAtLogin = SMAppService.mainApp.status == .enabled {
        didSet {
            guard opensAtLogin != (SMAppService.mainApp.status == .enabled) else { return }
            do {
                if opensAtLogin { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                lastError = "Could not change Open at Login: \(error.localizedDescription)"
                opensAtLogin = SMAppService.mainApp.status == .enabled
            }
        }
    }
}
