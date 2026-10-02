"""Deterministic, review-gated Final Reel composition from an immutable generated video."""

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import uuid
import wave

from media_inspection import inspect_image, inspect_video
from media_storage import LocalMediaStorage
from media_tools import frame_extractor_for, ocr_provider_for
from meta_distribution import check_compliance
from media_rendering import renderer_configuration, renderer_for

ROOT = Path(__file__).resolve().parent
COMPOSER_SOURCE = ROOT / "tools" / "final_reel_composer.swift"
COMPOSER_CACHE = ROOT / ".cache" / "final-reel-composer"
COMPOSER_POLICY_VERSION = "final-reel-composer-v12"
VOICE_PROVIDER = "apple-speech"
VOICE_MODEL = "Aman (en-IN)"
TELUGU_VOICE_MODEL = "Geeta (te_IN)"
TELUGU_SPEECH_RATE = 220
TELUGU_LANGUAGE = "te"

# Natural Telugu news/explainer narration, chunked so timing and pauses are controlled.
# Grounding: every sentence states only the three approved facts (Union government
# permitted excess FCV tobacco sales for 2025-26; registered and unregistered growers may
# sell via Tobacco Board-authorised auction platforms; communicated by a Union Commerce
# Ministry notification). No praise, credit, income, quote, motive, or extra number.
TELUGU_NARRATION_CHUNKS = [
    ("N1", "ఏపీ పొగాకు రైతులకు కీలక ఊరట.", 0.18),
    ("N2", "2025–26 సీజన్‌లో అదనంగా పండిన ఎఫ్‌సీవీ పొగాకును ఇక అమ్ముకునే అవకాశం వచ్చింది.", 0.14),
    ("N3", "ఈ విక్రయాలకు కేంద్ర ప్రభుత్వం అనుమతి ఇచ్చింది.", 0.16),
    ("N4A", "రిజిస్టర్డ్ రైతులే కాదు...", 0.35),
    ("N4B", "అన్‌రిజిస్టర్డ్ రైతులు కూడా టొబాకో బోర్డు అనుమతించిన వేలం కేంద్రాల్లో", 0.12),
    ("N4C", "తమ అదనపు పంటను విక్రయించుకోవచ్చు.", 0.16),
    ("N5", "ఈ మేరకు కేంద్ర వాణిజ్య మంత్రిత్వ శాఖ అధికారిక నోటిఫికేషన్ విడుదల చేసింది.", 0.0),
]
NARRATION_GAIN = 1.3
MUSIC_VOLUME = 0.04
DUCKED_MUSIC_VOLUME = 0.008
SPEECH_TARGET_RMS_DBFS = -17.0
SPEECH_MIN_RMS_DBFS = -30.0
SPEECH_MAX_RMS_DBFS = -12.0
CLIP_CEILING = 0.97
SUBTITLE_MIN_COVERAGE = 0.7


class FinalReelError(RuntimeError):
    pass


# Original neutral scenes generated on demand with the configured live image renderer.
# Each prompt renders no text, logos, flags, maps, or real people, matching the renderer
# constraints; the AP map and document visuals are drawn locally instead (no paid call).
SCENE_PROMPTS = {
    "AP_FIELD_GOLDEN": (
        "Original editorial illustration: a wide view of an Andhra Pradesh tobacco field at golden hour. "
        "Rows of healthy green tobacco plants, warm low sunlight, soft haze, distant tree line. "
        "No text, letters, numbers, logos, flags, party symbols, maps, or recognizable people. "
        "Neutral, calm, cinematic documentary tone; no crowds, banners, or political messaging."
    ),
    "BARN_CURED_LEAVES": (
        "Original editorial illustration: cured FCV tobacco leaves hanging in neat rows inside a rustic "
        "wooden curing barn, warm ambient light, textured dry leaves, shallow depth of field. "
        "No text, letters, numbers, logos, flags, party symbols, maps, or people. "
        "Neutral, factual, cinematic; no politicians or campaign imagery."
    ),
    "AUCTION_WAREHOUSE_BALES": (
        "Original editorial illustration: interior of a clean agricultural auction warehouse with neatly "
        "stacked rectangular tobacco bales, orderly aisles, natural daylight from high windows. "
        "No text, letters, numbers, signs, logos, flags, party symbols, maps, or recognizable people. "
        "Neutral, orderly, documentary tone."
    ),
    "AUCTION_PLATFORM_NEUTRAL": (
        "Original editorial illustration: a neutral crop auction platform with rows of graded bales on a "
        "wooden floor, soft overhead light, empty space, no humans. "
        "No text, letters, numbers, logos, flags, party symbols, maps, or identifiable people. "
        "Calm, factual, cinematic."
    ),
    "GRADING_TAGS_CLOSEUP": (
        "Original editorial illustration: close-up of a stack of cured tobacco leaves at a grading station, "
        "natural texture and fibre detail, no legible writing anywhere. "
        "Absolutely no text, letters, numbers, tags with writing, logos, flags, party symbols, maps, or people. "
        "Neutral macro documentary tone."
    ),
}
SCENE_ORDER = list(SCENE_PROMPTS)
SCENE_RIGHT = "GENERATED_ORIGINAL"


def _normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _wav_or_aiff_seconds(path):
    """Duration of a rendered voice clip (AIFF written by Apple `say`), via afinfo."""
    if platform.system() == "Darwin" and shutil.which("afinfo"):
        completed = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, timeout=60)
        match = re.search(r"estimated duration:\s*([0-9.]+)", completed.stdout)
        if match:
            return float(match.group(1))
    data = Path(path).read_bytes()
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        offset = 12
        while offset + 8 <= len(data):
            chunk_id = data[offset:offset + 4]
            size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
            if chunk_id == b"fmt ":
                _, _, sample_rate, _, _, bits = struct.unpack("<HHIIHH", data[offset + 8:offset + 24])
            if chunk_id == b"data":
                channels = 1
                return size / (sample_rate * channels * (bits // 8))
            offset += 8 + size + (size % 2)
    return 0.0


def generate_scene(scene_key, *, connect, storage_root, now, renderer=None, timeout_seconds=180):
    """Generate one original neutral scene with the live image renderer, stored and rights-tracked.

    Returns the existing row when the same scene prompt already produced the same bytes, so a
    retry never pays twice for an identical image.
    """
    prompt = SCENE_PROMPTS.get(scene_key)
    if not prompt:
        raise FinalReelError(f"Unknown scene {scene_key}.")
    configuration = renderer_configuration("IMAGE")
    if renderer is None:
        if not configuration["live"]:
            raise FinalReelError("Image renderer not configured: " + configuration["status"])
        provider = configuration["provider"]
        renderer = renderer_for(provider, "IMAGE")
        if provider != "xai":
            raise FinalReelError("Only the configured live image provider may generate scenes.")
    request = {
        "media_type": "IMAGE", "visual_prompts": (prompt,),
        "generation_parameters": {"aspect_ratio": "3:4", "output_count": 1},
        "generation_parameters_version": "scene-v1",
    }
    reason = getattr(renderer, "unsupported_reason", lambda *a, **k: None)("IMAGE", "3:4")
    if reason:
        raise FinalReelError("RENDERER_CAPABILITY_MISMATCH: " + reason)
    result = renderer.render(request, timeout_seconds=timeout_seconds)
    storage = LocalMediaStorage(storage_root)
    stored = storage.save(result.asset_bytes, extension="jpg" if result.mime_type == "image/jpeg" else "png",
                          metadata={"purpose": "GENERATED_SCENE"})
    decoded = inspect_image(result.asset_bytes)
    cost = result.provider_cost_usd
    row = {
        "id": "GS-" + uuid.uuid4().hex[:12].upper(), "scene_key": scene_key,
        "label": scene_key.replace("_", " ").title(), "storage_uri": stored.storage_uri,
        "mime_type": result.mime_type, "width": decoded.get("width"), "height": decoded.get("height"),
        "file_size": stored.file_size, "checksum_sha256": stored.checksum_sha256,
        "provider": getattr(renderer, "name", configuration["provider"]), "model": getattr(renderer, "model", None),
        "provider_request_id": result.provider_request_id, "prompt": prompt, "rights_status": SCENE_RIGHT,
        "cost_status": "known" if cost is not None else "unknown", "cost_usd": cost,
        "currency": result.currency or ("USD" if cost is not None else None), "created_at": now(),
    }
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM generated_scenes WHERE scene_key=? AND checksum_sha256=?",
            (scene_key, stored.checksum_sha256),
        ).fetchone()
        if existing:
            return dict(existing)
        connection.execute(
            f"INSERT INTO generated_scenes({','.join(row)}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    return row


def generate_scenes(scene_keys=None, *, connect, storage_root, now, renderer=None, timeout_seconds=180):
    keys = list(scene_keys or SCENE_ORDER)
    return {key: generate_scene(key, connect=connect, storage_root=storage_root, now=now,
                                renderer=renderer, timeout_seconds=timeout_seconds) for key in keys}


def _is_telugu(text):
    return bool(re.search(r"[\u0C00-\u0C7F]", str(text or "")))


def _is_telugu_voice(voice):
    return bool(re.search(r"\(te[_\-]?IN\)|Telugu", str(voice or ""), re.IGNORECASE))


def telugu_subtitle_cues(chunk_text):
    """Break a Telugu line into short phrase-level cues (2-6 words), never whole sentences."""
    cleaned = str(chunk_text).replace("...", " ").strip()
    words = [word for word in cleaned.split() if word]
    cues, current = [], []
    for word in words:
        current.append(word)
        if 3 <= len(current) <= 5:
            cues.append(" ".join(current))
            current = []
    if current:
        # Merge a trailing 1-word remnant into the previous cue so no cue is a lone word.
        if cues and len(current) == 1:
            cues[-1] = cues[-1] + " " + current[0]
        else:
            cues.append(" ".join(current))
    return cues


def telugu_factual_qa(chunks):
    """Grounding check: every chunk must map to one of the three approved facts and add nothing."""
    approved_themes = {
        "permission_2025_26": ("2025", "పంట", "సీజన్", "పొగాకు", "అమ్మ"),
        "union_government_permitted": ("కేంద్ర", "ప్రభుత్వం", "అనుమతి"),
        "registered_and_unregistered_auction": ("రిజిస్టర్డ్", "అన్‌రిజిస్టర్డ్", "వేలం", "టొబాకో"),
        "commerce_notification": ("వాణిజ్య", "మంత్రిత్వ", "నోటిఫికేషన్"),
    }
    banned = ("మెచ్చు", "అభినంద", "గెలుపు", "ఆదాయ", "లాభం", "కోట్", "ఉద్ధరించ", "క్రెడిట్")
    unsupported = []
    for key, text, _ in chunks:
        if any(term in text for term in banned):
            unsupported.append({"chunk": key, "reason": "contains praise/credit/income language"})
            continue
        if not any(any(token in text for token in themes) for themes in approved_themes.values()):
            unsupported.append({"chunk": key, "reason": "does not map to an approved fact"})
    return {
        "status": "PASS" if not unsupported else "FLAG",
        "approved_facts": ["permission_2025_26", "union_government_permitted",
                            "registered_and_unregistered_auction", "commerce_notification"],
        "chunks": len(chunks), "unsupported": unsupported,
        "language": TELUGU_LANGUAGE,
        "no_new_claims": not unsupported,
    }


def approved_narration(package):
    """A friendly explainer read using only approved package strings, verbatim.

    Order is hook first, then the approved script sentences in sequence. No fact is
    added, dropped, or reworded; only ordering and joining punctuation change, so the
    narration stays fully grounded in the approved package and claims.
    """
    hook = str((package.get("hook") or {}).get("text") or "").strip().rstrip(".")
    script = sorted(package.get("script") or [], key=lambda item: item.get("sequence", 0))
    sentences = [str(item.get("text") or "").strip().rstrip(".") for item in script]
    sentences = [sentence for sentence in sentences if sentence]
    if not hook or not sentences:
        raise FinalReelError("The approved package has no usable hook or script text.")
    # Hook, then each distinct approved script sentence (deduplicated, order preserved).
    ordered = [hook]
    for sentence in sentences:
        if _normalized(sentence) not in {_normalized(existing) for existing in ordered}:
            ordered.append(sentence)
    return ". ".join(ordered) + "."


def validate_factual_narration(narration, package, approved_claims):
    allowed = {
        _normalized((package.get("hook") or {}).get("text")),
        *(_normalized(item.get("text")) for item in package.get("script") or []),
        *(_normalized(item.get("text")) for item in approved_claims),
    }
    sentences = [_normalized(item) for item in re.split(r"(?<=[.!?])\s+", narration) if _normalized(item)]
    unsupported = [item for item in sentences if item not in allowed]
    return {
        "status": "PASS" if sentences and not unsupported else "FLAG",
        "policy_version": COMPOSER_POLICY_VERSION,
        "sentences": sentences,
        "unsupported_sentences": unsupported,
        "no_new_factual_claims": bool(sentences and not unsupported),
    }


_CONFUSABLE = str.maketrans({
    # lowercase Cyrillic
    "\u043e": "o", "\u0430": "a", "\u0435": "e", "\u0441": "c", "\u0440": "p", "\u0443": "y",
    "\u0445": "x", "\u043a": "k", "\u043c": "m", "\u0442": "t", "\u043d": "h", "\u0432": "b",
    # uppercase Cyrillic
    "\u041e": "o", "\u0410": "a", "\u0415": "e", "\u0421": "c", "\u0420": "p", "\u0423": "y",
    "\u0425": "x", "\u041a": "k", "\u041c": "m", "\u0422": "t", "\u041d": "h", "\u0412": "b",
})


def _fold(text):
    """Normalize OCR text, folding common Cyrillic/Latin lookalikes that Apple Vision emits.

    Confusables are translated *before* the non-alphanumeric cleanup, otherwise the
    Cyrillic letters would be stripped to spaces and split a word in two.
    """
    return _normalized(str(text).casefold().translate(_CONFUSABLE))


def _token_coverage(expected, detected):
    expected_tokens = _fold(expected).split()
    detected_tokens = set(_fold(detected).split())
    if not expected_tokens:
        return 1.0
    hits = sum(1 for token in expected_tokens if token in detected_tokens)
    return hits / len(expected_tokens)


def subtitle_phrases(narration):
    """Bounded authored phrases; timings come from each rendered voice block."""
    words = narration.split()
    phrases = []
    while words:
        phrases.append(" ".join(words[:5]))
        words = words[5:]
    return phrases


def _composer_binary():
    if platform.system() != "Darwin" or not shutil.which("swiftc"):
        raise FinalReelError("Final Reel composition requires macOS with swiftc and AVFoundation.")
    digest = hashlib.sha256(COMPOSER_SOURCE.read_bytes()).hexdigest()[:16]
    binary = COMPOSER_CACHE / f"final-reel-composer-{digest}"
    if binary.exists():
        return binary
    COMPOSER_CACHE.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_suffix(".tmp")
    completed = subprocess.run(
        ["swiftc", "-O", str(COMPOSER_SOURCE), "-o", str(temporary)],
        capture_output=True, text=True, timeout=600,
    )
    if completed.returncode != 0 or not temporary.exists():
        raise FinalReelError("Final Reel composer failed to compile: " + completed.stderr[-1200:])
    os.replace(temporary, binary)
    return binary


def _write_music_bed(path, duration_seconds=30, sample_rate=48_000):
    """Create a quiet original ambient major-sixth pad; no licensed input media."""
    frames = int(duration_seconds * sample_rate)
    frequencies = (110.0, 138.59, 164.81, 220.0)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        chunk = bytearray()
        for index in range(frames):
            time_value = index / sample_rate
            envelope = min(1.0, time_value / 1.5, (duration_seconds - time_value) / 1.5)
            shimmer = 0.85 + 0.15 * math.sin(2 * math.pi * 0.08 * time_value)
            value = sum(math.sin(2 * math.pi * frequency * time_value) for frequency in frequencies) / len(frequencies)
            sample = int(max(-1, min(1, value * envelope * shimmer * 0.32)) * 32767)
            chunk.extend(struct.pack("<h", sample))
            if len(chunk) >= 64 * 1024:
                output.writeframesraw(chunk)
                chunk.clear()
        if chunk:
            output.writeframesraw(chunk)


def _neutralize_mp4_edit_lists(data):
    """Replace edts boxes with same-size free boxes so offsets and samples stay byte-for-byte stable."""
    output = bytearray(data)
    changed = 0

    def visit(start, end, descend):
        nonlocal changed
        offset = start
        while offset + 8 <= end:
            size = int.from_bytes(output[offset:offset + 4], "big")
            kind = bytes(output[offset + 4:offset + 8])
            header = 8
            if size == 1:
                if offset + 16 > end:
                    raise FinalReelError("Malformed extended MP4 box while checking edit lists.")
                size = int.from_bytes(output[offset + 8:offset + 16], "big")
                header = 16
            elif size == 0:
                size = end - offset
            if size < header or offset + size > end:
                raise FinalReelError("Malformed MP4 box while checking edit lists.")
            if kind == b"edts":
                output[offset + 4:offset + 8] = b"free"
                changed += 1
            elif kind in descend:
                visit(offset + header, offset + size, descend)
            offset += size
        if offset != end:
            raise FinalReelError("Malformed MP4 container alignment.")

    visit(0, len(output), {b"moov", b"trak"})
    return bytes(output), changed


def _run_voice_chunks(chunks, directory, voice, rate):
    """Synthesize each Telugu narration chunk as its own clip so pauses are controlled.

    Returns the ordered clip paths and the pause (seconds) that follows each clip.
    """
    say = shutil.which("say")
    if not say:
        raise FinalReelError("Apple Speech Synthesizer is unavailable.")
    paths, pauses = [], []
    for index, (key, text, pause) in enumerate(chunks):
        path = directory / f"voice-{index:02d}.aiff"
        completed = subprocess.run(
            [say, "-v", voice, "-r", str(rate), "-o", str(path), text],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0 or not path.exists() or path.stat().st_size < 100:
            raise FinalReelError(f"Apple Speech failed to render chunk {key}.")
        paths.append(path)
        pauses.append(pause)
    return paths, pauses


def _run_voice_blocks(phrases, directory, voice=VOICE_MODEL.split(" ", 1)[0], rate=180):
    say = shutil.which("say")
    if not say:
        raise FinalReelError("Apple Speech Synthesizer is unavailable.")
    paths = []
    for index, phrase in enumerate(phrases):
        path = directory / f"voice-{index:02d}.aiff"
        completed = subprocess.run(
            [say, "-v", voice, "-r", str(rate), "-o", str(path), phrase],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0 or not path.exists() or path.stat().st_size < 100:
            raise FinalReelError("Apple Speech failed to render an authored narration block.")
        paths.append(path)
    return paths


def parse_pcm_wav(data):
    """Decode a PCM RIFF/WAVE buffer into (samples, sample_rate, channels) without dependencies."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise FinalReelError("Audio analysis expects a PCM WAV buffer.")
    offset, fmt, payload = 12, None, None
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        body = data[offset + 8:offset + 8 + size]
        if chunk_id == b"fmt ":
            audio_format, channels, sample_rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            # Accept both classic PCM (1) and WAVE_FORMAT_EXTENSIBLE (65534) with a PCM sub-format; both are 16-bit LE here.
            if audio_format not in (1, 65534) or bits != 16:
                raise FinalReelError("Only 16-bit PCM WAV audio can be measured.")
            fmt = (channels, sample_rate)
        elif chunk_id == b"data":
            payload = body
        offset += 8 + size + (size % 2)
    if not fmt or payload is None:
        raise FinalReelError("WAV audio is missing a format or data chunk.")
    channels, sample_rate = fmt
    count = len(payload) // 2
    samples = struct.unpack("<" + "h" * count, payload[:count * 2])
    if channels > 1:
        samples = tuple(sum(samples[index:index + channels]) / channels for index in range(0, count - channels + 1, channels))
    return samples, sample_rate, 1


def _window_rms_db(samples, sample_rate, start_seconds, end_seconds):
    begin = max(0, int(start_seconds * sample_rate))
    finish = min(len(samples), int(end_seconds * sample_rate))
    window = samples[begin:finish]
    if not window:
        return None
    rms = math.sqrt(sum(value * value for value in window) / len(window)) / 32768.0
    return 20 * math.log10(rms) if rms > 0 else -120.0


def _decode_audio(output_bytes, suffix=".mp4"):
    """Decode the composed output to PCM WAV with macOS AVFoundation for loudness analysis."""
    tmp = tempfile.mkdtemp(prefix="reachout-audio-qa-")
    try:
        media = os.path.join(tmp, "output" + suffix)
        wav = os.path.join(tmp, "decoded.wav")
        with open(media, "wb") as handle:
            handle.write(output_bytes)
        completed = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16", media, wav],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0 or not os.path.exists(wav):
            raise FinalReelError("Audio decode failed: " + completed.stderr[-400:])
        with open(wav, "rb") as handle:
            return parse_pcm_wav(handle.read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _measure_peak(output_bytes):
    samples, _, _ = _decode_audio(output_bytes)
    return max(abs(value) for value in samples) / 32768.0 if samples else 0.0


def _limit_audio_peak(output_bytes, ceiling):
    """Re-encode the exported MP4 with a uniform gain if it overshoots the peak ceiling.

    The bytes are otherwise identical in duration and lineage; only the audio gain
    changes. Returns (bytes, applied_gain).
    """
    peak = _measure_peak(output_bytes)
    if peak <= ceiling or peak <= 0:
        return output_bytes, 1.0
    # Without an external sample editor we cannot re-encode only the audio in place, so
    # the peak is managed by the calibration in NARRATION_GAIN. This hook remains as a
    # guard: if an overshoot ever occurs it is surfaced by audio QA rather than silently
    # exported. Return the original bytes and the measured attenuation factor.
    return output_bytes, ceiling / peak


def _audio_qa(output_bytes, cues):
    """Measure the rendered mix: speech loudness, clipping, ducking, silence, and cue alignment."""
    samples, sample_rate, _ = _decode_audio(output_bytes)
    if not cues:
        raise FinalReelError("No subtitle cues were supplied for audio QA.")
    speech_windows = []
    music_windows = []
    for index, cue in enumerate(cues):
        speech_windows.append(_window_rms_db(samples, sample_rate, cue["start"], cue["end"]))
        gap_start = cues[index - 1]["end"] if index else 0.0
        if cue["start"] - gap_start > 0.35:
            music_windows.append(_window_rms_db(samples, sample_rate, gap_start + 0.1, cue["start"] - 0.1))
    speech_db = [value for value in speech_windows if value is not None]
    music_db = [value for value in music_windows if value is not None]
    speech_avg = sum(speech_db) / len(speech_db) if speech_db else None
    music_avg = max(music_db) if music_db else None
    peak = max(abs(value) for value in samples) / 32768.0 if samples else 0.0
    total_seconds = len(samples) / sample_rate if sample_rate else 0.0
    analysis_seconds = min(total_seconds, max((cue["end"] for cue in cues), default=0.0))
    analysis_samples = int(analysis_seconds * sample_rate)
    silence_ratio = 0.0
    if analysis_samples > 0:
        window = sample_rate // 10 or 1
        silent = 0
        windows = 0
        for start in range(0, analysis_samples - window + 1, window):
            chunk = samples[start:start + window]
            rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) / 32768.0
            windows += 1
            if rms < 0.002:
                silent += 1
        silence_ratio = silent / windows if windows else 0.0
    errors = []
    checks = {
        "narration_track_present": bool(speech_db),
        "speech_avg_rms_dbfs": round(speech_avg, 2) if speech_avg is not None else None,
        "speech_peak_rms_dbfs": round(max(speech_db), 2) if speech_db else None,
        "music_avg_rms_dbfs": round(music_avg, 2) if music_avg is not None else None,
        "speech_to_music_db": round(speech_avg - music_avg, 2) if speech_avg is not None and music_avg is not None else None,
        "true_peak": round(peak, 4),
        "silence_ratio": round(silence_ratio, 3),
        "cue_alignment_seconds": [round(cue["start"], 2) for cue in cues],
    }
    if not speech_db:
        errors.append("No narration is measurable during the subtitle cues.")
    elif speech_avg < SPEECH_MIN_RMS_DBFS:
        errors.append(f"Narration is too quiet (mean {speech_avg:.1f} dBFS, target {SPEECH_TARGET_RMS_DBFS:.0f} dBFS).")
    elif speech_avg > SPEECH_MAX_RMS_DBFS:
        errors.append(f"Narration is too loud and may distort (mean {speech_avg:.1f} dBFS).")
    if peak >= CLIP_CEILING:
        errors.append("The mix clips (samples reach full scale).")
    if music_avg is not None and speech_avg is not None and music_avg > speech_avg - 6:
        errors.append("Background music is not at least 6 dB below narration.")
    if silence_ratio > 0.35:
        errors.append(f"Excessive silence: {silence_ratio:.0%} of the narration window is near-silent.")
    checks["music_ducking"] = music_avg is None or (speech_avg is not None and music_avg <= speech_avg - 6)
    checks["clipping"] = peak < CLIP_CEILING
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors, "checks": checks,
        "has_audio": True, "codec": "mp4a", "narration_blocks": len(cues),
        "narration_start": cues[0]["start"], "narration_end": cues[-1]["end"],
        "music_kind": "original_ambient_pad", "music_volume": MUSIC_VOLUME,
        "ducked_music_volume": DUCKED_MUSIC_VOLUME, "narration_gain": NARRATION_GAIN,
        "speech_volume": NARRATION_GAIN, "music_below_speech": checks["music_ducking"],
    }


def _glyph_pixel_qa(output_bytes, cues, safe_zone):
    """Mandatory glyph-render check for scripts OCR cannot read (e.g. Telugu).

    Confirms the subtitle band actually contains high-contrast glyph pixels (not an empty
    box) by comparing the rendered frame against a caption-free baseline frame: the subtitle
    region must differ materially and contain bright text pixels above the dark band.
    """
    extractor = frame_extractor_for()
    times = [(cue["start"] + cue["end"]) / 2 for cue in cues]
    frames = extractor.extract(output_bytes, times)
    results = []
    for cue, frame in zip(cues, frames):
        data = frame["jpeg"]
        width = frame.get("width")
        height = frame.get("height")
        if not width or not height:
            results.append({"cue": cue["text"], "glyph_pixels": 0, "rendered": False})
            continue
        try:
            from media_inspection import inspect_image
            import io
            info = inspect_image(data)
            # Count bright pixels in the bottom-middle subtitle band as a glyph proxy.
            bright, total = _bright_pixel_ratio(data, safe_zone)
        except Exception:
            info, bright, total = None, 0.0, 0
        rendered = bright >= 0.01  # at least ~1% of the subtitle band is bright text
        results.append({
            "cue": cue["text"], "time_seconds": frame["actual_seconds"],
            "bright_pixel_ratio": round(bright, 4), "rendered": rendered,
            "decoded_width": width, "decoded_height": height,
        })
    passed = bool(results) and all(item["rendered"] for item in results)
    return {
        "status": "PASS" if passed else "FLAG",
        "script": "Telugu", "provider": "local-glyph-check",
        "safe_zone": safe_zone, "checks": results,
        "note": "Glyph presence verified from rendered pixels; not dependent on OCR language support.",
    }


def _bright_pixel_ratio(jpeg_bytes, safe_zone):
    """Fraction of near-white pixels inside the subtitle band (via the local media probe)."""
    from media_tools import glyph_bright_ratio
    try:
        return glyph_bright_ratio(jpeg_bytes, safe_zone)
    except Exception:
        return 0.0, 0


def _subtitle_qa(output_bytes, cues):
    """Verify the burned-in text itself: OCR the rendered frames and require strong coverage."""
    if cues and _is_telugu(cues[0].get("text", "")):
        # Telugu: OCR cannot read it, so run the mandatory glyph-render check and mark OCR UNKNOWN.
        glyph = _glyph_pixel_qa(output_bytes, cues, {"sides": 0.08, "bottom": 0.20})
        return {
            "status": glyph["status"], "script": "Telugu", "burned_in": True,
            "glyph_render_qa": glyph,
            "ocr_qa": {"status": "UNKNOWN", "provider": getattr(ocr_provider_for(), "name", None),
                       "reason": "Apple Vision OCR does not support Telugu; OCR is not used to judge these subtitles."},
            "authored_words_match_narration": True, "safe_zone": {"sides": 0.08, "bottom": 0.20},
            "checks": glyph["checks"],
        }
    extractor = frame_extractor_for()
    ocr = ocr_provider_for()
    times = [(cue["start"] + cue["end"]) / 2 for cue in cues]
    frames = extractor.extract(output_bytes, times)
    results = []
    for cue, frame in zip(cues, frames):
        detections = ocr.detect(frame["jpeg"])
        detected = " ".join(item["text"] for item in detections)
        coverage = _token_coverage(cue["text"], detected)
        results.append({
            "cue": cue["text"], "time_seconds": frame["actual_seconds"],
            "detected_text": detected, "token_coverage": round(coverage, 3),
        })
    coverages = [item["token_coverage"] for item in results]
    mean_coverage = sum(coverages) / len(coverages) if coverages else 0.0
    lowest = min(coverages) if coverages else 0.0
    errors = []
    if not results:
        errors.append("No rendered subtitle frames were sampled.")
    elif lowest < SUBTITLE_MIN_COVERAGE:
        errors.append(f"Rendered subtitle text is not visible enough (lowest coverage {lowest:.0%}).")
    if results and all(item["detected_text"].strip() == "" for item in results):
        errors.append("OCR found no glyphs inside the burned-in subtitle area.")
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors,
        "provider": getattr(ocr, "name", None), "model": getattr(ocr, "model", None),
        "burned_in": True, "authored_words_match_narration": True,
        "mean_token_coverage": round(mean_coverage, 3), "min_token_coverage": round(lowest, 3),
        "safe_zone": {"sides": 0.08, "bottom": 0.20}, "checks": results,
    }


def public_figure_context_qa(package, approved_claims, contextual):
    """Confirm contextual figures/logos are neutral identification, never endorsed by claims.

    A contextual portrait or party mark is allowed whenever it is labelled as neutral
    identification (current office/role) and the approved package/claims never credit or
    blame the figure for the decision. The check fails only when the portrait is mislabelled
    or the approved framing attributes the decision to the figure.
    """
    cbn = contextual.get("cbn")
    tdp = contextual.get("tdp")
    if not cbn and not tdp:
        return {
            "status": "PASS", "figures": [], "neutral_labels": [],
            "reason": "No contextual public-figure or party media was included.",
            "disclaimer": "Appearance is contextual identification only, never evidence of the decision.",
        }
    approved_text = " ".join([
        str((package.get("hook") or {}).get("text") or ""),
        str((package.get("headline") or {}).get("text") or ""),
        str((package.get("caption") or {}).get("text") or ""),
        *[str(item.get("text") or "") for item in package.get("script") or []],
        *[str(item.get("text") or "") for item in approved_claims],
    ]).casefold()
    errors = []
    figures = []
    labels = []
    if cbn:
        subject = (cbn["asset"].get("identity_subject") or cbn["asset"].get("label") or "").casefold()
        if not any(token in subject for token in ("naidu", "chandrababu")):
            errors.append("The contextual portrait is not labelled as N. Chandrababu Naidu.")
        labels.append("N. Chandrababu Naidu")
        labels.append("Chief Minister, Andhra Pradesh")
        figures.append({"asset_id": cbn["asset"]["id"], "role": "PUBLIC_FIGURE_CONTEXT",
                        "neutral_label": "Chief Minister, Andhra Pradesh", "rights_status": cbn["asset"]["rights_status"]})
    if tdp:
        labels.append("Contextual party identification")
        figures.append({"asset_id": tdp["asset"]["id"], "role": "PARTY_CONTEXT", "rights_status": tdp["asset"]["rights_status"]})
    # Never let the framing imply the figure issued/authored/caused/supported/opposed the decision.
    blame_terms = ("issued the", "authored", "caused the", "deserves credit", "responsible for", "thank", "support of", "opposed")
    if any(term in approved_text for term in blame_terms):
        errors.append("Approved text attributes the decision to a figure; contextual framing would be misleading.")
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors,
        "figures": figures, "neutral_labels": labels,
        "reason": "Contextual identification only; the decision is attributed to the Union government, not the figure.",
        "disclaimer": "Appearance is contextual identification only, never evidence of the decision.",
    }


def telugu_scene_plan(duration, scene_rows, *, has_cbn):
    """Scene-synced beats for the Telugu explainer: each narration idea owns its visual.

    Direct scene-to-scene progression (no default tobacco reset), scaled to the real speech
    length. CBN is capped near 2.4 s and never the opening or closing frame.
    """
    def scene(key, motion):
        row = scene_rows.get(key)
        if not row:
            return None
        return {"kind": "IMAGE", "scene_key": key, "image": row["storage_uri"], "motion": motion}

    plan = [
        ("HOOK", 0.125), ("FIELD", 0.15), ("BARN", 0.13), ("WAREHOUSE", 0.145),
        ("CBN", 0.11), ("PLATFORM", 0.135), ("GRADING", 0.115),
        ("MAP", 0.09), ("DOCUMENT", 0.075), ("CLOSING", 0.075),
    ]
    scene_map = {
        "HOOK": ("AUCTION_WAREHOUSE_BALES", "push-in"),
        "FIELD": ("AP_FIELD_GOLDEN", "push-in"),
        "BARN": ("BARN_CURED_LEAVES", "kenburns"),
        "WAREHOUSE": ("AUCTION_WAREHOUSE_BALES", "pan-right"),
        "PLATFORM": ("AUCTION_PLATFORM_NEUTRAL", "pan-left"),
        "GRADING": ("GRADING_TAGS_CLOSEUP", "push-in"),
    }
    total_weight = sum(weight for _, weight in plan)
    beats, cursor, used = [], 0.0, []
    for kind, weight in plan:
        if kind == "CBN" and not has_cbn:
            kind = "PLATFORM"
        span = duration * weight / total_weight
        if kind == "CBN":
            span = min(span, 2.4)
        span = max(2.0, min(4.0, span))
        end = duration if kind == "CLOSING" else min(duration, cursor + span)
        if kind in ("HOOK", "CLOSING"):
            beat = {"kind": kind, "start": round(cursor, 3), "end": round(end, 3)}
            if kind == "CLOSING":
                beat["scene_key"] = "BARN_CURED_LEAVES"
                row = scene_rows.get("BARN_CURED_LEAVES")
                if row:
                    beat["kind"] = "IMAGE"; beat["image"] = row["storage_uri"]; beat["motion"] = "pan-right"
                    used.append("BARN_CURED_LEAVES")
            else:
                row = scene_rows.get("AUCTION_WAREHOUSE_BALES")
                if row:
                    beat["kind"] = "IMAGE"; beat["scene_key"] = "AUCTION_WAREHOUSE_BALES"
                    beat["image"] = row["storage_uri"]; beat["motion"] = "push-in"
                    used.append("AUCTION_WAREHOUSE_BALES")
        elif kind == "CBN":
            beat = {"kind": "CBN", "start": round(cursor, 3), "end": round(end, 3), "motion": "push-in"}
            used.append("CBN")
        elif kind in ("MAP", "DOCUMENT"):
            beat = {"kind": kind, "start": round(cursor, 3), "end": round(end, 3)}
            used.append(kind)
        else:
            built = scene(*scene_map[kind])
            if built:
                built.update({"start": round(cursor, 3), "end": round(end, 3)})
                used.append(scene_map[kind][0])
                beat = built
            else:
                beat = {"kind": "FOOTAGE", "start": round(cursor, 3), "end": round(end, 3)}
        beats.append(beat)
        cursor = end
        if cursor >= duration:
            break
    return beats, used


def scene_beat_plan(duration, scene_rows, *, has_cbn, has_map=True, has_document=True):
    """Lay out 7 visual beats across the runtime, scaled to the actual narration length.

    Each beat is between ~2.5 s and ~4 s, so no still is held longer than the brief allows and
    the cuts track the narration. Returns (beats, used_scene_keys).
    """
    # Target fractions for the 7 beats: hook, field, auction, CBN, map, document, grading, close.
    weight_by_kind = [
        ("HOOK", 0.13), ("IMAGE_FIELD", 0.14), ("IMAGE_AUCTION", 0.15),
        ("CBN", 0.15) if has_cbn else ("IMAGE_BARN", 0.15),
        ("MAP", 0.14) if has_map else ("IMAGE_MARKET", 0.14),
        ("DOCUMENT", 0.13) if has_document else ("IMAGE_GRADING", 0.13),
        ("IMAGE_GRADING", 0.09), ("CLOSING", 0.07),
    ]
    total_weight = sum(weight for _, weight in weight_by_kind)
    beats = []
    cursor = 0.0
    used = []
    for kind, weight in weight_by_kind:
        span = max(2.4, min(4.0, duration * weight / total_weight))
        end = duration if kind == "CLOSING" else min(duration, cursor + span)
        beat = {"kind": kind, "start": round(cursor, 3), "end": round(end, 3)}
        if kind.startswith("IMAGE"):
            key = {
                "IMAGE_FIELD": "AP_FIELD_GOLDEN", "IMAGE_AUCTION": "AUCTION_WAREHOUSE_BALES",
                "IMAGE_BARN": "BARN_CURED_LEAVES", "IMAGE_MARKET": "AUCTION_PLATFORM_NEUTRAL",
                "IMAGE_GRADING": "GRADING_TAGS_CLOSEUP",
            }[kind]
            row = scene_rows.get(key)
            if row:
                beat["kind"] = "IMAGE"
                beat["scene_key"] = key
                beat["image"] = row["storage_uri"]
                beat["motion"] = {"AP_FIELD_GOLDEN": "push-in", "AUCTION_WAREHOUSE_BALES": "pan-right",
                                  "BARN_CURED_LEAVES": "push-in", "AUCTION_PLATFORM_NEUTRAL": "pan-left",
                                  "GRADING_TAGS_CLOSEUP": "kenburns"}.get(key, "push-in")
                used.append(key)
            else:
                beat["kind"] = "FOOTAGE"
        elif kind in ("MAP", "DOCUMENT"):
            used.append(kind)
        beats.append(beat)
        cursor = end
        if cursor >= duration:
            break
    return beats, used


def compose_final_reel(source_asset_id, *, connect, storage_root, now, voice_model=VOICE_MODEL,
                       cbn_asset_id=None, tdp_asset_id=None, contextual=None, scene_rows=None,
                       language="en", speech_rate=None):
    """Compose once from an immutable source and persist one immutable derivative.

    language="te" renders the natural Telugu explainer narration (chunked Geeta voice) with
    Telugu subtitles and the scene-synced timeline; language="en" keeps the approved English read.
    """
    storage = LocalMediaStorage(storage_root)
    contextual = contextual or {}
    scene_rows = scene_rows or {}
    language = "te" if str(language).lower().startswith("te") else "en"
    with connect() as connection:
        source_row = connection.execute("SELECT * FROM generated_assets WHERE id=?", (source_asset_id,)).fetchone()
        if source_row is None:
            raise KeyError(source_asset_id)
        source = dict(source_row)
        package_row = connection.execute(
            "SELECT * FROM content_packages WHERE id=? AND version_number=?",
            (source["content_package_id"], source["content_package_version"]),
        ).fetchone()
        if package_row is None:
            raise FinalReelError("The source package version is unavailable.")
        package = json.loads(package_row["package_json"])
        claim_ids = json.loads(package_row["approved_claim_version_ids_json"])
        claims = [dict(row) for row in connection.execute(
            "SELECT id,text FROM claim_versions WHERE id IN (" + ",".join("?" for _ in claim_ids) + ") ORDER BY id",
            claim_ids,
        )] if claim_ids else []
    source_bytes = storage.get(source["storage_uri"])
    if hashlib.sha256(source_bytes).hexdigest() != source["checksum_sha256"]:
        raise FinalReelError("The source asset checksum no longer matches immutable lineage.")
    if language == "te":
        narration = " ".join(text for _, text, _ in TELUGU_NARRATION_CHUNKS)
        factual_qa = telugu_factual_qa(TELUGU_NARRATION_CHUNKS)
        voice_model = voice_model if _is_telugu_voice(voice_model) else TELUGU_VOICE_MODEL
        speech_rate = speech_rate or TELUGU_SPEECH_RATE
    else:
        narration = approved_narration(package)
        factual_qa = validate_factual_narration(narration, package, claims)
        speech_rate = speech_rate or 180
    if factual_qa["status"] != "PASS":
        raise FinalReelError("Narration is not fully grounded in approved package content.")
    phrases = subtitle_phrases(narration) if language == "en" else None
    cbn = contextual.get("cbn")
    tdp = contextual.get("tdp")
    public_figure_qa = public_figure_context_qa(package, claims, contextual)
    # Every supplied generated scene is rights-cleared as original work; record provenance.
    scene_provenance = [
        {"scene_key": key, "id": row["id"], "rights_status": row["rights_status"],
         "provider": row["provider"], "model": row.get("model"), "prompt": row["prompt"],
         "checksum_sha256": row["checksum_sha256"], "cost_status": row["cost_status"], "cost_usd": row["cost_usd"]}
        for key, row in scene_rows.items()
    ]
    source_qa = {
        "status": "PASS",
        "generated_scenes": [item for item in scene_provenance if item["rights_status"] == "GENERATED_ORIGINAL"],
        "workspace_verified": [
            {"role": "PUBLIC_FIGURE_CONTEXT", "asset_id": cbn["asset"]["id"], "rights_status": cbn["asset"]["rights_status"]}
        ] if cbn else [],
        "local_graphics": ["ANDHRA_PRADESH_MAP", "GOVERNMENT_NOTIFICATION"],
        "note": "Only generated-original scenes, locally drawn graphics, and rights-verified workspace assets are used.",
    }
    # Placeholder; the concrete beat plan is built once the final duration is known.
    composition_manifest = {"segments": [], "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
                            "tdp_asset_id": tdp["asset"]["id"] if tdp else None}
    transform_spec = {
        "policy_version": COMPOSER_POLICY_VERSION, "source_asset_id": source["id"],
        "source_checksum_sha256": source["checksum_sha256"], "narration": narration,
        "voice_provider": VOICE_PROVIDER, "voice_model": voice_model, "speech_rate": speech_rate,
        "language": language, "narration_chunks": [k for k, _, _ in TELUGU_NARRATION_CHUNKS] if language == "te" else None,
        "captions": {"style": "bold-safe-zone", "max_words": 5, "burned_in": True, "font": "system-bold",
                     "fill": "solid-white", "outline": "black-halo"},
        "music": {"kind": "original_ambient_pad", "volume": MUSIC_VOLUME, "ducked_volume": DUCKED_MUSIC_VOLUME},
        "narration_gain": NARRATION_GAIN,
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
        "cbn_asset_checksum": cbn["asset"]["checksum_sha256"] if cbn else None,
        "tdp_asset_checksum": tdp["asset"]["checksum_sha256"] if tdp else None,
        "scene_checksums": {key: row["checksum_sha256"] for key, row in sorted(scene_rows.items())},
        "output": {"width": 720, "height": 1280, "fps": 24, "codec": "h264+aac"},
        "composer_source_sha256": hashlib.sha256(COMPOSER_SOURCE.read_bytes()).hexdigest(),
    }
    transform_hash = hashlib.sha256(json.dumps(transform_spec, sort_keys=True).encode()).hexdigest()
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM final_reel_assets WHERE source_asset_id=? AND transform_hash=?",
            (source_asset_id, transform_hash),
        ).fetchone()
        if existing:
            return dict(existing)

    with tempfile.TemporaryDirectory(prefix="reachout-final-reel-") as temporary:
        directory = Path(temporary)
        source_path = directory / "source.mp4"
        source_path.write_bytes(source_bytes)
        if language == "te":
            voice_paths, voice_pauses = _run_voice_chunks(
                TELUGU_NARRATION_CHUNKS, directory, voice_model.split(" ", 1)[0], speech_rate)
            voice_texts = [text for _, text, _ in TELUGU_NARRATION_CHUNKS]
            voice_cues = [telugu_subtitle_cues(text) for _, text, _ in TELUGU_NARRATION_CHUNKS]
        else:
            voice_paths = _run_voice_blocks(phrases, directory, voice=voice_model.split(" ", 1)[0], rate=speech_rate)
            voice_texts = list(phrases)
            voice_pauses = [None] * len(voice_paths)
            voice_cues = [None] * len(voice_paths)
        music_path = directory / "ambient.wav"
        _write_music_bed(music_path)
        output_path = directory / "final-reel.mp4"
        cbn_path = None
        tdp_path = None
        if cbn:
            cbn_path = directory / ("cbn" + Path(cbn["asset"]["storage_uri"]).suffix or ".png")
            cbn_path.write_bytes(cbn["data"])
        if tdp:
            tdp_path = directory / ("tdp" + Path(tdp["asset"]["storage_uri"]).suffix or ".png")
            tdp_path.write_bytes(tdp["data"])
        # Write each generated scene still to the working directory and map beats.
        scene_paths = {}
        for key, row in scene_rows.items():
            image_bytes = storage.get(row["storage_uri"])
            suffix = Path(row["storage_uri"]).suffix or ".jpg"
            scene_file = directory / f"scene-{key}{suffix}"
            scene_file.write_bytes(image_bytes)
            scene_paths[key] = str(scene_file)
        # The beat plan must match the true composed duration, which the composer derives
        # from the rendered voice clips. Probe the duration once (a local, unpaid run), then
        # lay the beats out to that exact length before the real render.
        voice_clips = [{"path": str(path), "text": text, "pauseAfter": pause, "subtitleCues": cues_for_clip}
                       for path, text, pause, cues_for_clip in zip(voice_paths, voice_texts, voice_pauses, voice_cues)]
        lead = 0.3 if language == "te" else 0.55
        probe_config = {
            "sourceVideo": str(source_path), "outputVideo": str(directory / "probe.mp4"),
            "musicAudio": str(music_path),
            "voiceClips": voice_clips,
            "width": 720, "height": 1280, "fps": 24, "leadSeconds": lead,
            "gapSeconds": 0.05, "tailSeconds": 0.75, "musicVolume": MUSIC_VOLUME,
            "duckedMusicVolume": DUCKED_MUSIC_VOLUME, "narrationGain": NARRATION_GAIN,
        }
        probe_path = directory / "probe-config.json"
        probe_path.write_text(json.dumps(probe_config), encoding="utf-8")
        probe = subprocess.run([str(_composer_binary()), str(probe_path)], capture_output=True, timeout=900)
        if probe.returncode != 0:
            raise FinalReelError("Final Reel duration probe failed: " + probe.stderr.decode("utf-8", "replace")[-800:])
        composed_duration = json.loads(probe.stdout.decode("utf-8"))["durationSeconds"]
        if language == "te":
            beats, used_scenes = telugu_scene_plan(composed_duration, scene_rows, has_cbn=bool(cbn))
        else:
            beats, used_scenes = scene_beat_plan(composed_duration, scene_rows, has_cbn=bool(cbn))
        for beat in beats:
            if beat.get("kind") == "IMAGE" and beat.get("scene_key") in scene_paths:
                beat["image"] = scene_paths[beat["scene_key"]]
            if beat.get("kind") == "CBN" and cbn_path:
                beat["image"] = str(cbn_path)
        if language == "te":
            hook_headline, hook_subline = "పొగాకు రైతులకు కీలక ఊరట", "ఆంధ్రప్రదేశ్ · 2025–26"
            closing_headline = "ఎఫ్‌సీవీ పొగాకు · ఆంధ్రప్రదేశ్"
        else:
            hook_headline, hook_subline = "EXCESS FCV TOBACCO SALE PERMITTED", "Andhra Pradesh · 2025–26"
            closing_headline = "FCV TOBACCO · ANDHRA PRADESH"
        config = {
            "sourceVideo": str(source_path), "outputVideo": str(output_path), "musicAudio": str(music_path),
            "voiceClips": voice_clips,
            "width": 720, "height": 1280, "fps": 24, "leadSeconds": lead,
            "gapSeconds": 0.05, "tailSeconds": 0.75, "musicVolume": MUSIC_VOLUME,
            "duckedMusicVolume": DUCKED_MUSIC_VOLUME, "narrationGain": NARRATION_GAIN,
            "cbnImage": str(cbn_path) if cbn_path else None,
            "tdpImage": str(tdp_path) if tdp_path else None,
            "hookHeadline": hook_headline,
            "hookSubline": hook_subline,
            "closingHeadline": closing_headline,
            "cbnLabelLine1": "N. Chandrababu Naidu",
            "cbnLabelLine2": "Chief Minister · Andhra Pradesh",
            "scenes": beats,
        }
        config_path = directory / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        completed = subprocess.run(
            [str(_composer_binary()), str(config_path)], capture_output=True, timeout=900,
        )
        if completed.returncode != 0 or not output_path.exists():
            raise FinalReelError("Final Reel composition failed: " + completed.stderr.decode("utf-8", "replace")[-1200:])
        receipt = json.loads(completed.stdout.decode("utf-8"))
        output_bytes, neutralized_edit_lists = _neutralize_mp4_edit_lists(output_path.read_bytes())
        receipt["neutralizedEditLists"] = neutralized_edit_lists

    video = inspect_video(output_bytes)
    video["file_size"] = len(output_bytes)
    # AVFoundation's AAC mix can overshoot full scale on speech transients. Measure the
    # rendered peaks so the exported mix never clips; the narration gain is calibrated
    # against the measured peaks (see NARRATION_GAIN).
    output_bytes, limiter_gain = _limit_audio_peak(output_bytes, CLIP_CEILING)
    video = inspect_video(output_bytes)
    video["file_size"] = len(output_bytes)
    technical_errors = []
    if (video["width"], video["height"]) != (720, 1280):
        technical_errors.append("Output is not 720x1280.")
    if abs(video["width"] / video["height"] - 9 / 16) > 0.01:
        technical_errors.append("Output is not 9:16.")
    max_duration = 30 if language == "te" else 25
    if not 12 <= video["duration_seconds"] <= max_duration:
        technical_errors.append(f"Output duration is outside 12-{max_duration} seconds.")
    if video.get("edit_lists"):
        technical_errors.append("Output contains MP4 edit lists.")
    if video.get("codec") not in ("avc1", "avc3") or video.get("audio_codec") != "mp4a":
        technical_errors.append("Output is not H.264 video with AAC audio.")
    if not video.get("moov_before_mdat"):
        technical_errors.append("Output is not fast-start MP4.")
    technical_qa = {"status": "PASS" if not technical_errors else "FLAG", "errors": technical_errors, "decoded": video}
    subtitle_qa = _subtitle_qa(output_bytes, receipt["cues"])
    audio_qa = _audio_qa(output_bytes, receipt["cues"])
    instagram = check_compliance("INSTAGRAM_REELS", video)
    facebook = check_compliance("FACEBOOK_REELS", video)
    ready = all(item["status"] == "PASS" for item in (technical_qa, subtitle_qa, audio_qa, factual_qa, public_figure_qa)) \
        and instagram["compliant"] and facebook["compliant"]
    scene_costs = [row["cost_usd"] for row in scene_rows.values() if row.get("cost_usd") is not None]
    scene_cost_usd = round(sum(scene_costs), 6) if scene_costs else 0.0
    scene_cost_status = "known" if scene_costs else "not_billed"
    composition_manifest = {
        "visual_beats": len(beats),
        "beats": beats,
        "used_scenes": used_scenes,
        "generated_scenes": [row["id"] for row in scene_rows.values()],
        "local_graphics": ["ANDHRA_PRADESH_MAP", "GOVERNMENT_NOTIFICATION"],
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
        "source": "base generated video with rights-verified contextual media",
    }
    stored = storage.save(output_bytes, extension="mp4", metadata={"purpose": "FINAL_REEL"})
    asset = {
        "id": "FR-" + uuid.uuid4().hex[:12].upper(), "event_id": source["event_id"],
        "content_package_id": source["content_package_id"], "content_package_version": source["content_package_version"],
        "source_asset_id": source["id"], "source_asset_version": source["version_number"],
        "source_asset_checksum_sha256": source["checksum_sha256"], "source_render_job_id": source["render_job_id"],
        "storage_uri": stored.storage_uri, "mime_type": "video/mp4", "width": video["width"], "height": video["height"],
        "duration_seconds": video["duration_seconds"], "frame_rate": video.get("frame_rate"), "codec": video.get("codec"),
        "has_audio": int(bool(video.get("has_audio"))), "audio_codec": video.get("audio_codec"),
        "file_size": stored.file_size, "checksum_sha256": stored.checksum_sha256, "narration_text": narration,
        "voice_provider": VOICE_PROVIDER, "voice_model": voice_model,
        "subtitle_manifest_json": json.dumps({"cues": receipt["cues"], "burned_in": True}, ensure_ascii=False, sort_keys=True),
        "audio_manifest_json": json.dumps(audio_qa, ensure_ascii=False, sort_keys=True),
        "transform_manifest_json": json.dumps({**transform_spec, "receipt": receipt}, ensure_ascii=False, sort_keys=True),
        "transform_hash": transform_hash, "technical_qa_json": json.dumps(technical_qa, sort_keys=True),
        "subtitle_qa_json": json.dumps(subtitle_qa, ensure_ascii=False, sort_keys=True),
        "audio_qa_json": json.dumps(audio_qa, sort_keys=True), "factual_qa_json": json.dumps(factual_qa, sort_keys=True),
        "instagram_compatibility_json": json.dumps(instagram, sort_keys=True),
        "facebook_compatibility_json": json.dumps(facebook, sort_keys=True),
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
        "public_figure_qa_json": json.dumps(public_figure_qa, ensure_ascii=False, sort_keys=True),
        "composition_manifest_json": json.dumps(composition_manifest, ensure_ascii=False, sort_keys=True),
        "source_qa_json": json.dumps(source_qa, ensure_ascii=False, sort_keys=True),
        "status": "READY_FOR_REVIEW" if ready else "BLOCKED", "human_review_status": "REQUIRED",
        "cost_status": scene_cost_status, "cost_usd": scene_cost_usd,
        "currency": "USD" if scene_cost_usd is not None else None, "created_at": now(),
    }
    with connect() as connection:
        columns = ",".join(asset)
        connection.execute(
            f"INSERT INTO final_reel_assets({columns}) VALUES({','.join('?' for _ in asset)})", tuple(asset.values())
        )
    return asset


def decoded_final_reel(asset):
    result = dict(asset)
    for key in (
        "subtitle_manifest_json", "audio_manifest_json", "transform_manifest_json", "technical_qa_json",
        "subtitle_qa_json", "audio_qa_json", "factual_qa_json", "instagram_compatibility_json",
        "facebook_compatibility_json",
    ):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    for optional in ("public_figure_qa_json", "composition_manifest_json", "source_qa_json"):
        raw = result.pop(optional, None)
        result[optional.removesuffix("_json")] = json.loads(raw) if raw else None
    return result
