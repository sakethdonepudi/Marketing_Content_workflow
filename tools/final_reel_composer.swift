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
    /// Music level while narration is speaking; kept well below speech so the mix ducks.
    let duckedMusicVolume: Float?
    /// Narration gain applied during the mix to reach a phone-audible level.
    let narrationGain: Float?
    /// Optional rights-cleared contextual images (already validated in the app layer).
    let cbnImage: String?
    let tdpImage: String?
    let hookHeadline: String?
    let hookSubline: String?
    let closingHeadline: String?
    let cbnLabelLine1: String?
    let cbnLabelLine2: String?
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

/// A caption bitmap that renders solid white glyphs with a high-contrast dark halo.
///
/// The previous version used `.strokeWidth: -2.0`, which in AppKit means "stroke AND
/// fill"; because the stroke is centred on the glyph edge it erased the glyph interior
/// and produced hollow outline-only text. That made burned-in subtitles unreadable and
/// unreadable by OCR. Here the glyphs are drawn filled in white, then the same text is
/// stroked underneath in black via a separate pass, giving a solid fill plus a real
/// outline without erasing it.
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
    let fontSize = size.width * 0.088
    let baseAttributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize, weight: .bold),
        .paragraphStyle: paragraph,
    ]
    let value = text.uppercased() as NSString
    let drawBounds = value.boundingRect(with: NSSize(width: size.width - 40, height: size.height - 16),
                                        options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: baseAttributes)
    let rect = NSRect(x: 20, y: max(8, (size.height - drawBounds.height) / 2),
                      width: size.width - 40, height: min(size.height - 16, drawBounds.height + 8))
    // Outline pass first: stroke-only text in black (positive strokeWidth = stroke only).
    let outlineAttributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize, weight: .bold),
        .paragraphStyle: paragraph,
        .foregroundColor: NSColor.clear,
        .strokeColor: NSColor.black,
        .strokeWidth: 6.0,
    ]
    value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: outlineAttributes)
    // Fill pass second: opaque white glyph interiors on top of the outline.
    let fillAttributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize, weight: .bold),
        .paragraphStyle: paragraph,
        .foregroundColor: NSColor.white,
    ]
    value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: fillAttributes)
    NSGraphicsContext.restoreGraphicsState()
    guard let image = bitmap.cgImage else { fail("caption bitmap encode failed") }
    return image
}

func captionLayer(text: String, start: Double, end: Double, total: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let side = size.width * 0.08
    let censureWidth = size.width - side * 2
    // Size the strip to the actual wrapped text height (1-2 lines) so there is never a
    // tall empty subtitle box. The bottom edge clears the Reels bottom safe zone.
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.lineBreakMode = .byWordWrapping
    let measureAttributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: censureWidth * 0.088, weight: .bold), .paragraphStyle: paragraph,
    ]
    let measured = (text.uppercased() as NSString).boundingRect(
        with: NSSize(width: censureWidth - 40, height: censureWidth), options: [.usesLineFragmentOrigin, .usesFontLeading],
        attributes: measureAttributes)
    let stripHeight = min(size.height * 0.22, max(measured.height + 44, size.height * 0.11))
    let bottom = size.height * 0.20
    layer.frame = CGRect(x: side, y: bottom, width: censureWidth, height: stripHeight)
    layer.backgroundColor = NSColor.black.withAlphaComponent(0.62).cgColor
    layer.cornerRadius = 20
    layer.masksToBounds = true
    layer.opacity = 0
    let textLayer = CALayer()
    textLayer.frame = layer.bounds
    textLayer.contents = captionBitmap(text: text, size: layer.bounds.size)
    // Render the bitmap 1:1 (never resizeAspect downscale) so glyphs stay crisp and large.
    textLayer.contentsGravity = .resize
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

/// Draw a card of text lines into a bitmap with an optional gold accent bar.
func cardBitmap(lines: [(String, CGFloat, Bool)], size: CGSize, accent: Bool) -> CGImage {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { fail("card bitmap allocation failed") }
    bitmap.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSColor.clear.setFill()
    NSRect(origin: .zero, size: size).fill()
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.lineBreakMode = .byWordWrapping
    let measuredHeights: [CGFloat] = lines.map { text, fontSize, _ in
        let font = NSFont.systemFont(ofSize: fontSize, weight: .heavy)
        return (text as NSString).boundingRect(
            with: NSSize(width: size.width - 24, height: size.height),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: font, .paragraphStyle: paragraph]).height
    }
    let totalHeight = measuredHeights.reduce(0, +) + CGFloat(max(0, lines.count - 1)) * 6
    var cursorY = (size.height + totalHeight) / 2
    for (index, line) in lines.enumerated() {
        let (text, fontSize, bold) = line
        let font = NSFont.systemFont(ofSize: fontSize, weight: bold ? .heavy : .semibold)
        let baseAttributes: [NSAttributedString.Key: Any] = [.font: font, .paragraphStyle: paragraph]
        let value = text as NSString
        let bounds = value.boundingRect(with: NSSize(width: size.width - 24, height: size.height),
                                        options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: baseAttributes)
        let rect = NSRect(x: 12, y: cursorY - bounds.height, width: size.width - 24, height: bounds.height + 2)
        // Outline pass then fill pass, exactly like the captions, so glyphs stay solid.
        let outline: [NSAttributedString.Key: Any] = [
            .font: font, .paragraphStyle: paragraph, .foregroundColor: NSColor.clear,
            .strokeColor: NSColor.black, .strokeWidth: 6.0,
        ]
        value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: outline)
        let fill: [NSAttributedString.Key: Any] = [.font: font, .paragraphStyle: paragraph, .foregroundColor: NSColor.white]
        value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: fill)
        cursorY -= bounds.height + 6
    }
    if accent {
        NSColor(calibratedRed: 0.93, green: 0.77, blue: 0.28, alpha: 1).setFill()
        NSRect(x: size.width / 2 - 46, y: 0, width: 92, height: 8).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    guard let image = bitmap.cgImage else { fail("card bitmap encode failed") }
    return image
}

/// A full-width overlay card that punches in and out over a time window.
func cardLayer(lines: [(String, CGFloat, Bool)], accent: Bool, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let height = size.height * 0.34
    layer.frame = CGRect(x: 0, y: size.height * 0.30, width: size.width, height: height)
    layer.backgroundColor = NSColor.black.withAlphaComponent(0.55).cgColor
    layer.opacity = 0
    let textLayer = CALayer()
    textLayer.frame = layer.bounds
    textLayer.contents = cardBitmap(lines: lines, size: layer.bounds.size, accent: accent)
    textLayer.contentsGravity = .resize
    textLayer.contentsScale = 2
    layer.addSublayer(textLayer)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.14, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "card-opacity")
    // Punch-in: scale down to rest for a clean modern news feel.
    let scale = CAKeyframeAnimation(keyPath: "transform.scale")
    scale.values = [1.12, 1.0, 1.0]
    scale.keyTimes = [0, 0.25, 1]
    scale.beginTime = AVCoreAnimationBeginTimeAtZero + start
    scale.duration = visible
    scale.fillMode = .both
    scale.isRemovedOnCompletion = false
    layer.add(scale, forKey: "card-scale")
    return layer
}

/// A contextual figure layer: the rights-cleared portrait with a restrained pan/zoom.
func figureLayer(imagePath: String, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.opacity = 0
    guard let image = NSImage(contentsOfFile: imagePath), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return layer
    }
    let photoHeight = size.height * 0.62
    let photoWidth = size.width
    let photo = CALayer()
    photo.frame = CGRect(x: 0, y: size.height * 0.16, width: photoWidth, height: photoHeight)
    photo.contents = cg
    photo.contentsGravity = .resizeAspectFill
    photo.masksToBounds = true
    photo.contentsScale = 2
    layer.addSublayer(photo)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.1, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "figure-opacity")
    // Slow Ken Burns push and pan; the portrait likeness itself is never altered.
    let transform = CAKeyframeAnimation(keyPath: "transform")
    transform.values = [
        CATransform3DMakeScale(1.0, 1.0, 1.0),
        CATransform3DMakeScale(1.08, 1.08, 1.0),
    ]
    transform.keyTimes = [0, 1]
    transform.beginTime = AVCoreAnimationBeginTimeAtZero + start
    transform.duration = visible
    transform.fillMode = .both
    transform.isRemovedOnCompletion = false
    photo.add(transform, forKey: "figure-kenburns")
    return layer
}

/// A small neutral label strip for the contextual figure.
func figureLabelLayer(line1: String, line2: String, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let height = size.height * 0.11
    layer.frame = CGRect(x: 0, y: size.height * 0.05, width: size.width, height: height)
    layer.backgroundColor = NSColor.black.withAlphaComponent(0.6).cgColor
    layer.opacity = 0
    let textLayer = CALayer()
    textLayer.frame = layer.bounds
    textLayer.contents = cardBitmap(lines: [(line1, size.width * 0.052, true), (line2, size.width * 0.04, false)],
                                    size: layer.bounds.size, accent: false)
    textLayer.contentsGravity = .resize
    textLayer.contentsScale = 2
    layer.addSublayer(textLayer)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.1, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "figure-label-opacity")
    return layer
}

/// A small contextual party mark in the corner.
func logoLayer(imagePath: String, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let side = size.width * 0.16
    layer.frame = CGRect(x: size.width - side - size.width * 0.06, y: size.height * 0.62, width: side, height: side)
    layer.opacity = 0
    guard let image = NSImage(contentsOfFile: imagePath), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return layer
    }
    let mark = CALayer()
    mark.frame = layer.bounds
    mark.contents = cg
    mark.contentsGravity = .resizeAspect
    mark.contentsScale = 2
    layer.addSublayer(mark)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.1, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "logo-opacity")
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

    let duckedMusicVolume = config.duckedMusicVolume ?? (config.musicVolume * 0.25)
    let narrationGain = config.narrationGain ?? 4.0
    let audioMix = AVMutableAudioMix()
    let voiceMix = AVMutableAudioMixInputParameters(track: narrationTrack)
    // NARRATION_GAIN is kept modest on purpose. Apple Speech already renders close to
    // full scale; a large gain clips at the encoder. Phone-audible loudness is reached
    // with a moderate gain plus the measured post-mix limiter applied in Python.
    voiceMix.setVolume(narrationGain, at: .zero)
    let bedMix = AVMutableAudioMixInputParameters(track: musicTrack)
    // Music bed automation. AVFoundation requires each scheduled ramp to be strictly
    // ordered and non-overlapping, so we use one continuous ramp that snaps down to a
    // ducked level across the whole spoken span and back up afterwards, plus a short
    // lead-in and tail. Voices play through at a fixed gain so narration dominates.
    func at(_ seconds: Double) -> CMTime { CMTime(seconds: min(max(0, seconds), targetDuration), preferredTimescale: 600) }
    let duckStart = max(0.05, narrationStart - 0.15)
    let duckEnd = min(targetDuration - 0.05, narrationEnd + 0.2)
    let fadeOutStart = max(duckEnd, targetDuration - 0.9)
    // 0 -> bed, smoothly duck across the spoken span, restore, then fade to silence.
    bedMix.setVolumeRamp(fromStartVolume: 0, toEndVolume: config.musicVolume,
                         timeRange: CMTimeRange(start: at(0), duration: at(duckStart)))
    bedMix.setVolume(config.musicVolume, at: at(duckStart))
    bedMix.setVolumeRamp(fromStartVolume: config.musicVolume, toEndVolume: duckedMusicVolume,
                         timeRange: CMTimeRange(start: at(duckStart), duration: at(0.25)))
    bedMix.setVolume(duckedMusicVolume, at: at(min(duckStart + 0.25, duckEnd)))
    bedMix.setVolumeRamp(fromStartVolume: duckedMusicVolume, toEndVolume: config.musicVolume,
                         timeRange: CMTimeRange(start: at(duckEnd), duration: at(min(0.3, max(0.05, fadeOutStart - duckEnd)))))
    bedMix.setVolume(config.musicVolume, at: at(fadeOutStart))
    bedMix.setVolumeRamp(fromStartVolume: config.musicVolume, toEndVolume: 0,
                         timeRange: CMTimeRange(start: at(fadeOutStart), duration: at(targetDuration - fadeOutStart)))
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
    // Contextual figure (rights-cleared) with a restrained pan/zoom and neutral label.
    if let cbnImage = config.cbnImage, !cbnImage.isEmpty {
        let contextEnd = min(targetDuration, 11.0)
        parentLayer.addSublayer(figureLayer(imagePath: cbnImage, start: 7.0, end: contextEnd, size: renderSize))
        parentLayer.addSublayer(figureLabelLayer(line1: config.cbnLabelLine1 ?? "", line2: config.cbnLabelLine2 ?? "",
                                                 start: 7.0, end: contextEnd, size: renderSize))
        if let tdpImage = config.tdpImage, !tdpImage.isEmpty {
            parentLayer.addSublayer(logoLayer(imagePath: tdpImage, start: 7.0, end: contextEnd, size: renderSize))
        }
    }
    // Hook card and closing card.
    if let headline = config.hookHeadline, !headline.isEmpty {
        parentLayer.addSublayer(cardLayer(lines: [(headline, renderSize.width * 0.072, true),
                                                  (config.hookSubline ?? "", renderSize.width * 0.046, false)],
                                          accent: true, start: 0.1, end: 2.5, size: renderSize))
    }
    if let closing = config.closingHeadline, !closing.isEmpty {
        let closingStart = max(2.6, targetDuration - 2.2)
        parentLayer.addSublayer(cardLayer(lines: [(closing, renderSize.width * 0.055, true)],
                                          accent: true, start: closingStart, end: targetDuration, size: renderSize))
    }
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
