import Foundation

struct Reading {
    var soc: Double?
    var pv: Double?
    /// Positive = exporting to the grid, negative = importing.
    var grid: Double?
    var load: Double?
    /// Server-side time of the reading (HH:MM:SS, in the server's timezone).
    var timestamp: String?
    var receivedAt: Date
}

/// Subscribes to web_app.py's /ws stream. Going through the web app rather
/// than Modbus keeps the dongle's single TCP slot free for the server.
@MainActor
final class MonitorClient: ObservableObject {
    enum State: Equatable {
        case idle, connecting, connected
        case failed(String)
    }

    /// The server polls every ~5s; anything this old is shown dimmed.
    static let staleAfter: TimeInterval = 60

    @Published private(set) var state: State = .idle
    @Published private(set) var reading: Reading?
    @Published private(set) var paused = false

    private let session = URLSession(configuration: .default)
    private var task: URLSessionWebSocketTask?
    private var url: URL?
    // Bumped on every (re)connect so callbacks from an old socket are ignored.
    private var generation = 0
    private var retryDelay: TimeInterval = 2
    private var retryWork: DispatchWorkItem?
    private var pingTimer: Timer?

    var isStale: Bool {
        guard let reading else { return true }
        return Date().timeIntervalSince(reading.receivedAt) > Self.staleAfter
    }

    func connect(to url: URL?) {
        disconnect()
        self.url = url
        reading = nil
        paused = false
        retryDelay = 2
        guard let url else {
            state = .idle
            return
        }
        open(url)
    }

    private func disconnect() {
        generation += 1
        retryWork?.cancel()
        retryWork = nil
        pingTimer?.invalidate()
        pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func open(_ url: URL) {
        disconnect()
        let gen = generation
        state = .connecting
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        receive(task, gen)
        // Pings detect a silently dropped connection (e.g. after sleep or a
        // server restart) that would otherwise just look like stale data.
        pingTimer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.ping(gen) }
        }
    }

    private func receive(_ task: URLSessionWebSocketTask, _ gen: Int) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, gen == self.generation else { return }
                switch result {
                case .success(let message):
                    self.state = .connected
                    self.retryDelay = 2
                    switch message {
                    case .string(let text): self.handle(Data(text.utf8))
                    case .data(let data): self.handle(data)
                    @unknown default: break
                    }
                    self.receive(task, gen)
                case .failure(let error):
                    self.fail(error.localizedDescription, gen)
                }
            }
        }
    }

    private func ping(_ gen: Int) {
        guard gen == generation, let task else { return }
        task.sendPing { [weak self] error in
            guard let error else { return }
            Task { @MainActor in self?.fail(error.localizedDescription, gen) }
        }
    }

    private func fail(_ message: String, _ gen: Int) {
        guard gen == generation, let url else { return }
        disconnect()
        state = .failed(message)
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in self?.open(url) }
        }
        retryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + retryDelay, execute: work)
        retryDelay = min(retryDelay * 2, 30)
    }

    private func handle(_ data: Data) {
        guard let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        switch message["type"] as? String {
        case "update":
            apply(message)
        case "history":
            if let last = (message["readings"] as? [[String: Any]])?.last { apply(last) }
        case "status":
            paused = message["paused"] as? Bool ?? false
        default:
            break
        }
    }

    private func apply(_ message: [String: Any]) {
        guard let data = message["data"] as? [String: Any] else { return }
        func number(_ key: String) -> Double? { (data[key] as? NSNumber)?.doubleValue }

        // The server omits registers that failed to read this cycle, so keep
        // the previous value rather than blanking it.
        let previous = reading
        let ppv1 = number("ppv1"), ppv2 = number("ppv2")
        let pv = (ppv1 == nil && ppv2 == nil) ? nil : (ppv1 ?? 0) + (ppv2 ?? 0)
        reading = Reading(
            soc: number("battery_soc") ?? previous?.soc,
            pv: pv ?? previous?.pv,
            grid: number("ac_active_power") ?? previous?.grid,
            load: number("total_load_power") ?? previous?.load,
            timestamp: message["timestamp"] as? String,
            receivedAt: Date()
        )
    }
}
