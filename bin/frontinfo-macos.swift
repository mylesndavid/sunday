// frontinfo-macos — print "<app name>\t<front window title>" for the frontmost
// app, WITHOUT AppleScript / System Events (which hangs for 5s+ waiting on an
// Automation-permission prompt that never appears in a background session).
//
// - App name: NSWorkspace.frontmostApplication — needs NO permission.
// - Window title: CGWindowListCopyWindowInfo — kCGWindowName is only populated
//   when the caller has Screen Recording permission, which the rewind capture
//   process already holds. So this reuses a permission we already require and
//   asks for nothing new.
//
// Compiled once and cached at ~/.sunday/bin/frontinfo by rewind_macos.py, same
// as the OCR helper. Falls back to `lsappinfo` (app name only) if this fails.

import AppKit
import CoreGraphics

let front = NSWorkspace.shared.frontmostApplication
let appName = front?.localizedName ?? ""
let pid = front?.processIdentifier ?? -1

var title = ""
if pid > 0,
   let windows = CGWindowListCopyWindowInfo(
     [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
  // On-screen windows come back front-to-back; take the frontmost normal-layer
  // window owned by the frontmost app that carries a non-empty name.
  for w in windows {
    guard let owner = w[kCGWindowOwnerPID as String] as? Int, Int32(owner) == pid else { continue }
    let layer = (w[kCGWindowLayer as String] as? Int) ?? 0
    if layer != 0 { continue }
    if let name = w[kCGWindowName as String] as? String, !name.isEmpty {
      title = name
      break
    }
  }
}

print(appName + "\t" + title)
