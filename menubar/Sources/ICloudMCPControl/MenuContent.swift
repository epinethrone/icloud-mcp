import AppKit
import SwiftUI

/// The menu behind the menu bar icon: a standard macOS menu, like Time Machine's or a VPN's. The state reads as a greyed
/// header, problems are listed only when there are any, and every action is an ordinary command.
struct MenuContent: View {
    @Environment(Controller.self) private var controller
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Text(controller.busy.map { "\($0)…" } ?? "iCloud MCP: \(controller.overall.title)")
        if let detail {
            Text(detail)
        }
        if let error = controller.lastError {
            Label(error, systemImage: "xmark.octagon")
        }

        if !controller.issues.isEmpty {
            Divider()
            ForEach(controller.issues) { issue in
                Button { controller.checkAgain() } label: {      // clicking a problem checks again
                    Label {
                        Text(issue.title)
                        if let detail = issue.detail { Text(detail) }
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                    }
                }
            }
        }

        Divider()
        actions

        Divider()
        if let status = controller.status, let url = URL(string: "\(status.publicUrl)/outbox") {
            let waiting = status.waitingForApproval.values.reduce(0, +)
            if waiting > 0 {
                Link("Open Approval Page (\(waiting))", destination: url)
            }
        }
        Button("Settings…") {
            NSApp.activate()
            openSettings()
        }
        .keyboardShortcut(",")
        Button("Quit iCloud MCP Control") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    /// The second header line: what the server offers, or what went wrong.
    private var detail: String? {
        switch controller.overall {
        case .running, .issues, .paused:
            guard let status = controller.status else { return nil }
            let tools = status.tools.total == 1 ? "1 tool" : "\(status.tools.total) tools"
            let apps = status.connectedApps == 1 ? "1 app connected" : "\(status.connectedApps) apps connected"
            return "\(tools) · \(apps)"
        case .unreachable:
            return controller.statusError?.errorDescription
        case .notConfigured:
            return "Choose the server's data folder in Settings."
        case .stopped, .starting:
            return nil
        }
    }

    @ViewBuilder private var actions: some View {
        let busy = controller.busy != nil
        switch controller.overall {
        case .notConfigured:
            Button("Set Up…") {
                NSApp.activate()
                openSettings()
            }
        case .stopped:
            Button("Start Server") { controller.startAll() }
                .disabled(busy)
        default:
            if let status = controller.status {
                Button(status.paused ? "Resume Server" : "Pause Server") { controller.setPaused(!status.paused) }
                    .disabled(busy)
            }
            Menu("Restart") {
                Button("Server") { controller.restart(.server) }
                if controller.job(.helper) != nil {
                    Button("Mac Helper") { controller.restart(.helper) }
                }
                if controller.job(.tunnel) != nil {
                    Button("Tunnel") { controller.restart(.tunnel) }
                }
                if controller.roles.count > 1 {
                    Divider()
                    Button("Everything") { controller.restartAll() }
                }
            }
            .disabled(busy)
            if !controller.roles.isEmpty {
                Button("Stop…") { confirmStop() }
                    .disabled(busy)
            }
        }
    }

    /// Stopping cuts every connected app off, so it asks first and offers pausing as the gentler choice.
    private func confirmStop() {
        let alert = NSAlert()
        alert.messageText = "Stop the server, Mac helper and tunnel?"
        alert.informativeText = "Claude, scheduled agents and every other connected app lose the connector until you start it again. Pausing keeps everything connected but refuses requests."
        let stop = alert.addButton(withTitle: "Stop")
        stop.hasDestructiveAction = true
        let canPause = controller.status?.paused == false
        if canPause { alert.addButton(withTitle: "Pause Instead") }
        alert.addButton(withTitle: "Cancel")
        NSApp.activate()
        switch alert.runModal() {
        case .alertFirstButtonReturn: controller.stopAll()
        case .alertSecondButtonReturn where canPause: controller.setPaused(true)
        default: break
        }
    }
}
