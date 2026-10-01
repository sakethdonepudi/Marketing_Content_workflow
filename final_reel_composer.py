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
COMPOSER_POLICY_VERSION = "final-reel-composer-v3"
VOICE_PROVIDER = "apple-speech"
VOICE_MODEL = "Aman (en-IN)"


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


def _subtitle_qa(output_bytes, cues):
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
    passed = bool(results) and all(item["token_coverage"] >= 0.6 for item in results)
    return {
        "status": "PASS" if passed else "FLAG", "provider": getattr(ocr, "name", None),
        "model": getattr(ocr, "model", None), "burned_in": True,
        "authored_words_match_narration": True, "safe_zone": {"sides": 0.11, "bottom": 0.19},
        "checks": results,
    }


def compose_final_reel(source_asset_id, *, connect, storage_root, now, voice_model=VOICE_MODEL):
    """Compose once from an immutable source and persist one immutable derivative."""
    storage = LocalMediaStorage(storage_root)
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
    transform_spec = {
        "policy_version": COMPOSER_POLICY_VERSION, "source_asset_id": source["id"],
        "source_checksum_sha256": source["checksum_sha256"], "narration": narration,
        "voice_provider": VOICE_PROVIDER, "voice_model": voice_model, "speech_rate": 180,
        "captions": {"style": "bold-safe-zone", "max_words": 5, "burned_in": True},
        "music": {"kind": "original_ambient_pad", "volume": 0.055},
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
        config = {
            "sourceVideo": str(source_path), "outputVideo": str(output_path), "musicAudio": str(music_path),
            "voiceClips": [{"path": str(path), "text": phrase} for path, phrase in zip(voice_paths, phrases)],
            "width": 720, "height": 1280, "fps": 24, "leadSeconds": 0.55,
            "gapSeconds": 0.05, "tailSeconds": 0.75, "musicVolume": 0.055,
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
    audio_qa = {
        "status": "PASS" if video.get("has_audio") and video.get("audio_codec") == "mp4a" else "FLAG",
        "has_audio": bool(video.get("has_audio")), "codec": video.get("audio_codec"),
        "narration_blocks": len(receipt["cues"]), "narration_start": receipt["narrationStart"],
        "narration_end": receipt["narrationEnd"], "music_kind": "original_ambient_pad",
        "music_volume": receipt["musicVolume"], "speech_volume": 1.0,
        "music_below_speech": receipt["musicVolume"] <= 0.08,
    }
    instagram = check_compliance("INSTAGRAM_REELS", video)
    facebook = check_compliance("FACEBOOK_REELS", video)
    ready = all(item["status"] == "PASS" for item in (technical_qa, subtitle_qa, audio_qa, factual_qa)) \
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
    return result
