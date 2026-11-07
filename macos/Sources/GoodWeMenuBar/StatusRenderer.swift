import AppKit

/// The four values shown in the menu bar, with the dashboard's icons.
enum Metric: CaseIterable {
    case soc, pv, grid, load

    var icon: String {
        switch self {
        case .soc: return "🔋"
        case .pv: return "☀️"
        case .grid: return "↗️"
        case .load: return "🏠"
        }
    }

    func value(in reading: Reading) -> Double? {
        switch self {
        case .soc: return reading.soc
        case .pv: return reading.pv
        case .grid: return reading.grid
        case .load: return reading.load
        }
    }
}

enum Format {
    static let placeholder = "–"

    static func soc(_ percent: Double?) -> String {
        guard let percent else { return placeholder }
        return String(format: "%.0f%%", percent)
    }

    /// "1.4" / "1400"; `signed` adds "+" to positive values (export).
    /// `unitSeparator` nil omits the unit entirely.
    static func power(_ watts: Double?, unit: PowerUnit, signed: Bool = false, unitSeparator: String? = nil) -> String {
        guard let watts else { return placeholder }
        let magnitude = unit == .kW
            ? String(format: "%.1f", abs(watts) / 1000)
            : String(format: "%.0f", abs(watts))
        var sign = ""
        // Decide on the rounded text so 0.04 kW never shows as "+0.0".
        if Double(magnitude) != 0 {
            if watts < 0 { sign = "−" } else if signed { sign = "+" }
        }
        let suffix = unitSeparator.map { $0 + unit.rawValue } ?? ""
        return sign + magnitude + suffix
    }

    static func value(_ metric: Metric, of reading: Reading, unit: PowerUnit, unitSeparator: String? = nil) -> String {
        let value = metric.value(in: reading)
        switch metric {
        case .soc: return soc(value)
        case .grid: return power(value, unit: unit, signed: true, unitSeparator: unitSeparator)
        case .pv, .load: return power(value, unit: unit, unitSeparator: unitSeparator)
        }
    }
}

struct StatusCell {
    let icon: String
    let value: String
}

@MainActor
enum StatusRenderer {
    static func cells(for reading: Reading, settings: AppSettings) -> [StatusCell] {
        Metric.allCases.map { metric in
            StatusCell(
                icon: metric.icon,
                value: Format.value(metric, of: reading, unit: settings.unit,
                                    unitSeparator: settings.showUnits ? "" : nil)
            )
        }
    }

    static func rowTitle(_ cells: [StatusCell], showIcons: Bool, dimmed: Bool) -> NSAttributedString {
        let font = NSFont.monospacedDigitSystemFont(ofSize: 0, weight: .regular)
        let color: NSColor = dimmed ? .tertiaryLabelColor : .labelColor
        let text = cells
            .map { showIcons ? $0.icon + "\u{2009}" + $0.value : $0.value }
            .joined(separator: showIcons ? "  " : " · ")
        return NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color])
    }

    /// Two rows of small text filling the menu bar's height:
    ///   🔋 SoC     ☀️ PV
    ///   ↗️ Export  🏠 Load
    static func gridImage(_ cells: [StatusCell], showIcons: Bool, dimmed: Bool) -> NSImage {
        let barHeight = NSStatusBar.system.thickness
        let valueFont = NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .medium)
        let iconFont = NSFont.systemFont(ofSize: 8.5)
        // Without emoji the image can be a template, so macOS handles the
        // light/dark menu bar and highlight states itself.
        let template = !showIcons
        let color: NSColor = template
            ? .black.withAlphaComponent(dimmed ? 0.4 : 1)
            : (dimmed ? .tertiaryLabelColor : .labelColor)

        let strings: [NSAttributedString] = cells.map { cell in
            let string = NSMutableAttributedString()
            if showIcons {
                string.append(NSAttributedString(string: cell.icon + "\u{2009}", attributes: [.font: iconFont]))
            }
            string.append(NSAttributedString(string: cell.value, attributes: [.font: valueFont, .foregroundColor: color]))
            return string
        }
        let sizes = strings.map { $0.size() }
        let columnGap: CGFloat = 7
        let leftWidth = ceil(max(sizes[0].width, sizes[2].width))
        let rightWidth = ceil(max(sizes[1].width, sizes[3].width))
        let rowHeight = barHeight / 2

        let image = NSImage(size: NSSize(width: leftWidth + columnGap + rightWidth, height: barHeight), flipped: true) { _ in
            for (index, string) in strings.enumerated() {
                let x = index % 2 == 0 ? 0 : leftWidth + columnGap
                let row = CGFloat(index / 2)
                // Nudge the rows apart slightly; the line boxes are a touch
                // taller than half the bar but the glyphs are not.
                let y = row * rowHeight + (rowHeight - sizes[index].height) / 2 + (row == 0 ? 0.5 : -0.5)
                string.draw(at: NSPoint(x: x, y: y))
            }
            return true
        }
        image.isTemplate = template
        // Redraw on every use so dynamic colours follow the menu bar appearance.
        image.cacheMode = .never
        return image
    }
}
