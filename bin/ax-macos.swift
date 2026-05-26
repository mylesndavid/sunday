// Native macOS UI control via the Accessibility API — Sunday's hands +
// eyes for any app, not just the browser. Same idea as a screen reader:
// read the frontmost app's UI as a labeled tree (with on-screen positions),
// then click/type/key like a human.
//
// Build:  swiftc -O -o ax-macos ax-macos.swift
// Usage:
//   ax-macos snapshot            # JSON of actionable elements in the front app
//   ax-macos click <x> <y>       # click at screen point
//   ax-macos type <text...>      # type unicode text into the focused field
//   ax-macos key  <combo>        # e.g. cmd+t, enter, cmd+shift+4
//
// Needs Accessibility permission (System Settings → Privacy & Security →
// Accessibility). Prints {"error":"AX_NOT_TRUSTED"} and triggers the prompt
// when it's missing.

import Foundation
import ApplicationServices
import AppKit
import CoreGraphics

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    print("{\"error\":\"\(msg)\"}")
    exit(1)
}

func ensureTrusted() {
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    if !AXIsProcessTrustedWithOptions(opts) {
        fail("AX_NOT_TRUSTED")
    }
}

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func str(_ el: AXUIElement, _ name: String) -> String? {
    attr(el, name) as? String
}

func point(_ el: AXUIElement, _ name: String) -> CGPoint? {
    guard let v = attr(el, name), CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero
    return AXValueGetValue(v as! AXValue, .cgPoint, &p) ? p : nil
}
func size(_ el: AXUIElement, _ name: String) -> CGSize? {
    guard let v = attr(el, name), CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var s = CGSize.zero
    return AXValueGetValue(v as! AXValue, .cgSize, &s) ? s : nil
}

let ACTIONABLE: Set<String> = [
    "AXButton", "AXTextField", "AXTextArea", "AXLink", "AXMenuItem", "AXMenuBarItem",
    "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXTab", "AXTabGroup",
    "AXDisclosureTriangle", "AXStaticText", "AXSearchField", "AXCell", "AXRow", "AXSlider",
    "AXIncrementor", "AXSegmentedControl", "AXToolbarButton",
]

struct Node { let role: String; let label: String; let x: Double; let y: Double; let w: Double; let h: Double }

func jsonEscape(_ s: String) -> String {
    var o = ""
    for c in s.unicodeScalars {
        switch c {
        case "\"": o += "\\\""
        case "\\": o += "\\\\"
        case "\n": o += "\\n"
        case "\r": o += "\\r"
        case "\t": o += "\\t"
        default: o += (c.value < 0x20) ? "" : String(c)
        }
    }
    return o
}

func snapshot() {
    ensureTrusted()
    guard let app = NSWorkspace.shared.frontmostApplication else { fail("no frontmost app") }
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    var nodes: [Node] = []
    func walk(_ el: AXUIElement, _ depth: Int) {
        if depth > 14 || nodes.count > 350 { return }
        let role = str(el, kAXRoleAttribute as String) ?? ""
        let label = str(el, kAXTitleAttribute as String)
            ?? str(el, kAXDescriptionAttribute as String)
            ?? str(el, "AXValue")
            ?? str(el, kAXHelpAttribute as String) ?? ""
        if ACTIONABLE.contains(role), let p = point(el, kAXPositionAttribute as String),
           let sz = size(el, kAXSizeAttribute as String), sz.width > 1, sz.height > 1,
           !label.trimmingCharacters(in: .whitespaces).isEmpty {
            nodes.append(Node(role: role, label: String(label.prefix(90)),
                              x: p.x + sz.width / 2, y: p.y + sz.height / 2,
                              w: sz.width, h: sz.height))
        }
        if let kids = attr(el, kAXChildrenAttribute as String) as? [AXUIElement] {
            for k in kids { walk(k, depth + 1) }
        }
    }
    walk(axApp, 0)
    var items: [String] = []
    for n in nodes {
        items.append("{\"role\":\"\(jsonEscape(n.role))\",\"label\":\"\(jsonEscape(n.label))\",\"x\":\(Int(n.x)),\"y\":\(Int(n.y)),\"w\":\(Int(n.w)),\"h\":\(Int(n.h))}")
    }
    print("{\"app\":\"\(jsonEscape(app.localizedName ?? ""))\",\"elements\":[\(items.joined(separator: ","))]}")
}

func click(_ x: Double, _ y: Double) {
    ensureTrusted()
    let pt = CGPoint(x: x, y: y)
    let src = CGEventSource(stateID: .combinedSessionState)
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(40_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(20_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
    print("{\"ok\":true,\"x\":\(Int(x)),\"y\":\(Int(y))}")
}

func typeText(_ text: String) {
    ensureTrusted()
    let src = CGEventSource(stateID: .combinedSessionState)
    for ch in text {
        var u = Array(String(ch).utf16)
        let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true)!
        down.keyboardSetUnicodeString(stringLength: u.count, unicodeString: &u)
        down.post(tap: .cghidEventTap)
        let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false)!
        up.keyboardSetUnicodeString(stringLength: u.count, unicodeString: &u)
        up.post(tap: .cghidEventTap)
        usleep(6_000)
    }
    print("{\"ok\":true,\"typed\":\(text.count)}")
}

let KEYCODES: [String: CGKeyCode] = [
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
]

func keyCombo(_ combo: String) {
    ensureTrusted()
    var flags: CGEventFlags = []
    var keyName = ""
    for part in combo.lowercased().split(separator: "+").map({ String($0) }) {
        switch part {
        case "cmd", "command", "meta": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "opt", "option", "alt": flags.insert(.maskAlternate)
        case "ctrl", "control": flags.insert(.maskControl)
        default: keyName = part
        }
    }
    guard let code = KEYCODES[keyName] else { fail("unknown key: \(keyName)") }
    let src = CGEventSource(stateID: .combinedSessionState)
    let down = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true)!
    down.flags = flags
    down.post(tap: .cghidEventTap)
    let up = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false)!
    up.flags = flags
    up.post(tap: .cghidEventTap)
    print("{\"ok\":true,\"combo\":\"\(jsonEscape(combo))\"}")
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: ax-macos snapshot|click|type|key …") }
switch args[1] {
case "snapshot": snapshot()
case "click":
    guard args.count >= 4, let x = Double(args[2]), let y = Double(args[3]) else { fail("click needs <x> <y>") }
    click(x, y)
case "type":
    typeText(args.dropFirst(2).joined(separator: " "))
case "key":
    guard args.count >= 3 else { fail("key needs <combo>") }
    keyCombo(args[2])
default:
    fail("unknown subcommand: \(args[1])")
}
