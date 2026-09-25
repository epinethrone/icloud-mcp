import Foundation

/// What the server reports about itself (GET /admin/v1/status).
struct ServerStatus: Decodable, Equatable, Sendable {
    struct Tools: Decodable, Equatable, Sendable {
        let total: Int
        let byArea: [String: Int]
    }
    struct Helper: Decodable, Equatable, Sendable {
        struct Agent: Decodable, Equatable, Sendable { let version: String? }
        let online: Bool
        let lastSeenSecondsAgo: Int?
        let helper: Agent?
    }
    let version: String
    let uptimeSeconds: Int
    let paused: Bool
    let publicUrl: String
    let tools: Tools
    let helper: Helper?
    let waitingForApproval: [String: Int]
    let connectedApps: Int
    let overridesActive: [String]
}

/// One app signed in to the server (GET /admin/v1/apps). Metadata only: the server never returns tokens or secrets.
struct ConnectedApp: Decodable, Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let host: String
    let connectedAt: Int?
    let lastUsed: Int?
}

/// The result of the server's own health check (GET /admin/v1/health).
struct HealthReport: Decodable, Equatable, Sendable {
    struct Area: Decodable, Equatable, Sendable {
        let ok: Bool
        let error: String?
        let ms: Int?
    }
    let ok: Bool
    let areas: [String: Area]
}

enum AdminError: LocalizedError, Equatable {
    case notConfigured
    case tokenUnreadable(String)
    case unreachable
    case unauthorized
    case refused(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured: "Choose the server's data folder in Settings."
        case .tokenUnreadable(let folder): "No admin token in \(folder). Set ADMIN_PORT in the server's settings and restart it."
        case .unreachable: "The server is not answering on its admin port."
        case .unauthorized: "The server refused the admin token. Restart the server, then try again."
        case .refused(let message): message
        }
    }
}

/// Talks to the server's loopback-only admin API. The token is read from DATA_DIR/admin-token for every request batch and is
/// only ever sent in a request header, never on a command line.
struct AdminClient: Sendable {
    let port: Int
    let dataFolder: String

    private var tokenURL: URL { URL(fileURLWithPath: dataFolder).appendingPathComponent("admin-token") }

    private func token() throws -> String {
        guard !dataFolder.isEmpty else { throw AdminError.notConfigured }
        guard let raw = try? String(contentsOf: tokenURL, encoding: .utf8) else {
            throw AdminError.tokenUnreadable((dataFolder as NSString).abbreviatingWithTildeInPath)
        }
        return raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 60
        config.urlCache = nil
        config.httpCookieStorage = nil
        return URLSession(configuration: config)
    }()

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private func request(_ method: String, _ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 8) async throws -> Data {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/admin/v1/\(path)")!, timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("Bearer \(try token())", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.session.data(for: req)
        } catch {
            throw AdminError.unreachable
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 || code == 403 { throw AdminError.unauthorized }
        if !(200..<300).contains(code) {
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw AdminError.refused(message ?? "The server answered with an error (\(code)).")
        }
        return data
    }

    func status() async throws -> ServerStatus {
        try Self.decoder.decode(ServerStatus.self, from: try await request("GET", "status"))
    }

    func health() async throws -> HealthReport {
        try Self.decoder.decode(HealthReport.self, from: try await request("GET", "health", timeout: 60))
    }

    func setPaused(_ paused: Bool) async throws {
        _ = try await request("POST", "pause", body: ["paused": paused])
    }

    func signOutAll() async throws -> Int {
        let data = try await request("POST", "sign-out-all")
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["signed_out"] as? Int ?? 0
    }

    func apps() async throws -> [ConnectedApp] {
        struct Answer: Decodable { let apps: [ConnectedApp] }
        return try Self.decoder.decode(Answer.self, from: try await request("GET", "apps")).apps
    }

    func signOut(appID: String) async throws {
        _ = try await request("POST", "apps/sign-out", body: ["id": appID])
    }

    func setOwnerPasscode(_ passcode: String) async throws {
        _ = try await request("POST", "owner-passcode", body: ["passcode": passcode])
    }

    func setAppPassword(_ password: String) async throws {
        _ = try await request("POST", "app-password", body: ["password": password], timeout: 40)
    }

    func restart() async throws {
        _ = try await request("POST", "restart")
    }
}
