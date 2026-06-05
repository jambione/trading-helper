// sck_audio.swift — capture system audio via ScreenCaptureKit, write raw float32 PCM to stdout
// Usage: swift sck_audio.swift [sampleRate] [channels]
// stderr: status messages  stdout: raw interleaved float32 PCM

import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreAudio

let sampleRate = Int(CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "48000") ?? 48000
let channels   = Int(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "2") ?? 2

class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?

    func stream(_ s: SCStream, didOutputSampleBuffer buf: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard let blockBuf = CMSampleBufferGetDataBuffer(buf) else { return }
        var totalLen = 0
        var dataPtr: UnsafeMutablePointer<CChar>? = nil
        CMBlockBufferGetDataPointer(blockBuf, atOffset: 0,
                                    lengthAtOffsetOut: nil,
                                    totalLengthOut: &totalLen,
                                    dataPointerOut: &dataPtr)
        guard let ptr = dataPtr, totalLen > 0 else { return }
        let data = Data(bytes: ptr, count: totalLen)
        FileHandle.standardOutput.write(data)
    }

    func stream(_ s: SCStream, didStopWithError error: Error) {
        fputs("[sck_audio] stream stopped: \(error)\n", stderr)
        exit(1)
    }

    func start() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                fputs("[sck_audio] ERROR: no display found\n", stderr); exit(1)
            }
            let cfg = SCStreamConfiguration()
            cfg.capturesAudio              = true
            cfg.sampleRate                 = sampleRate
            cfg.channelCount               = channels
            cfg.excludesCurrentProcessAudio = true
            // Minimal video (required by SCStream even for audio-only capture)
            cfg.width  = 2
            cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let filter = SCContentFilter(display: display, excludingWindows: [])
            stream = SCStream(filter: filter, configuration: cfg, delegate: self)
            try stream!.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue.global())
            try await stream!.startCapture()
            fputs("[sck_audio] capturing system audio \(sampleRate)Hz \(channels)ch float32 → stdout\n", stderr)
        } catch {
            fputs("[sck_audio] ERROR: \(error)\n", stderr)
            exit(1)
        }
    }
}

let capturer = Capturer()
let sem = DispatchSemaphore(value: 0)

signal(SIGTERM) { _ in exit(0) }
signal(SIGINT)  { _ in exit(0) }

Task {
    await capturer.start()
    // keep alive
    while true { try? await Task.sleep(nanoseconds: 1_000_000_000) }
}

dispatchMain()
