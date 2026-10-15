// discord_ocr.swift — one-shot capture of a window + Apple Vision OCR → stdout
//
// Finds the target app window (default: Discord), captures the display that
// contains it, crops to the window bounds, runs Vision OCR, prints lines
// top→bottom. Exit 0 success / 1 error.
//
// Capture uses CGDisplayCreateImage via dlsym: the symbol still works at runtime
// on macOS 15+/26 (Python/Quartz path is ~10–30ms), but the SDK marks it
// unavailable for compile-time linking — dynamic lookup avoids that.
//
// Usage:   discord_ocr [--owner <appName>] [--title <substring>] [--accurate]
// Build:   bash scripts/build_ocr.sh

import Foundation
import Vision
import CoreGraphics
import AppKit
import Darwin
import ScreenCaptureKit

let _app = NSApplication.shared
_app.setActivationPolicy(.accessory)

// ── Dynamically resolve CGDisplayCreateImage (SDK-unavailable, runtime-OK) ──
private typealias CGDisplayCreateImageFn = @convention(c) (CGDirectDisplayID) -> CGImage?

private let _cgDisplayCreateImage: CGDisplayCreateImageFn? = {
    guard let handle = dlopen(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
        RTLD_LAZY
    ) else { return nil }
    guard let sym = dlsym(handle, "CGDisplayCreateImage") else { return nil }
    return unsafeBitCast(sym, to: CGDisplayCreateImageFn.self)
}()

// ── Args ────────────────────────────────────────────────────────────────────
var ownerName = "Discord"
var titleSub: String? = nil
var accurate = false
do {
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--owner":
            if i + 1 < args.count { ownerName = args[i + 1]; i += 1 }
        case "--title":
            if i + 1 < args.count { titleSub = args[i + 1]; i += 1 }
        case "--accurate":
            accurate = true
        default: break
        }
        i += 1
    }
}

func fail(_ msg: String) -> Never {
    fputs("[discord_ocr] ERROR: \(msg)\n", stderr)
    exit(1)
}

// ── OCR ─────────────────────────────────────────────────────────────────────
func recognize(_ cg: CGImage, accurate: Bool) -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = accurate ? .accurate : .fast
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([request]) } catch { return [] }
    guard let obs = request.results else { return [] }
    let sorted = obs.sorted { $0.boundingBox.midY > $1.boundingBox.midY }
    return sorted.compactMap { $0.topCandidates(1).first?.string }
}

// ── Window metadata ─────────────────────────────────────────────────────────
struct WinMeta {
    let id: CGWindowID
    let bounds: CGRect
    let owner: String
    let title: String
    var area: CGFloat { bounds.width * bounds.height }
}

func findWindow(ownerNeedle: String, titleNeedle: String?) -> WinMeta? {
    let opts = CGWindowListOption(arrayLiteral: .optionOnScreenOnly, .excludeDesktopElements)
    guard let info = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    let needle = ownerNeedle.lowercased()
    let titleN = titleNeedle?.lowercased()
    var best: WinMeta? = nil
    for w in info {
        let owner = (w[kCGWindowOwnerName as String] as? String) ?? ""
        guard owner.lowercased().contains(needle) else { continue }
        let title = (w[kCGWindowName as String] as? String) ?? ""
        if let titleN, !titleN.isEmpty, !title.lowercased().contains(titleN) {
            continue
        }
        guard let wid = w[kCGWindowNumber as String] as? UInt32 else { continue }
        guard let b = w[kCGWindowBounds as String] as? [String: Any] else { continue }
        let x = CGFloat((b["X"] as? NSNumber)?.doubleValue ?? 0)
        let y = CGFloat((b["Y"] as? NSNumber)?.doubleValue ?? 0)
        let width = CGFloat((b["Width"] as? NSNumber)?.doubleValue ?? 0)
        let height = CGFloat((b["Height"] as? NSNumber)?.doubleValue ?? 0)
        guard width >= 200, height >= 200 else { continue }
        let hit = WinMeta(
            id: CGWindowID(wid),
            bounds: CGRect(x: x, y: y, width: width, height: height),
            owner: owner,
            title: title
        )
        if best == nil || hit.area > best!.area { best = hit }
    }
    return best
}

func displayID(containing point: CGPoint) -> CGDirectDisplayID {
    var count: UInt32 = 0
    CGGetActiveDisplayList(0, nil, &count)
    var displays = [CGDirectDisplayID](repeating: 0, count: Int(count))
    CGGetActiveDisplayList(count, &displays, &count)
    for d in displays where CGDisplayBounds(d).contains(point) {
        return d
    }
    return CGMainDisplayID()
}

func captureWindow(_ win: WinMeta) -> CGImage? {
    if !CGPreflightScreenCaptureAccess() {
        _ = CGRequestScreenCaptureAccess()
    }
    guard let createImage = _cgDisplayCreateImage else { return nil }

    let center = CGPoint(x: win.bounds.midX, y: win.bounds.midY)
    let did = displayID(containing: center)
    guard let full = createImage(did) else { return nil }

    let db = CGDisplayBounds(did)
    let scaleX = CGFloat(full.width) / db.width
    let scaleY = CGFloat(full.height) / db.height

    let inter = win.bounds.intersection(db)
    guard !inter.isNull, inter.width >= 50, inter.height >= 50 else { return nil }

    let local = CGRect(
        x: (inter.origin.x - db.origin.x) * scaleX,
        y: (inter.origin.y - db.origin.y) * scaleY,
        width: inter.width * scaleX,
        height: inter.height * scaleY
    ).integral

    return full.cropping(to: local)
}

// ── ScreenCaptureKit (2026-10-15) ─────────────────────────────────────────────
// CGDisplayCreateImage is a legacy API that recent macOS restricts: it worked for the first minute after a
// restart and then failed on every poll (display capture/crop failed). ScreenCaptureKit captures the window
// itself (no display crop; works when another window covers it) and is the supported path.
var sckError = ""

func captureSCK(ownerNeedle: String, titleNeedle: String?) -> CGImage? {
    guard #available(macOS 14.0, *) else { sckError = "macOS < 14"; return nil }
    let sem = DispatchSemaphore(value: 0)
    var out: CGImage? = nil
    let needle = ownerNeedle.lowercased()
    let titleN = titleNeedle?.lowercased()
    Task.detached {
        defer { sem.signal() }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
            let cands = content.windows.filter { w in
                let owner = (w.owningApplication?.applicationName ?? "").lowercased()
                guard owner.contains(needle), w.frame.width >= 200, w.frame.height >= 200 else { return false }
                if let t = titleN, !t.isEmpty { return (w.title ?? "").lowercased().contains(t) }
                return true
            }
            guard let w = cands.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else {
                sckError = "no window for owner '\(ownerNeedle)'"
                return
            }
            let filter = SCContentFilter(desktopIndependentWindow: w)
            let cfg = SCStreamConfiguration()
            let scale = CGFloat(filter.pointPixelScale)
            cfg.width = max(1, Int(w.frame.width * scale))
            cfg.height = max(1, Int(w.frame.height * scale))
            cfg.showsCursor = false
            out = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
        } catch {
            sckError = "\(error)"
        }
    }
    if sem.wait(timeout: .now() + 10) == .timedOut { sckError = "timed out" }
    return out
}

// ── Main ────────────────────────────────────────────────────────────────────
if !CGPreflightScreenCaptureAccess() {
    _ = CGRequestScreenCaptureAccess()
    if !CGPreflightScreenCaptureAccess() {
        fail(
            "Screen Recording not granted. System Settings → Privacy & Security → " +
            "Screen & System Audio Recording → enable Ghostty/Terminal (launcher) " +
            "and/or Discord OCR, then re-run."
        )
    }
}

var captured: CGImage? = captureSCK(ownerNeedle: ownerName, titleNeedle: titleSub)
if captured == nil {
    // Fallback: the legacy display capture + crop.
    guard let win = findWindow(ownerNeedle: ownerName, titleNeedle: titleSub) else {
        fail("no on-screen window found for owner '\(ownerName)' — is Discord open and visible? (ScreenCaptureKit: \(sckError))")
    }
    captured = captureWindow(win)
    if captured == nil {
        fail(
            "display capture/crop failed for window id=\(win.id) " +
            "(\(Int(win.bounds.width))x\(Int(win.bounds.height))). " +
            "Grant Screen Recording; keep Discord on-screen. (ScreenCaptureKit: \(sckError))"
        )
    }
}
let cg = captured!

let lines = recognize(cg, accurate: accurate)
FileHandle.standardOutput.write(Data((lines.joined(separator: "\n") + "\n").utf8))
exit(0)
