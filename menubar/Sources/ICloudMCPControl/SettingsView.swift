import AppKit
import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            Tab("General", systemImage: "gearshape") { GeneralPane() }
            Tab("Security", systemImage: "lock") { SecurityPane() }
            Tab("Connection", systemImage: "network") { ConnectionPane() }
        }
        .frame(width: 500)
        .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - General

private struct GeneralPane: View {
    @Environment(Controller.self) private var controller

    var body: some View {
        @Bindable var controller = controller
        Form {
            Section {
                Toggle("Open at Login", isOn: $controller.opensAtLogin)
            }
            if !controller.roles.isEmpty {
                Section("Services") {
                    ForEach(controller.roles, id: \.self) { role in
                        LabeledContent(role.title, value: serviceValue(role))
                    }
                }
            }
            Section("Server") {
                if let status = controller.status {
                    if status.version != "unknown" { LabeledContent("Version", value: status.version) }
                    LabeledContent("Running for", value: Duration.seconds(status.uptimeSeconds)
                        .formatted(.units(allowed: [.days, .hours, .minutes], width: .wide, maximumUnitCount: 2)))
                    LabeledContent("Tools", value: "\(status.tools.total)")
                    LabeledContent("Address") {
                        HStack(spacing: 6) {
                            Text("\(status.publicUrl)/mcp")
                                .textSelection(.enabled)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString("\(status.publicUrl)/mcp", forType: .string)
                            } label: {
                                Image(systemName: "doc.on.doc")
                            }
                            .buttonStyle(.borderless)
                            .help("Copy the connector address")
                            .accessibilityLabel("Copy the connector address")
                        }
                    }
                } else {
                    Text(controller.statusError?.errorDescription ?? "Waiting for the server…")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .scrollDisabled(true)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func serviceValue(_ role: ServiceJob.Role) -> String {
        let state = controller.states[role] ?? .unknown
        if role == .helper, let helper = controller.status?.helper {
            return (helper.online ? "Online" : "Offline") + (helper.helper?.version.map { ", version \($0)" } ?? "")
        }
        return state.title
    }
}

// MARK: - Security

private struct SecurityPane: View {
    @Environment(Controller.self) private var controller
    @State private var showPasscode = false
    @State private var showAppPassword = false
    @State private var confirmSignOutAll = false
    @State private var signingOut: ConnectedApp?
    @State private var signingOutGroup: (name: String, apps: [ConnectedApp])?
    @State private var message: String?

    /// Apps with the same name and host (every Claude session signs in on its own) shown together.
    private var groups: [(key: String, name: String, host: String, apps: [ConnectedApp])] {
        var order: [String] = []
        var byKey: [String: [ConnectedApp]] = [:]
        for app in controller.apps {
            let key = app.name + "|" + app.host
            if byKey[key] == nil { order.append(key) }
            byKey[key, default: []].append(app)
        }
        return order.map { key in
            let apps = byKey[key]!
            let host = ["127.0.0.1", "localhost", "::1"].contains(apps[0].host) ? "This Mac" : apps[0].host
            return (key, apps[0].name, host, apps)
        }
    }

    var body: some View {
        Form {
            Section {
                LabeledContent {
                    Button("Change…") { showPasscode = true }
                } label: {
                    Text("Owner Passcode")
                    Text("Apps enter it when they connect. It also opens the approval page.")
                }
                LabeledContent {
                    Button("Replace…") { showAppPassword = true }
                } label: {
                    Text("iCloud App-Specific Password")
                    Text("How the server signs in to your iCloud account.")
                }
            }

            Section {
                if controller.apps.isEmpty {
                    Text(controller.status == nil ? "Not available while the server is not responding." : "No apps are signed in.")
                        .foregroundStyle(.secondary)
                }
                ForEach(groups, id: \.key) { group in
                    if group.apps.count == 1 {
                        appRow(group.apps[0], title: group.name, detail: detail(group.apps[0], host: group.host))
                    } else {
                        DisclosureGroup {
                            ForEach(group.apps) { app in
                                appRow(app, title: used(app), detail: connected(app))
                            }
                            LabeledContent {
                                Button("Sign Out All \(group.apps.count)…") { signingOutGroup = (group.name, group.apps) }
                            } label: {
                                Text("Every \(group.name) Session")
                            }
                        } label: {
                            LabeledContent {
                                Text("\(group.apps.count) sign-ins")
                            } label: {
                                Text(group.name)
                                Text(group.host.isEmpty ? "Each session signs in separately." : "\(group.host) · each session signs in separately")
                            }
                        }
                    }
                }
                if controller.apps.count > 1 {
                    LabeledContent {
                        Button("Sign Out All…", role: .destructive) { confirmSignOutAll = true }
                    } label: {
                        Text("Every App")
                    }
                }
            } header: {
                Text("Connected Apps")
            } footer: {
                VStack(alignment: .leading, spacing: 6) {
                    if let message { Text(message) }
                    Text("A signed-out app can sign in again: connect it from the app itself and enter the owner passcode. To keep an app out for good, also change the passcode.")
                    HStack(spacing: 16) {
                        Link("Reconnect Claude", destination: URL(string: "https://claude.ai/settings/connectors")!)
                        if let status = controller.status {
                            Button("Copy Connector Address") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString("\(status.publicUrl)/mcp", forType: .string)
                                message = "Copied \(status.publicUrl)/mcp."
                            }
                            .buttonStyle(.link)
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxHeight: 560)
        .disabled(controller.status == nil)
        .task { await controller.loadApps() }
        .sheet(isPresented: $showPasscode) { PasscodeSheet() }
        .sheet(isPresented: $showAppPassword) { AppPasswordSheet() }
        .confirmationDialog("Sign out \(signingOut?.name ?? "this app")?", isPresented: Binding(
            get: { signingOut != nil }, set: { if !$0 { signingOut = nil } }), presenting: signingOut) { app in
            Button("Sign Out", role: .destructive) {
                Task {
                    do {
                        try await controller.signOut(app)
                        message = "\(app.name) was signed out."
                    } catch {
                        message = error.localizedDescription
                    }
                }
            }
        } message: { app in
            Text("\(app.name) loses access until it connects again with the owner passcode.")
        }
        .confirmationDialog("Sign out every \(signingOutGroup?.name ?? "") session?", isPresented: Binding(
            get: { signingOutGroup != nil }, set: { if !$0 { signingOutGroup = nil } })) {
            Button("Sign Out \(signingOutGroup?.apps.count ?? 0) Sessions", role: .destructive) {
                guard let group = signingOutGroup else { return }
                Task {
                    do {
                        for app in group.apps { try await controller.signOut(app) }
                        message = "\(group.apps.count) \(group.name) sessions were signed out."
                    } catch {
                        message = error.localizedDescription
                    }
                }
            }
        } message: {
            Text("They lose access until they connect again with the owner passcode.")
        }
        .confirmationDialog("Sign out all apps?", isPresented: $confirmSignOutAll) {
            Button("Sign Out All Apps", role: .destructive) {
                Task {
                    do {
                        let n = try await controller.signOutAll()
                        message = n == 1 ? "1 app was signed out." : "\(n) apps were signed out."
                    } catch {
                        message = error.localizedDescription
                    }
                }
            }
        } message: {
            Text("Claude and every other connected app lose access until you reconnect them with the owner passcode. Scheduled agents that use it stop working until then.")
        }
    }

    private func appRow(_ app: ConnectedApp, title: String, detail: String) -> some View {
        LabeledContent {
            Button("Sign Out…") { signingOut = app }
        } label: {
            Text(title)
            Text(detail)
        }
    }

    private func detail(_ app: ConnectedApp, host: String) -> String {
        [host.isEmpty ? nil : host, connected(app), used(app)].compactMap { $0 }.joined(separator: " · ")
    }

    private func connected(_ app: ConnectedApp) -> String {
        guard let t = app.connectedAt else { return "Connected earlier" }
        return "Connected " + Date(timeIntervalSince1970: TimeInterval(t)).formatted(date: .abbreviated, time: .omitted)
    }

    private func used(_ app: ConnectedApp) -> String {
        guard let t = app.lastUsed else { return "Not used recently" }
        return "Used " + Date(timeIntervalSince1970: TimeInterval(t)).formatted(.relative(presentation: .named))
    }
}

private struct PasscodeSheet: View {
    @Environment(Controller.self) private var controller
    @Environment(\.dismiss) private var dismiss
    @State private var passcode = ""
    @State private var confirm = ""
    @State private var signOut = false
    @State private var working = false
    @State private var error: String?

    private var problem: String? {
        if passcode.isEmpty { return nil }
        if passcode.count < 12 { return "Use at least 12 characters." }
        if !confirm.isEmpty && confirm != passcode { return "The passcodes do not match." }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Change Owner Passcode")
                .font(.headline)
            VStack(alignment: .leading, spacing: 10) {
                SecureField("New passcode", text: $passcode)
                SecureField("Confirm passcode", text: $confirm)
                Toggle("Also sign out all apps", isOn: $signOut)
                    .toggleStyle(.checkbox)
            }
            .textFieldStyle(.roundedBorder)
            .disabled(working)
            Text(error ?? problem ?? (signOut
                ? "Every app has to reconnect with the new passcode."
                : "Apps that are already connected stay signed in. The server restarts to use the new passcode."))
                .font(.callout)
                .foregroundStyle(error != nil || problem != nil ? .red : .secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                if working { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Change Passcode") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || passcode.count < 12 || confirm != passcode)
            }
        }
        .padding(20)
        .frame(width: 420)
    }

    private func save() {
        working = true
        error = nil
        Task {
            do {
                try await controller.changePasscode(passcode, signOut: signOut)
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
            working = false
        }
    }
}

private struct AppPasswordSheet: View {
    @Environment(Controller.self) private var controller
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var working = false
    @State private var error: String?
    @State private var done: HealthReport?
    @State private var finished = false

    private var looksValid: Bool {
        password.filter { $0.isLetter }.count == 16
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Replace App-Specific Password")
                .font(.headline)
            if finished {
                Label(resultText, systemImage: done?.ok == false ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .symbolRenderingMode(.multicolor)
                Text("You can now revoke the old password on your Apple Account page.")
                    .foregroundStyle(.secondary)
                HStack {
                    Link("Open Apple Account", destination: URL(string: "https://account.apple.com")!)
                    Spacer()
                    Button("Done") { dismiss() }
                        .keyboardShortcut(.defaultAction)
                }
            } else {
                Text("Create a new app-specific password on your Apple Account page, then paste it here. It is tested with iCloud before anything is saved.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Link("Open Apple Account", destination: URL(string: "https://account.apple.com")!)
                SecureField("New password", text: $password, prompt: Text("xxxx-xxxx-xxxx-xxxx"))
                    .textFieldStyle(.roundedBorder)
                    .disabled(working)
                if let error {
                    Text(error)
                        .font(.callout)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    if working {
                        ProgressView().controlSize(.small)
                        Text("Testing with iCloud…").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Cancel", role: .cancel) { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button("Test and Save") { save() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(working || !looksValid)
                }
            }
        }
        .padding(20)
        .frame(width: 440)
    }

    private var resultText: String {
        guard let done else { return "Saved. The server restarted with the new password." }
        return done.ok ? "Saved. Every iCloud service signs in with the new password." : "Saved, but some services still report a problem. See the menu bar."
    }

    private func save() {
        working = true
        error = nil
        Task {
            do {
                done = try await controller.changeAppPassword(password)
                password = ""
                finished = true
            } catch {
                self.error = error.localizedDescription
            }
            working = false
        }
    }
}

// MARK: - Connection

private struct ConnectionPane: View {
    @Environment(Controller.self) private var controller

    var body: some View {
        @Bindable var controller = controller
        Form {
            Section {
                LabeledContent {
                    Button("Choose…") { chooseFolder() }
                } label: {
                    Text("Data Folder")
                    Text(controller.dataFolder.isEmpty ? "Not chosen" : (controller.dataFolder as NSString).abbreviatingWithTildeInPath)
                        .textSelection(.enabled)
                }
                TextField("Admin Port", value: $controller.adminPort, format: .number.grouping(.never))
            } footer: {
                Text("The server's DATA_DIR, which holds admin-token. The admin API is off until ADMIN_PORT is set in the server's settings; use the same port here.")
            }
            Section {
                ForEach(ServiceJob.Role.allCases, id: \.self) { role in
                    Picker(role.title, selection: Binding(
                        get: { controller.labels[role] ?? "" },
                        set: { controller.labels[role] = $0.isEmpty ? nil : $0 })) {
                        Text("None").tag("")
                        ForEach(controller.discovered) { job in
                            Text(job.label).tag(job.label)
                        }
                    }
                }
            } header: {
                HStack {
                    Text("Background Services")
                    Spacer()
                    Button("Look Again") { controller.rediscover() }
                        .buttonStyle(.borderless)
                        .font(.callout)
                }
            } footer: {
                Text("The launch agents in ~/Library/LaunchAgents that run the server, the Mac helper and a tunnel, if you use one.")
            }
        }
        .formStyle(.grouped)
        .scrollDisabled(true)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.showsHiddenFiles = true
        panel.message = "Choose the server's data folder (its DATA_DIR)."
        panel.prompt = "Choose"
        if !controller.dataFolder.isEmpty { panel.directoryURL = URL(fileURLWithPath: controller.dataFolder) }
        NSApp.activate()
        if panel.runModal() == .OK, let url = panel.url { controller.dataFolder = url.path }
    }
}
