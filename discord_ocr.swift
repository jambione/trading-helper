// discord_ocr.swift — one-shot capture of a window + Apple Vision OCR → stdout
//
// Captures a single on-screen window (default: the Discord app) via
// ScreenCaptureKit, runs Vision text recognition, and prints each recognized
// line to stdout (top-to-bottom). Exits 0 on success, 1 on error. Stateless:
// the Python poller (discord_source.py) invokes it once per poll and controls
// cadence — same spirit as sck_audio.swift.
//
// Usage:   discord_ocr [--owner <appName>] [--title <substring>]
//   --owner   owning application name to match (case-insensitive contains).
//             Default "Discord".
//   --title   optional window-title substring filter (case-insensitive).
//             If omitted, the largest on-screen window for --owner is used.
//
// Requires Screen Recording permission (already granted for sck_audio).
// Build:   swiftc discord_ocr.swift -o discord_ocr

import Foundation
import ScreenCaptureKit
import Vision
import CoreGraphics
import AppKit

// SCScreenshotManager needs a window-server connection that a bare CLI tool
// doesn't establish (audio-only SCStream does not). Initializing NSApplication
// as an accessory app makes that connection and avoids the CGS_REQUIRE_INIT
// assertion.
let _app = NSApplication.shared
_app.setActivationPolicy(.accessory)

// ── Args ────────────────────────────────────────────────────────────────────
var ownerName = "Discord"
var titleSub: String? = nil
do {
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--owner": if i + 1 < args.count { ownerName = args[i + 1]; i += 1 }
        case "--title": if i + 1 < args.count { titleSub = args[i + 1]; i += 1 }
        default: break
        }
        i += 1
    }
}

func fail(_ msg: String) -> Never {
    fputs("[discord_ocr] ERROR: \(msg)\n", stderr)
    exit(1)
}

// ── OCR a CGImage → lines (top-to-bottom) ────────────────────────────────────
func recognize(_ cg: CGImage) -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false   // ticker symbols, not prose
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([request]) } catch { return [] }
    guard let obs = request.results else { return [] }
    // Vision's y-origin is bottom-left → higher midY = higher on screen.
    let sorted = obs.sorted { $0.boundingBox.midY > $1.boundingBox.midY }
    return sorted.compactMap { $0.topCandidates(1).first?.string }
}

// ── Find the target window, capture it, OCR, print ───────────────────────────
@MainActor
func run() async {
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)
    } catch {
        fail("could not query shareable content (Screen Recording permission?): \(error)")
    }

    let needle = ownerName.lowercased()
    var candidates = content.windows.filter { w in
        guard let app = w.owningApplication else { return false }
        let name = app.applicationName.lowercased()
        guard name.contains(needle) else { return false }
        // Skip tiny/zero windows (tooltips, the voice PiP, etc.)
        return w.frame.width >= 200 && w.frame.height >= 200
    }
    if let sub = titleSub?.lowercased(), !sub.isEmpty {
        let filtered = candidates.filter { ($0.title ?? "").lowercased().contains(sub) }
        if !filtered.isEmpty { candidates = filtered }
    }
    guard let win = candidates.max(by: {
        ($0.frame.width * $0.frame.height) < ($1.frame.width * $1.frame.height)
    }) else {
        fail("no on-screen window found for owner '\(ownerName)'")
    }

    let cfg = SCStreamConfiguration()
    let scale = 2  // capture at 2x for crisp small text
    cfg.width  = Int(win.frame.width)  * scale
    cfg.height = Int(win.frame.height) * scale
    cfg.showsCursor = false

    let filter = SCContentFilter(desktopIndependentWindow: win)
    let cg: CGImage
    do {
        cg = try await SCScreenshotManager.captureImage(
            contentFilter: filter, configuration: cfg)
    } catch {
        fail("window capture failed: \(error)")
    }

    let lines = recognize(cg)
    FileHandle.standardOutput.write(Data((lines.joined(separator: "\n") + "\n").utf8))
    exit(0)
}

Task { await run() }
dispatchMain()
