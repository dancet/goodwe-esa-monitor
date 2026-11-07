#!/bin/bash
# Builds "GoodWe Monitor.app" into macos/build/. Copy it to /Applications
# for "Launch at login" to work reliably.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
BIN="$(swift build -c release --show-bin-path)/GoodWeMenuBar"

APP="build/GoodWe Monitor.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/GoodWeMenuBar"

mkdir -p "$APP/Contents/Resources"
rm -rf build/AppIcon.iconset
swift make_icon.swift build/AppIcon.iconset
iconutil -c icns build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.goodwe-esa-monitor.menubar</string>
    <key>CFBundleName</key>
    <string>GoodWe Monitor</string>
    <key>CFBundleDisplayName</key>
    <string>GoodWe Monitor</string>
    <key>CFBundleExecutable</key>
    <string>GoodWeMenuBar</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSLocalNetworkUsageDescription</key>
    <string>Connects to the GoodWe ESA Monitor web app on your local network.</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <!-- The monitor serves plain ws:// on the LAN. -->
        <key>NSAllowsArbitraryLoads</key>
        <true/>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP"
echo "Built $PWD/$APP"
