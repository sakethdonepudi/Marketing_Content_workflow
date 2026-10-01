// Native Final Reel composer for ReachOut (macOS only).
// Loops approved 9:16 footage without stretching, mixes authored voice blocks with
// a quiet music bed, burns safe-zone captions, and writes a zero-based MP4 timeline.
import AVFoundation
import AppKit
import CoreText
import Foundation
import QuartzCore

struct VoiceClip: Codable {
    let path: String
    let text: String
}

struct ComposerConfig: Codable {
    let sourceVideo: String
    let outputVideo: String
    let musicAudio: String
    let voiceClips: [VoiceClip]
    let width: Int
    let height: Int
    let fps: Int
    let leadSeconds: Double
    let gapSeconds: Double
    let tailSeconds: Double
    let musicVolume: Float
}

struct CueReceipt: Codable {
    let text: String
    let start: Double
    let end: Double
}

struct ComposerReceipt: Codable {
    let durationSeconds: Double
    let width: Int
    let height: Int
    let fps: Int
    let sourceLoops: Int
    let narrationStart: Double
    let narrationEnd: Double
    let musicVolume: Float
    let cues: [CueReceipt]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(2)
}

func seconds(_ time: CMTime) -> Double {
    let value = CMTimeGetSeconds(time)
    return value.isFinite ? value : 0
}

func audioDuration(_ path: String) -> Double {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    let value = seconds(asset.duration)
    if value <= 0 { fail("voice clip has no duration: \(path)") }
    return value
}

func captionBitmap(text: String, size: CGSize) -> CGImage {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { fail("caption bitmap allocation failed") }
    bitmap.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSColor.clear.setFill()
    NSRect(origin: .zero, size: size).fill()
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.lineBreakMode = .byWordWrapping
    let fontSize = size.width * 0.073
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize, weight: .bold),
        .foregroundColor: NSColor.white,
        .strokeColor: NSColor.black,
        .strokeWidth: -2.0,
        .paragraphStyle: paragraph,
    ]
    let value = text.uppercased() as NSString
    let bounds = value.boundingRect(with: NSSize(width: size.width - 28, height: size.height - 12),
                                    options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes)
    let rect = NSRect(x: 14, y: max(6, (size.height - bounds.height) / 2),
                      width: size.width - 28, height: min(size.height - 12, bounds.height + 4))
    value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes)
    NSGraphicsContext.restoreGraphicsState()
    guard let image = bitmap.cgImage else { fail("caption bitmap encode failed") }
    return image
}

func captionLayer(text: String, start: Double, end: Double, total: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let side = size.width * 0.11
    let bottom = size.height * 0.19
    layer.frame = CGRect(x: side, y: bottom, width: size.width - side * 2, height: size.height * 0.13)
    layer.backgroundColor = NSColor.black.withAlphaComponent(0.58).cgColor
    layer.cornerRadius = 18
    layer.masksToBounds = true
    layer.opacity = 0
    let textLayer = CALayer()
    textLayer.frame = layer.bounds
    textLayer.contents = captionBitmap(text: text, size: layer.bounds.size)
    textLayer.contentsGravity = .resizeAspect
    textLayer.contentsScale = 2
    layer.addSublayer(textLayer)
    let visible = max(0.05, end - start)
    let animation = CAKeyframeAnimation(keyPath: "opacity")
    animation.values = [0, 1, 1, 0]
    animation.keyTimes = [0, 0.04, 0.92, 1]
    animation.beginTime = AVCoreAnimationBeginTimeAtZero + start
    animation.duration = visible
    animation.fillMode = .both
    animation.isRemovedOnCompletion = false
    layer.add(animation, forKey: "caption-opacity")
    return layer
}

func compose(_ config: ComposerConfig) throws -> ComposerReceipt {
    guard !config.voiceClips.isEmpty else { fail("at least one voice clip is required") }
    let voiceDurations = config.voiceClips.map { audioDuration($0.path) }
    let narrationStart = config.leadSeconds
    var cursor = narrationStart
    var cues: [CueReceipt] = []
    for (index, clip) in config.voiceClips.enumerated() {
        let end = cursor + voiceDurations[index]
        cues.append(CueReceipt(text: clip.text, start: cursor, end: end))
        cursor = end + (index == config.voiceClips.count - 1 ? 0 : config.gapSeconds)
    }
    let narrationEnd = cursor
    let targetDuration = max(12.0, narrationEnd + config.tailSeconds)
    if targetDuration > 25.0 { fail("authored narration exceeds the 25 second Reel target") }
    let target = CMTime(seconds: targetDuration, preferredTimescale: 600)

    let source = AVURLAsset(url: URL(fileURLWithPath: config.sourceVideo))
    guard let sourceVideoTrack = source.tracks(withMediaType: .video).first else { fail("source has no video track") }
    let composition = AVMutableComposition()
    guard let videoTrack = composition.addMutableTrack(withMediaType: .video,
                                                       preferredTrackID: kCMPersistentTrackID_Invalid) else {
        fail("could not create video track")
    }
    var videoCursor = CMTime.zero
    var loops = 0
    while CMTimeCompare(videoCursor, target) < 0 {
        let remaining = CMTimeSubtract(target, videoCursor)
        let take = CMTimeCompare(source.duration, remaining) < 0 ? source.duration : remaining
        try videoTrack.insertTimeRange(CMTimeRange(start: .zero, duration: take), of: sourceVideoTrack, at: videoCursor)
        videoCursor = CMTimeAdd(videoCursor, take)
        loops += 1
    }

    guard let narrationTrack = composition.addMutableTrack(withMediaType: .audio,
                                                           preferredTrackID: kCMPersistentTrackID_Invalid),
          let musicTrack = composition.addMutableTrack(withMediaType: .audio,
                                                       preferredTrackID: kCMPersistentTrackID_Invalid) else {
        fail("could not create audio tracks")
    }
    for (index, clip) in config.voiceClips.enumerated() {
        let asset = AVURLAsset(url: URL(fileURLWithPath: clip.path))
        guard let track = asset.tracks(withMediaType: .audio).first else { fail("voice clip has no audio track") }
        try narrationTrack.insertTimeRange(CMTimeRange(start: .zero, duration: asset.duration), of: track,
                                           at: CMTime(seconds: cues[index].start, preferredTimescale: 600))
    }
    let music = AVURLAsset(url: URL(fileURLWithPath: config.musicAudio))
    guard let musicSource = music.tracks(withMediaType: .audio).first else { fail("music bed has no audio track") }
    var musicCursor = CMTime.zero
    while CMTimeCompare(musicCursor, target) < 0 {
        let remaining = CMTimeSubtract(target, musicCursor)
        let take = CMTimeCompare(music.duration, remaining) < 0 ? music.duration : remaining
        try musicTrack.insertTimeRange(CMTimeRange(start: .zero, duration: take), of: musicSource, at: musicCursor)
        musicCursor = CMTimeAdd(musicCursor, take)
    }

    let audioMix = AVMutableAudioMix()
    let voiceMix = AVMutableAudioMixInputParameters(track: narrationTrack)
    voiceMix.setVolume(1.0, at: .zero)
    let bedMix = AVMutableAudioMixInputParameters(track: musicTrack)
    bedMix.setVolumeRamp(fromStartVolume: 0, toEndVolume: config.musicVolume,
                         timeRange: CMTimeRange(start: .zero, duration: CMTime(seconds: 0.8, preferredTimescale: 600)))
    bedMix.setVolume(config.musicVolume, at: CMTime(seconds: 0.8, preferredTimescale: 600))
    bedMix.setVolumeRamp(fromStartVolume: config.musicVolume, toEndVolume: 0,
                         timeRange: CMTimeRange(start: CMTime(seconds: max(0, targetDuration - 1), preferredTimescale: 600),
                                                duration: CMTime(seconds: 1, preferredTimescale: 600)))
    audioMix.inputParameters = [voiceMix, bedMix]

    let renderSize = CGSize(width: config.width, height: config.height)
    let videoComposition = AVMutableVideoComposition()
    videoComposition.renderSize = renderSize
    videoComposition.frameDuration = CMTime(value: 1, timescale: CMTimeScale(config.fps))
    let instruction = AVMutableVideoCompositionInstruction()
    instruction.timeRange = CMTimeRange(start: .zero, duration: target)
    let layerInstruction = AVMutableVideoCompositionLayerInstruction(assetTrack: videoTrack)
    layerInstruction.setTransform(sourceVideoTrack.preferredTransform, at: .zero)
    instruction.layerInstructions = [layerInstruction]
    videoComposition.instructions = [instruction]

    let parentLayer = CALayer()
    parentLayer.frame = CGRect(origin: .zero, size: renderSize)
    parentLayer.isGeometryFlipped = false
    let videoLayer = CALayer()
    videoLayer.frame = parentLayer.frame
    parentLayer.addSublayer(videoLayer)
    for cue in cues {
        parentLayer.addSublayer(captionLayer(text: cue.text, start: cue.start, end: cue.end,
                                            total: targetDuration, size: renderSize))
    }
    videoComposition.animationTool = AVVideoCompositionCoreAnimationTool(postProcessingAsVideoLayer: videoLayer,
                                                                          in: parentLayer)

    let outputURL = URL(fileURLWithPath: config.outputVideo)
    try? FileManager.default.removeItem(at: outputURL)
    let reader = try AVAssetReader(asset: composition)
    let videoOutput = AVAssetReaderVideoCompositionOutput(videoTracks: [videoTrack], videoSettings: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    ])
    videoOutput.videoComposition = videoComposition
    videoOutput.alwaysCopiesSampleData = false
    let audioOutput = AVAssetReaderAudioMixOutput(audioTracks: [narrationTrack, musicTrack], audioSettings: [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ])
    audioOutput.audioMix = audioMix
    guard reader.canAdd(videoOutput), reader.canAdd(audioOutput) else { fail("reader outputs are unsupported") }
    reader.add(videoOutput)
    reader.add(audioOutput)

    let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
    writer.shouldOptimizeForNetworkUse = true
    let videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: config.width,
        AVVideoHeightKey: config.height,
        AVVideoCompressionPropertiesKey: [
            AVVideoAverageBitRateKey: 5_000_000,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            AVVideoExpectedSourceFrameRateKey: config.fps,
        ],
    ])
    videoInput.expectsMediaDataInRealTime = false
    let audioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: [
        AVFormatIDKey: kAudioFormatMPEG4AAC,
        AVSampleRateKey: 48_000,
        AVNumberOfChannelsKey: 1,
        AVEncoderBitRateKey: 160_000,
    ])
    audioInput.expectsMediaDataInRealTime = false
    guard writer.canAdd(videoInput), writer.canAdd(audioInput) else { fail("writer inputs are unsupported") }
    writer.add(videoInput)
    writer.add(audioInput)
    guard writer.startWriting() else { fail("writer could not start: \(writer.error?.localizedDescription ?? "unknown")") }
    guard reader.startReading() else { fail("reader could not start: \(reader.error?.localizedDescription ?? "unknown")") }
    writer.startSession(atSourceTime: .zero)

    let group = DispatchGroup()
    group.enter()
    videoInput.requestMediaDataWhenReady(on: DispatchQueue(label: "final-reel-video")) {
        while videoInput.isReadyForMoreMediaData {
            if let sample = videoOutput.copyNextSampleBuffer() {
                if !videoInput.append(sample) {
                    reader.cancelReading(); videoInput.markAsFinished(); group.leave(); break
                }
            } else {
                videoInput.markAsFinished(); group.leave(); break
            }
        }
    }
    group.enter()
    audioInput.requestMediaDataWhenReady(on: DispatchQueue(label: "final-reel-audio")) {
        while audioInput.isReadyForMoreMediaData {
            if let sample = audioOutput.copyNextSampleBuffer() {
                if !audioInput.append(sample) {
                    reader.cancelReading(); audioInput.markAsFinished(); group.leave(); break
                }
            } else {
                audioInput.markAsFinished(); group.leave(); break
            }
        }
    }
    group.wait()
    let finished = DispatchSemaphore(value: 0)
    writer.finishWriting { finished.signal() }
    finished.wait()
    guard writer.status == .completed else { fail("writer failed: \(writer.error?.localizedDescription ?? "unknown")") }
    guard reader.status == .completed else { fail("reader failed: \(reader.error?.localizedDescription ?? "unknown")") }
    return ComposerReceipt(durationSeconds: targetDuration, width: config.width, height: config.height, fps: config.fps,
                           sourceLoops: loops, narrationStart: narrationStart, narrationEnd: narrationEnd,
                           musicVolume: config.musicVolume, cues: cues)
}

guard CommandLine.arguments.count == 2 else { fail("usage: final-reel-composer <config.json>") }
do {
    let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
    let config = try JSONDecoder().decode(ComposerConfig.self, from: data)
    let receipt = try compose(config)
    let encoded = try JSONEncoder().encode(receipt)
    FileHandle.standardOutput.write(encoded)
} catch {
    fail("composition failed: \(error)")
}
