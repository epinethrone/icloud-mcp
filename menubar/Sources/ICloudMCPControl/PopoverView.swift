import AppKit
import SwiftUI

struct PopoverView: View {
    @Environment(Controller.self) private var controller
    @State private var confirmStop = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, 16)
                .padding(.top, 14)
                .padding(.bottom, 12)

            switch controller.overall {
            case .notConfigured:
                notConfigured
            case .stopped:
                stopped
            case .unreachable:
                unreachable
            default:
                running
            }

            if let error = controller.lastError {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 10)
            }

            Divider()
            footer
                .padding(12)
        }
        .frame(width: 320)
        .confirmationDialog("Stop the server, Mac helper and tunnel?", isPresented: $confirmStop) {
            Button("Stop All", role: .destructive) { controller.stopAll() }
            if controller.status?.paused == false {
                Button("Pause Instead") { controller.setPaused(true) }
            }
        } message: {
            Text("Claude, scheduled agents and every other connected app lose the connector until you start it again. Pausing keeps everything connected but refuses requests.")
        }
        .onAppear { controller.popoverVisible = true }
        .onDisappear { controller.popoverVisible = false }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: controller.overall.symbol)
                .font(.system(size: 22, weight: .regular))
                .foregroundStyle(tint)
                .symbolRenderingMode(.hierarchical)
                .frame(width: 32, height: 32)
                .contentTransition(.symbolEffect(.replace))
            VStack(alignment: .leading, spacing: 1) {
                Text("iCloud MCP")
                    .font(.headline)
                Text(controller.summaryLine)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let busy = controller.busy {
                ProgressView()
                    .controlSize(.small)
                    .help(busy)
                    .accessibilityLabel(busy)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var tint: Color {
        switch controller.overall {
        case .running: .green
        case .paused: .secondary                        // the owner's own choice, not a warning
        case .issues, .unreachable: .orange
        case .stopped: .secondary
        case .notConfigured, .starting: .secondary
        }
    }

    // MARK: States

    private var running: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let status = controller.status {
                Toggle(isOn: Binding(get: { !status.paused }, set: { controller.setPaused(!$0) })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Accept Requests")
                        Text(status.paused ? "Tools answer that the server is paused." : "Connected apps can use the tools.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.switch)
                .disabled(controller.busy != nil)
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            }

            Divider()

            VStack(spacing: 8) {
                ForEach(controller.roles, id: \.self) { role in
                    row(role.title, value: jobValue(role))
                }
                if let status = controller.status {
                    row("Connected apps", value: "\(status.connectedApps)")
                    let waiting = status.waitingForApproval.values.reduce(0, +)
                    if waiting > 0, let url = URL(string: "\(status.publicUrl)/outbox") {
                        HStack {
                            Text("Waiting for approval")
                            Spacer()
                            Link("\(waiting) \(Image(systemName: "arrow.up.forward"))", destination: url)
                                .accessibilityLabel("\(waiting) waiting, open the approval page")
                        }
                        .font(.callout)
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)

            if !controller.issues.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(controller.issues) { issue in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .symbolRenderingMode(.multicolor)          // Apple's warning: dark mark on yellow, readable
                                .accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(issue.title)
                                    .font(.callout.weight(.medium))
                                if let detail = issue.detail {
                                    Text(detail)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
        }
    }

    private var notConfigured: some View {
        message("Choose the folder where your server keeps its data to connect to it.") {
            OpenSettingsButton(title: "Open Settings")
                .buttonStyle(.glassProminent)
        }
    }

    private var stopped: some View {
        message("The server is stopped. Connected apps cannot use it until you start it.") {
            Button("Start Server") { controller.startAll() }
                .buttonStyle(.glassProminent)
                .disabled(controller.busy != nil)
        }
    }

    private var unreachable: some View {
        message(controller.statusError?.errorDescription ?? "The server is not answering.") {
            if controller.job(.server) != nil {
                Button("Restart Server") { controller.restart(.server) }
                    .buttonStyle(.glassProminent)
                    .disabled(controller.busy != nil)
            } else {
                OpenSettingsButton(title: "Open Settings")
                    .buttonStyle(.glassProminent)
            }
        }
    }

    private func message<Action: View>(_ text: String, @ViewBuilder action: () -> Action) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(text)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            action()
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 14)
    }

    // MARK: Rows

    private func row(_ title: String, value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
        .font(.callout)
        .accessibilityElement(children: .combine)
    }

    private func jobValue(_ role: ServiceJob.Role) -> String {
        let state = controller.states[role] ?? .unknown
        switch role {
        case .server:
            if state == .running, let version = controller.status?.version, version != "unknown" { return "Running · \(version)" }
        case .helper:
            if let helper = controller.status?.helper {
                let version = helper.helper?.version.map { " · \($0)" } ?? ""
                return (helper.online ? "Online" : "Offline") + version
            }
        case .tunnel:
            break
        }
        return state.title
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 8) {
            Menu("Restart") {
                if controller.job(.server) != nil || controller.status != nil {
                    Button("Restart Server") { controller.restart(.server) }
                }
                if controller.job(.helper) != nil {
                    Button("Restart Mac Helper") { controller.restart(.helper) }
                }
                if controller.job(.tunnel) != nil {
                    Button("Restart Tunnel") { controller.restart(.tunnel) }
                }
                if controller.roles.count > 1 {
                    Divider()
                    Button("Restart All") { controller.restartAll() }
                }
            }
            .menuStyle(.button)
            .buttonStyle(.glass)
            .fixedSize()
            .disabled(controller.busy != nil || (controller.roles.isEmpty && controller.status == nil))

            OpenSettingsButton()
                .buttonStyle(.glass)

            Spacer()

            Menu {
                if let status = controller.status, let url = URL(string: "\(status.publicUrl)/outbox") {
                    Link("Open Approval Page", destination: url)
                }
                if !controller.roles.isEmpty {
                    Divider()
                    if controller.overall == .stopped {
                        Button("Start All") { controller.startAll() }
                    } else {
                        Button("Stop All…") { confirmStop = true }
                    }
                }
                Divider()
                Button("Quit iCloud MCP Control") { NSApp.terminate(nil) }
            } label: {
                Image(systemName: "ellipsis")
                    .accessibilityLabel("More")
            }
            .menuStyle(.button)
            .menuIndicator(.hidden)
            .buttonStyle(.glass)
            .fixedSize()
        }
    }
}
