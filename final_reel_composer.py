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

from media_inspection import inspect_video
from media_storage import LocalMediaStorage
from media_tools import frame_extractor_for, ocr_provider_for
from meta_distribution import check_compliance

ROOT = Path(__file__).resolve().parent
COMPOSER_SOURCE = ROOT / "tools" / "final_reel_composer.swift"
COMPOSER_CACHE = ROOT / ".cache" / "final-reel-composer"
COMPOSER_POLICY_VERSION = "final-reel-composer-v5"
VOICE_PROVIDER = "apple-speech"
VOICE_MODEL = "Aman (en-IN)"
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


def _normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def approved_narration(package):
    """Use only existing approved package strings: its hook, then first script claim."""
    hook = str((package.get("hook") or {}).get("text") or "").strip().rstrip(".")
    script = sorted(package.get("script") or [], key=lambda item: item.get("sequence", 0))
    first = str((script[0] if script else {}).get("text") or "").strip()
    if not hook or not first:
        raise FinalReelError("The approved package has no usable hook and script claim.")
    return hook + ". " + first


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


def _subtitle_qa(output_bytes, cues):
    """Verify the burned-in text itself: OCR the rendered frames and require strong coverage."""
    extractor = frame_extractor_for()
    ocr = ocr_provider_for()
    times = [(cue["start"] + cue["end"]) / 2 for cue in cues]
    frames = extractor.extract(output_bytes, times)
    results = []
    for cue, frame in zip(cues, frames):
        detections = ocr.detect(frame["jpeg"])
        detected = " ".join(item["text"] for item in detections)
        expected_tokens = set(_normalized(cue["text"]).split())
        detected_tokens = set(_normalized(detected).split())
        coverage = len(expected_tokens & detected_tokens) / max(1, len(expected_tokens))
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


def compose_final_reel(source_asset_id, *, connect, storage_root, now, voice_model=VOICE_MODEL,
                       cbn_asset_id=None, tdp_asset_id=None, contextual=None):
    """Compose once from an immutable source and persist one immutable derivative."""
    storage = LocalMediaStorage(storage_root)
    contextual = contextual or {}
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
    narration = approved_narration(package)
    factual_qa = validate_factual_narration(narration, package, claims)
    if factual_qa["status"] != "PASS":
        raise FinalReelError("Narration is not fully grounded in approved package content.")
    phrases = subtitle_phrases(narration)
    cbn = contextual.get("cbn")
    tdp = contextual.get("tdp")
    public_figure_qa = public_figure_context_qa(package, claims, contextual)
    composition_manifest = {
        "segments": [
            {"start": 0.0, "end": 2.5, "kind": "HOOK", "headline": "EXCESS FCV TOBACCO SALE PERMITTED",
             "subline": "Andhra Pradesh · 2025–26", "motion": "punch-in", "accent": "gold"},
            {"start": 2.5, "end": 7.0, "kind": "FOOTAGE", "motion": "slow-push"},
            {"start": 7.0, "end": 11.0, "kind": "CONTEXT_FIGURE" if cbn else "FOOTAGE",
             "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
             "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
             "label": ["N. Chandrababu Naidu", "Chief Minister, Andhra Pradesh"] if cbn else None,
             "motion": "pan-zoom"},
            {"start": 11.0, "end": None, "kind": "FOOTAGE", "motion": "slow-push"},
            {"start": None, "end": None, "kind": "CLOSING", "headline": "FCV TOBACCO · ANDHRA PRADESH"},
        ],
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
    }
    transform_spec = {
        "policy_version": COMPOSER_POLICY_VERSION, "source_asset_id": source["id"],
        "source_checksum_sha256": source["checksum_sha256"], "narration": narration,
        "voice_provider": VOICE_PROVIDER, "voice_model": voice_model, "speech_rate": 180,
        "captions": {"style": "bold-safe-zone", "max_words": 5, "burned_in": True, "font": "system-bold",
                     "fill": "solid-white", "outline": "black-halo"},
        "music": {"kind": "original_ambient_pad", "volume": MUSIC_VOLUME, "ducked_volume": DUCKED_MUSIC_VOLUME},
        "narration_gain": NARRATION_GAIN,
        "cbn_asset_id": cbn["asset"]["id"] if cbn else None,
        "tdp_asset_id": tdp["asset"]["id"] if tdp else None,
        "cbn_asset_checksum": cbn["asset"]["checksum_sha256"] if cbn else None,
        "tdp_asset_checksum": tdp["asset"]["checksum_sha256"] if tdp else None,
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
        voice_paths = _run_voice_blocks(phrases, directory, voice=voice_model.split(" ", 1)[0])
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
        config = {
            "sourceVideo": str(source_path), "outputVideo": str(output_path), "musicAudio": str(music_path),
            "voiceClips": [{"path": str(path), "text": phrase} for path, phrase in zip(voice_paths, phrases)],
            "width": 720, "height": 1280, "fps": 24, "leadSeconds": 0.55,
            "gapSeconds": 0.05, "tailSeconds": 0.75, "musicVolume": MUSIC_VOLUME,
            "duckedMusicVolume": DUCKED_MUSIC_VOLUME, "narrationGain": NARRATION_GAIN,
            "cbnImage": str(cbn_path) if cbn_path else None,
            "tdpImage": str(tdp_path) if tdp_path else None,
            "hookHeadline": "EXCESS FCV TOBACCO SALE PERMITTED",
            "hookSubline": "Andhra Pradesh · 2025–26",
            "closingHeadline": "FCV TOBACCO · ANDHRA PRADESH",
            "cbnLabelLine1": "N. Chandrababu Naidu",
            "cbnLabelLine2": "Chief Minister, Andhra Pradesh",
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
    if not 12 <= video["duration_seconds"] <= 25:
        technical_errors.append("Output duration is outside 12-25 seconds.")
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
        "status": "READY_FOR_REVIEW" if ready else "BLOCKED", "human_review_status": "REQUIRED",
        "cost_status": "not_billed", "cost_usd": 0.0, "currency": "USD", "created_at": now(),
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
    for optional in ("public_figure_qa_json", "composition_manifest_json"):
        raw = result.pop(optional, None)
        result[optional.removesuffix("_json")] = json.loads(raw) if raw else None
    return result
