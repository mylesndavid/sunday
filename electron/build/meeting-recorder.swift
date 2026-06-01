// Meeting recorder — captures BOTH sides of a call to two WAV tracks:
//   <dir>/system.wav   the people coming through your speakers/headphones
//   <dir>/mic.wav      you
// Two tracks (not mixed) so transcription can label "You" vs "Others" for
// free. Runs until it receives SIGINT/SIGTERM (the app sends that to stop),
// then finalizes both files cleanly.
//
// Usage:  meeting-recorder <output-dir>
// Needs Screen Recording (system audio) + Microphone permission.
//
// Build:
//   swiftc -O -framework ScreenCaptureKit -framework AVFoundation \
//          -framework CoreMedia meeting-recorder.swift -o meeting-recorder

import AVFoundation
import ScreenCaptureKit

// ── system-audio capture via ScreenCaptureKit ──────────────────────────────
@available(macOS 13.0, *)
final class SystemAudioTap: NSObject, SCStreamOutput {
    private var stream: SCStream?
    private var file: AVAudioFile?
    private let url: URL
    init(url: URL) { self.url = url }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw NSError(domain: "rec", code: 2) }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 2
        cfg.width = 2; cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let s = SCStream(filter: filter, configuration: cfg, delegate: nil)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInitiated))
        self.stream = s
        try await s.startCapture()
    }
    func stop() async {
        try? await stream?.stopCapture()
        file = nil
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, let pcm = sb.asPCMBuffer else { return }
        do {
            if file == nil {
                file = try AVAudioFile(forWriting: url, settings: pcm.format.settings,
                                       commonFormat: .pcmFormatFloat32, interleaved: false)
            }
            try file?.write(from: pcm)
        } catch { FileHandle.standardError.write("system write: \(error)\n".data(using: .utf8)!) }
    }
}

// ── mic capture via AVCaptureSession ───────────────────────────────────────
final class MicTap: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let session = AVCaptureSession()
    private var file: AVAudioFile?
    private let url: URL
    init(url: URL) { self.url = url }

    func start() throws {
        session.beginConfiguration()
        guard let dev = AVCaptureDevice.default(for: .audio) else { throw NSError(domain: "rec", code: 3) }
        let input = try AVCaptureDeviceInput(device: dev)
        if session.canAddInput(input) { session.addInput(input) }
        let out = AVCaptureAudioDataOutput()
        out.setSampleBufferDelegate(self, queue: .global(qos: .userInitiated))
        if session.canAddOutput(out) { session.addOutput(out) }
        session.commitConfiguration()
        session.startRunning()
    }
    func stop() { session.stopRunning(); file = nil }
    func captureOutput(_ o: AVCaptureOutput, didOutput sb: CMSampleBuffer, from c: AVCaptureConnection) {
        guard sb.isValid, let pcm = sb.asPCMBuffer else { return }
        do {
            if file == nil {
                file = try AVAudioFile(forWriting: url, settings: pcm.format.settings,
                                       commonFormat: .pcmFormatFloat32, interleaved: false)
            }
            try file?.write(from: pcm)
        } catch { FileHandle.standardError.write("mic write: \(error)\n".data(using: .utf8)!) }
    }
}

@available(macOS 13.0, *)
extension CMSampleBuffer {
    var asPCMBuffer: AVAudioPCMBuffer? {
        try? withAudioBufferList { abl, _ -> AVAudioPCMBuffer? in
            guard let absd = self.formatDescription?.audioStreamBasicDescription,
                  let fmt = AVAudioFormat(standardFormatWithSampleRate: absd.mSampleRate,
                                          channels: absd.mChannelsPerFrame) else { return nil }
            return AVAudioPCMBuffer(pcmFormat: fmt, bufferListNoCopy: abl.unsafePointer)
        }
    }
}

// ── entry ──
guard #available(macOS 13.0, *) else {
    FileHandle.standardError.write("needs macOS 13+\n".data(using: .utf8)!); exit(1)
}
let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: meeting-recorder <output-dir>\n".data(using: .utf8)!); exit(1)
}
let dir = URL(fileURLWithPath: args[1])
try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

let sysTap = SystemAudioTap(url: dir.appendingPathComponent("system.wav"))
let micTap = MicTap(url: dir.appendingPathComponent("mic.wav"))

// Clean finalize on stop signal.
let stopSem = DispatchSemaphore(value: 0)
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
let sigInt  = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN)
let onStop: () -> Void = { stopSem.signal() }
sigTerm.setEventHandler(handler: onStop); sigInt.setEventHandler(handler: onStop)
sigTerm.resume(); sigInt.resume()

Task {
    do {
        try micTap.start()
        try await sysTap.start()
        FileHandle.standardError.write("recording → \(dir.path)\n".data(using: .utf8)!)
    } catch {
        FileHandle.standardError.write("start error: \(error)\n".data(using: .utf8)!)
        stopSem.signal()
    }
}

// Block until stop signal, then finalize.
DispatchQueue.global().async {
    stopSem.wait()
    Task {
        await sysTap.stop()
        micTap.stop()
        FileHandle.standardError.write("stopped + finalized\n".data(using: .utf8)!)
        exit(0)
    }
}
RunLoop.main.run()
