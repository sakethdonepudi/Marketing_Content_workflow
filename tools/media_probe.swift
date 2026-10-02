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
case "frames":
    guard args.count >= 5 else { fail("frames needs <video> <out-dir> <t,...>") }
    frames(args[2], args[3], args[4].split(separator: ",").compactMap { Double($0) })
default: fail("unknown command")
}
