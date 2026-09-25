import Foundation

/// One of the background jobs (launchd agents) that make up an install: the server, the Mac helper and, optionally, a tunnel.
struct ServiceJob: Identifiable, Hashable, Sendable {
    enum Role: String, CaseIterable, Sendable {
        case server, helper, tunnel
        var title: String {
            switch self {
            case .server: "Server"
            case .helper: "Mac helper"
            case .tunnel: "Tunnel"
            }
        }
    }
    let label: String
    let plist: URL
    let arguments: [String]
    var id: String { label }
}

enum JobState: Equatable, Sendable {
    case running, notRunning, stopped, unknown
    var title: String {
        switch self {
        case .running: "Running"
        case .notRunning: "Not running"
        case .stopped: "Stopped"
        case .unknown: "Unknown"
        }
    }
}

/// Finds the install's launchd agents and starts, stops and restarts them with launchctl. Every job is expected to have
/// KeepAlive, so "stop" unloads the job (bootout) rather than killing it, and "start" loads it again (bootstrap).
enum Services {
    static var domain: String { "gui/\(getuid())" }

    static func discover() -> [ServiceJob] {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "plist" }.compactMap { url in
            guard let dict = NSDictionary(contentsOf: url) as? [String: Any], let label = dict["Label"] as? String else { return nil }
            let args = (dict["ProgramArguments"] as? [String]) ?? [dict["Program"] as? String].compactMap { $0 }
            let job = ServiceJob(label: label, plist: url, arguments: args)
            return job.label.lowercased().contains("icloud") || job.arguments.joined().lowercased().contains("icloud") ? job : nil
        }.sorted { $0.label < $1.label }
    }

    /// How strongly a job looks like the given part of an install, from its label and command line (0 = not at all). Other
    /// jobs can live in the same install folder, so the command itself counts for more than a matching path.
    static func score(_ job: ServiceJob, as role: ServiceJob.Role) -> Int {
        let label = job.label.lowercased()
        let command = job.arguments.joined(separator: " ").lowercased()
        let text = label + " " + command
        guard text.contains("icloud") else { return 0 }
        switch role {
        case .helper:
            return (command.contains("icloud_mac_helper") ? 4 : 0) + (label.contains("mac-helper") ? 2 : 0)
        case .tunnel:
            return (command.contains("cloudflared") ? 3 : 0) + (text.contains("tunnel") ? 3 : 0)
        case .server:
            if score(job, as: .helper) > 0 || score(job, as: .tunnel) > 0 { return 0 }
            let runs = ["-m icloud_mcp", "icloud-mcp-server", "run-server", "icloud_mcp/__main__"].contains { command.contains($0) }
            return (runs ? 4 : 0) + (label.hasSuffix("icloud-mcp") || label.hasSuffix("icloud-mcp-server") ? 2 : 0)
        }
    }

    /// What a job most likely is. Nil for jobs unrelated to icloud-mcp.
    static func guessRole(_ job: ServiceJob) -> ServiceJob.Role? {
        let best = ServiceJob.Role.allCases.map { ($0, score(job, as: $0)) }.max { $0.1 < $1.1 }
        return (best?.1 ?? 0) > 0 ? best?.0 : nil
    }

    /// The job that best fits a role, if any fits at all.
    static func best(for role: ServiceJob.Role, in jobs: [ServiceJob]) -> ServiceJob? {
        jobs.filter { score($0, as: role) > 0 }.max { score($0, as: role) < score($1, as: role) }
    }

    /// A best guess at the server's data folder, from its command line: <install>/data next to a run script, or a
    /// DATA_DIR-looking argument. Only folders that already hold an admin token are returned.
    static func guessDataFolders(server: ServiceJob?) -> [String] {
        var candidates: [String] = []
        if let server, let dict = NSDictionary(contentsOf: server.plist) as? [String: Any] {
            if let env = dict["EnvironmentVariables"] as? [String: String], let dir = env["DATA_DIR"] { candidates.append(dir) }
            if let cwd = dict["WorkingDirectory"] as? String { candidates.append((cwd as NSString).appendingPathComponent("data")) }
            for arg in server.arguments where arg.hasPrefix("/") {
                var dir = (arg as NSString).deletingLastPathComponent
                for _ in 0..<3 {                       // <install>/app/run-server.py or <install>/app/.venv/bin/python
                    candidates.append((dir as NSString).appendingPathComponent("data"))
                    dir = (dir as NSString).deletingLastPathComponent
                }
            }
        }
        candidates.append((NSHomeDirectory() as NSString).appendingPathComponent(".icloud-mcp"))
        var seen = Set<String>()
        return candidates.filter { seen.insert($0).inserted }.filter {
            FileManager.default.isReadableFile(atPath: ($0 as NSString).appendingPathComponent("admin-token"))
        }
    }

    static func state(of job: ServiceJob) async -> JobState {
        let (code, output) = await launchctl(["print", "\(domain)/\(job.label)"])
        if code != 0 { return .stopped }                            // not loaded: stopped from here or never started
        return output.contains("state = running") ? .running : .notRunning
    }

    static func restart(_ job: ServiceJob) async -> Bool {
        if await state(of: job) == .stopped { return await start(job) }
        return await launchctl(["kickstart", "-k", "\(domain)/\(job.label)"]).0 == 0
    }

    static func stop(_ job: ServiceJob) async -> Bool {
        await launchctl(["bootout", "\(domain)/\(job.label)"]).0 == 0
    }

    static func start(_ job: ServiceJob) async -> Bool {
        let (code, _) = await launchctl(["bootstrap", domain, job.plist.path])
        if code == 0 { return true }
        return await state(of: job) != .stopped
    }

    private static func launchctl(_ args: [String]) async -> (Int32, String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                process.arguments = args
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = pipe
                do { try process.run() } catch { cont.resume(returning: (-1, "")); return }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()   // read while it runs: a full pipe would stall it
                process.waitUntilExit()
                cont.resume(returning: (process.terminationStatus, String(decoding: data, as: UTF8.self)))
            }
        }
    }
}
