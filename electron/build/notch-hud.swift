// Sunday notch HUD — a native AppKit accessory app that renders a HUD at the
// real MacBook notch. NSScreen.safeAreaInsets / auxiliaryTopLeftArea only
// return real values inside a GUI app process; Electron can't read or draw the
// notch. Same technique as Boring Notch / NotchNook / DynamicNotchKit.
//
// - Finds the screen with a notch; renders there. No notch → draws nothing.
// - Idle: a black bar the size of the notch (merges with it).
// - Working: the bar widens with the live agent count beside the notch.
// - Click: a single dark shape that FLARES out of the notch (Dynamic Island
//   style) — narrow at the notch, widening into a rounded body below.
// - Polls the daemon's /v1/status for the count + model.
//
// Launched by Electron with the daemon URL as argv[1].
// Build: swiftc -O -o notch-hud notch-hud.swift

import AppKit

let DAEMON = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "http://127.0.0.1:8765"
let TOKEN = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : ""
let RADIUS: CGFloat = 14
let AMBER = NSColor(calibratedRed: 0.91, green: 0.58, blue: 0.18, alpha: 1)
let APP_BUNDLE_ID = "com.sunday.desktop"
let WS_URL: URL? = {
    var s = DAEMON
    if s.hasPrefix("https://") { s = "wss://" + s.dropFirst("https://".count) }
    else if s.hasPrefix("http://") { s = "ws://" + s.dropFirst("http://".count) }
    var u = s + "/v1/ws"
    if !TOKEN.isEmpty { u += "?token=" + TOKEN }
    return URL(string: u)
}()

// Authenticated GET against the daemon (status polls). Adds the bearer.
func authedRequest(_ url: URL) -> URLRequest {
    var r = URLRequest(url: url)
    if !TOKEN.isEmpty { r.setValue("Bearer \(TOKEN)", forHTTPHeaderField: "Authorization") }
    return r
}

extension NSScreen {
    var notchRect: NSRect? {
        guard #available(macOS 12.0, *),
              let left = auxiliaryTopLeftArea?.width,
              let right = auxiliaryTopRightArea?.width else { return nil }
        let h = safeAreaInsets.top
        guard h > 0 else { return nil }
        let w = frame.width - left - right
        guard w > 1 else { return nil }
        return NSRect(x: frame.midX - w / 2, y: frame.maxY - h, width: w, height: h)
    }
}
func notchedScreen() -> NSScreen? { NSScreen.screens.first(where: { $0.notchRect != nil }) }

/// Non-activating NSPanel that can still take key focus when we explicitly
/// ask for it (so the inline reply input works). Without this override the
/// .nonactivatingPanel style mask prevents the text field from ever becoming
/// first responder — clicks land but typing goes nowhere.
final class KeyablePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// ── compact / working bar: square top, rounded bottom ────────────────────
final class BarView: NSView {
    var onClick: (() -> Void)?
    private let countLabel = NSTextField(labelWithString: "")   // right shoulder (idle/agents)
    private let timerLabel = NSTextField(labelWithString: "")   // left shoulder (meeting timer)
    private let recDot = NSView()                               // right shoulder (meeting red dot)
    var meetingMode = false
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.cornerRadius = RADIUS
        layer?.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]  // bottom only
        countLabel.font = .monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        countLabel.textColor = AMBER
        countLabel.isBezeled = false; countLabel.drawsBackground = false; countLabel.alignment = .right
        addSubview(countLabel)
        // Meeting: timer on the LEFT.
        timerLabel.font = .monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        timerLabel.textColor = .white
        timerLabel.isBezeled = false; timerLabel.drawsBackground = false; timerLabel.alignment = .left
        timerLabel.isHidden = true
        addSubview(timerLabel)
        // Meeting: pulsing red dot on the RIGHT.
        recDot.wantsLayer = true
        recDot.layer?.cornerRadius = 5
        recDot.layer?.backgroundColor = NSColor.systemRed.cgColor
        recDot.isHidden = true
        addSubview(recDot)
    }
    required init?(coder: NSCoder) { fatalError() }
    func setStatus(_ text: String) { countLabel.stringValue = text; needsLayout = true }
    /// Meeting recording: timer text on the left, red dot on the right.
    func setMeeting(_ on: Bool, timer: String) {
        meetingMode = on
        timerLabel.stringValue = timer
        timerLabel.isHidden = !on
        recDot.isHidden = !on
        countLabel.isHidden = on        // hide the idle counter while recording
        needsLayout = true
        if on { startPulse() } else { recDot.layer?.removeAllAnimations() }
    }
    private func startPulse() {
        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 1.0; pulse.toValue = 0.25
        pulse.duration = 0.9; pulse.autoreverses = true
        pulse.repeatCount = .infinity
        recDot.layer?.add(pulse, forKey: "pulse")
    }
    override func layout() {
        super.layout()
        countLabel.frame = NSRect(x: bounds.maxX - 66, y: bounds.midY - 9, width: 56, height: 18)
        timerLabel.frame = NSRect(x: 14, y: bounds.midY - 9, width: 80, height: 18)
        recDot.frame     = NSRect(x: bounds.maxX - 22, y: bounds.midY - 5, width: 10, height: 10)
    }
    override func mouseDown(with event: NSEvent) { onClick?() }
}

// ── interjection view: Sunday's proactive note with engagement controls
//    (👍 / 👎 / inline reply) inside the notch dropdown. ──
/// SF Symbol button styled to match the notch — monochrome, white tint, soft
/// circular background. Replaces emoji thumbs (which were too playful) and
/// the dismiss "×" all use the same builder.
private func symbolBtn(_ name: String, weight: NSFont.Weight = .regular, size: CGFloat = 14) -> NSButton {
    let b = NSButton()
    b.isBordered = false
    b.bezelStyle = .regularSquare
    b.imagePosition = .imageOnly
    b.wantsLayer = true
    b.layer?.cornerRadius = 14
    b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.07).cgColor
    if let img = NSImage(systemSymbolName: name, accessibilityDescription: nil) {
        let cfg = NSImage.SymbolConfiguration(pointSize: size, weight: weight)
        b.image = img.withSymbolConfiguration(cfg)
        b.contentTintColor = .white
    } else {
        b.title = name   // fallback for older systems
    }
    return b
}

final class InterjectionView: NSView, NSTextFieldDelegate {
    var notchH: CGFloat = 38
    let content = NSView()
    let body = NSTextField(wrappingLabelWithString: "")
    let upBtn = symbolBtn("hand.thumbsup", weight: .regular, size: 14)
    let downBtn = symbolBtn("hand.thumbsdown", weight: .regular, size: 14)
    let dismissBtn = symbolBtn("xmark", weight: .medium, size: 11)
    let replyField = NSTextField()
    var onUp: (() -> Void)?
    var onDown: (() -> Void)?
    var onDismiss: (() -> Void)?
    var onSubmit: ((String) -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.cornerRadius = 24
        layer?.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]
        // No border — the previous amber outline read as a glitch.
        addSubview(content)
        body.font = .systemFont(ofSize: 13.5, weight: .medium)
        body.textColor = .white
        body.isBezeled = false; body.drawsBackground = false
        body.maximumNumberOfLines = 5
        body.lineBreakMode = .byWordWrapping

        upBtn.target = self; upBtn.action = #selector(thumbUp)
        downBtn.target = self; downBtn.action = #selector(thumbDown)
        dismissBtn.target = self; dismissBtn.action = #selector(dismissClicked)
        // The dismiss × is smaller and chromeless to read as secondary.
        dismissBtn.layer?.cornerRadius = 11
        dismissBtn.layer?.backgroundColor = NSColor.clear.cgColor

        replyField.placeholderString = "Reply…"
        replyField.font = .systemFont(ofSize: 13)
        replyField.textColor = .white
        replyField.backgroundColor = NSColor.white.withAlphaComponent(0.07)
        replyField.isBezeled = false
        replyField.focusRingType = .none
        replyField.delegate = self
        replyField.target = self
        replyField.action = #selector(submitReply)
        // Inset the text inside the field so the placeholder isn't pinned at x=0.
        if let cell = replyField.cell as? NSTextFieldCell {
            cell.placeholderAttributedString = NSAttributedString(string: "Reply…", attributes: [
                .foregroundColor: NSColor.white.withAlphaComponent(0.35),
                .font: NSFont.systemFont(ofSize: 13),
            ])
        }

        content.addSubview(body)
        content.addSubview(upBtn)
        content.addSubview(downBtn)
        content.addSubview(dismissBtn)
        content.addSubview(replyField)
    }
    required init?(coder: NSCoder) { fatalError() }
    override var isFlipped: Bool { false }

    @objc func thumbUp()        { onUp?() }
    @objc func thumbDown()      { onDown?() }
    @objc func dismissClicked() { onDismiss?() }
    @objc func submitReply()    {
        let t = replyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        replyField.stringValue = ""
        onSubmit?(t)
    }

    func focusReply() {
        DispatchQueue.main.async {
            self.window?.makeFirstResponder(self.replyField)
        }
    }

    func layoutContent() {
        content.frame = bounds
        let W = bounds.width, H = bounds.height
        let inset: CGFloat = 18
        // Dismiss × in the top-right corner of the content area (below notch).
        let bodyTop = H - notchH - 4
        let xSize: CGFloat = 22
        dismissBtn.frame = NSRect(x: W - inset - xSize, y: bodyTop - xSize, width: xSize, height: xSize)
        // Text body uses the rest of the row above the controls.
        let rowY: CGFloat = 14
        let btnSize: CGFloat = 28
        body.frame = NSRect(
            x: inset,
            y: rowY + btnSize + 12,
            width: W - inset * 2 - xSize - 6,
            height: bodyTop - (rowY + btnSize + 12) - 4
        )
        // Controls row: reply input stretched, thumbs on right.
        let gap: CGFloat = 6
        let thumbsW = btnSize * 2 + gap
        let fieldW = W - inset * 2 - thumbsW - 10
        replyField.frame = NSRect(x: inset + 8, y: rowY, width: fieldW, height: btnSize)
        downBtn.frame    = NSRect(x: inset + fieldW + 10, y: rowY, width: btnSize, height: btnSize)
        upBtn.frame      = NSRect(x: inset + fieldW + 10 + btnSize + gap, y: rowY, width: btnSize, height: btnSize)
    }
}

// ── expanded: a clean dark panel hanging from the notch — sides run
//    straight up to a flush top, rounded bottom (a big version of the bar) ──
final class FlareView: NSView {
    var onClick: (() -> Void)?
    var notchH: CGFloat = 38
    // Minimal mode: a fresh detection shows ONLY the sentence, centered, in a
    // short panel just below the notch — no title/dot/subtitle chrome.
    var minimal = false
    let content = NSView()                 // holds the labels; faded in on open
    let title = NSTextField(labelWithString: "Sunday")
    let dot = NSView()                     // status dot (amber when working)
    let subtitle = NSTextField(labelWithString: "")
    let body = NSTextField(wrappingLabelWithString: "Nothing running")

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.cornerRadius = 28
        layer?.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]  // bottom corners only; top flush
        content.wantsLayer = true
        addSubview(content)
        title.font = .systemFont(ofSize: 16, weight: .semibold); title.textColor = .white
        title.isBezeled = false; title.drawsBackground = false
        dot.wantsLayer = true; dot.layer?.cornerRadius = 3.5
        dot.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.25).cgColor
        subtitle.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        subtitle.textColor = NSColor.white.withAlphaComponent(0.45)
        subtitle.isBezeled = false; subtitle.drawsBackground = false
        body.font = .systemFont(ofSize: 13); body.textColor = NSColor.white.withAlphaComponent(0.82)
        body.maximumNumberOfLines = 6
        content.addSubview(title); content.addSubview(dot); content.addSubview(subtitle); content.addSubview(body)
    }
    required init?(coder: NSCoder) { fatalError() }
    override var isFlipped: Bool { false }   // y up, top = maxY

    func setWorking(_ working: Bool) {
        dot.layer?.backgroundColor = (working ? AMBER : NSColor.white.withAlphaComponent(0.22)).cgColor
    }

    func layoutContent() {
        content.frame = bounds
        let W = bounds.width, H = bounds.height
        let inset: CGFloat = 24
        let bodyTop = H - notchH          // content starts just below the notch/menu-bar strip
        if minimal {
            // Just the words, centered, in amber, in a short panel that hangs
            // only a little below the notch.
            title.isHidden = true; dot.isHidden = true; subtitle.isHidden = true
            body.isHidden = false
            body.alignment = .center
            body.font = .systemFont(ofSize: 14, weight: .semibold)
            body.textColor = AMBER
            body.maximumNumberOfLines = 2
            body.frame = NSRect(x: inset, y: 9, width: W - inset * 2, height: bodyTop - 12)
            return
        }
        title.isHidden = false; dot.isHidden = false; subtitle.isHidden = false
        body.alignment = .left
        body.font = .systemFont(ofSize: 13)
        body.textColor = NSColor.white.withAlphaComponent(0.82)
        body.maximumNumberOfLines = 6
        title.frame    = NSRect(x: inset, y: bodyTop - 36, width: W - inset * 2, height: 24)
        dot.frame      = NSRect(x: inset, y: bodyTop - 53, width: 7, height: 7)
        subtitle.frame = NSRect(x: inset + 14, y: bodyTop - 56, width: W - inset * 2 - 14, height: 16)
        body.frame     = NSRect(x: inset, y: 22, width: W - inset * 2, height: bodyTop - 84)
    }
    override func mouseDown(with event: NSEvent) { onClick?() }
}

// ── controller ───────────────────────────────────────────────────────────
final class NotchHUD: NSObject, NSApplicationDelegate {
    let panel = KeyablePanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    let bar = BarView(frame: .zero)
    let flare = FlareView(frame: .zero)
    let interject = InterjectionView(frame: .zero)
    var expanded = false
    var agentCount = 0, agentTasks: [String] = [], modelName = ""
    var nowText: String? = nil, nowSince: Double? = nil
    var notifying = false, notifyText = ""
    // flashNow: a fresh observer detection briefly drops the FULL sentence
    // from the notch, then settles back to the compact counter.
    var flashingNow = false
    // Interjection (proactive note Sunday wants to surface, with engagement
    // affordances right in the notch — thumbs + inline reply).
    var interjecting = false
    var interjection: (id: Int, text: String)? = nil
    // Meeting recording state (from /v1/status.meeting).
    var meetingRecording = false
    var meetingSince: Double? = nil
    // "Hey Sunday" was heard — the notch pops to a Listening state until the
    // reply lands (or a timeout, in case only the wake word was spoken).
    var listening = false

    /// Compact human duration of the current activity (e.g. "14m", "1h 04m").
    func durationString() -> String? {
        guard let since = nowSince else { return nil }
        let secs = max(0, Int(Date().timeIntervalSince1970 - since))
        if secs < 60 { return "\(max(secs, 1))s" }
        let m = secs / 60
        if m < 60 { return "\(m)m" }
        return "\(m/60)h \(String(format: "%02d", m % 60))m"
    }

    /// Meeting timer — always mm:ss (or h:mm:ss), monospaced, ticking live.
    func meetingDurationString() -> String {
        let since = meetingSince ?? Date().timeIntervalSince1970
        let secs = max(0, Int(Date().timeIntervalSince1970 - since))
        let h = secs / 3600, m = (secs % 3600) / 60, s = secs % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%02d:%02d", m, s)
    }

    /// Right-shoulder bar text. The observer's "now" line is intentionally
    /// NOT surfaced here anymore — it noisy without earning its keep. The
    /// "now" is still used internally (sticky conversations, proac trigger),
    /// just not displayed. Only sub-agents and pending interjections get
    /// shoulder real estate.
    func shoulderText() -> String {
        if agentCount > 0 { return "● \(agentCount)" }
        return ""
    }
    var wsTask: URLSessionWebSocketTask?
    var dismissWork: DispatchWorkItem?

    func applicationDidFinishLaunching(_ n: Notification) {
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = false
        panel.level = NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()))
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        let container = NSView(); container.autoresizesSubviews = false; panel.contentView = container
        bar.onClick = { [weak self] in
            guard let self else { return }
            // Tapping the bar during a meeting stops the recording — the HUD
            // is the control, not just a readout.
            if self.meetingRecording { self.requestMeetingStop() } else { self.toggle() }
        }
        flare.onClick = { [weak self] in self?.flareClicked() }
        interject.onUp      = { [weak self] in self?.engageInterjection(feedback: "up") }
        interject.onDown    = { [weak self] in self?.engageInterjection(feedback: "down") }
        interject.onDismiss = { [weak self] in self?.dismissInterjectionExplicitly() }
        interject.onSubmit  = { [weak self] reply in self?.engageInterjection(feedback: nil, reply: reply) }
        container.addSubview(bar); container.addSubview(flare); container.addSubview(interject)
        NotificationCenter.default.addObserver(self, selector: #selector(relayout),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)
        relayout(); poll(); connectWS()
        Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.poll() }
        // Tick the meeting timer once a second while recording (the 1.5s poll
        // is too coarse for a live mm:ss).
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self, self.meetingRecording, !self.expanded else { return }
            self.bar.setMeeting(true, timer: self.meetingDurationString())
        }
    }

    func toggle() { expanded.toggle(); layout(animated: true) }
    func flareClicked() {
        if notifying { activateSunday(); dismissNotify() } else { toggle() }
    }
    func activateSunday() {
        NSRunningApplication.runningApplications(withBundleIdentifier: APP_BUNDLE_ID).first?.activate()
    }
    /// Ask the app to stop the active meeting (the Mac app polls for this).
    func requestMeetingStop() {
        guard let url = URL(string: "\(DAEMON)/v1/meetings/stop-request") else { return }
        var req = authedRequest(url); req.httpMethod = "POST"
        URLSession.shared.dataTask(with: req).resume()
        // Optimistically drop the meeting bar; the next poll confirms.
        meetingRecording = false
        notify("Wrapping up the meeting…")
    }

    // ── notification: drop a preview from the notch when a reply lands and
    //    Sunday isn't the frontmost app ──
    // ── "Hey Sunday" listening: pop the notch the instant the wake word lands,
    //    before the answer, so it feels responsive. Auto-clears if no reply
    //    arrives (e.g. you said only "Hey Sunday"). ──
    func setListening() {
        listening = true
        notifying = false; flashingNow = false; interjecting = false; expanded = false
        layout(animated: true)
        dismissWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.endListening() }
        dismissWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 12, execute: work)
    }
    func endListening() {
        listening = false
        if !expanded { layout(animated: true) }
    }

    func notify(_ text: String) {
        listening = false   // the reply supersedes the Listening state
        notifyText = String(text.prefix(180))
        notifying = true; expanded = false
        layout(animated: true)
        dismissWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.dismissNotify() }
        dismissWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 6, execute: work)
    }
    func dismissNotify() { notifying = false; layout(animated: true) }

    // ── interjection: Sunday wants to say something. Show it with thumbs +
    //    inline reply input. Engagement (any of: 👍/👎/reply/click) folds
    //    into main chat; otherwise the daemon's auto-dismiss handles it. ──
    func showInterjection(id: Int, text: String) {
        interjection = (id, text)
        interjecting = true
        flashingNow = false; notifying = false; expanded = false
        layout(animated: true)
        // Make the panel temporarily key so the text field can receive input,
        // then place the cursor in the reply field — you should be able to
        // just start typing immediately.
        panel.makeKeyAndOrderFront(nil)
        interject.focusReply()
    }
    /// Explicit dismiss (the × button) — fire-and-forget POST to /dismiss so
    /// the daemon extends the cooldown like the user said "not now".
    func dismissInterjectionExplicitly() {
        if let inter = interjection,
           let url = URL(string: "\(DAEMON)/v1/interjections/\(inter.id)/dismiss") {
            var req = authedRequest(url); req.httpMethod = "POST"
            URLSession.shared.dataTask(with: req).resume()
        }
        dismissInterjection()
    }
    func dismissInterjection() {
        interjection = nil; interjecting = false
        layout(animated: true)
    }
    /// Engage via thumb. feedback: "up" | "down" | nil (plain click).
    func engageInterjection(feedback: String?, reply: String? = nil) {
        guard let inter = interjection else { return }
        let base = URL(string: DAEMON)!.appendingPathComponent("/v1/interjections/\(inter.id)/engage")
        var req = authedRequest(base)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [:]
        if let f = feedback { body["feedback"] = f }
        if let r = reply, !r.isEmpty { body["reply"] = r }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()
        dismissInterjection()
        if reply != nil { activateSunday() }   // they typed — open chat
    }

    // ── observer detection: drop the full "now" sentence from the notch for a
    //    few seconds, then settle back to the compact counter. The counter
    //    stays up top; the sentence is only here transiently (or on click). ──
    func flashNow(_ text: String) {
        guard !text.isEmpty else { return }
        flashingNow = true
        notifying = false
        expanded = false
        layout(animated: true)
        dismissWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.endFlashNow() }
        dismissWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }
    func endFlashNow() {
        flashingNow = false
        // Don't fight a user who expanded it manually in the meantime.
        if !expanded { layout(animated: true) }
    }

    func connectWS() {
        guard let url = WS_URL else { return }
        let task = URLSession.shared.webSocketTask(with: url)
        wsTask = task; task.resume(); listen(task)
    }
    func listen(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                if case .string(let s) = msg, let d = s.data(using: .utf8),
                   let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                    let kind = j["type"] as? String
                    if kind == "stream_end",
                       let text = j["content_full"] as? String, !text.isEmpty {
                        DispatchQueue.main.async {
                            if NSWorkspace.shared.frontmostApplication?.bundleIdentifier != APP_BUNDLE_ID {
                                self.notify(text)
                            }
                        }
                    } else if kind == "interjection",
                              let id = j["id"] as? Int,
                              let text = j["text"] as? String, !text.isEmpty {
                        DispatchQueue.main.async { self.showInterjection(id: id, text: text) }
                    } else if kind == "toast", let text = j["text"] as? String, !text.isEmpty {
                        DispatchQueue.main.async { self.notify(text) }   // e.g. "Meeting notes ready"
                    } else if kind == "wake_listening" {
                        DispatchQueue.main.async { self.setListening() }  // "Hey Sunday" heard
                    } else if kind == "wake_reply", let text = j["text"] as? String {
                        DispatchQueue.main.async { self.notify(text.isEmpty ? "…" : text) }
                    }
                }
                self.listen(task)   // keep receiving
            case .failure:
                self.wsTask = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.connectWS() }
            }
        }
    }

    @objc func relayout() { layout(animated: false) }

    func layout(animated: Bool) {
        guard let screen = notchedScreen(), let notch = screen.notchRect else { panel.orderOut(nil); return }
        let nh = notch.height, nw = notch.width
        let showInterject = interjecting && interjection != nil
        let showFlare = (expanded || notifying || flashingNow || listening) && !showInterject
        // A fresh detection or the "Hey Sunday" listening pop (not a manual
        // expand, not a reply notify) renders as the minimal "just the words" drop.
        let minimal = (flashingNow || listening) && !expanded && !notifying
        var target = NSRect.zero
        if showInterject {
            // Sunday is interjecting — show text + thumbs + inline reply.
            let W: CGFloat = max(420, nw + 220)
            let H: CGFloat = nh + 140
            target = NSRect(x: screen.frame.midX - W / 2, y: screen.frame.maxY - H, width: W, height: H)
            bar.isHidden = true; flare.isHidden = true; interject.isHidden = false
            interject.frame = NSRect(x: 0, y: 0, width: W, height: H)
            interject.autoresizingMask = [.width, .height]
            interject.notchH = nh
            interject.body.stringValue = interjection?.text ?? ""
            interject.layoutContent(); interject.needsDisplay = true
        } else if showFlare {
            let W: CGFloat = minimal ? max(300, nw + 140) : max(360, nw + 180)
            // minimal hangs only a little below the notch (less far down).
            let H: CGFloat = nh + (minimal ? 44 : (notifying ? 160 : 210))
            target = NSRect(x: screen.frame.midX - W / 2, y: screen.frame.maxY - H, width: W, height: H)
            bar.isHidden = true; flare.isHidden = false
            flare.frame = NSRect(x: 0, y: 0, width: W, height: H)
            flare.autoresizingMask = [.width, .height]   // track the window during the animated resize
            flare.notchH = nh
            flare.minimal = minimal
            if minimal {
                // "Hey Sunday" → "Listening…"; otherwise the observer sentence.
                flare.body.stringValue = listening ? "Listening…" : (nowText ?? "")
            } else if notifying {
                flare.subtitle.stringValue = "tap to open"
                flare.body.stringValue = notifyText
                flare.setWorking(false)
            } else if let n = nowText, !n.isEmpty {
                // observer is live — make "what you're doing right now" the prominent line
                flare.subtitle.stringValue = durationString() ?? modelName
                flare.body.stringValue = n + (agentTasks.isEmpty ? "" : "\n\n" + agentTasks.map { "• \($0)" }.joined(separator: "\n"))
                flare.setWorking(true)
            } else {
                flare.subtitle.stringValue = modelName
                flare.body.stringValue = agentTasks.isEmpty ? "Nothing running" : agentTasks.map { "• \($0)" }.joined(separator: "\n")
                flare.setWorking(agentCount > 0)
            }
            flare.layoutContent(); flare.needsDisplay = true
        } else if meetingRecording {
            // Dynamic-island meeting bar: timer on the left, notch in the
            // middle, pulsing red dot on the right.
            let W = nw + 110 + 36, H = nh
            target = NSRect(x: screen.frame.midX - W / 2, y: screen.frame.maxY - H, width: W, height: H)
            flare.isHidden = true; bar.isHidden = false; interject.isHidden = true
            bar.frame = NSRect(x: 0, y: 0, width: W, height: H)
            bar.autoresizingMask = [.width, .height]
            bar.setMeeting(true, timer: meetingDurationString())
        } else {
            let status = shoulderText()
            // The compact bar now shows only a short counter (e.g. "27s") or a
            // tiny agent count — so the shoulder is small and never sticks far
            // out past the notch. The full sentence lives in the flash / click.
            bar.setMeeting(false, timer: "")
            let shoulders: CGFloat = status.isEmpty ? 0 : 74
            let W = nw + shoulders, H = nh
            target = NSRect(x: screen.frame.midX - W / 2, y: screen.frame.maxY - H, width: W, height: H)
            flare.isHidden = true; bar.isHidden = false; interject.isHidden = true
            bar.frame = NSRect(x: 0, y: 0, width: W, height: H)
            bar.autoresizingMask = [.width, .height]
            bar.setStatus(status)
        }

        let firstShow = !panel.isVisible
        if firstShow { panel.orderFrontRegardless() }

        if !animated || firstShow {
            flare.isHidden = !showFlare; bar.isHidden = showFlare
            if showFlare { flare.content.alphaValue = 1 }
            panel.setFrame(target, display: true)
            return
        }

        if showFlare {
            // OPENING: grow the window with a gentle overshoot-settle and fade
            // the content in. (The bar is hidden; the card is what's revealed.)
            flare.isHidden = false; bar.isHidden = true
            flare.content.alphaValue = 0
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.32
                ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.16, 1.0, 0.3, 1.0)  // overshoot-settle
                panel.animator().setFrame(target, display: true)
                flare.content.animator().alphaValue = 1
            }
        } else {
            // CLOSING: keep the card on screen, fade it out as the window
            // shrinks (smooth ease, NO overshoot — that's what felt jerky),
            // and only swap to the bar once the shrink finishes.
            bar.isHidden = true
            let finalStatus = shoulderText()
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.28
                ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                panel.animator().setFrame(target, display: true)
                flare.content.animator().alphaValue = 0
            }, completionHandler: { [weak self] in
                guard let self = self, !self.expanded, !self.notifying else { return }
                self.flare.isHidden = true
                self.bar.frame = NSRect(x: 0, y: 0, width: target.width, height: target.height)
                self.bar.setStatus(finalStatus)
                self.bar.isHidden = false
            })
        }
    }

    func poll() {
        guard let url = URL(string: "\(DAEMON)/v1/status") else { return }
        URLSession.shared.dataTask(with: authedRequest(url)) { [weak self] data, _, _ in
            guard let self, let data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let agents = (j["agents"] as? [[String: Any]]) ?? []
            let tasks = agents.compactMap { $0["task"] as? String }
            let model = (j["model"] as? String).map { $0.components(separatedBy: "/").last ?? $0 } ?? ""
            let nowFromServer = j["now"] as? String
            let sinceFromServer: Double? = {
                if let d = j["since"] as? Double { return d }
                if let n = j["since"] as? NSNumber { return n.doubleValue }
                return nil
            }()
            // Meeting recording state.
            let mtg = j["meeting"] as? [String: Any]
            let recording = (mtg?["recording"] as? Bool) ?? false
            let mtgSince: Double? = {
                if let d = mtg?["since"] as? Double { return d }
                if let n = mtg?["since"] as? NSNumber { return n.doubleValue }
                return nil
            }()
            DispatchQueue.main.async {
                let recChanged = recording != self.meetingRecording
                self.meetingRecording = recording
                self.meetingSince = mtgSince
                if recChanged { self.relayout() }
                let prevNow = self.nowText ?? ""
                let newNow = (nowFromServer?.isEmpty ?? true) ? "" : (nowFromServer ?? "")
                let changed = (agents.count != self.agentCount)
                    || newNow != prevNow
                    || (sinceFromServer != self.nowSince)
                self.agentCount = agents.count
                self.agentTasks = tasks
                self.modelName = model
                self.nowText = newNow.isEmpty ? nil : newNow
                self.nowSince = sinceFromServer
                // Per user feedback: the notch no longer auto-flashes the
                // observer's "what you're doing" detection. The data is still
                // captured + used internally (sticky conversations, proac
                // trigger, atoms) — just no longer surfaced as UI noise.
                // The notch stays quiet until Sunday actually has something
                // to say (an interjection) or there's an active sub-agent.
                if changed || self.expanded {
                    self.relayout()
                } else {
                    self.bar.setStatus(self.shoulderText())
                }
            }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = NotchHUD()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
