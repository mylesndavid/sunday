// Apple Vision OCR — same engine as Live Text. Free, local, fast (≈50ms
// on M-series). Reads an image path as $1, prints recognized text to
// stdout, exits 0. On any failure prints to stderr and exits non-zero.
//
// Build:    swiftc -O -o ocr-macos ocr-macos.swift
// Run:      ./ocr-macos /path/to/screen.png
//
// Sunday's satellite calls this from rewind_macos.py instead of paying
// OpenAI for vision OCR. Compiled binary lives at ~/.sunday/bin/ocr-macos.

import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: ocr-macos <image-path>\n".data(using: .utf8)!)
    exit(64)
}

let path = CommandLine.arguments[1]
guard let nsimg = NSImage(contentsOfFile: path),
      let cg = nsimg.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("could not load image at \(path)\n".data(using: .utf8)!)
    exit(65)
}

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        FileHandle.standardError.write("vision error: \(error)\n".data(using: .utf8)!)
        exit(70)
    }
    guard let observations = request.results as? [VNRecognizedTextObservation] else { exit(0) }
    // Top-down, left-right by bounding box origin. Vision returns coords
    // in normalized image space with origin at bottom-left, so flip Y.
    let sorted = observations.sorted { a, b in
        let ay = 1 - a.boundingBox.origin.y - a.boundingBox.size.height
        let by = 1 - b.boundingBox.origin.y - b.boundingBox.size.height
        if abs(ay - by) > 0.01 { return ay < by }
        return a.boundingBox.origin.x < b.boundingBox.origin.x
    }
    let lines = sorted.compactMap { $0.topCandidates(1).first?.string }
    print(lines.joined(separator: "\n"))
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["en-US"]

do {
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
} catch {
    FileHandle.standardError.write("perform error: \(error)\n".data(using: .utf8)!)
    exit(71)
}
