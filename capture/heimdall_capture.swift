// heimdall-capture: records what one window shows and what its app plays.
//
// Used by Heimdall's "Grabar clase" page (recorder.py). It never downloads
// anything: it uses Apple's ScreenCaptureKit, the same API as the system
// screen recorder, so content the system refuses to capture (DRM) simply
// arrives black or silent. Heimdall reports that; it does not work around it.
//
//   heimdall-capture list
//   heimdall-capture record --window <id> --out <dir> [--interval 2] [--duration 0] [--stdin-stop]
//
// `record` writes <dir>/audio.wav (mono 16-bit PCM, 16 kHz, what Whisper uses) and
// <dir>/frames/frame_<ms>.jpg (only when the picture changed), and prints one
// JSON object per line on stdout: started, frame, level, stopped, error.
// It stops on SIGINT/SIGTERM, after --duration s, or with --stdin-stop on
// a "stop" line / EOF in stdin.
//
// Build: sh tools/build_capture.sh   (needs Xcode Command Line Tools)

import AVFoundation
import CoreImage
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

// MARK: - output

let outLock = NSLock()
func emit(_ d: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: d),
          let s = String(data: data, encoding: .utf8) else { return }
    outLock.lock(); print(s); fflush(stdout); outLock.unlock()
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["event": "error", "code": code, "message": message])
    exit(1)
}

func hostNow() -> Double { CMClockGetTime(CMClockGetHostTimeClock()).seconds }

let stopSignal = DispatchSemaphore(value: 0)

// MARK: - WAV writer (mono 16-bit PCM)

final class WavWriter {
    let handle: FileHandle
    let sampleRate: Int
    private(set) var samples = 0

    init(url: URL, sampleRate: Int) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        self.sampleRate = sampleRate
        handle.write(Data(count: 44))  // header written in close()
    }

    func write(_ buf: [Float]) {
        guard !buf.isEmpty else { return }
        let pcm = buf.map { Int16(max(-1, min(1, $0)) * 32767).littleEndian }
        pcm.withUnsafeBufferPointer { handle.write(Data(buffer: $0)) }
        samples += buf.count
    }

    func padSilence(to index: Int) {
        let missing = index - samples
        if missing > 0 { write([Float](repeating: 0, count: missing)) }
    }

    func close() {
        let dataBytes = UInt32(samples * 2)
        var h = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; h.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; h.append(Data(bytes: &x, count: 2)) }
        h.append("RIFF".data(using: .ascii)!); u32(36 + dataBytes)
        h.append("WAVE".data(using: .ascii)!)
        h.append("fmt ".data(using: .ascii)!); u32(16)
        u16(1)  // PCM
        u16(1); u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        h.append("data".data(using: .ascii)!); u32(dataBytes)
        handle.seek(toFileOffset: 0); handle.write(h); try? handle.close()
    }
}

// MARK: - recorder

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let outDir: URL
    let framesDir: URL
    let interval: Double
    let t0: Double
    let wav: WavWriter
    let ci = CIContext()
    let videoQ = DispatchQueue(label: "video")
    let audioQ = DispatchQueue(label: "audio")

    var lastSavedAt = -1e9
    var lastThumb: [Float] = []
    var lastCheckAt = -1e9
    var levelSum: Float = 0
    var levelCount = 0
    var lastLevelAt = 0.0
    var nFrames = 0

    init(outDir: URL, interval: Double) throws {
        self.outDir = outDir
        framesDir = outDir.appendingPathComponent("frames")
        try FileManager.default.createDirectory(at: framesDir, withIntermediateDirectories: true)
        self.interval = interval
        wav = try WavWriter(url: outDir.appendingPathComponent("audio.wav"), sampleRate: 16000)
        t0 = hostNow()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Typically the window was closed. Keep what was recorded.
        emit(["event": "error", "code": "stream", "message": error.localizedDescription])
        stopSignal.signal()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard sb.isValid else { return }
        let t = sb.presentationTimeStamp.seconds - t0
        switch type {
        case .audio: handleAudio(sb, t: t)
        case .screen: handleFrame(sb, t: t)
        default: break
        }
    }

    // Audio: average channels to mono, keep it aligned with wall time.
    func handleAudio(_ sb: CMSampleBuffer, t: Double) {
        var mono: [Float] = []
        try? sb.withAudioBufferList { abl, _ in
            let chans = abl.count
            guard chans > 0, let first = abl.first, first.mData != nil else { return }
            let n = Int(first.mDataByteSize) / 4
            mono = [Float](repeating: 0, count: n)
            for b in abl {
                guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
                for i in 0..<min(n, Int(b.mDataByteSize) / 4) { mono[i] += p[i] }
            }
            let k = 1 / Float(chans)
            for i in 0..<n { mono[i] *= k }
        }
        guard !mono.isEmpty else { return }
        let idx = Int(max(0, t) * Double(wav.sampleRate))
        if idx > wav.samples + wav.sampleRate / 10 { wav.padSilence(to: idx) }
        wav.write(mono)

        for s in mono { levelSum += s * s }
        levelCount += mono.count
        if t - lastLevelAt >= 1 {
            let rms = levelCount > 0 ? (levelSum / Float(levelCount)).squareRoot() : 0
            emit(["event": "level", "t": round(t * 10) / 10, "rms": Double(rms)])
            levelSum = 0; levelCount = 0; lastLevelAt = t
        }
    }

    // Video: every `interval` s, measure the frame; save it only if it changed.
    func handleFrame(_ sb: CMSampleBuffer, t: Double) {
        guard t - lastCheckAt >= interval,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int,
              SCFrameStatus(rawValue: raw) == .complete,
              let pix = sb.imageBuffer else { return }
        lastCheckAt = t
        let image = CIImage(cvPixelBuffer: pix)
        guard let cg = ci.createCGImage(image, from: image.extent) else { return }

        let thumb = grayThumb(cg, w: 64, h: 36)
        let dark = Double(thumb.filter { $0 < 12 }.count) / Double(thumb.count)
        var diff = 255.0
        if lastThumb.count == thumb.count {
            var s: Float = 0
            for i in 0..<thumb.count { s += abs(thumb[i] - lastThumb[i]) }
            diff = Double(s / Float(thumb.count))
        }
        var file = ""
        if diff > 2.0 || t - lastSavedAt >= 30 {
            file = String(format: "frame_%09d.jpg", Int(t * 1000))
            saveJPEG(cg, to: framesDir.appendingPathComponent(file))
            lastSavedAt = t; nFrames += 1
        }
        lastThumb = thumb
        emit(["event": "frame", "t": round(t * 10) / 10, "diff": round(diff * 100) / 100,
              "dark": round(dark * 1000) / 1000, "saved": !file.isEmpty, "file": file])
    }

    func grayThumb(_ cg: CGImage, w: Int, h: Int) -> [Float] {
        var px = [UInt8](repeating: 0, count: w * h)
        let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: 0)!
        ctx.interpolationQuality = .low
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        return px.map { Float($0) }
    }

    func saveJPEG(_ cg: CGImage, to url: URL) {
        guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
        else { return }
        CGImageDestinationAddImage(dest, cg, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
        CGImageDestinationFinalize(dest)
    }
}

// MARK: - commands

func argValue(_ name: String, _ args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func shareable() async -> SCShareableContent {
    do {
        return try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
    } catch {
        fail("permission", "macOS no dio permiso de Grabación de pantalla: \(error.localizedDescription)")
    }
}

func listWindows() async {
    let content = await shareable()
    let me = ProcessInfo.processInfo.processIdentifier
    var out: [[String: Any]] = []
    for w in content.windows where w.windowLayer == 0 && w.frame.width >= 320 && w.frame.height >= 200 {
        guard let app = w.owningApplication, app.processID != me else { continue }
        out.append(["id": Int(w.windowID), "app": app.applicationName, "bundle": app.bundleIdentifier,
                    "title": w.title ?? "", "width": Int(w.frame.width), "height": Int(w.frame.height)])
    }
    emit(["event": "windows", "windows": out])
}

func record(_ args: [String]) async {
    guard let idStr = argValue("--window", args), let wid = UInt32(idStr),
          let outPath = argValue("--out", args) else {
        fail("usage", "record --window <id> --out <dir> [--interval s] [--duration s]")
    }
    let interval = Double(argValue("--interval", args) ?? "2") ?? 2
    let duration = Double(argValue("--duration", args) ?? "0") ?? 0

    let content = await shareable()
    guard let win = content.windows.first(where: { $0.windowID == wid }),
          let app = win.owningApplication else {
        fail("window", "La ventana ya no existe. Actualiza la lista.")
    }
    guard let display = content.displays.first(where: { $0.frame.intersects(win.frame) })
            ?? content.displays.first else { fail("display", "No hay pantalla disponible.") }

    let rec: Recorder
    do { rec = try Recorder(outDir: URL(fileURLWithPath: outPath), interval: interval) }
    catch { fail("io", "No se pudo crear \(outPath): \(error.localizedDescription)") }

    // Picture: the chosen window only, at up to 1920 px wide.
    let vcfg = SCStreamConfiguration()
    let scale = min(2.0, 1920.0 / max(1, win.frame.width))
    vcfg.width = max(2, Int(win.frame.width * scale))
    vcfg.height = max(2, Int(win.frame.height * scale))
    vcfg.minimumFrameInterval = CMTime(value: 1, timescale: 2)
    vcfg.showsCursor = false
    vcfg.queueDepth = 3
    let vstream = SCStream(filter: SCContentFilter(desktopIndependentWindow: win),
                           configuration: vcfg, delegate: rec)

    // Sound: everything the window's app plays (other apps are excluded).
    let acfg = SCStreamConfiguration()
    acfg.capturesAudio = true
    acfg.excludesCurrentProcessAudio = true
    acfg.sampleRate = 16000
    acfg.channelCount = 1
    acfg.width = 2; acfg.height = 2
    acfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    let astream = SCStream(filter: SCContentFilter(display: display, including: [app], exceptingWindows: []),
                           configuration: acfg, delegate: rec)
    do {
        try vstream.addStreamOutput(rec, type: .screen, sampleHandlerQueue: rec.videoQ)
        try astream.addStreamOutput(rec, type: .audio, sampleHandlerQueue: rec.audioQ)
        try await vstream.startCapture()
        try await astream.startCapture()
    } catch {
        fail("start", "No se pudo empezar a grabar: \(error.localizedDescription)")
    }
    emit(["event": "started", "app": app.applicationName, "title": win.title ?? "",
          "audio": rec.outDir.appendingPathComponent("audio.wav").path, "frames": rec.framesDir.path])

    // Wait for stop: signal, "stop" on stdin, or the duration.
    let done = stopSignal
    signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
    let sources = [SIGINT, SIGTERM].map { sig -> DispatchSourceSignal in
        let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        s.setEventHandler { done.signal() }
        s.resume(); return s
    }
    // With --stdin-stop the parent controls the lifetime through a pipe:
    // "stop" or EOF (the parent died) ends the recording. Without it, stdin
    // is ignored, so running from a shell with a closed stdin doesn't stop
    // the capture after the first buffer.
    if args.contains("--stdin-stop") {
        Thread.detachNewThread {
            while let line = readLine() { if line.trimmingCharacters(in: .whitespaces) == "stop" { break } }
            done.signal()
        }
    }
    if duration > 0 {
        DispatchQueue.global().asyncAfter(deadline: .now() + duration) { done.signal() }
    }
    await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
        DispatchQueue.global().async { done.wait(); c.resume() }
    }
    _ = sources

    try? await vstream.stopCapture()
    try? await astream.stopCapture()
    rec.audioQ.sync {}
    rec.videoQ.sync {}
    let seconds = Double(rec.wav.samples) / Double(rec.wav.sampleRate)
    rec.wav.close()
    emit(["event": "stopped", "audio_seconds": round(seconds * 10) / 10, "frames": rec.nFrames])
}

@main
struct HeimdallCapture {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        switch args.first {
        case "list": await listWindows()
        case "record": await record(args)
        default: fail("usage", "heimdall-capture list | record --window <id> --out <dir>")
        }
        exit(0)
    }
}
