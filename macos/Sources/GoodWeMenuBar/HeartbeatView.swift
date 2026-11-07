import AppKit
import QuartzCore

/// A slim rounded bar at the left of the status item that fills from bottom to
/// top between updates and drops back to empty when one arrives, so values that
/// haven't changed still visibly tick over. Core Animation runs the fill, so
/// there are no timers or status item redraws per frame.
@MainActor
final class HeartbeatView: NSView {
    enum Mode {
        /// Not shown (disabled, or paused — the ⏸ already says so).
        case hidden
        /// Empty track only: connecting, disconnected or stale.
        case idle
        /// Updates arriving: the fill runs.
        case live
    }

    static let barWidth: CGFloat = 3
    static let gap: CGFloat = 5
    /// Space the status item content leaves at its left for the bar.
    static var reservedWidth: CGFloat { barWidth + gap }

    private static let fillKey = "transform.translation.y"

    private let track = CALayer()
    private let fill = CALayer()
    private(set) var mode: Mode = .hidden
    /// With Reduce Motion on, the fill flips between empty and full instead.
    private var reducedMotionFull = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        // Layer-hosting: we own the layer tree, AppKit doesn't redraw it.
        layer = CALayer()
        wantsLayer = true
        track.masksToBounds = true
        track.addSublayer(fill)
        layer?.addSublayer(track)
        isHidden = true
        updateColors()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    /// Clicks go through to the status item button underneath.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        layoutBar()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }

    private func layoutBar() {
        let height = (bounds.height * 0.62).rounded()
        let wasEmpty = fillOffset <= -track.bounds.height + 0.5
        withoutAnimation {
            track.frame = CGRect(x: 0, y: ((bounds.height - height) / 2).rounded(),
                                 width: Self.barWidth, height: height)
            track.cornerRadius = Self.barWidth / 2
            // The fill is a full-height pill slid up from below the track, so
            // its leading edge stays rounded while the track clips the rest.
            fill.frame = track.bounds
            fill.cornerRadius = Self.barWidth / 2
            if wasEmpty { fill.setValue(-height, forKeyPath: Self.fillKey) }
        }
    }

    private func updateColors() {
        // Layer colours don't follow the appearance, so resolve them against
        // the menu bar's (light/dark depends on the wallpaper).
        effectiveAppearance.performAsCurrentDrawingAppearance {
            withoutAnimation {
                track.backgroundColor = NSColor.labelColor.withAlphaComponent(0.15).cgColor
                fill.backgroundColor = NSColor.labelColor.withAlphaComponent(0.5).cgColor
            }
        }
    }

    // MARK: - State

    func setMode(_ newMode: Mode) {
        guard newMode != mode else { return }
        let wasLive = mode == .live
        mode = newMode
        isHidden = newMode == .hidden

        if newMode == .live {
            // Start empty and wait for the next update to begin filling.
            fill.removeAllAnimations()
            withoutAnimation {
                fill.setValue(-track.bounds.height, forKeyPath: Self.fillKey)
                fill.opacity = 1
            }
        } else if wasLive {
            // Freeze wherever the fill got to, then fade it out to the track.
            let current = fillOffset
            fill.removeAllAnimations()
            withoutAnimation { fill.setValue(current, forKeyPath: Self.fillKey) }
            CATransaction.begin()
            CATransaction.setAnimationDuration(newMode == .hidden ? 0 : 0.6)
            fill.opacity = 0
            CATransaction.commit()
        }
    }

    /// An update arrived: drop to empty, then fill over the expected interval
    /// so the bar is full just as the next one is due. If it's late the bar
    /// simply stays full.
    func beat(interval: TimeInterval) {
        guard mode == .live else { return }
        let empty = -track.bounds.height

        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            reducedMotionFull.toggle()
            fill.removeAllAnimations()
            withoutAnimation { fill.setValue(reducedMotionFull ? 0 : empty, forKeyPath: Self.fillKey) }
            return
        }

        let interval = max(interval, 1)
        let drop = min(0.2, interval / 4)
        let animation = CAKeyframeAnimation(keyPath: Self.fillKey)
        animation.values = [fillOffset, empty, 0]
        animation.keyTimes = [0, NSNumber(value: drop / interval), 1]
        animation.calculationMode = .linear
        animation.duration = interval

        fill.removeAllAnimations()
        withoutAnimation { fill.setValue(0, forKeyPath: Self.fillKey) } // full once it ends
        fill.add(animation, forKey: "fill")
    }

    /// Where the fill is on screen right now (0 = full, -height = empty).
    private var fillOffset: CGFloat {
        let layer = fill.presentation() ?? fill
        return (layer.value(forKeyPath: Self.fillKey) as? NSNumber).map { CGFloat($0.doubleValue) } ?? 0
    }

    private func withoutAnimation(_ body: () -> Void) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        body()
        CATransaction.commit()
    }
}
