// Reads the *exact* notch geometry from macOS so the HUD can match it
// clinically. Electron's screen API doesn't expose the notch width — only
// NSScreen does (safeAreaInsets + auxiliaryTop{Left,Right}Area).
//
// Prints JSON: screen width/height (points), notch height (safe-area top),
// notch width (screen − the two usable shoulders), and whether there's a
// physical notch. All in top-left-origin points (what Electron uses).
//
// Build: swiftc -O -o notch-metrics notch-metrics.swift

import AppKit

let screen = NSScreen.main ?? NSScreen.screens.first
guard let s = screen else { print("{\"hasNotch\":false}"); exit(0) }

let f = s.frame
let safeTop = s.safeAreaInsets.top      // notch / menu-bar height in points

var notchWidth = 0.0
var hasNotch = false
if #available(macOS 12.0, *) {
    if let left = s.auxiliaryTopLeftArea, let right = s.auxiliaryTopRightArea {
        // The two areas flank the notch; what's left between them is the notch.
        let w = f.width - left.width - right.width
        if w > 1 && safeTop > 0 {
            notchWidth = w
            hasNotch = true
        }
    }
}

print("{\"screenWidth\":\(Int(f.width)),\"screenHeight\":\(Int(f.height)),\"notchHeight\":\(Int(safeTop.rounded())),\"notchWidth\":\(Int(notchWidth.rounded())),\"hasNotch\":\(hasNotch)}")
