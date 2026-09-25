// swift-tools-version: 6.2
// iCloud MCP Control: a menu bar app to watch and control an icloud-mcp server on this Mac.
import PackageDescription

let package = Package(
    name: "ICloudMCPControl",
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(
            name: "ICloudMCPControl",
            path: "Sources/ICloudMCPControl",
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
    ]
)
