import AppKit
import Combine
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let settings = AppSettings.shared
    private let client = MonitorClient()
    private var statusItem: NSStatusItem!
    private let heartbeat = HeartbeatView()
    private var lastBeat = -1
    /// Width of the title or image the button is showing, to place the bar.
    private var contentWidth: CGFloat = 0
    private var statusMenuItem: NSMenuItem?
    private var menuTimer: Timer?
    private var settingsWindow: NSWindow?
    private var refreshTimer: Timer?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        if let button = statusItem.button {
            button.addSubview(heartbeat)
            button.postsFrameChangedNotifications = true
            NotificationCenter.default.addObserver(
                forName: NSView.frameDidChangeNotification, object: button, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.layoutHeartbeat() }
            }
        }

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
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
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

        let mode = heartbeatMode
        let inset = mode == .hidden ? 0 : HeartbeatView.reservedWidth
        func title(_ string: NSAttributedString) -> NSAttributedString {
            inset > 0 ? StatusRenderer.withLeadingSpace(string, width: inset) : string
        }

        if let reading = client.reading {
            let cells = StatusRenderer.cells(for: reading, settings: settings)
            let dimmed = client.isStale || client.paused
            switch settings.layout {
            case .row:
                button.image = nil
                button.imagePosition = .noImage
                var row = StatusRenderer.rowTitle(cells, showIcons: settings.showIcons, dimmed: dimmed)
                if client.paused {
                    let paused = NSMutableAttributedString(string: "⏸ ")
                    paused.append(row)
                    row = paused
                }
                button.attributedTitle = title(row)
                contentWidth = button.attributedTitle.size().width
            case .grid:
                let image = StatusRenderer.gridImage(cells, showIcons: settings.showIcons,
                                                     dimmed: dimmed, leadingInset: inset)
                button.attributedTitle = NSAttributedString()
                button.image = image
                button.imagePosition = .imageOnly
                contentWidth = image.size.width
            }
        } else {
            button.image = nil
            let placeholder = NSAttributedString(
                string: client.paused ? "⚡ ⏸" : "⚡ \(Format.placeholder)",
                attributes: [.font: NSFont.menuBarFont(ofSize: 0)]
            )
            button.attributedTitle = title(placeholder)
            contentWidth = button.attributedTitle.size().width
        }

        heartbeat.setMode(mode)
        layoutHeartbeat()
        if mode == .live, client.beatCount != lastBeat {
            heartbeat.beat(interval: client.expectedInterval)
        }
        lastBeat = client.beatCount
    }

    private var heartbeatMode: HeartbeatView.Mode {
        if !settings.showHeartbeat || client.paused { return .hidden }
        if client.state == .connected, client.reading != nil, !client.isStale { return .live }
        return .idle
    }

    /// The button centres its content, so the bar goes where the reserved
    /// space at the content's left edge ends up.
    private func layoutHeartbeat() {
        guard let button = statusItem.button else { return }
        let x = ((button.bounds.width - contentWidth) / 2).rounded()
        heartbeat.frame = NSRect(x: max(x, 0), y: 0, width: HeartbeatView.barWidth, height: button.bounds.height)
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
                let time = reading.timestamp.map { " · \($0)" } ?? ""
                return "Updated \(Self.age(since: reading.receivedAt))\(time)\(stale)"
            }
            return "Connected, waiting for data…"
        }
    }

    private static func age(since date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 1 { return "just now" }
        if seconds < 60 { return "\(seconds)s ago" }
        return "\(seconds / 60)m ago"
    }

    // MARK: - Menu

    func menuWillOpen(_ menu: NSMenu) {
        // Keep "Updated Xs ago" ticking while the menu is open; .common mode
        // because the run loop is in event-tracking mode then.
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.statusMenuItem?.title = self.statusSummary
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        menuTimer = timer
    }

    func menuDidClose(_ menu: NSMenu) {
        menuTimer?.invalidate()
        menuTimer = nil
    }

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
        let status = info(statusSummary)
        statusMenuItem = status
        menu.addItem(status)
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
