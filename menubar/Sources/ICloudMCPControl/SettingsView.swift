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
}

// MARK: - Security

private struct SecurityPane: View {
    @Environment(Controller.self) private var controller
    @State private var showPasscode = false
    @State private var showAppPassword = false
    @State private var confirmSignOut = false
    @State private var signOutResult: String?

    var body: some View {
        Form {
            Section {
                LabeledContent {
                    Button("Change…") { showPasscode = true }
                } label: {
                    Text("Owner Passcode")
                    Text("Used to connect apps and to approve messages.")
                }
                LabeledContent {
                    Button("Replace…") { showAppPassword = true }
                } label: {
                    Text("iCloud App-Specific Password")
                    Text("How the server signs in to your iCloud account.")
                }
            }
            Section {
                LabeledContent {
                    Button("Sign Out All…", role: .destructive) { confirmSignOut = true }
                        .disabled(controller.status == nil)
                } label: {
                    Text("Connected Apps")
                    Text(signOutResult ?? connectedText)
                }
            }
        }
        .formStyle(.grouped)
        .scrollDisabled(true)
        .fixedSize(horizontal: false, vertical: true)
        .disabled(controller.status == nil)
        .sheet(isPresented: $showPasscode) { PasscodeSheet() }
        .sheet(isPresented: $showAppPassword) { AppPasswordSheet() }
        .confirmationDialog("Sign out all apps?", isPresented: $confirmSignOut) {
            Button("Sign Out All Apps", role: .destructive) {
                Task {
                    do {
                        let n = try await controller.signOutAll()
                        signOutResult = n == 1 ? "1 app was signed out." : "\(n) apps were signed out."
                    } catch {
                        signOutResult = error.localizedDescription
                    }
                }
            }
        } message: {
            Text("Claude and every other app connected to this server lose access until you reconnect them with the owner passcode. Scheduled agents that use it stop working until then.")
        }
    }

    private var connectedText: String {
        guard let n = controller.status?.connectedApps else { return "Not available while the server is not responding." }
        switch n {
        case 0: return "No apps are signed in."
        case 1: return "1 app is signed in."
        default: return "\(n) apps are signed in."
        }
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
            Form {
                SecureField("New Passcode", text: $passcode)
                SecureField("Confirm", text: $confirm)
                Toggle("Also sign out all apps", isOn: $signOut)
            }
            .formStyle(.grouped)
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
                Form {
                    SecureField("New Password", text: $password, prompt: Text("xxxx-xxxx-xxxx-xxxx"))
                }
                .formStyle(.grouped)
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
