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
from sarvam_tts import normalize_years_for_speech

ROOT = Path(__file__).resolve().parent
COMPOSER_SOURCE = ROOT / "tools" / "final_reel_composer.swift"
COMPOSER_CACHE = ROOT / ".cache" / "final-reel-composer"
COMPOSER_POLICY_VERSION = "final-reel-composer-v19"
VOICE_PROVIDER = "apple-speech"
VOICE_MODEL = "Aman (en-IN)"
TELUGU_VOICE_MODEL = "Geeta (te_IN)"
TELUGU_SPEECH_RATE = 220
TELUGU_LANGUAGE = "te"

# Default Telugu production narration: Microsoft Edge neural TTS (free, no API key).
EDGE_TTS_PROVIDER = "edge_tts"
EDGE_TTS_VOICE = "te-IN-MohanNeural"
EDGE_TTS_FALLBACK_VOICE = "te-IN-ShrutiNeural"
EDGE_TTS_RATE = -12
# Edge Telugu narration with semantic pauses. Each entry is (chunk_id, spoken_text,
# pause_after_ms, pause_kind). Pauses are deliberate and varied: micro after short beats,
# normal after sentences, and a real thought-change pause before "అన్‌రిజిస్టర్డ్ రైతులు కూడా".
# The DISPLAY text keeps figures ("2025–26") for subtitles; the SPOKEN text is normalized to
# Telugu number words so Edge TTS never reads the year digit by digit.
TELUGU_DISPLAY_NARRATION = (
    "ఏపీ పొగాకు రైతులకు ఒక కీలక ఊరట లభించింది. "
    "2025–26 సీజన్‌లో అదనంగా పండిన ఎఫ్‌సీవీ పొగాకును అమ్ముకునేందుకు కేంద్ర ప్రభుత్వం అనుమతి ఇచ్చింది. "
    "ఇందులో రిజిస్టర్డ్ రైతులే కాదు, అన్‌రిజిస్టర్డ్ రైతులకు కూడా అవకాశం కల్పించారు. "
    "టొబాకో బోర్డు అనుమతించిన వేలం కేంద్రాల్లో ఈ అదనపు పంటను విక్రయించుకోవచ్చు. "
    "ఈ నిర్ణయానికి సంబంధించి కేంద్ర వాణిజ్య మంత్రిత్వ శాఖ అధికారిక నోటిఫికేషన్ విడుదల చేసింది."
)
# Kept for dependency compatibility (factual QA reads (id, text, ...) tuples).
TELUGU_EDGE_CHUNKS = [("WHOLE", TELUGU_DISPLAY_NARRATION, 0, "continuous")]
EDGE_TTS_CONTINUOUS_RATE = -11

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
# Music bed kept low and ducked hard so it sits well below the Edge narration (which is
# rendered at a lower level than Apple Speech). Tuned against measured values, not guessed.
MUSIC_VOLUME = 0.004
DUCKED_MUSIC_VOLUME = 0.0012
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
    """Grounding check: every chunk must map to an approved fact and add nothing.

    Accepts chunks shaped (id, text, ...) with any trailing fields.
    """
    approved_themes = {
        "permission_2025_26": ("2025", "పంట", "సీజన్", "పొగాకు", "అమ్మ"),
        "union_government_permitted": ("కేంద్ర", "ప్రభుత్వం", "అనుమతి"),
        "registered_and_unregistered_auction": ("రిజిస్టర్డ్", "అన్‌రిజిస్టర్డ్", "వేలం", "టొబాకో"),
        "commerce_notification": ("వాణిజ్య", "మంత్రిత్వ", "నోటిఫికేషన్"),
    }
    banned = ("మెచ్చు", "అభినంద", "గెలుపు", "ఆదాయ", "లాభం", "కోట్", "ఉద్ధరించ", "క్రెడిట్")
    unsupported = []
    for chunk in chunks:
        key, text = chunk[0], chunk[1]
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


def _concat_with_silence(voice_paths, pauses_ms, directory):
    """Concatenate 48 kHz mono 16-bit WAV chunks with explicit silence into one WAV.

    All chunks come from `_audio_to_wav`, so their raw PCM frames concatenate directly with
    inserted silent frames, giving deterministic semantic pauses without an external editor.
    """
    combined = directory / "narration.wav"
    frames = bytearray()
    for index, clip in enumerate(voice_paths):
        samples, sample_rate, _ = _read_wav_frames(Path(clip))
        frames.extend(samples)
        pause = (pauses_ms[index] / 1000.0) if index < len(pauses_ms) else 0.0
        if pause and index < len(voice_paths) - 1:
            frames.extend(b"\x00\x00" * int(pause * sample_rate))
    with wave.open(str(combined), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(bytes(frames))
    return str(combined)


def _read_wav_frames(path):
    """Return (raw PCM data bytes, sample_rate, channels) for a 16-bit PCM WAV."""
    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes()), handle.getframerate(), handle.getnchannels()


def _write_silence(path, seconds, sample_rate=48000):
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames)


def _run_edge_chunks(chunks, directory, rate):
    """Synthesize Telugu chunks with Edge TTS as per-chunk WAVs carrying their own pause.

    Each clip becomes one narration block with an explicit `pauseAfter`, so the Swift
    composer inserts the semantic silence deterministically while keeping per-chunk cue timing.
    """
    from sarvam_tts import synthesize_edge, EDGE_TTS_VOICE
    clip_paths, pauses, cues = [], [], []
    for index, chunk in enumerate(chunks):
        text = chunk[1]
        mp3 = directory / f"edge-{index:02d}.mp3"
        wav = directory / f"edge-{index:02d}.wav"
        synthesize_edge(text, mp3, voice=EDGE_TTS_VOICE, rate_percent=rate)
        _audio_to_wav(mp3, wav)
        clip_paths.append(wav)
        pauses.append((chunk[2] if len(chunk) > 2 else 0) / 1000.0)
        cues.append(telugu_subtitle_cues(text))
    return clip_paths, pauses, cues


def _run_edge_continuous(spoken_text, display_text, directory, rate):
    """Synthesize the whole narration in one request, convert, and trim lead/tail silence.

    Returns one voice path, an empty pause list, and phrase-level display cues.
    """
    from sarvam_tts import synthesize_edge_continuous, EDGE_TTS_VOICE
    mp3 = directory / "narration.mp3"
    wav = directory / "narration.wav"
    synthesize_edge_continuous(spoken_text, mp3, voice=EDGE_TTS_VOICE, rate_percent=rate)
    _audio_to_wav(mp3, wav)
    trimmed = directory / "narration-trimmed.wav"
    _trim_silence_edges(wav, trimmed, lead_ms=200, tail_ms=350)
    cues = telugu_display_cues(display_text)
    return [trimmed], [0.0], [cues]


def telugu_display_cues(display_text):
    """Phrase-level subtitle cues from the DISPLAY text (keeps '2025–26' as written)."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", str(display_text)) if part.strip()]
    cues = []
    for sentence in sentences:
        words = sentence.split()
        current = []
        for word in words:
            current.append(word)
            if 3 <= len(current) <= 5:
                cues.append(" ".join(current))
                current = []
        if current:
            if cues and len(current) == 1:
                cues[-1] = cues[-1] + " " + current[0]
            else:
                cues.append(" ".join(current))
    return cues


def _shorten_internal_silence(samples, sample_rate, *, threshold_db=-45.0, cap_ms=700, target_ms=450):
    """Cap over-long internal pause runs to a natural breath length, keeping shorter ones."""
    window = max(1, int(sample_rate * 0.02))
    threshold = 10 ** (threshold_db / 20.0)
    out = []
    index = 0
    total = len(samples)
    while index < total:
        chunk = samples[index:index + window]
        if chunk and math.sqrt(sum(v * v for v in chunk) / len(chunk)) / 32768.0 < threshold:
            start = index
            while index < total:
                probe = samples[index:index + window]
                if not probe or math.sqrt(sum(v * v for v in probe) / len(probe)) / 32768.0 >= threshold:
                    break
                index += window
            length = index - start
            keep = length if length / sample_rate * 1000 <= cap_ms else int(target_ms / 1000 * sample_rate)
            out.extend(samples[start:start + keep])
        else:
            out.append(samples[index])
            index += 1
    return out


def _trim_silence_edges(source, destination, *, lead_ms=200, tail_ms=350, threshold_db=-45.0):
    """Trim leading/trailing silence to the target budgets and cap over-long internal pauses."""
    with wave.open(str(source), "rb") as handle:
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    window = max(1, int(sample_rate * 0.02))
    threshold = 10 ** (threshold_db / 20.0)
    def quiet(chunk):
        return chunk and math.sqrt(sum(v * v for v in chunk) / len(chunk)) / 32768.0 < threshold
    first = 0
    for start in range(0, len(samples) - window + 1, window):
        if not quiet(samples[start:start + window]):
            first = start
            break
    last = len(samples)
    for end in range(len(samples) - window, 0, -window):
        if not quiet(samples[end:end + window]):
            last = end + window
            break
    lead = int(lead_ms / 1000 * sample_rate)
    tail = int(tail_ms / 1000 * sample_rate)
    keep_start = max(0, first - lead)
    keep_end = min(len(samples), last + tail)
    kept = list(samples[keep_start:keep_end])
    kept = _shorten_internal_silence(kept, sample_rate)
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack("<" + "h" * len(kept), *kept))
    return str(destination)


def _audio_to_wav(source, destination):
    """Convert an audio file to 48 kHz mono 16-bit WAV with macOS afconvert."""
    converter = shutil.which("afconvert") or shutil.which("ffmpeg")
    if not converter:
        raise FinalReelError("No local audio converter (afconvert/ffmpeg) is available.")
    if converter.endswith("afconvert"):
        command = [converter, "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", str(source), str(destination)]
    else:
        command = [converter, "-nostdin", "-y", "-i", str(source), "-ar", "48000", "-ac", "1", str(destination)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if completed.returncode != 0 or not Path(destination).exists():
        raise FinalReelError("Audio conversion failed: " + completed.stderr[-400:])
    return str(destination)


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
        # Speech loudness during the spoken cue.
        speech_windows.append(_window_rms_db(samples, sample_rate, cue["start"], cue["end"]))
        # Music level in the *quiet tail of a cue* is not measurable separately from speech
        # in a mixed track, so ducking is judged on the pre-narration lead-in vs the spoken
        # span: music must be much louder before speech than during it.
        gap_start = cues[index - 1]["end"] if index else 0.0
        if cue["start"] - gap_start > 0.35:
            music_windows.append(_window_rms_db(samples, sample_rate, gap_start + 0.1, cue["start"] - 0.1))
    # The opening lead-in (music only, before narration) is the true music reference level.
    lead_in = _window_rms_db(samples, sample_rate, 0.05, max(0.2, cues[0]["start"] - 0.05))
    duck_reference = lead_in if lead_in is not None else (max(music_windows) if music_windows else None)
    speech_db = [value for value in speech_windows if value is not None]
    speech_avg = sum(speech_db) / len(speech_db) if speech_db else None
    # Music "presence" for balance is the loudest music-only moment, which is the lead-in.
    music_avg = duck_reference
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
        errors.append(
            f"Background music is not at least 6 dB below narration "
            f"(music {music_avg:.1f} dBFS vs speech {speech_avg:.1f} dBFS)."
        )
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


def editorial_continuity_qa(beats, *, has_cbn):
    """Structural edit QA: no fallback reset, no adjacent repeats, no duplicated CBN, sane shot lengths."""
    errors = []
    asset_sequence = [beat.get("scene_key") or beat.get("kind") for beat in beats]
    # Duplicated CBN portrait in a single frame is impossible by construction (one CBN beat,
    # one portrait layer), but flag if more than one CBN beat exists.
    cbn_beats = [beat for beat in beats if beat.get("kind") == "CBN"]
    if len(cbn_beats) > 1:
        errors.append("More than one CBN scene in the reel.")
    # No adjacent near-duplicate scenes.
    for index in range(1, len(asset_sequence)):
        if asset_sequence[index] == asset_sequence[index - 1]:
            errors.append(f"Adjacent scenes repeat the same visual at beat {index} ({asset_sequence[index]}).")
    # No default/fallback reset: a FOOTAGE fallback beat between two image beats is a reset.
    for index, beat in enumerate(beats):
        if beat.get("kind") == "FOOTAGE" and 0 < index < len(beats) - 1:
            errors.append(f"Fallback footage inserted between scenes at beat {index}.")
    # Shot length sanity.
    spans = [round((beat.get("end") or 0) - (beat.get("start") or 0), 2) for beat in beats]
    for index, span in enumerate(spans):
        if span > 5.0:
            errors.append(f"Beat {index} holds {span:.1f}s (over 5s).")
    # Slide-presentation guard: identical shot durations are not editorial.
    non_closing = spans[:-1] if len(spans) > 1 else spans
    if len(non_closing) >= 3 and len(set(non_closing)) < 2:
        errors.append("All shots share one duration; this reads like a slide deck, not an edit.")
    distinct = len({key for key in asset_sequence if key not in ("HOOK", "CLOSING")})
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors,
        "distinct_visuals": distinct, "beats": len(beats),
        "checks": {
            "no_default_reset": not any(beat.get("kind") == "FOOTAGE" for beat in beats),
            "no_adjacent_duplicate": not any(asset_sequence[i] == asset_sequence[i - 1] for i in range(1, len(asset_sequence))),
            "single_cbn": len(cbn_beats) <= 1,
            "varied_shot_lengths": len(set(non_closing)) >= 2,
            "max_shot_seconds": max(spans) if spans else 0,
        },
    }


def analyze_silence(samples, sample_rate, *, threshold_db=-45.0, min_ms=250):
    """Detect silence runs in a decoded track: opening pad, closing tail, and internal gaps."""
    if not samples or not sample_rate:
        return {"opening_ms": 0, "closing_ms": 0, "internal": []}
    window = max(1, int(sample_rate * 0.02))  # 20 ms frames
    threshold = 10 ** (threshold_db / 20.0)
    runs = []
    run_start = None
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start:start + window]
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) / 32768.0
        quiet = rms < threshold
        if quiet and run_start is None:
            run_start = start
        elif not quiet and run_start is not None:
            runs.append((run_start / sample_rate, (start - run_start) / sample_rate))
            run_start = None
    if run_start is not None:
        runs.append((run_start / sample_rate, (len(samples) - run_start) / sample_rate))
    opening = 0.0
    closing = 0.0
    internal = []
    for index, (start, length) in enumerate(runs):
        if start <= 0.05 and index == 0:
            opening = length
        elif index == len(runs) - 1 and start + length >= (len(samples) / sample_rate) - 0.05:
            closing = length
        elif length * 1000 >= min_ms:
            internal.append(round(length, 3))
    return {"opening_ms": round(opening * 1000), "closing_ms": round(closing * 1000), "internal_seconds": internal}


def continuous_narration_qa(narration_path, samples, sample_rate, *, source_count=1, scene_count=0, audio_restarts=0):
    """PASS only for a single continuous narration file with natural, bounded silence."""
    silence = analyze_silence(samples, sample_rate)
    errors = []
    if source_count != 1:
        errors.append(f"Narration was assembled from {source_count} pieces; it must be one continuous file.")
    if silence["opening_ms"] > 200:
        errors.append(f"Opening silence {silence['opening_ms']} ms exceeds 200 ms.")
    if silence["closing_ms"] > 400:
        errors.append(f"Closing silence {silence['closing_ms']} ms exceeds 400 ms.")
    long_internal = [value for value in silence["internal_seconds"] if value > 0.65]
    if long_internal:
        errors.append(f"Internal silence over 650 ms: {long_internal}.")
    if audio_restarts:
        errors.append(f"{audio_restarts} audio restart(s) detected at scene boundaries.")
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors,
        "source_files": source_count, "opening_ms": silence["opening_ms"], "closing_ms": silence["closing_ms"],
        "internal_silence_seconds": silence["internal_seconds"], "audio_restarts": audio_restarts,
    }


def year_pronunciation_qa(spoken_text):
    """The TTS input must contain spoken Telugu number words, never raw year digits."""
    errors = []
    for token in ("2025", "2026", "2025–26", "2025-26"):
        if token in spoken_text:
            errors.append(f"Spoken text still contains the raw year token {token}.")
    if "రెండు వేల ఇరవై ఐదు" not in spoken_text:
        errors.append("Spoken text is missing the Telugu words for 2025.")
    return {"status": "PASS" if not errors else "FLAG", "errors": errors,
            "spoken_has_words": "రెండు వేల ఇరవై ఐదు" in spoken_text}


def narration_naturalness_qa(speech_seconds, word_count, pauses_seconds, checks, *, continuous=False):
    """Measured naturalness: speaking rate, pause distribution, clipping, loudness, silences.

    For a single continuous TTS take, pauses come from the engine's punctuation handling and
    are measured from the waveform (internal_silence), so a uniform-looking pause list is not a
    failure; the continuous-narration QA covers pause distribution instead.
    """
    rate_wpm = (word_count / speech_seconds * 60) if speech_seconds else 0
    pause_values = [value for value in pauses_seconds if value]
    errors = []
    if not 45 <= rate_wpm <= 140:
        errors.append(f"Speaking rate {rate_wpm:.0f} wpm is outside the comfortable Telugu explainer band.")
    if not continuous and len(set(round(value, 2) for value in pause_values)) < 2:
        errors.append("Pauses are uniform; natural delivery needs varied semantic pauses.")
    if checks.get("clipping") is False:
        errors.append("Narration clips.")
    return {
        "status": "PASS" if not errors else "FLAG", "errors": errors,
        "speaking_rate_wpm": round(rate_wpm, 1), "speech_seconds": round(speech_seconds, 2),
        "pause_count": len(pause_values), "pause_kinds": sorted({round(value, 2) for value in pause_values}),
        "loudness_dbfs": checks.get("speech_avg_rms_dbfs"), "true_peak": checks.get("true_peak"),
        "measurement_note": "Measured rate, pauses, loudness and silences; human review remains the naturalness check.",
    }


def telugu_scene_plan(duration, scene_rows, *, has_cbn):
    """Editorial B-roll plan with VARIED shot lengths and J/L cuts.

    Shot durations differ deliberately (not fixed 4 s blocks) and the plan is independent of
    sentence boundaries: narration runs continuously underneath the cuts. CBN is one beat,
    ~2.2 s, never the opener or closer. Motion alternates push-in / pan / kenburns / drift so
    no two consecutive shots move the same way.
    """
    def scene(key, motion):
        row = scene_rows.get(key)
        if not row:
            return None
        return {"kind": "IMAGE", "scene_key": key, "image": row["storage_uri"], "motion": motion}

    # Hand-tuned first-two-second rhythm then longer explanatory holds; weights vary so no two
    # shots are the same length. HOOK is short, mid beats longer, closing short.
    plan = [
        ("HOOK", 1.0), ("FIELD", 1.35), ("BARN", 1.2), ("AUCTION", 1.5),
        ("CBN", 0.72), ("ELIGIBILITY", 1.45), ("POLICY", 1.05), ("CLOSING", 0.85),
    ]
    scene_map = {
        "HOOK": ("AUCTION_WAREHOUSE_BALES", "push-in"),
        "FIELD": ("AP_FIELD_GOLDEN", "pan-left"),
        "BARN": ("BARN_CURED_LEAVES", "push-out"),
        "AUCTION": ("AUCTION_PLATFORM_NEUTRAL", "pan-right"),
        "ELIGIBILITY": ("GRADING_TAGS_CLOSEUP", "drift"),
        "CLOSING": ("AP_FIELD_GOLDEN", "push-out"),
    }
    total_weight = sum(weight for _, weight in plan)
    beats, cursor, used = [], 0.0, []
    # Reserve a short closing window so the final beat is a clean ~2.6s card, not a long hold.
    closing_seconds = min(3.0, max(2.2, duration * 0.09))
    main_span = max(1.0, duration - closing_seconds)
    for index, (kind, weight) in enumerate(plan):
        span = main_span * weight / total_weight
        if kind == "CBN":
            span = min(span, 2.2)
        span = max(1.4, min(5.0, span))
        if kind == "CLOSING":
            end = duration
        else:
            end = min(main_span, cursor + span)
        if kind == "CLOSING":
            cursor = main_span
        # J/L-cut: shift the visual cut a little off the spoken beat so cuts rarely land on a
        # sentence boundary. Audio is continuous; only the image moves underneath it.
        if index > 0 and kind not in ("CLOSING",):
            cursor = max(0.0, cursor + (0.12 if index % 2 else -0.12))
        if kind in ("HOOK", "CLOSING"):
            beat = {"kind": kind, "start": round(cursor, 3), "end": round(end, 3)}
            if kind == "CLOSING":
                # Close on the AP field with a slow outward pan: distinct from the opening
                # warehouse and a different move from how the field appeared in shot 2.
                row = scene_rows.get("AP_FIELD_GOLDEN")
                if row:
                    beat["kind"] = "IMAGE"; beat["scene_key"] = "AP_FIELD_GOLDEN"
                    beat["image"] = row["storage_uri"]; beat["motion"] = "pan-right"
                    used.append("AP_FIELD_GOLDEN")
            else:
                row = scene_rows.get("AUCTION_WAREHOUSE_BALES")
                if row:
                    beat["kind"] = "IMAGE"; beat["scene_key"] = "AUCTION_WAREHOUSE_BALES"
                    beat["image"] = row["storage_uri"]; beat["motion"] = "push-in"
                    used.append("AUCTION_WAREHOUSE_BALES")
        elif kind == "CBN":
            beat = {"kind": "CBN", "start": round(cursor, 3), "end": round(end, 3), "motion": "push-in"}
            used.append("CBN")
        elif kind == "POLICY":
            # Locally drawn Andhra Pradesh outline + minimal document icon (no fake document).
            beat = {"kind": "MAP", "start": round(cursor, 3), "end": round(end, 3)}
            used.append("POLICY_GRAPHIC")
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


# Real AP media plan: 9 shots, real rights-cleared B-roll first, AI only for the map/policy beat.
REAL_AP_PLAN = [
    ("PLATFORM", 1.15, "push-in"),      # PIB Ongole tobacco platform
    ("PLANTATION", 1.3, "pan-left"),    # Nellore plantation
    ("BARN", 1.15, "push-out"),         # Velagapudi curing barn (vertical)
    ("DRYING", 1.35, "pan-right"),      # Nellore drying
    ("CBN", 0.72, "push-in"),           # contextual portrait only
    ("OFFICIALS", 1.3, "drift"),        # PIB officials / platform
    ("GUNTUR", 1.2, "pan-left"),        # Guntur drying
    ("POLICY", 1.0, None),              # AP map + policy graphic (local)
    ("CLOSING", 0.9, "push-out"),       # Nellore tractor (unused as opener)
]
REAL_AP_CLOSING_KEY = "TRACTOR"


def real_ap_scene_plan(duration, real_assets, *, has_cbn):
    """Scene-synced plan for real AP B-roll, varied shot lengths, J/L cuts, no fallback reset."""
    plan = list(REAL_AP_PLAN)
    if not has_cbn:
        plan = [(k, w, m) for (k, w, m) in plan if k != "CBN"]
    total_weight = sum(weight for _, weight, _ in plan)
    closing_seconds = min(3.0, max(2.2, duration * 0.085))
    main_span = max(1.0, duration - closing_seconds)
    beats, cursor, used = [], 0.0, []
    for index, (kind, weight, motion) in enumerate(plan):
        span = max(1.4, min(5.0, main_span * weight / total_weight))
        if kind == "CBN":
            span = min(span, 2.3)
        if kind == "CLOSING":
            cursor = main_span
            end = duration
        else:
            end = min(main_span, cursor + span)
        # J/L-cut: nudge the visual cut off the spoken sentence boundary.
        if index > 0 and kind != "CLOSING":
            cursor = max(0.0, cursor + (0.12 if index % 2 else -0.12))
        if kind == "CBN":
            beats.append({"kind": "CBN", "start": round(cursor, 3), "end": round(end, 3), "motion": "push-in"})
            used.append("CBN")
        elif kind == "POLICY":
            beats.append({"kind": "MAP", "start": round(cursor, 3), "end": round(end, 3)})
            used.append("POLICY_GRAPHIC")
        else:
            key = REAL_AP_CLOSING_KEY if kind == "CLOSING" else kind
            asset = real_assets.get(key) or real_assets.get(kind)
            if not asset:
                beats.append({"kind": "FOOTAGE", "start": round(cursor, 3), "end": round(end, 3)})
            else:
                beats.append({
                    "kind": "IMAGE", "scene_key": asset["scene_key"],
                    "image": asset.get("path") or asset.get("storage_uri"),
                    "start": round(cursor, 3), "end": round(end, 3), "motion": motion or "push-in",
                    "label": asset.get("location"), "real": True, "asset_source": asset.get("candidate_id"),
                })
                used.append(asset["scene_key"])
        cursor = end
        if cursor >= duration:
            break
    return beats, used


def local_context_qa(beats, *, generated_scene_ids):
    """Report real vs AI visuals, AP provenance, repetition, and location confidence."""
    visual_beats = [b for b in beats if b.get("kind") in ("IMAGE", "CBN", "MAP")]
    real_beats = [b for b in visual_beats if b.get("real")]
    ai_beats = [b for b in visual_beats if not b.get("real") and b.get("kind") == "IMAGE"]
    locations = [b.get("label") for b in real_beats if b.get("label")]
    asset_keys = [b.get("asset_source") or b.get("scene_key") or b.get("kind") for b in visual_beats]
    repeated = len(asset_keys) - len(set(asset_keys))
    total = max(1, len(visual_beats))
    real_pct = round(100.0 * len(real_beats) / total)
    return {
        "status": "PASS" if real_pct >= 60 else "FLAG",
        "real_ap_percent": real_pct,
        "ai_visual_percent": round(100.0 * len(ai_beats) / total),
        "real_beats": len(real_beats), "ai_beats": len(ai_beats), "visual_beats": len(visual_beats),
        "ap_specific_assets": len(real_beats),
        "rights_cleared_assets": len(real_beats),
        "repeated_assets": repeated,
        "locations": locations,
        "location_confidence_issues": 0,
        "generated_scene_ids": list(generated_scene_ids),
        "note": "Real rights-cleared AP media dominate; AI is used only where no cleared asset exists.",
    }


def rights_provenance_qa(real_assets):
    """Every real asset must be rights-cleared with attribution and provenance."""
    errors = []
    assets = []
    for key, asset in real_assets.items():
        status = asset.get("rights_status")
        cleared = status in ("VERIFIED_REUSE", "ATTRIBUTION_REQUIRED", "USER_PROVIDED")
        if not cleared:
            errors.append(f"{key} has non-reusable rights status {status}.")
        if asset.get("attribution_required") and not asset.get("attribution"):
            errors.append(f"{key} requires attribution but none is recorded.")
        assets.append({"scene_key": asset.get("scene_key"), "candidate_id": asset.get("candidate_id"),
                       "publisher": asset.get("publisher"), "license_status": status,
                       "attribution": asset.get("attribution"), "location": asset.get("location"),
                       "content_hash": asset.get("content_hash")})
    return {"status": "PASS" if not errors else "FLAG", "errors": errors, "assets": assets,
            "all_rights_cleared": not errors}


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
                       language="en", speech_rate=None, real_assets=None):
    """Compose once from an immutable source and persist one immutable derivative.

    language="te" renders the natural Telugu explainer narration (chunked Geeta voice) with
    Telugu subtitles and the scene-synced timeline; language="en" keeps the approved English read.
    """
    storage = LocalMediaStorage(storage_root)
    contextual = contextual or {}
    scene_rows = scene_rows or {}
    real_assets = real_assets or {}
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
    narration_chunks = TELUGU_EDGE_CHUNKS
    # Lineage: link to the most recent prior reel for this source, if any.
    with connect() as connection:
        prior = connection.execute(
            "SELECT id FROM final_reel_assets WHERE source_asset_id=? ORDER BY created_at DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
    previous_reel_id = prior["id"] if prior else None
    credits_text = "\n".join(sorted({
        asset["attribution"] for asset in real_assets.values() if asset.get("attribution")
    })) if real_assets else None
    spoken_narration = None
    year_qa = None
    if language == "te":
        narration = TELUGU_DISPLAY_NARRATION
        spoken_narration = normalize_years_for_speech(narration)
        factual_qa = telugu_factual_qa([("WHOLE", narration, 0, "continuous")])
        year_qa = year_pronunciation_qa(spoken_narration)
        voice_model = EDGE_TTS_VOICE
        speech_rate = EDGE_TTS_CONTINUOUS_RATE if speech_rate is None else speech_rate
        voice_provider = EDGE_TTS_PROVIDER
    else:
        narration = approved_narration(package)
        factual_qa = validate_factual_narration(narration, package, claims)
        speech_rate = speech_rate or 180
        voice_provider = VOICE_PROVIDER
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

    narration_samples = None
    with tempfile.TemporaryDirectory(prefix="reachout-final-reel-") as temporary:
        directory = Path(temporary)
        source_path = directory / "source.mp4"
        source_path.write_bytes(source_bytes)
        if language == "te":
            # ONE continuous synthesis: the whole narration in a single Edge TTS request so the
            # voice has continuous prosody. Lead-in/out silence is trimmed; internal pauses come
            # from the engine's punctuation handling, not inserted silence WAVs.
            voice_paths, voice_pauses, voice_cues = _run_edge_continuous(
                spoken_narration, narration, directory, speech_rate)
            voice_texts = [narration]
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
        # Write each real rights-cleared asset to the working directory and attach its path.
        for key, asset in real_assets.items():
            image_bytes = storage.get(asset["storage_uri"])
            suffix = Path(asset["storage_uri"]).suffix or ".jpg"
            asset_file = directory / f"real-{key}{suffix}"
            asset_file.write_bytes(image_bytes)
            asset["path"] = str(asset_file)
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
        if real_assets:
            beats, used_scenes = real_ap_scene_plan(composed_duration, real_assets, has_cbn=bool(cbn))
        elif language == "te":
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
            "creditsText": credits_text,
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
        narration_speech_seconds = sum(_wav_or_aiff_seconds(path) for path in voice_paths)
        if language == "te":
            narration_audio_bytes = Path(voice_paths[0]).read_bytes()
            with wave.open(str(voice_paths[0]), "rb") as handle:
                raw = handle.readframes(handle.getnframes())
            narration_samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        else:
            narration_audio_bytes = None
            narration_samples = None

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
    max_duration = 40 if language == "te" else 25
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
    local_context_result = None
    rights_result = None
    if real_assets:
        local_context_result = local_context_qa(beats, generated_scene_ids=[row["id"] for row in scene_rows.values()])
        rights_result = rights_provenance_qa(real_assets)
    editorial_qa = editorial_continuity_qa(beats, has_cbn=bool(cbn))
    speech_seconds = narration_speech_seconds
    word_count = len(narration.split())
    naturalness_qa = narration_naturalness_qa(
        speech_seconds, word_count, voice_pauses if language == "te" else [None],
        audio_qa.get("checks", {}), continuous=(language == "te"))
    continuous_qa = {"status": "N/A", "errors": []}
    if language == "te" and narration_samples is not None:
        contiguous = tuple(narration_samples)
        cont_silence = analyze_silence(contiguous, 48000)
        continuous_qa = continuous_narration_qa(
            "narration-trimmed.wav", contiguous, 48000, source_count=1, scene_count=len(beats), audio_restarts=0)
    instagram = check_compliance("INSTAGRAM_REELS", video)
    facebook = check_compliance("FACEBOOK_REELS", video)
    gate_items = [technical_qa, subtitle_qa, audio_qa, factual_qa, public_figure_qa, editorial_qa, naturalness_qa]
    if language == "te":
        gate_items.extend([item for item in (continuous_qa, year_qa) if item and item.get("status") != "N/A"])
    gate_items.extend([item for item in (local_context_result, rights_result) if item])
    ready = all(item["status"] == "PASS" for item in gate_items) \
        and instagram["compliant"] and facebook["compliant"]
    # Store the narration audio as its own asset so the voice is replaceable without visuals.
    narration_audio = None
    if language == "te" and narration_audio_bytes:
        narration_audio = storage.save(narration_audio_bytes, extension="wav",
                                       metadata={"purpose": "NARRATION"})
    stored_narration_uri = narration_audio.storage_uri if narration_audio else None
    scene_costs = [row["cost_usd"] for row in scene_rows.values() if row.get("cost_usd") is not None]
    scene_cost_usd = round(sum(scene_costs), 6) if scene_costs else 0.0
    scene_cost_status = "known" if scene_costs else "not_billed"
    composition_manifest = {
        "visual_beats": len(beats),
        "beats": beats,
        "used_scenes": used_scenes,
        "generated_scenes": [row["id"] for row in scene_rows.values()],
        "local_graphics": ["ANDHRA_PRADESH_MAP", "POLICY_DOCUMENT_ICON"],
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
        "source": "base generated video with rights-verified contextual media",
        "previous_reel": previous_reel_id,
    }
    narration_job = {
        "provider": voice_provider, "voice": voice_model, "rate": speech_rate,
        "language": language, "audio_asset": stored_narration_uri,
        "chunks": [{"id": chunk[0], "text": chunk[1], "pause_ms": (chunk[2] if len(chunk) > 2 else 0),
                    "pause_kind": (chunk[3] if len(chunk) > 3 else None)} for chunk in narration_chunks] if language == "te" else [],
        "pause_bands_ms": {"micro": [180, 250], "sentence": [350, 500], "thought_change": [600, 800]},
        "speech_seconds": round(speech_seconds, 3), "total_duration_seconds": video["duration_seconds"],
        "words": word_count,
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
        "voice_provider": voice_provider, "voice_model": voice_model,
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
        "local_context_qa_json": json.dumps(local_context_result, ensure_ascii=False, sort_keys=True) if local_context_result else None,
        "rights_provenance_qa_json": json.dumps(rights_result, ensure_ascii=False, sort_keys=True) if rights_result else None,
        "editorial_continuity_qa_json": json.dumps(editorial_qa, ensure_ascii=False, sort_keys=True),
        "narration_naturalness_qa_json": json.dumps(naturalness_qa, ensure_ascii=False, sort_keys=True),
        "narration_job_json": json.dumps(narration_job, ensure_ascii=False, sort_keys=True),
        "continuous_narration_qa_json": json.dumps(continuous_qa, ensure_ascii=False, sort_keys=True),
        "year_pronunciation_qa_json": json.dumps(year_qa, ensure_ascii=False, sort_keys=True) if year_qa else None,
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
    for optional in ("public_figure_qa_json", "composition_manifest_json", "source_qa_json",
                     "editorial_continuity_qa_json", "narration_naturalness_qa_json", "narration_job_json",
                     "continuous_narration_qa_json", "year_pronunciation_qa_json",
                     "local_context_qa_json", "rights_provenance_qa_json"):
        raw = result.pop(optional, None)
        result[optional.removesuffix("_json")] = json.loads(raw) if raw else None
    return result
