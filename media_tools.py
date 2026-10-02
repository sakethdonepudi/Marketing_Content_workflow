"""Local, free media tooling: OCR, video frame sampling, and image derivatives.

OCR and frame extraction use a small Swift helper (tools/media_probe.swift) built on
Apple Vision and AVFoundation. It is compiled once per source revision and cached.
Image derivatives use Pillow. Every tool reports unavailability explicitly instead
of pretending an analysis happened.
"""

from abc import ABC, abstractmethod
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parent
PROBE_SOURCE = ROOT / "tools" / "media_probe.swift"
PROBE_CACHE = Path(os.environ.get("MEDIA_PROBE_CACHE", ROOT / ".cache" / "media-probe"))
_BUILD_LOCK = threading.Lock()


class MediaToolUnavailable(RuntimeError):
    pass


def _probe_binary(build=True):
    """Return the compiled probe path, compiling it on first use (macOS + swiftc only)."""
    if platform.system() != "Darwin" or not PROBE_SOURCE.exists():
        raise MediaToolUnavailable("Local media probe requires macOS with Apple Vision/AVFoundation.")
    digest = hashlib.sha256(PROBE_SOURCE.read_bytes()).hexdigest()[:16]
    binary = PROBE_CACHE / f"media-probe-{digest}"
    if binary.exists():
        return binary
    if not build:
        raise MediaToolUnavailable("Local media probe is not built yet.")
    compiler = shutil.which("swiftc")
    if not compiler:
        raise MediaToolUnavailable("swiftc is not installed; the local media probe cannot be built.")
    with _BUILD_LOCK:
        if not binary.exists():
            PROBE_CACHE.mkdir(parents=True, exist_ok=True)
            temporary = binary.with_suffix(".tmp")
            completed = subprocess.run(
                [compiler, "-O", str(PROBE_SOURCE), "-o", str(temporary)], capture_output=True, text=True, timeout=600,
            )
            if completed.returncode != 0 or not temporary.exists():
                raise MediaToolUnavailable("Local media probe failed to compile.")
            os.replace(temporary, binary)
    return binary


def _run_probe(args, timeout):
    completed = subprocess.run([str(_probe_binary()), *args], capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise MediaToolUnavailable("Local media probe failed: " + completed.stderr.decode("utf-8", "replace")[:200])
    return json.loads(completed.stdout.decode("utf-8"))


def warm_media_probe():
    """Compile the probe in the background so the first QA run is not delayed (no-op when unsupported)."""
    def build():
        try:
            _probe_binary()
        except Exception:
            pass
    threading.Thread(target=build, daemon=True).start()


# ---------- OCR ----------

class OCRProvider(ABC):
    name = None
    model = None
    local = True

    @abstractmethod
    def detect(self, image_bytes):
        """Return a list of {"text", "confidence", "box": [x, y, w, h] normalized, top-left origin}."""
        raise NotImplementedError


class UnavailableOCRProvider(OCRProvider):
    name = None

    def __init__(self, reason="No OCR provider is configured."):
        self.reason = reason

    def detect(self, image_bytes):
        raise MediaToolUnavailable(self.reason)


class AppleVisionOCRProvider(OCRProvider):
    name = "apple-vision"
    model = "VNRecognizeTextRequest(accurate)"

    def detect(self, image_bytes):
        with tempfile.NamedTemporaryFile(suffix=".img") as handle:
            handle.write(image_bytes)
            handle.flush()
            result = _run_probe(["ocr", handle.name], timeout=120)
        return [
            {"text": item["text"], "confidence": item.get("confidence"), "box": item.get("box")}
            for item in result.get("detections", []) if str(item.get("text", "")).strip()
        ]


def glyph_bright_ratio(image_bytes, safe_zone=None):
    """Fraction of near-white pixels in the bottom-middle subtitle band (script-agnostic glyph check).

    Used to confirm burned-in subtitles actually drew glyph pixels for scripts Apple Vision
    OCR cannot read, such as Telugu.
    """
    if platform.system() != "Darwin" or not PROBE_SOURCE.exists() or not shutil.which("swiftc"):
        raise MediaToolUnavailable("No local glyph checker is available on this host.")
    zone = json.dumps(safe_zone or {"sides": 0.08, "bottom": 0.20})
    with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
        handle.write(image_bytes)
        handle.flush()
        result = _run_probe(["glyph", handle.name, zone], timeout=120)
    return float(result.get("bright_ratio", 0.0)), int(result.get("total", 0))


def ocr_provider_for(name=None):
    name = (name if name is not None else os.environ.get("OCR_PROVIDER", "auto")).strip().lower()
    if name in ("", "none", "off"):
        return UnavailableOCRProvider("OCR is disabled (OCR_PROVIDER=none).")
    if name in ("auto", "apple-vision"):
        if platform.system() == "Darwin" and PROBE_SOURCE.exists() and shutil.which("swiftc"):
            return AppleVisionOCRProvider()
        return UnavailableOCRProvider("No local OCR engine is available on this host.")
    return UnavailableOCRProvider(f"Unsupported OCR provider {name!r}.")


# ---------- video frames ----------

class FrameExtractor(ABC):
    name = None

    @abstractmethod
    def extract(self, video_bytes, times):
        """Return [{"requested_seconds", "actual_seconds", "jpeg": bytes, "width", "height"}]."""
        raise NotImplementedError


class UnavailableFrameExtractor(FrameExtractor):
    def __init__(self, reason="No video frame extractor is available."):
        self.reason = reason

    def extract(self, video_bytes, times):
        raise MediaToolUnavailable(self.reason)


class AVFoundationFrameExtractor(FrameExtractor):
    name = "avfoundation"

    def extract(self, video_bytes, times):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(video_bytes)
            result = _run_probe(["frames", str(source), directory, ",".join(f"{value:.3f}" for value in times)], timeout=180)
            return [
                {**{key: item[key] for key in ("requested_seconds", "actual_seconds", "width", "height")},
                 "jpeg": Path(item["path"]).read_bytes()}
                for item in result.get("frames", [])
            ]


def frame_extractor_for(name=None):
    name = (name if name is not None else os.environ.get("VIDEO_FRAME_EXTRACTOR", "auto")).strip().lower()
    if name in ("", "none", "off"):
        return UnavailableFrameExtractor("Video frame extraction is disabled.")
    if name in ("auto", "avfoundation") and platform.system() == "Darwin" and PROBE_SOURCE.exists() and shutil.which("swiftc"):
        return AVFoundationFrameExtractor()
    return UnavailableFrameExtractor("No local video frame extractor is available on this host.")


def sample_times(duration_seconds, extra=0):
    """Beginning, ~25%, midpoint, ~75%, and end (slightly before the last frame), plus optional extras."""
    duration = max(0.0, float(duration_seconds or 0))
    if duration <= 0:
        return []
    end = max(0.0, duration - min(0.2, duration / 10))
    points = [0.0, duration * 0.25, duration * 0.5, duration * 0.75, end]
    for index in range(extra):
        points.append(duration * (index + 1) / (extra + 1))
    return sorted({round(point, 3) for point in points})


# ---------- image derivatives ----------

def _pillow():
    try:
        from PIL import Image
    except ImportError as error:
        raise MediaToolUnavailable("Pillow is required for image derivatives (pip install Pillow).") from error
    return Image


def prepare_video_source(image_bytes, *, max_bytes, min_short_side=720):
    """Derive a provider-friendly JPEG for image-to-video without cropping or stretching.

    Quality is lowered first; only if that is not enough is the image scaled down
    uniformly (aspect ratio preserved) to no less than min_short_side.
    """
    Image = _pillow()
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            original.load()
            source_size = original.size
            image = original.convert("RGB")
    except OSError:
        # A provider-origin JPEG may be structurally inspectable by the app's
        # conservative parser while a local codec cannot fully decode it. A
        # bounded JPEG can still be copied into a separate immutable derivative;
        # oversized or non-JPEG input fails before provider submission.
        if image_bytes[:2] == b"\xff\xd8" and image_bytes[-2:] == b"\xff\xd9" and len(image_bytes) <= max_bytes:
            from media_inspection import inspect_image
            inspected = inspect_image(image_bytes)
            return bytes(image_bytes), {
                "operation": "immutable_jpeg_passthrough", "format": "JPEG", "quality": None,
                "source_width": inspected["width"], "source_height": inspected["height"],
                "width": inspected["width"], "height": inspected["height"], "max_bytes": max_bytes,
                "crop": None, "stretch": None, "attempts": [{"bytes": len(image_bytes), "codec_decode": False}],
            }
        raise
    width, height = source_size
    attempts = []
    scale = 1.0
    while True:
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = image if target == source_size else image.resize(target, Image.Resampling.LANCZOS)
        for quality in (92, 88, 84, 80):
            buffer = io.BytesIO()
            resized.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
            data = buffer.getvalue()
            attempts.append({"width": target[0], "height": target[1], "quality": quality, "bytes": len(data)})
            if len(data) <= max_bytes:
                return data, {
                    "operation": "reencode" if target == source_size else "uniform_downscale+reencode",
                    "format": "JPEG", "quality": quality, "source_width": width, "source_height": height,
                    "width": target[0], "height": target[1], "max_bytes": max_bytes, "crop": None, "stretch": None,
                    "attempts": attempts,
                }
        next_scale = scale * 0.85
        if min(width, height) * next_scale < min_short_side:
            raise ValueError(
                f"Video source derivative still exceeds {max_bytes} bytes at the minimum short side of {min_short_side}px."
            )
        scale = next_scale


def analysis_jpeg(image_bytes, long_edge=1568, quality=85):
    """Downscaled copy for vision-model analysis; never stored as the asset itself."""
    Image = _pillow()
    with Image.open(io.BytesIO(image_bytes)) as original:
        image = original.convert("RGB")
    ratio = min(1.0, long_edge / max(image.size))
    if ratio < 1.0:
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()
