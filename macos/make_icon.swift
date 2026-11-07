// Renders the web app's ⚡ favicon as a macOS app icon iconset.
// Usage: swift make_icon.swift OUTPUT.iconset
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func render(pixels: Int) -> Data {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let size = CGFloat(pixels)

    // Standard macOS icon grid: 824/1024 rounded square, centred.
    let inset = size * 100 / 1024
    let tile = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
    let path = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225)
    NSGradient(starting: NSColor(calibratedRed: 0.16, green: 0.18, blue: 0.24, alpha: 1),
               ending: NSColor(calibratedRed: 0.06, green: 0.07, blue: 0.10, alpha: 1))!
        .draw(in: path, angle: -90)

    let bolt = NSAttributedString(string: "⚡", attributes: [.font: NSFont.systemFont(ofSize: tile.width * 0.62)])
    let boltSize = bolt.size()
    bolt.draw(at: NSPoint(x: (size - boltSize.width) / 2, y: (size - boltSize.height) / 2))

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for points in [16, 32, 128, 256, 512] {
    try render(pixels: points).write(to: output.appendingPathComponent("icon_\(points)x\(points).png"))
    try render(pixels: points * 2).write(to: output.appendingPathComponent("icon_\(points)x\(points)@2x.png"))
}
