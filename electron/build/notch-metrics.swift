// Reads the *exact* notch geometry from macOS so the HUD can match it
// clinically. Electron's screen API doesn't expose the notch width — only
// NSScreen does (safeAreaInsets + auxiliaryTop{Left,Right}Area).
//
// Prints JSON: screen width/height (points), notch height (safe-area top),
// notch width (screen − the two usable shoulders), and whether there's a
// physical notch. All in top-left-origin points (what Electron uses).
//
// IMPORTANT: NSScreen.safeAreaInsets and auxiliaryTop*Area return zero/nil
// until AppKit is initialized and connected to the window server. A bare CLI
// tool that just reads NSScreen.main gets 0 → reports no notch. So we spin up
// a headless NSApplication (.accessory: no Dock icon, no window) and let the
// run loop tick once before reading.
//
// Build: swiftc -O -o notch-metrics notch-metrics.swift

import AppKit

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // headless: no Dock icon, no menu, no window
app.finishLaunching()
// Let AppKit connect to the WindowServer so screen metrics populate.
RunLoop.current.run(until: Date().addingTimeInterval(0.15))

// Prefer the screen that actually has a notch (built-in display on a notched
// MacBook); fall back to the main/first screen otherwise.
func pickScreen() -> NSScreen? {
    if #available(macOS 12.0, *) {
        for sc in NSScreen.screens where sc.safeAreaInsets.top > 0 && sc.auxiliaryTopLeftArea != nil {
            return sc
        }
    }
    return NSScreen.main ?? NSScreen.screens.first
}

guard let s = pickScreen() else { print("{\"hasNotch\":false}"); exit(0) }

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
