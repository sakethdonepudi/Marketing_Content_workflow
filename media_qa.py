"""Separate deterministic text QA and replaceable semantic visual QA."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


class VisualQAProvider(ABC):
    name = None
    model = None

    @abstractmethod
    def qa(self, *, generated_asset, content_package, approved_claims, expected_entities, expected_visual_description):
        raise NotImplementedError


class NoopVisualQAProvider(VisualQAProvider):
    """Explicitly records the absence of reliable semantic media inspection."""

    def qa(self, **context):
        del context
        return MediaQAResult(
            status="NOT_PERFORMED",
            flags=("PUBLIC_FIGURE_IDENTITY_REQUIRES_HUMAN_REVIEW",),
            details={
                "reason": "No semantic visual QA provider is configured.",
                "human_review_required": True,
            },
        )
