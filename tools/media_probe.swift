// Local, free media probe for ReachOut media QA (macOS only).
//   media-probe ocr <image-path>                      -> JSON text detections (Apple Vision)
//   media-probe frames <video-path> <out-dir> <t,...> -> JSON extracted JPEG frames (AVFoundation)
import AVFoundation
import AppKit
import Foundation
import Vision

func emit(_ value: Any) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(message.data(using: .utf8)!)
    exit(2)
}

func ocr(_ path: String) {
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fail("unreadable image") }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do { try handler.perform([request]) } catch { fail("vision failed: \(error)") }
    var detections: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        detections.append([
            "text": candidate.string, "confidence": Double(candidate.confidence),
            "box": [Double(box.origin.x), Double(1 - box.origin.y - box.height), Double(box.width), Double(box.height)],
        ])
    }
    emit(["engine": "apple-vision", "revision": request.revision, "width": cgImage.width, "height": cgImage.height,
          "detections": detections])
}

// Glyph-presence check: fraction of near-white pixels inside the bottom-middle subtitle band.
// Works for any script (e.g. Telugu) that Apple Vision OCR cannot read.
func glyph(_ path: String, _ zoneJSON: String) {
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fail("unreadable image") }
    let width = cgImage.width, height = cgImage.height
    guard width > 0, height > 0 else { fail("empty image") }
    let zone = (try? JSONSerialization.jsonObject(with: zoneJSON.data(using: .utf8) ?? Data())) as? [String: Double] ?? [:]
    let sides = zone["sides"] ?? 0.08, bottom = zone["bottom"] ?? 0.20
    let bandTop = Int(Double(height) * (1 - bottom - 0.14))
    let bandBottom = Int(Double(height) * (1 - bottom))
    let x0 = Int(Double(width) * sides), x1 = Int(Double(width) * (1 - sides))
    guard let data = cgImage.dataProvider?.data, let ptr = CFDataGetBytePtr(data) else { fail("no pixel data") }
    let bpr = cgImage.bytesPerRow, bpp = cgImage.bitsPerPixel / 8
    var bright = 0, total = 0
    for y in max(0, bandTop)..<min(height, bandBottom) {
        for x in max(0, x0)..<min(width, x1) {
            let offset = y * bpr + x * bpp
            let r = Int(ptr[offset]), g = Int(ptr[offset + 1]), b = Int(ptr[offset + 2])
            total += 1
            if r > 200 && g > 200 && b > 200 { bright += 1 }
        }
    }
    emit(["engine": "glyph-pixel", "width": width, "height": height,
          "bright": bright, "total": total, "bright_ratio": Double(bright) / Double(max(1, total))])
}

func frames(_ path: String, _ outDir: String, _ times: [Double]) {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.requestedTimeToleranceBefore = CMTime(seconds: 0.05, preferredTimescale: 600)
    generator.requestedTimeToleranceAfter = CMTime(seconds: 0.05, preferredTimescale: 600)
    var output: [[String: Any]] = []
    for (index, seconds) in times.enumerated() {
        var actual = CMTime.zero
        guard let cgImage = try? generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: &actual) else {
            fail("frame extraction failed at \(seconds)s")
        }
        let rep = NSBitmapImageRep(cgImage: cgImage)
        guard let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.9]) else { fail("jpeg encode failed") }
        let file = (outDir as NSString).appendingPathComponent(String(format: "frame-%02d.jpg", index))
        do { try jpeg.write(to: URL(fileURLWithPath: file)) } catch { fail("write failed") }
        output.append(["requested_seconds": seconds, "actual_seconds": actual.seconds, "path": file,
                       "width": cgImage.width, "height": cgImage.height])
    }
    emit(["engine": "avfoundation", "frames": output])
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: media-probe ocr <image> | frames <video> <out-dir> <t,...>") }
switch args[1] {
case "ocr": ocr(args[2])
case "glyph":
    guard args.count >= 4 else { fail("glyph needs <image> <zone-json>") }
    glyph(args[2], args[3])
case "frames":
    guard args.count >= 5 else { fail("frames needs <video> <out-dir> <t,...>") }
    frames(args[2], args[3], args[4].split(separator: ",").compactMap { Double($0) })
default: fail("unknown command")
}
