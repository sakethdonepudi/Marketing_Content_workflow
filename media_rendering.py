"""Renderer provider boundary and deterministic fixture renderer."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import struct
import time
import uuid
import zlib


RENDER_PROMPT_VERSION = os.environ.get("RENDER_PROMPT_VERSION", "media-render-prompt-v1")
RENDER_GENERATION_CONFIG_VERSION = os.environ.get("RENDER_GENERATION_CONFIG_VERSION", "media-render-config-v1")
SUPPORTED_MEDIA_TYPES = (
    "IMAGE", "VIDEO", "AUDIO", "THUMBNAIL", "CAROUSEL_SLIDE", "VOICEOVER",
    "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO",
)


class RendererError(Exception):
    code = "UNKNOWN_PROVIDER_ERROR"
    retryable = False

    def __init__(self, message, **provider_state):
        super().__init__(message)
        for key, value in provider_state.items():
            setattr(self, key, value)


class RendererTimeoutError(RendererError):
    code = "TIMEOUT"
    retryable = True


class RendererNetworkError(RendererError):
    code = "NETWORK_ERROR"
    retryable = True


class RendererRateLimitError(RendererError):
    code = "RATE_LIMITED"
    retryable = True


class RendererServerError(RendererError):
    code = "PROVIDER_5XX"
    retryable = True


class InvalidRendererResponse(RendererError):
    code = "INVALID_RESPONSE"


class MissingRendererConfiguration(RendererError):
    code = "LIVE_RENDERER_NOT_CONFIGURED"


class RendererAuthError(RendererError):
    code = "AUTH_ERROR"


class RendererRejectedError(RendererError):
    code = "PROVIDER_REJECTED"


class RendererContentPolicyError(RendererError):
    code = "CONTENT_POLICY_REJECTED"


class RendererInvalidRequestError(RendererError):
    code = "INVALID_REQUEST"


class RendererDownloadError(RendererError):
    code = "DOWNLOAD_FAILED"


class RendererPollingExhaustedError(RendererError):
    code = "POLL_ATTEMPTS_EXHAUSTED"


@dataclass(frozen=True)
class ProviderSubmission:
    provider_job_id: str
    provider_request_id: str | None = None
    status: str = "QUEUED"
    submitted_at: str | None = None


@dataclass(frozen=True)
class ProviderPollResult:
    status: str
    output_url: str | None = None
    metadata: dict | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class RenderResult:
    asset_bytes: bytes
    mime_type: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    frame_rate: float | None = None
    provider_request_id: str | None = None
    provider_asset_id: str | None = None
    original_provider_url: str | None = None
    detected_text: tuple = ()
    provider_metadata: dict | None = None
    request_count: int | None = None
    credits_consumed: float | None = None
    provider_units: float | None = None
    generation_seconds: float | None = None
    frame_count: int | None = None
    image_count: int | None = None
    provider_cost_usd: float | None = None
    calculated_cost_usd: float | None = None
    pricing_version: str | None = None
    latency_ms: int | None = None
    provider_job_id: str | None = None
    provider_status: str | None = None
    submitted_at: str | None = None
    last_polled_at: str | None = None
    poll_count: int = 0
    provider_started_at: str | None = None
    provider_completed_at: str | None = None
    lifecycle_events: tuple = ()
    currency: str | None = None
    input_units: float | None = None
    output_units: float | None = None


class MediaRenderer(ABC):
    name = "renderer"
    mode = "live"

    @property
    @abstractmethod
    def model(self):
        raise NotImplementedError

    @abstractmethod
    def render(self, request, *, timeout_seconds):
        raise NotImplementedError


class AsyncMediaRenderer(MediaRenderer):
    """Provider-neutral submit/poll/download lifecycle behind the render contract."""

    mode = "live"

    def __init__(self, *, poll_interval_seconds=None, max_poll_attempts=None, max_poll_retries=None):
        self.poll_interval_seconds = max(0.0, float(
            poll_interval_seconds if poll_interval_seconds is not None else
            os.environ.get("LIVE_RENDERER_POLL_INTERVAL_SECONDS", "2")
        ))
        self.max_poll_attempts = max(1, int(
            max_poll_attempts if max_poll_attempts is not None else
            os.environ.get("LIVE_RENDERER_MAX_POLL_ATTEMPTS", "30")
        ))
        self.max_poll_retries = max(0, int(
            max_poll_retries if max_poll_retries is not None else
            os.environ.get("LIVE_RENDERER_MAX_RETRIES", "1")
        ))

    @abstractmethod
    def submit(self, request, *, timeout_seconds):
        raise NotImplementedError

    @abstractmethod
    def poll(self, provider_job_id, *, timeout_seconds):
        raise NotImplementedError

    @abstractmethod
    def download(self, output_url, poll_result, *, timeout_seconds):
        """Return a normalized RenderResult containing downloaded bytes."""
        raise NotImplementedError

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _attach_state(error, submission, events, poll_count, last_polled_at, poll_result=None):
        error.provider_job_id = submission.provider_job_id
        error.provider_request_id = submission.provider_request_id
        error.provider_status = poll_result.status if poll_result else submission.status
        error.submitted_at = submission.submitted_at
        error.last_polled_at = last_polled_at
        error.poll_count = poll_count
        error.provider_started_at = poll_result.started_at if poll_result else None
        error.provider_completed_at = poll_result.completed_at if poll_result else None
        error.lifecycle_events = tuple(events)
        return error

    def render(self, request, *, timeout_seconds):
        started = time.monotonic()
        submission = self.submit(request, timeout_seconds=timeout_seconds)
        if not isinstance(submission, ProviderSubmission) or not submission.provider_job_id:
            raise InvalidRendererResponse("Provider submission did not return a valid job ID.")
        if not submission.submitted_at:
            submission = ProviderSubmission(
                provider_job_id=submission.provider_job_id,
                provider_request_id=submission.provider_request_id,
                status=submission.status,
                submitted_at=self._utc_now(),
            )
        events = ({"event_type": "SUBMITTED", "status": submission.status, "at": submission.submitted_at},)
        events = list(events)
        retries = 0
        poll_count = 0
        last_polled_at = None
        last_result = None
        while poll_count < self.max_poll_attempts:
            poll_count += 1
            last_polled_at = self._utc_now()
            try:
                last_result = self.poll(submission.provider_job_id, timeout_seconds=timeout_seconds)
            except (RendererRateLimitError, RendererServerError, RendererNetworkError, RendererTimeoutError) as error:
                retryable = retries < self.max_poll_retries
                events.append({
                    "event_type": "RATE_LIMITED" if isinstance(error, RendererRateLimitError) else "POLLED",
                    "status": error.code, "at": last_polled_at, "retryable": retryable,
                })
                if not retryable:
                    raise self._attach_state(error, submission, events, poll_count, last_polled_at)
                retries += 1
                delay = getattr(error, "retry_after_seconds", None)
                delay = self.poll_interval_seconds if delay is None else max(0.0, float(delay))
                if delay:
                    time.sleep(delay)
                continue
            if not isinstance(last_result, ProviderPollResult):
                error = InvalidRendererResponse("Provider polling returned an invalid response.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at)
            status = last_result.status.upper()
            events.append({"event_type": "POLLED", "status": status, "at": last_polled_at})
            if status in ("QUEUED", "PENDING", "PROCESSING", "RUNNING", "IN_PROGRESS"):
                if self.poll_interval_seconds:
                    time.sleep(self.poll_interval_seconds)
                continue
            if status in ("FAILED", "ERROR", "CANCELLED", "REJECTED", "CONTENT_POLICY_REJECTED"):
                error_type = RendererContentPolicyError if status == "CONTENT_POLICY_REJECTED" else RendererRejectedError
                error = error_type(f"Provider job ended with status {status}.")
                events.append({"event_type": "FAILED", "status": status, "at": self._utc_now()})
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if status not in ("COMPLETED", "SUCCEEDED", "SUCCESS"):
                error = InvalidRendererResponse(f"Provider returned unknown job status {status}.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if not last_result.output_url:
                error = InvalidRendererResponse("Provider completed without an output URL.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            events.append({"event_type": "COMPLETED", "status": status, "at": self._utc_now()})
            try:
                result = self.download(last_result.output_url, last_result, timeout_seconds=timeout_seconds)
            except RendererError as error:
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if not isinstance(result, RenderResult):
                error = InvalidRendererResponse("Provider download did not normalize to RenderResult.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            events.append({"event_type": "DOWNLOADED", "status": status, "at": self._utc_now()})
            return RenderResult(
                **{
                    **result.__dict__,
                    "provider_job_id": submission.provider_job_id,
                    "provider_request_id": result.provider_request_id or submission.provider_request_id,
                    "provider_status": status,
                    "submitted_at": submission.submitted_at,
                    "last_polled_at": last_polled_at,
                    "poll_count": poll_count,
                    "provider_started_at": last_result.started_at,
                    "provider_completed_at": last_result.completed_at or self._utc_now(),
                    "lifecycle_events": tuple(events),
                    "latency_ms": result.latency_ms or max(1, round((time.monotonic() - started) * 1000)),
                }
            )
        error = RendererPollingExhaustedError(
            f"Provider job did not complete within {self.max_poll_attempts} poll attempts."
        )
        raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def deterministic_png(width, height, seed):
    """Return a valid, deterministic RGB PNG without external dependencies."""
    digest = hashlib.sha256(seed.encode()).digest()
    base = tuple(35 + value % 120 for value in digest[:3])
    accent = tuple(90 + value % 140 for value in digest[3:6])
    rows = []
    for y in range(height):
        band = (y // max(1, height // 12)) % 2
        color = base if band == 0 else accent
        rows.append(b"\x00" + bytes(color) * width)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _png_chunk(b"IEND", b"")


class DeterministicImageRenderer(MediaRenderer):
    name = "deterministic-image-fixture"
    mode = "fixture"

    @property
    def model(self):
        return "fixture-raster-v1"

    def render(self, request, *, timeout_seconds):
        del timeout_seconds
        if request.get("media_type") != "IMAGE":
            raise InvalidRendererResponse("The deterministic fixture renderer supports IMAGE only.")
        width = int(request["generation_parameters"]["width"])
        height = int(request["generation_parameters"]["height"])
        started = time.monotonic()
        seed = request["generation_parameters"]["seed"]
        data = deterministic_png(width, height, seed)
        return RenderResult(
            asset_bytes=data, mime_type="image/png", width=width, height=height,
            provider_request_id="fixture-" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:16],
            provider_asset_id="fixture-asset-" + hashlib.sha256(data).hexdigest()[:16],
            detected_text=(),
            provider_metadata={
                "fixture": True, "deterministic": True, "semantic_qa_performed": False,
                "text_overlay_detection": "provider metadata only; no OCR performed",
            },
            request_count=1, image_count=1, generation_seconds=0.0,
            latency_ms=max(1, round((time.monotonic() - started) * 1000)),
        )


def configured_renderer_name(media_type):
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError("unsupported media type")
    family = "IMAGE" if media_type in ("IMAGE", "THUMBNAIL", "CAROUSEL_SLIDE") else (
        "VIDEO" if media_type in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO") else "AUDIO"
    )
    value = os.environ.get(f"RENDERER_PROVIDER_{family}", "").strip()
    return value or None


def renderer_configuration(media_type):
    name = configured_renderer_name(media_type)
    if not name:
        return {"configured": False, "live": False, "provider": None, "status": "LIVE_RENDERER_NOT_CONFIGURED"}
    if name == "fixture":
        return {"configured": True, "live": False, "provider": name, "status": "FIXTURE_ONLY"}
    if not os.environ.get("LIVE_RENDERER_API_KEY", "").strip():
        return {"configured": False, "live": False, "provider": name, "status": "LIVE_RENDERER_CREDENTIALS_MISSING"}
    return {"configured": False, "live": False, "provider": name, "status": "LIVE_RENDERER_PROVIDER_UNSUPPORTED"}


def renderer_for(name, media_type):
    if name == "fixture":
        if media_type != "IMAGE":
            raise MissingRendererConfiguration("Fixture rendering currently supports IMAGE only.")
        return DeterministicImageRenderer()
    raise MissingRendererConfiguration(f"Renderer provider {name!r} is not implemented or configured.")
