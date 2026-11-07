// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "GoodWeMenuBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "GoodWeMenuBar", path: "Sources/GoodWeMenuBar"),
    ]
)
