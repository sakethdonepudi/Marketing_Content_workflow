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
    /// Ordered multi-visual beats. When present these replace the single looping source.
    let scenes: [SceneBeat]?
}

struct SceneBeat: Codable {
    let kind: String          // HOOK, IMAGE, CBN, MAP, DOCUMENT, CLOSING
    let image: String?        // still image path for IMAGE / CBN
    let start: Double
    let end: Double
    let motion: String?       // push-in, pan-left, pan-right, kenburns
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
    let fontSize = size.width * 0.076
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
        .font: NSFont.systemFont(ofSize: censureWidth * 0.076, weight: .bold), .paragraphStyle: paragraph,
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
    let height = size.height * 0.27
    // Sits in the upper-middle band, above the burned-in subtitle strip near the bottom.
    layer.frame = CGRect(x: 0, y: size.height * 0.44, width: size.width, height: height)
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

/// A contextual figure layer: the rights-cleared portrait as a soft circular inset
/// with a gold ring and a restrained Ken Burns pan. The likeness is never altered.
func figureLayer(imagePath: String, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.opacity = 0
    guard let image = NSImage(contentsOfFile: imagePath), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return layer
    }
    let diameter = min(size.width * 0.62, size.height * 0.46)
    let centerX = size.width / 2
    let centerY = size.height * 0.60
    let ring = CALayer()
    ring.frame = CGRect(x: centerX - diameter / 2 - 6, y: centerY - diameter / 2 - 6,
                        width: diameter + 12, height: diameter + 12)
    ring.cornerRadius = (diameter + 12) / 2
    ring.backgroundColor = NSColor.white.cgColor
    ring.shadowColor = NSColor.black.cgColor
    ring.shadowOpacity = 0.28
    ring.shadowRadius = 18
    ring.shadowOffset = CGSize(width: 0, height: 8)
    layer.addSublayer(ring)
    let photo = CALayer()
    photo.frame = CGRect(x: centerX - diameter / 2, y: centerY - diameter / 2, width: diameter, height: diameter)
    photo.contents = cg
    photo.contentsGravity = .resizeAspectFill
    photo.masksToBounds = true
    photo.cornerRadius = diameter / 2
    photo.borderColor = NSColor(calibratedRed: 0.72, green: 0.53, blue: 0.04, alpha: 1).cgColor
    photo.borderWidth = 5
    photo.contentsScale = 2
    layer.addSublayer(photo)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.12, 0.88, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "figure-opacity")
    // Pop-in with a slight overshoot, then a slow Ken Burns push; likeness never altered.
    let transform = CAKeyframeAnimation(keyPath: "transform")
    transform.values = [
        CATransform3DMakeScale(0.86, 0.86, 1.0),
        CATransform3DMakeScale(1.03, 1.03, 1.0),
        CATransform3DMakeScale(1.0, 1.0, 1.0),
        CATransform3DMakeScale(1.07, 1.07, 1.0),
    ]
    transform.keyTimes = [0, 0.22, 0.45, 1]
    transform.beginTime = AVCoreAnimationBeginTimeAtZero + start
    transform.duration = visible
    transform.fillMode = .both
    transform.isRemovedOnCompletion = false
    layer.add(transform, forKey: "figure-kenburns")
    return layer
}

/// A news-style lower-third name bar with a gold accent tab.
func lowerThirdLayer(line1: String, line2: String, start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let barHeight = size.height * 0.08
    // Sits high on the frame (below the top edge, above the portrait and the bottom
    // subtitle band) so the name and the burned-in words never sit close enough for
    // OCR to merge them into one blob.
    layer.frame = CGRect(x: size.width * 0.08, y: size.height * 0.10, width: size.width * 0.84, height: barHeight)
    layer.backgroundColor = NSColor.black.withAlphaComponent(0.72).cgColor
    layer.cornerRadius = 10
    layer.masksToBounds = true
    layer.opacity = 0
    let tab = CALayer()
    tab.frame = CGRect(x: 0, y: 0, width: 7, height: barHeight)
    tab.backgroundColor = NSColor(calibratedRed: 0.72, green: 0.53, blue: 0.04, alpha: 1).cgColor
    layer.addSublayer(tab)
    let textLayer = CALayer()
    textLayer.frame = CGRect(x: 18, y: 0, width: layer.bounds.width - 26, height: barHeight)
    textLayer.contents = cardBitmap(lines: [(line1, size.width * 0.044, true), (line2, size.width * 0.033, false)],
                                    size: textLayer.bounds.size, accent: false)
    textLayer.contentsGravity = .resize
    textLayer.contentsScale = 2
    layer.addSublayer(textLayer)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [0, 0.1, 0.92, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "lower-third-opacity")
    // Slide in from the left.
    let position = CAKeyframeAnimation(keyPath: "transform.translation.x")
    position.values = [-90, 0, 0]
    position.keyTimes = [0, 0.18, 1]
    position.beginTime = AVCoreAnimationBeginTimeAtZero + start
    position.duration = visible
    position.fillMode = .both
    position.isRemovedOnCompletion = false
    layer.add(position, forKey: "lower-third-slide")
    return layer
}

/// A thin progress bar along the bottom that fills across the whole runtime.
func progressLayer(total: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    let height: CGFloat = 6
    layer.frame = CGRect(x: 0, y: 0, width: size.width, height: height)
    layer.backgroundColor = NSColor.white.withAlphaComponent(0.22).cgColor
    let fill = CALayer()
    fill.frame = CGRect(x: 0, y: 0, width: 0, height: height)
    fill.backgroundColor = NSColor(calibratedRed: 0.93, green: 0.77, blue: 0.28, alpha: 1).cgColor
    layer.addSublayer(fill)
    let width = CABasicAnimation(keyPath: "bounds.size.width")
    width.fromValue = 0
    width.toValue = size.width
    width.beginTime = AVCoreAnimationBeginTimeAtZero
    width.duration = total
    width.fillMode = .both
    width.isRemovedOnCompletion = false
    fill.add(width, forKey: "progress-fill")
    return layer
}

/// A soft cinematic vignette that darkens the edges without touching the subject.
func vignetteLayer(size: CGSize) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.backgroundColor = NSColor.clear.cgColor
    layer.shadowColor = NSColor.black.cgColor
    layer.shadowOpacity = 0.35
    layer.shadowRadius = size.width * 0.22
    layer.shadowOffset = .zero
    layer.mask = {
        let mask = CALayer()
        mask.frame = layer.bounds
        mask.backgroundColor = NSColor.white.cgColor
        let hole = CALayer()
        hole.frame = layer.bounds.insetBy(dx: size.width * 0.18, dy: size.height * 0.18)
        hole.cornerRadius = size.width * 0.3
        hole.backgroundColor = NSColor.black.cgColor
        mask.addSublayer(hole)
        return mask
    }()
    return layer
}

/// A full-frame still beat (generated scene or map/document graphic) with a subtle
/// Ken Burns move and a soft crossfade in/out so cuts never feel static.
func stillLayer(imagePath: String, start: Double, end: Double, motion: String?, size: CGSize,
                fade: Double = 0.35) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.opacity = 0
    layer.masksToBounds = true
    guard let image = NSImage(contentsOfFile: imagePath),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return layer
    }
    let photo = CALayer()
    photo.frame = layer.bounds
    photo.contents = cg
    photo.contentsGravity = .resizeAspectFill
    photo.masksToBounds = true
    photo.contentsScale = 2
    layer.addSublayer(photo)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]
    opacity.keyTimes = [NSNumber(value: 0), NSNumber(value: min(0.2, fade / visible)),
                        NSNumber(value: max(0.0, 1 - fade / visible)), NSNumber(value: 1)]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible
    opacity.fillMode = .both
    opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "still-opacity")
    // Subtle push or pan; never a static hold.
    let scale = CAKeyframeAnimation(keyPath: "transform.scale")
    let pan = CAKeyframeAnimation(keyPath: "transform.translation.x")
    switch motion ?? "push-in" {
    case "pan-left":
        scale.values = [1.08, 1.08]; pan.values = [size.width * 0.03, -size.width * 0.03]
    case "pan-right":
        scale.values = [1.08, 1.08]; pan.values = [-size.width * 0.03, size.width * 0.03]
    case "kenburns":
        scale.values = [1.02, 1.1]; pan.values = [size.width * 0.015, -size.width * 0.02]
    default:
        scale.values = [1.02, 1.1]; pan.values = [0, 0]
    }
    for animation in [scale, pan] {
        animation.beginTime = AVCoreAnimationBeginTimeAtZero + start
        animation.duration = visible
        animation.fillMode = .both
        animation.isRemovedOnCompletion = false
        photo.add(animation, forKey: animation.keyPath)
    }
    return layer
}

/// A locally drawn Andhra Pradesh state-map silhouette graphic (no paid generation).
func mapGraphicLayer(start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.backgroundColor = NSColor(calibratedRed: 0.97, green: 0.98, blue: 0.96, alpha: 1).cgColor
    layer.opacity = 0
    // Point path approximating the Andhra Pradesh outline (stylized, non-survey).
    let unitPoints: [(CGFloat, CGFloat)] = [
        (0.30, 0.14), (0.52, 0.10), (0.70, 0.16), (0.80, 0.30), (0.74, 0.46),
        (0.82, 0.58), (0.70, 0.72), (0.58, 0.86), (0.40, 0.82), (0.28, 0.66),
        (0.22, 0.50), (0.18, 0.34),
    ]
    let shape = CAShapeLayer()
    let mapWidth = size.width * 0.62, mapHeight = size.height * 0.5
    let originX = (size.width - mapWidth) / 2, originY = size.height * 0.24
    let path = CGMutablePath()
    for (index, point) in unitPoints.enumerated() {
        let x = originX + point.0 * mapWidth, y = originY + (1 - point.1) * mapHeight
        if index == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
    }
    path.closeSubpath()
    shape.path = path
    shape.fillColor = NSColor(calibratedRed: 0.19, green: 0.44, blue: 0.25, alpha: 0.16).cgColor
    shape.strokeColor = NSColor(calibratedRed: 0.19, green: 0.44, blue: 0.25, alpha: 0.9).cgColor
    shape.lineWidth = 4
    layer.addSublayer(shape)
    let label = CALayer()
    label.frame = CGRect(x: 0, y: size.height * 0.76, width: size.width, height: size.height * 0.1)
    label.contents = cardBitmap(lines: [("ANDHRA PRADESH", size.width * 0.06, true)],
                                size: label.bounds.size, accent: false)
    label.contentsGravity = .resize
    label.contentsScale = 2
    layer.addSublayer(label)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]; opacity.keyTimes = [0, 0.12, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible; opacity.fillMode = .both; opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "map-opacity")
    return layer
}

/// A locally drawn official-notification style abstract graphic (no paid generation).
func documentGraphicLayer(start: Double, end: Double, size: CGSize) -> CALayer {
    let layer = CALayer()
    layer.frame = CGRect(origin: .zero, size: size)
    layer.backgroundColor = NSColor(calibratedRed: 0.95, green: 0.96, blue: 0.94, alpha: 1).cgColor
    layer.opacity = 0
    let page = CALayer()
    page.frame = CGRect(x: size.width * 0.14, y: size.height * 0.20, width: size.width * 0.72, height: size.height * 0.44)
    page.backgroundColor = NSColor.white.cgColor
    page.cornerRadius = 10
    page.shadowColor = NSColor.black.cgColor
    page.shadowOpacity = 0.16; page.shadowRadius = 14; page.shadowOffset = CGSize(width: 0, height: 8)
    layer.addSublayer(page)
    var y = page.bounds.height - 28
    let widths: [CGFloat] = [0.82, 0.9, 0.74, 0.88, 0.6]
    for (index, ratio) in widths.enumerated() {
        let bar = CALayer()
        bar.frame = CGRect(x: 20, y: y, width: (page.bounds.width - 40) * ratio, height: index == 0 ? 14 : 8)
        bar.cornerRadius = 4
        bar.backgroundColor = (index == 0
            ? NSColor(calibratedRed: 0.19, green: 0.44, blue: 0.25, alpha: 0.85)
            : NSColor(calibratedRed: 0.72, green: 0.75, blue: 0.71, alpha: 1)).cgColor
        page.addSublayer(bar)
        y -= index == 0 ? 30 : 22
    }
    let seal = CALayer()
    seal.frame = CGRect(x: page.bounds.width - 70, y: 20, width: 44, height: 44)
    seal.cornerRadius = 22
    seal.borderWidth = 3
    seal.borderColor = NSColor(calibratedRed: 0.72, green: 0.53, blue: 0.04, alpha: 1).cgColor
    page.addSublayer(seal)
    let label = CALayer()
    label.frame = CGRect(x: 0, y: size.height * 0.68, width: size.width, height: size.height * 0.09)
    label.contents = cardBitmap(lines: [("GOVERNMENT NOTIFICATION", size.width * 0.048, true)],
                                size: label.bounds.size, accent: false)
    label.contentsGravity = .resize
    label.contentsScale = 2
    layer.addSublayer(label)
    let visible = max(0.1, end - start)
    let opacity = CAKeyframeAnimation(keyPath: "opacity")
    opacity.values = [0, 1, 1, 0]; opacity.keyTimes = [0, 0.12, 0.9, 1]
    opacity.beginTime = AVCoreAnimationBeginTimeAtZero + start
    opacity.duration = visible; opacity.fillMode = .both; opacity.isRemovedOnCompletion = false
    layer.add(opacity, forKey: "doc-opacity")
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
    // Cinematic vignette and a thin gold progress bar across the runtime.
    parentLayer.addSublayer(vignetteLayer(size: renderSize))
    parentLayer.addSublayer(progressLayer(total: targetDuration, size: renderSize))
    // Multi-visual beats: full-frame stills (generated scenes, map, document) over the base footage.
    if let scenes = config.scenes, !scenes.isEmpty {
        for beat in scenes {
            let motion = beat.motion
            switch beat.kind {
            case "IMAGE":
                if let image = beat.image, !image.isEmpty {
                    parentLayer.addSublayer(stillLayer(imagePath: image, start: beat.start, end: beat.end,
                                                       motion: motion, size: renderSize))
                }
            case "MAP":
                parentLayer.addSublayer(mapGraphicLayer(start: beat.start, end: beat.end, size: renderSize))
            case "DOCUMENT":
                parentLayer.addSublayer(documentGraphicLayer(start: beat.start, end: beat.end, size: renderSize))
            case "CBN":
                if let image = beat.image, !image.isEmpty {
                    parentLayer.addSublayer(stillLayer(imagePath: image, start: beat.start, end: beat.end,
                                                       motion: "kenburns", size: renderSize))
                }
                parentLayer.addSublayer(figureLayer(imagePath: config.cbnImage ?? beat.image ?? "",
                                                    start: beat.start, end: beat.end, size: renderSize))
                parentLayer.addSublayer(lowerThirdLayer(line1: config.cbnLabelLine1 ?? "", line2: config.cbnLabelLine2 ?? "",
                                                        start: beat.start, end: beat.end, size: renderSize))
                if let tdpImage = config.tdpImage, !tdpImage.isEmpty {
                    parentLayer.addSublayer(logoLayer(imagePath: tdpImage, start: beat.start, end: beat.end, size: renderSize))
                }
            default:
                break
            }
        }
    } else if let cbnImage = config.cbnImage, !cbnImage.isEmpty {
        // Legacy single-loop fallback: contextual figure with a lower-third name bar.
        let contextEnd = min(targetDuration, 11.0)
        parentLayer.addSublayer(figureLayer(imagePath: cbnImage, start: 7.0, end: contextEnd, size: renderSize))
        parentLayer.addSublayer(lowerThirdLayer(line1: config.cbnLabelLine1 ?? "", line2: config.cbnLabelLine2 ?? "",
                                                start: 7.0, end: contextEnd, size: renderSize))
        if let tdpImage = config.tdpImage, !tdpImage.isEmpty {
            parentLayer.addSublayer(logoLayer(imagePath: tdpImage, start: 7.0, end: contextEnd, size: renderSize))
        }
    }
    // Hook card (upper half only) and closing card, both clear of the bottom subtitle band.
    if let headline = config.hookHeadline, !headline.isEmpty {
        parentLayer.addSublayer(cardLayer(lines: [(headline, renderSize.width * 0.062, true),
                                                  (config.hookSubline ?? "", renderSize.width * 0.04, false)],
                                          accent: true, start: 0.1, end: 3.4, size: renderSize))
    }
    if let closing = config.closingHeadline, !closing.isEmpty {
        let closingStart = max(2.6, targetDuration - 2.6)
        parentLayer.addSublayer(cardLayer(lines: [(closing, renderSize.width * 0.048, true)],
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
