import AppKit
import SwiftUI

@main
struct ICloudMCPControlApp: App {
    @State private var controller = Controller()

    var body: some Scene {
        MenuBarExtra {
            PopoverView()
                .environment(controller)
        } label: {
            Image(systemName: controller.overall.symbol)
                .accessibilityLabel("iCloud MCP, \(controller.overall.title)")
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environment(controller)
        }
        .windowResizability(.contentSize)

        // Design review only: `--preview` shows the popover's content in an ordinary window, so it can be screenshotted.
        WindowGroup("Preview", id: "preview") {
            if CommandLine.arguments.contains("--preview") {
                PopoverView()
                    .environment(controller)
                    .onAppear { NSApp.setActivationPolicy(.regular) }
            }
        }
        .windowResizability(.contentSize)
        .defaultLaunchBehavior(CommandLine.arguments.contains("--preview") ? .presented : .suppressed)
    }
}

/// Settings opened from a menu bar app (no Dock icon) can appear behind other apps; this brings the app forward first.
struct OpenSettingsButton: View {
    @Environment(\.openSettings) private var openSettings
    var title = "Settings…"

    var body: some View {
        Button(title) {
            NSApp.activate()
            openSettings()
        }
    }
}
