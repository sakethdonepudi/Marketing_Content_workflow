"""Separate deterministic text QA and replaceable semantic visual QA."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import difflib
import os
import re


MEDIA_QA_POLICY_VERSION = os.environ.get("MEDIA_QA_POLICY_VERSION", "media-qa-v1")


@dataclass(frozen=True)
class MediaQAResult:
    status: str
    flags: tuple = ()
    details: dict | None = None
    confidence: float | None = None
    provider: str | None = None
    model: str | None = None


def _overlay_text(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return block.get("text") or block.get("headline") or ""
    return ""


def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def evaluate_text_overlay(request, detected_text):
    expected = tuple(
        text for text in (_overlay_text(item) for item in request.get("intended_text_overlays", ())) if text
    )
    detected = tuple(str(item) for item in (detected_text or ()) if str(item).strip())
    if not detected:
        return MediaQAResult(
            status="NOT_PERFORMED",
            flags=(),
            details={
                "expected_text": list(expected), "detected_text": [], "ocr_performed": False,
                "reason": "No reliable OCR adapter or provider-returned detected text was available.",
            },
        )
    expected_normalized = {_normalized_text(item) for item in expected}
    detected_normalized = {_normalized_text(item) for item in detected}
    missing = sorted(expected_normalized - detected_normalized)
    unexpected = sorted(detected_normalized - expected_normalized)
    flags = []
    if missing:
        flags.append("EXPECTED_TEXT_MISSING_OR_ALTERED")
    if unexpected:
        flags.append("UNEXPECTED_GENERATED_TEXT")
    return MediaQAResult(
        status="FAILED" if flags else "PASSED",
        flags=tuple(flags),
        details={
            "expected_text": list(expected), "detected_text": list(detected),
            "missing_normalized": missing, "unexpected_normalized": unexpected,
            "ocr_performed": False, "source": "provider_returned_metadata",
        },
    )


# Checks a semantic provider may report. Each is PASS, FLAG, or UNKNOWN; none can approve content.
SEMANTIC_QA_CHECKS = (
    ("SCENE_MATCHES_SUBJECT", "Scene matches the requested visual brief", "all"),
    ("NO_GENERATION_ARTIFACTS", "No obvious generation artifacts or glitches", "all"),
    ("NO_UNEXPECTED_PEOPLE", "No unexpected people", "all"),
    ("NO_UNEXPECTED_PUBLIC_FIGURES", "No possible unexpected public figures (advisory, never identity verification)", "all"),
    ("NO_UNINTENDED_SYMBOLS", "No unexpected party symbols or logos", "all"),
    ("NO_UNEXPECTED_FLAGS", "No unexpected flags", "all"),
    ("NO_UNSUPPORTED_TEXT", "No unexpected text", "all"),
    ("NO_FACTUAL_VISUAL_CONTRADICTION", "No visual contradiction of approved claims", "all"),
    ("NO_INTRODUCED_FACTS", "Introduces no facts beyond the package", "all"),
    ("REFERENCE_CONSISTENCY", "Consistent with the reference/source image", "reference"),
    ("FRAME_CONSISTENCY", "Consistent subject and scene across sampled frames", "video"),
    ("NO_TEMPORAL_ARTIFACTS", "No severe temporal artifacts between frames", "video"),
)
SEMANTIC_CHECK_STATUSES = ("PASS", "FLAG", "UNKNOWN")
SEMANTIC_CHECK_IDS = tuple(key for key, _, _ in SEMANTIC_QA_CHECKS)


def applicable_semantic_checks(*, has_reference=False, is_video=False):
    return [
        (key, label) for key, label, scope in SEMANTIC_QA_CHECKS
        if scope == "all" or (scope == "reference" and has_reference) or (scope == "video" and is_video)
    ]


def normalize_semantic_checks(checks, *, has_reference=False, is_video=False):
    """Return one entry per applicable check; missing or invalid statuses become UNKNOWN."""
    supplied = {item.get("check"): item for item in (checks or ()) if isinstance(item, dict)}
    normalized = []
    for key, label in applicable_semantic_checks(has_reference=has_reference, is_video=is_video):
        item = supplied.get(key) or {}
        status = item.get("status") if item.get("status") in SEMANTIC_CHECK_STATUSES else "UNKNOWN"
        note = item.get("note") or item.get("reason")
        normalized.append({"check": key, "label": label, "status": status, "note": str(note)[:300] if note else None})
    return normalized


def aggregate_status(checks):
    statuses = [item["status"] for item in checks]
    if "FLAG" in statuses:
        return "FLAG"
    if statuses and all(status == "PASS" for status in statuses):
        return "PASS"
    return "UNKNOWN"


# ---------- OCR policy ----------

OCR_POLICY_CHECKS = (
    ("NO_UNEXPECTED_TEXT", "No text outside the approved package"),
    ("NO_RANDOM_LETTERING", "No random or garbled lettering"),
    ("NO_MALFORMED_HEADLINE", "No malformed version of the approved headline"),
    ("NO_WATERMARK", "No watermarks or stock-photo marks"),
    ("NO_INVENTED_NUMBERS", "No numbers absent from approved claims"),
    ("NO_UNSUPPORTED_NAMES", "No names absent from approved claims"),
    ("NO_POLITICAL_SLOGANS", "No party slogans or logo text"),
)
_WATERMARK = re.compile(r"(©|\(c\)|getty|shutterstock|istock|adobe ?stock|alamy|watermark|dreamstime|123rf)", re.I)
_POLITICAL = re.compile(r"\b(tdp|ysrcp|ysr|bjp|inc|congress|janasena|jsp|vote|elect|party|zindabad|jai)\b", re.I)
_OCR_NUMBER = re.compile(r"\d[\d,.]*")
_OCR_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def evaluate_ocr_policy(detections, *, allowed_texts, approved_claim_texts):
    """Deterministically compare OCR detections with the approved package; OCR text is evidence, not truth."""
    allowed = [_normalized_text(item) for item in allowed_texts if item]
    claims = " ".join(approved_claim_texts)
    claims_lower = claims.casefold()
    seen, reviewed = set(), []
    for item in detections or ():
        text = str(item.get("text") or "").strip()
        key = _normalized_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        categories = []
        if any(key == value or (len(key) > 3 and key in value) for value in allowed):
            numbers_ok = all(number.replace(",", "") in claims.replace(",", "") for number in _OCR_NUMBER.findall(text))
            if not numbers_ok:
                categories.append("NO_INVENTED_NUMBERS")
        else:
            categories.append("NO_UNEXPECTED_TEXT")
            letters = re.sub(r"[^A-Za-z]", "", text)
            if len(letters) <= 3 or not re.search(r"[aeiouAEIOU]", letters) or (item.get("confidence") or 1) < 0.5:
                categories.append("NO_RANDOM_LETTERING")
            if any(difflib.SequenceMatcher(None, key, value).ratio() >= 0.6 for value in allowed):
                categories.append("NO_MALFORMED_HEADLINE")
            if _WATERMARK.search(text):
                categories.append("NO_WATERMARK")
            if any(number.replace(",", "") not in claims.replace(",", "") for number in _OCR_NUMBER.findall(text)):
                categories.append("NO_INVENTED_NUMBERS")
            if any(name.casefold() not in claims_lower for name in _OCR_NAME.findall(text)):
                categories.append("NO_UNSUPPORTED_NAMES")
            if _POLITICAL.search(text):
                categories.append("NO_POLITICAL_SLOGANS")
        reviewed.append({**item, "text": text, "flagged_checks": categories})
    checks = []
    for key, label in OCR_POLICY_CHECKS:
        hits = [item["text"] for item in reviewed if key in item["flagged_checks"]]
        checks.append({"check": key, "label": label, "status": "FLAG" if hits else "PASS",
                       "note": ("Detected: " + "; ".join(hits))[:300] if hits else None})
    return {"status": aggregate_status(checks), "checks": checks, "detections": reviewed}


class VisualQAProvider(ABC):
    name = None
    model = None

    @abstractmethod
    def qa(self, *, generated_asset, content_package, approved_claims, expected_entities, expected_visual_description):
        raise NotImplementedError


class NoopVisualQAProvider(VisualQAProvider):
    """Explicitly records the absence of reliable semantic media inspection."""

    def qa(self, **context):
        asset = context.get("generated_asset") or {}
        has_reference = bool(asset.get("source_asset"))
        return MediaQAResult(
            status="NOT_PERFORMED",
            flags=("PUBLIC_FIGURE_IDENTITY_REQUIRES_HUMAN_REVIEW",),
            details={
                "reason": "No semantic visual QA provider is configured.",
                "human_review_required": True,
                "checks": normalize_semantic_checks(
                    (), has_reference=has_reference, is_video=str(asset.get("media_type", "")).endswith("VIDEO"),
                ),
                "identity_verified_by_model": False,
            },
        )
