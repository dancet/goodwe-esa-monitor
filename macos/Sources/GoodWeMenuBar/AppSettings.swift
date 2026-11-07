import Foundation

enum Layout: String, CaseIterable, Identifiable {
    case row, grid
    var id: Self { self }
    var label: String { self == .row ? "Single row" : "2×2 grid" }
}

enum PowerUnit: String, CaseIterable, Identifiable {
    case kW, W
    var id: Self { self }
}

/// User preferences, persisted to UserDefaults on every change.
@MainActor
final class AppSettings: ObservableObject {
    static let shared = AppSettings()
    static let defaultPort = 8765

    private let defaults = UserDefaults.standard

    /// Host (IP or name) of the machine running web_app.py — not the inverter.
    @Published var host: String { didSet { defaults.set(host, forKey: "host") } }
    @Published var port: Int { didSet { defaults.set(port, forKey: "port") } }
    @Published var layout: Layout { didSet { defaults.set(layout.rawValue, forKey: "layout") } }
    @Published var unit: PowerUnit { didSet { defaults.set(unit.rawValue, forKey: "unit") } }
    @Published var showIcons: Bool { didSet { defaults.set(showIcons, forKey: "showIcons") } }
    @Published var showUnits: Bool { didSet { defaults.set(showUnits, forKey: "showUnits") } }
    @Published var showHeartbeat: Bool { didSet { defaults.set(showHeartbeat, forKey: "showHeartbeat") } }

    private init() {
        host = defaults.string(forKey: "host") ?? ""
        port = defaults.object(forKey: "port") as? Int ?? Self.defaultPort
        layout = Layout(rawValue: defaults.string(forKey: "layout") ?? "") ?? .row
        unit = PowerUnit(rawValue: defaults.string(forKey: "unit") ?? "") ?? .kW
        showIcons = defaults.object(forKey: "showIcons") as? Bool ?? true
        showUnits = defaults.object(forKey: "showUnits") as? Bool ?? false
        showHeartbeat = defaults.object(forKey: "showHeartbeat") as? Bool ?? true
    }

    /// Dashboard URL, e.g. http://192.168.1.20:8765/. Tolerates a pasted
    /// "http://host:port/" in the host field; an explicit port there wins.
    var dashboardURL: URL? {
        var text = host.trimmingCharacters(in: .whitespaces)
        for prefix in ["http://", "https://", "ws://", "wss://"] where text.lowercased().hasPrefix(prefix) {
            text.removeFirst(prefix.count)
        }
        if let slash = text.firstIndex(of: "/") { text = String(text[..<slash]) }
        guard !text.isEmpty else { return nil }

        var components = URLComponents()
        components.scheme = "http"
        components.path = "/"
        let parts = text.split(separator: ":")
        if parts.count == 2, let explicitPort = Int(parts[1]) {
            components.host = String(parts[0])
            components.port = explicitPort
        } else {
            components.host = text
            components.port = port
        }
        return components.url
    }

    var webSocketURL: URL? {
        guard let base = dashboardURL,
              var components = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        components.scheme = "ws"
        components.path = "/ws"
        return components.url
    }
}
