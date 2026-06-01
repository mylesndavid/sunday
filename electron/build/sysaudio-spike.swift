// System-audio capture spike — proves ScreenCaptureKit can grab clean system
// audio (the people coming through your speakers/headphones on a call) and
// write it to a WAV. This is the one genuinely-new piece meeting mode needs;
// everything downstream (transcribe → summarize) already exists.
//
// Usage:  sysaudio-spike <seconds> <out.wav>
// Needs Screen Recording permission (SCK system-audio is gated behind it).
//
// macOS 13+ (ScreenCaptureKit audio). Build:
//   swiftc -O -framework ScreenCaptureKit -framework AVFoundation \
//          -framework CoreMedia sysaudio-spike.swift -o sysaudio-spike

import AVFoundation
import ScreenCaptureKit

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput {
    var stream: SCStream?
    var audioFile: AVAudioFile?
    let outURL: URL
    var samplesWritten = 0
    var done = false

    init(outURL: URL) { self.outURL = outURL }

    func start(seconds: Double) async throws {
        // Grab the shareable content; we attach audio to a display capture
        // (SCK requires a content filter even when we only want audio).
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            FileHandle.standardError.write("no display found\n".data(using: .utf8)!)
            exit(2)
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true   // don't record our own output
        cfg.sampleRate = 48000
        cfg.channelCount = 2
        // Keep video minimal — SCK needs a video config even for audio-only.
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global(qos: .userInitiated))
        self.stream = stream
        try await stream.startCapture()
        FileHandle.standardError.write("recording \(seconds)s of system audio…\n".data(using: .utf8)!)

        // Run for the requested duration, then stop + finalize.
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        try await stream.stopCapture()
        done = true
        audioFile = nil   // closes/finalizes the WAV
        FileHandle.standardError.write("done — wrote \(samplesWritten) sample buffers\n".data(using: .utf8)!)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, !done else { return }
        guard let pcm = sampleBuffer.asPCMBuffer else { return }
        do {
            if audioFile == nil {
                audioFile = try AVAudioFile(forWriting: outURL, settings: pcm.format.settings,
                                            commonFormat: .pcmFormatFloat32, interleaved: false)
            }
            try audioFile?.write(from: pcm)
            samplesWritten += 1
        } catch {
            FileHandle.standardError.write("write error: \(error)\n".data(using: .utf8)!)
        }
    }
}

// CMSampleBuffer → AVAudioPCMBuffer helper.
@available(macOS 13.0, *)
extension CMSampleBuffer {
    var asPCMBuffer: AVAudioPCMBuffer? {
        try? withAudioBufferList { audioBufferList, _ -> AVAudioPCMBuffer? in
            guard let absd = self.formatDescription?.audioStreamBasicDescription else { return nil }
            guard let format = AVAudioFormat(standardFormatWithSampleRate: absd.mSampleRate,
                                             channels: absd.mChannelsPerFrame) else { return nil }
            return AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: audioBufferList.unsafePointer)
        }
    }
}

// ── entry ──
guard #available(macOS 13.0, *) else {
    FileHandle.standardError.write("needs macOS 13+\n".data(using: .utf8)!)
    exit(1)
}
let args = CommandLine.arguments
let seconds = args.count > 1 ? (Double(args[1]) ?? 8) : 8
let outPath = args.count > 2 ? args[2] : "/tmp/sysaudio-spike.wav"

let rec = Recorder(outURL: URL(fileURLWithPath: outPath))
let sem = DispatchSemaphore(value: 0)
Task {
    do { try await rec.start(seconds: seconds) }
    catch { FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!) }
    sem.signal()
}
sem.wait()
