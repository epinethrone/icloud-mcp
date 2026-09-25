import AppKit
import SwiftUI

@main
struct ICloudMCPControlApp: App {
    @State private var controller = Controller()

    var body: some Scene {
        MenuBarExtra {
            MenuContent()
                .environment(controller)
        } label: {
            Image(systemName: controller.overall.symbol)
                .accessibilityLabel("iCloud MCP, \(controller.overall.title)")
        }
        .menuBarExtraStyle(.menu)

        Settings {
            SettingsView()
                .environment(controller)
        }
        .windowResizability(.contentSize)
    }
}
