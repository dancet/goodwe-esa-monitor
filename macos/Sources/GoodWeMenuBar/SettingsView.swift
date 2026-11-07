import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var client: MonitorClient

    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    @State private var launchError: String?

    var body: some View {
        Form {
            Section {
                TextField("Host", text: $settings.host, prompt: Text("192.168.1.20"))
                TextField("Port", value: $settings.port, format: .number.grouping(.never))
                LabeledContent("Status") { statusText }
            } header: {
                Text("Monitor server")
            } footer: {
                Text("The machine running the GoodWe ESA Monitor web app (Docker or web_app.py), not the inverter itself.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Menu bar") {
                Picker("Layout", selection: $settings.layout) {
                    ForEach(Layout.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                Picker("Power unit", selection: $settings.unit) {
                    ForEach(PowerUnit.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                Toggle("Show icons", isOn: $settings.showIcons)
                Toggle("Show unit after values", isOn: $settings.showUnits)
                Toggle("Show update indicator", isOn: $settings.showHeartbeat)
            }

            Section("General") {
                Toggle("Launch at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin, perform: setLaunchAtLogin)
                if let launchError {
                    Text(launchError).font(.caption).foregroundStyle(.red)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .fixedSize()
    }

    @ViewBuilder private var statusText: some View {
        switch client.state {
        case .idle:
            Text("Not configured").foregroundStyle(.secondary)
        case .connecting:
            Text("Connecting…").foregroundStyle(.secondary)
        case .connected:
            Text(client.paused ? "Connected (paused)" : "Connected").foregroundStyle(.green)
        case .failed(let message):
            Text(message).foregroundStyle(.red).lineLimit(2)
        }
    }

    private func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchError = nil
        } catch {
            launchError = error.localizedDescription
        }
    }
}
