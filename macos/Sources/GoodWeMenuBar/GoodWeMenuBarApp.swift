import AppKit

@main
enum GoodWeMenuBarApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        // No Dock icon or app menu: the status item is the whole UI. LSUIElement
        // in Info.plist does the same for the bundled app; this covers `swift run`.
        app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}
