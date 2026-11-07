import AppKit
import Combine
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let settings = AppSettings.shared
    private let client = MonitorClient()
    private var statusItem: NSStatusItem!
    private var settingsWindow: NSWindow?
    private var refreshTimer: Timer?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        // objectWillChange fires before the new value lands; hopping to the
        // next run loop pass means render() sees it.
        Publishers.Merge(settings.objectWillChange, client.objectWillChange)
            .receive(on: RunLoop.main)
            .sink { [weak self] in self?.render() }
            .store(in: &cancellables)

        settings.$host.combineLatest(settings.$port)
            .dropFirst()
            .debounce(for: .milliseconds(800), scheduler: RunLoop.main)
            .sink { [weak self] _ in self?.reconnect() }
            .store(in: &cancellables)

        // Re-render periodically so a reading that stops updating gets dimmed.
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.render() }
        }

        reconnect()
        render()
        if settings.webSocketURL == nil { openSettings() }
    }

    private func reconnect() {
        client.connect(to: settings.webSocketURL)
    }

    // MARK: - Status item

    private func render() {
        guard let button = statusItem.button else { return }
        button.toolTip = statusSummary

        guard let reading = client.reading else {
            button.image = nil
            button.attributedTitle = NSAttributedString(string: client.paused ? "⚡ ⏸" : "⚡ \(Format.placeholder)")
            return
        }

        let cells = StatusRenderer.cells(for: reading, settings: settings)
        let dimmed = client.isStale || client.paused
        switch settings.layout {
        case .row:
            button.image = nil
            button.imagePosition = .noImage
            var title = StatusRenderer.rowTitle(cells, showIcons: settings.showIcons, dimmed: dimmed)
            if client.paused {
                let paused = NSMutableAttributedString(string: "⏸ ")
                paused.append(title)
                title = paused
            }
            button.attributedTitle = title
        case .grid:
            button.attributedTitle = NSAttributedString()
            button.image = StatusRenderer.gridImage(cells, showIcons: settings.showIcons, dimmed: dimmed)
            button.imagePosition = .imageOnly
        }
    }

    private var statusSummary: String {
        switch client.state {
        case .idle: return "Not configured — open Settings"
        case .connecting: return "Connecting…"
        case .failed(let message): return "Disconnected: \(message)"
        case .connected:
            if client.paused { return "Inverter communication paused" }
            if let reading = client.reading {
                let stale = client.isStale ? " (stale)" : ""
                return "Updated \(reading.timestamp ?? "?")\(stale)"
            }
            return "Connected, waiting for data…"
        }
    }

    // MARK: - Menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        if let reading = client.reading {
            let unit = settings.unit
            menu.addItem(info("🔋  Battery  \(Format.soc(reading.soc))"))
            menu.addItem(info("☀️  Solar  \(Format.power(reading.pv, unit: unit, unitSeparator: " "))"))
            if let grid = reading.grid {
                let power = Format.power(abs(grid), unit: unit, unitSeparator: " ")
                menu.addItem(info(grid >= 0 ? "↗️  Exporting  \(power)" : "↙️  Importing  \(power)"))
            } else {
                menu.addItem(info("↗️  Grid  \(Format.placeholder)"))
            }
            menu.addItem(info("🏠  House  \(Format.power(reading.load, unit: unit, unitSeparator: " "))"))
            menu.addItem(.separator())
        }
        menu.addItem(info(statusSummary))
        menu.addItem(.separator())

        let dashboard = NSMenuItem(title: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "d")
        dashboard.target = self
        dashboard.isEnabled = settings.dashboardURL != nil
        menu.addItem(dashboard)
        let prefs = NSMenuItem(title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        prefs.target = self
        menu.addItem(prefs)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    private func info(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    @objc private func openDashboard() {
        if let url = settings.dashboardURL { NSWorkspace.shared.open(url) }
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(
                rootView: SettingsView(settings: settings, client: client)
            ))
            window.title = "GoodWe Monitor Settings"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }
}
