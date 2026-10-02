"""Renderer provider boundary and deterministic fixture renderer."""

from abc import ABC, abstractmethod
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import os
import socket
import struct
import time
from urllib.parse import urlparse
import uuid
import zlib

from media_inspection import sniff_image_mime


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


class RendererProviderJobFailed(RendererError):
    code = "PROVIDER_JOB_FAILED"


POLL_FAILURE_ERRORS = {
    "invalid_argument": RendererInvalidRequestError,
    "permission_denied": RendererRejectedError,
    "content_policy": RendererContentPolicyError,
}


@dataclass(frozen=True)
class ProviderSubmission:
    provider_job_id: str
    provider_request_id: str | None = None
    status: str = "QUEUED"
    submitted_at: str | None = None
    metadata: dict | None = None


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
    # Optional callable receiving each lifecycle event as it happens (live progress + durable audit).
    event_sink = None

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

    def _emit(self, events, event):
        events.append(event)
        if self.event_sink:
            try:
                self.event_sink(event)
            except Exception:
                # Audit persistence must never abandon an in-flight paid job; events are re-persisted at the end.
                event.pop("_persisted", None)

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
                metadata=submission.metadata,
            )
        events = []
        self._emit(events, {
            "event_type": "SUBMITTED", "status": submission.status, "at": submission.submitted_at,
            "provider_job_id": submission.provider_job_id, **(submission.metadata or {}),
        })
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
                self._emit(events, {
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
            except RendererError as error:
                self._emit(events, {"event_type": "FAILED", "status": error.code, "at": self._utc_now()})
                raise self._attach_state(error, submission, events, poll_count, last_polled_at)
            if not isinstance(last_result, ProviderPollResult):
                error = InvalidRendererResponse("Provider polling returned an invalid response.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at)
            status = last_result.status.upper()
            self._emit(events, {
                "event_type": "POLLED", "status": status, "at": last_polled_at,
                "progress": (last_result.metadata or {}).get("progress"),
            })
            if status in ("QUEUED", "PENDING", "PROCESSING", "RUNNING", "IN_PROGRESS"):
                if self.poll_interval_seconds:
                    time.sleep(self.poll_interval_seconds)
                continue
            if status in ("FAILED", "ERROR", "CANCELLED", "REJECTED", "CONTENT_POLICY_REJECTED"):
                failure_code = (last_result.metadata or {}).get("failure_code")
                if status == "CONTENT_POLICY_REJECTED":
                    error_type = RendererContentPolicyError
                elif failure_code:
                    error_type = POLL_FAILURE_ERRORS.get(failure_code, RendererProviderJobFailed)
                else:
                    error_type = RendererRejectedError
                detail = (last_result.metadata or {}).get("failure_message")
                error = error_type(f"Provider job ended with status {status}" + (f" ({failure_code}): {detail}" if failure_code else "."))
                self._emit(events, {"event_type": "FAILED", "status": failure_code or status, "at": self._utc_now()})
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if status not in ("COMPLETED", "SUCCEEDED", "SUCCESS"):
                error = InvalidRendererResponse(f"Provider returned unknown job status {status}.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if not last_result.output_url:
                error = InvalidRendererResponse("Provider completed without an output URL.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            self._emit(events, {"event_type": "COMPLETED", "status": status, "at": self._utc_now()})
            try:
                result = self.download(last_result.output_url, last_result, timeout_seconds=timeout_seconds)
            except RendererError as error:
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            if not isinstance(result, RenderResult):
                error = InvalidRendererResponse("Provider download did not normalize to RenderResult.")
                raise self._attach_state(error, submission, events, poll_count, last_polled_at, last_result)
            self._emit(events, {"event_type": "DOWNLOADED", "status": status, "at": self._utc_now()})
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

    def resume_status(self, provider_job_id, *, timeout_seconds, submitted_at=None, provider_request_id=None):
        """Perform one read-only provider status check without ever submitting again.

        A completed job is downloaded and normalized. A non-terminal job is returned
        as ProviderPollResult so the caller can leave it pending. Unknown and terminal
        failure states are intentionally left to the orchestrator to fail safely.
        """
        checked_at = self._utc_now()
        polled = self.poll(provider_job_id, timeout_seconds=timeout_seconds)
        if not isinstance(polled, ProviderPollResult):
            raise InvalidRendererResponse("Provider status check returned an invalid response.")
        status = polled.status.upper()
        if status not in ("COMPLETED", "SUCCEEDED", "SUCCESS"):
            return polled
        if not polled.output_url:
            raise InvalidRendererResponse("Provider completed without an output URL.")
        result = self.download(polled.output_url, polled, timeout_seconds=timeout_seconds)
        if not isinstance(result, RenderResult):
            raise InvalidRendererResponse("Provider download did not normalize to RenderResult.")
        return RenderResult(**{
            **result.__dict__, "provider_job_id": provider_job_id,
            "provider_request_id": result.provider_request_id or provider_request_id,
            "provider_status": status, "submitted_at": submitted_at,
            "last_polled_at": checked_at, "poll_count": 1,
            "provider_started_at": polled.started_at,
            "provider_completed_at": polled.completed_at or checked_at,
            "lifecycle_events": (
                {"event_type": "POLLED", "status": status, "at": checked_at, "provider_job_id": provider_job_id},
                {"event_type": "COMPLETED", "status": status, "at": polled.completed_at or checked_at,
                 "provider_job_id": provider_job_id},
                {"event_type": "DOWNLOADED", "status": status, "at": self._utc_now(),
                 "provider_job_id": provider_job_id},
            ),
        })


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


_FIXTURE_GLYPHS = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01111"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    " ": ("00000",) * 7,
}


def _text_spans(text, scale, width):
    """Return (row -> [(x0, x1)]) spans for centered 5x7 bitmap text, or None if it does not fit."""
    advance = 6 * scale
    text_width = len(text) * advance - scale
    if scale < 1 or text_width > width:
        return None
    left = (width - text_width) // 2
    rows = []
    for glyph_row in range(7):
        spans = []
        for index, char in enumerate(text):
            bits = _FIXTURE_GLYPHS[char][glyph_row]
            for column, bit in enumerate(bits):
                if bit == "1":
                    x0 = left + index * advance + column * scale
                    spans.append((x0, x0 + scale))
        rows.append(spans)
    return rows


def deterministic_png(width, height, seed):
    """Return a valid, deterministic RGB PNG placeholder without external dependencies.

    The image is visibly labelled as a fixture so it can never be mistaken for a
    real render; a seed-derived code strip keeps regenerated binaries distinct.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    top = (22 + digest[0] % 10, 26 + digest[1] % 10, 30 + digest[2] % 12)
    bottom = (12 + digest[3] % 6, 14 + digest[4] % 6, 16 + digest[5] % 8)
    accent = (188, 214, 162)
    soft = (150, 160, 154)
    rows = []
    for y in range(height):
        t = y / max(1, height - 1)
        color = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom))
        rows.append(bytearray(color * width))

    def fill(y, x0, x1, rgb):
        if 0 <= y < height:
            x0, x1 = max(0, x0), min(width, x1)
            if x1 > x0:
                rows[y][x0 * 3:x1 * 3] = bytes(rgb) * (x1 - x0)

    unit = max(1, min(width, height) // 60)
    margin, border = unit * 3, max(1, unit // 2)
    for y in range(margin, height - margin):
        if y < margin + border or y >= height - margin - border:
            fill(y, margin, width - margin, accent)
        else:
            fill(y, margin, margin + border, accent)
            fill(y, width - margin - border, width - margin, accent)

    def draw(text, scale, top_y, rgb):
        spans = _text_spans(text, scale, width - 2 * margin)
        if not spans:
            return 0
        offset = margin
        for glyph_row, row_spans in enumerate(spans):
            for dy in range(scale):
                for x0, x1 in row_spans:
                    fill(top_y + glyph_row * scale + dy, offset + x0, offset + x1, rgb)
        return 7 * scale

    big = max(1, (width - 2 * margin) // (8 * 6 + 8))
    small = max(1, big // 3)
    block = 7 * big * 2 + big * 3 + 7 * small
    y = (height - block) // 2
    drawn = draw("FIXTURE", big, y, accent)
    y += drawn + big * 2
    drawn = draw("PREVIEW", big, y, accent)
    y += drawn + big * 3
    draw("NOT AI GENERATED", small, y, soft)

    cell = max(1, unit)
    strip_y = height - margin - border - cell * 3
    strip_x = (width - 16 * cell * 2) // 2
    for index in range(16):
        if digest[6 + index] % 2:
            for dy in range(cell):
                fill(strip_y + dy, strip_x + index * cell * 2, strip_x + index * cell * 2 + cell, soft)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")


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


XAI_IMAGE_GENERATIONS_URL = "https://api.x.ai/v1/images/generations"
XAI_IMAGE_ASPECT_RATIOS = (
    "1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1",
    "21:9", "5:2",
)
XAI_USD_TICKS_PER_DOLLAR = 10_000_000_000
LIVE_RENDERER_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
LIVE_RENDERER_MAX_RETRY_AFTER_SECONDS = 60.0
_CONTENT_POLICY_MARKERS = ("content policy", "content_policy", "moderation", "safety", "prohibited", "not allowed")


def _retry_after_seconds(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(LIVE_RENDERER_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def https_request(method, url, *, headers=None, body=None, timeout_seconds, max_bytes=LIVE_RENDERER_MAX_DOWNLOAD_BYTES):
    """Minimal HTTPS transport returning (status, lower-cased headers, body) without following redirects."""
    endpoint = urlparse(url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
        raise RendererInvalidRequestError("Live renderer URLs must be credential-free HTTPS URLs.")
    path = endpoint.path or "/"
    if endpoint.query:
        path += "?" + endpoint.query
    connection = HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=min(timeout_seconds, 10))
    sent = False
    try:
        try:
            connection.connect()
        except (socket.timeout, TimeoutError) as error:
            raise RendererTimeoutError("Renderer connection timed out before the request was sent.") from error
        except OSError as error:
            raise RendererNetworkError("Renderer host could not be reached.") from error
        if connection.sock:
            connection.sock.settimeout(timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            sent = True
            response = connection.getresponse()
            raw = response.read(max_bytes + 1)
        except (socket.timeout, TimeoutError) as error:
            timeout = RendererTimeoutError("Renderer response exceeded the configured timeout.")
            # Once a paid generation request is sent, a retry could bill twice.
            timeout.retryable = not sent
            raise timeout from error
        except (OSError, HTTPException) as error:
            network = RendererNetworkError("Renderer connection closed before a complete response.")
            network.retryable = not sent
            raise network from error
    finally:
        connection.close()
    if len(raw) > max_bytes:
        raise RendererDownloadError("Renderer response exceeded the maximum allowed size.")
    return response.status, {key.lower(): value for key, value in response.getheaders()}, raw


def _raise_for_provider_status(status, headers, body):
    if status < 400:
        return
    if status in (401, 403):
        raise RendererAuthError(f"xAI rejected the renderer credentials (HTTP {status}).")
    if status == 429:
        error = RendererRateLimitError("xAI rate-limited the render request (HTTP 429).")
        error.retry_after_seconds = _retry_after_seconds(headers.get("retry-after"))
        raise error
    if status == 408:
        raise RendererTimeoutError("xAI timed out the render request (HTTP 408).")
    if status >= 500:
        raise RendererServerError(f"xAI returned a server error (HTTP {status}).")
    # Response bodies are inspected only for classification and never persisted.
    text = body[:4000].decode("utf-8", "replace").casefold()
    if any(marker in text for marker in _CONTENT_POLICY_MARKERS):
        raise RendererContentPolicyError(f"xAI rejected the prompt under its content policy (HTTP {status}).")
    if status in (400, 422):
        raise RendererInvalidRequestError(f"xAI rejected the render request as invalid (HTTP {status}).")
    raise RendererRejectedError(f"xAI rejected the render request (HTTP {status}).")


class XAIImageRenderer(MediaRenderer):
    """Live xAI Grok Imagine still-image adapter.

    The xAI image endpoint is synchronous: one POST returns the generated image,
    so there is no provider job to poll. The adapter owns all xAI-specific
    request and response shapes and returns a normalized RenderResult.
    """

    name = "xai"
    mode = "live"

    def __init__(self, api_key=None, model=None, resolution=None, quality=None, transport=None):
        self._api_key = api_key if api_key is not None else live_renderer_credential(self.name)[0]
        self._model = model or os.environ.get("XAI_IMAGE_MODEL", "grok-imagine-image-2.0")
        self.resolution = resolution or os.environ.get("XAI_IMAGE_RESOLUTION", "2k")
        self.quality = quality or os.environ.get("XAI_IMAGE_QUALITY", "medium")
        self._transport = transport or https_request

    @property
    def model(self):
        return self._model

    def unsupported_reason(self, media_type, aspect_ratio, **requirements):
        del requirements
        if media_type != "IMAGE":
            return f"The xAI renderer adapter supports IMAGE only, not {media_type}."
        if aspect_ratio not in XAI_IMAGE_ASPECT_RATIOS:
            return (
                f"The xAI renderer cannot produce aspect ratio {aspect_ratio}; supported ratios are "
                + ", ".join(XAI_IMAGE_ASPECT_RATIOS) + ". Generated media is never cropped to fit."
            )
        return None

    @staticmethod
    def build_prompt(request):
        media_brief = request.get("media_brief") or {}
        approved_prompt = media_brief.get("generation_prompt")
        if isinstance(approved_prompt, str) and approved_prompt.strip():
            return approved_prompt.strip()
        constraints = request.get("creative_constraints") or {}
        style = constraints.get("non_factual_style_elements") or ()
        if isinstance(style, str):
            style = (style,)
        parts = [
            "Create one neutral editorial illustration for an informational news post.",
            "Scene direction: " + " ".join(str(item) for item in request.get("visual_prompts") or ()),
            "Thumbnail concept: " + str(request.get("thumbnail_concept") or ""),
        ]
        if style:
            parts.append("Visual style: " + "; ".join(str(item) for item in style))
        parts.append(
            "Strict constraints: render no text, letters, numbers, captions, logos, flags, party symbols, maps, "
            "or recognizable real people. Do not depict crowds, rallies, endorsements, or persuasive political "
            "messaging. Do not imply any fact, date, statistic, quotation, or event. Any text overlay is applied "
            "separately and reviewed by a human."
        )
        return "\n".join(parts)[:4000]

    def build_payload(self, request):
        aspect = request["generation_parameters"]["aspect_ratio"]
        reason = self.unsupported_reason(request.get("media_type"), aspect)
        if reason:
            raise RendererInvalidRequestError(reason)
        return {
            "model": self.model, "prompt": self.build_prompt(request),
            "n": int(request["generation_parameters"].get("output_count", 1)), "aspect_ratio": aspect,
            "resolution": self.resolution, "quality": self.quality, "response_format": "url",
        }

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc).isoformat()

    def _fail(self, error, events, submitted_at, request_id=None):
        events.append({"event_type": "FAILED", "status": error.code, "at": self._utc_now()})
        error.provider_request_id = request_id
        error.provider_status = "FAILED"
        error.submitted_at = submitted_at
        error.poll_count = 0
        error.lifecycle_events = tuple(events)
        return error

    def render(self, request, *, timeout_seconds):
        if not self._api_key:
            raise MissingRendererConfiguration("Image renderer not configured: no xAI credential is available; no render was started.")
        payload = self.build_payload(request)
        started = time.monotonic()
        submitted_at = self._utc_now()
        events = [{
            "event_type": "SUBMITTED", "status": "SUBMITTED", "at": submitted_at,
            "synchronous_provider": True, "provider_request": payload,
        }]
        try:
            status, headers, body = self._transport(
                "POST", XAI_IMAGE_GENERATIONS_URL,
                headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                body=json.dumps(payload).encode("utf-8"), timeout_seconds=timeout_seconds,
            )
        except RendererError as error:
            raise self._fail(error, events, submitted_at)
        request_id = headers.get("x-request-id") or headers.get("request-id")
        try:
            _raise_for_provider_status(status, headers, body)
            try:
                data = json.loads(body.decode("utf-8"))
                items = data["data"]
                usage = data.get("usage") or {}
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise InvalidRendererResponse("xAI returned a malformed image response.") from error
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                raise InvalidRendererResponse("xAI completed without any generated image.")
            item = items[0]
            completed_at = self._utc_now()
            events.append({"event_type": "COMPLETED", "status": "COMPLETED", "at": completed_at})
            output_url = item.get("url")
            if item.get("b64_json"):
                try:
                    asset_bytes = base64.b64decode(item["b64_json"], validate=True)
                except (binascii.Error, ValueError) as error:
                    raise InvalidRendererResponse("xAI returned invalid base64 image data.") from error
            elif output_url:
                if urlparse(output_url).scheme != "https":
                    raise InvalidRendererResponse("xAI returned a non-HTTPS output URL.")
                try:
                    # The provider credential is never sent to the asset download host.
                    download_status, _, asset_bytes = self._transport(
                        "GET", output_url, headers={"Accept": "image/*"}, timeout_seconds=timeout_seconds,
                    )
                except RendererError as error:
                    raise RendererDownloadError(f"Generated image download failed: {error.code}.") from error
                if download_status != 200:
                    raise RendererDownloadError(f"Generated image download failed with HTTP {download_status}.")
            else:
                raise InvalidRendererResponse("xAI completed without an output URL or image data.")
            if not asset_bytes:
                raise RendererDownloadError("Generated image download returned no bytes.")
        except RendererError as error:
            raise self._fail(error, events, submitted_at, request_id)
        events.append({"event_type": "DOWNLOADED", "status": "COMPLETED", "at": self._utc_now()})
        ticks = usage.get("cost_in_usd_ticks")
        cost = ticks / XAI_USD_TICKS_PER_DOLLAR if isinstance(ticks, int) and not isinstance(ticks, bool) else None
        declared_mime = item.get("mime_type")
        return RenderResult(
            asset_bytes=asset_bytes, mime_type=sniff_image_mime(asset_bytes) or declared_mime or "application/octet-stream",
            provider_request_id=request_id, original_provider_url=output_url, detected_text=(),
            provider_metadata={
                "fixture": False, "synchronous_provider": True, "declared_mime_type": declared_mime,
                "provider_request": payload, "revised_prompt": item.get("revised_prompt"),
                "cost_in_usd_ticks": ticks, "text_overlay_detection": "not performed; no OCR adapter configured",
                "semantic_qa_performed": False,
            },
            request_count=1, image_count=len(items), input_units=usage.get("input_tokens"),
            output_units=usage.get("output_tokens"), provider_cost_usd=cost,
            currency="USD" if cost is not None else None,
            pricing_version="xai-reported-cost-ticks" if cost is not None else None,
            latency_ms=max(1, round((time.monotonic() - started) * 1000)),
            provider_status="COMPLETED", submitted_at=submitted_at, poll_count=0,
            provider_completed_at=completed_at, lifecycle_events=tuple(events),
        )


XAI_VIDEO_GENERATIONS_URL = "https://api.x.ai/v1/videos/generations"
XAI_VIDEO_STATUS_URL = "https://api.x.ai/v1/videos/{}"
XAI_VIDEO_MODELS_URL = "https://api.x.ai/v1/video-generation-models"
XAI_VIDEO_MODEL_PREFERENCE = ("grok-imagine-video-1.5", "grok-imagine-video")
VIDEO_GENERATION_MODES = ("TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO")

# Per provider/media-type capabilities from the xAI Imagine API reference; never one global aspect ratio.
PROVIDER_CAPABILITIES = {
    ("xai", "IMAGE"): {"aspect_ratios": XAI_IMAGE_ASPECT_RATIOS, "modes": ("IMAGE",), "asynchronous": False},
    ("xai", "VIDEO"): {
        "aspect_ratios": ("9:16", "16:9", "1:1", "4:3", "3:4", "3:2", "2:3"),
        "duration_seconds": (1, 15), "resolutions": ("480p", "720p", "1080p"),
        "modes": VIDEO_GENERATION_MODES, "reference_resolution_cap": "720p", "asynchronous": True,
    },
}


def provider_capabilities(provider, media_type):
    return PROVIDER_CAPABILITIES.get((provider, media_family(media_type)))


def ratio_label(width, height, candidates, tolerance=0.02):
    """Match decoded dimensions to a supported ratio label, or None when none is within tolerance."""
    if not width or not height:
        return None
    actual = width / height
    for label in candidates:
        left, right = (float(part) for part in label.split(":"))
        if abs(actual - left / right) <= tolerance:
            return label
    return None


class XAIVideoRenderer(AsyncMediaRenderer):
    """Live xAI Grok Imagine video adapter: submit → bounded poll → download, normalized to RenderResult.

    The model is validated against GET /v1/video-generation-models before any paid submission.
    """

    name = "xai"
    mode = "live"

    def __init__(self, api_key=None, model=None, resolution=None, transport=None, poll_interval_seconds=None,
                 max_poll_attempts=None, max_poll_retries=None):
        super().__init__(
            poll_interval_seconds=poll_interval_seconds if poll_interval_seconds is not None else os.environ.get("XAI_VIDEO_POLL_INTERVAL_SECONDS", "5"),
            max_poll_attempts=max_poll_attempts if max_poll_attempts is not None else os.environ.get("XAI_VIDEO_MAX_POLL_ATTEMPTS", "120"),
            max_poll_retries=max_poll_retries,
        )
        self._api_key = api_key if api_key is not None else live_renderer_credential(self.name)[0]
        self._model = model or os.environ.get("XAI_VIDEO_MODEL", "").strip() or XAI_VIDEO_MODEL_PREFERENCE[0]
        self.resolution = resolution or os.environ.get("XAI_VIDEO_RESOLUTION", "720p")
        self._transport = transport or https_request
        self.max_source_bytes = int(os.environ.get("XAI_VIDEO_MAX_SOURCE_BYTES", str(20 * 1024 * 1024)))
        self.max_video_bytes = int(os.environ.get("XAI_VIDEO_MAX_BYTES", str(300 * 1024 * 1024)))
        self.download_timeout_seconds = float(os.environ.get("XAI_VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "180"))
        # Set by the orchestrator: loads an exact generated asset by id, verifying its checksum.
        self.asset_loader = None
        self.discovered_models = None

    @property
    def model(self):
        return self._model

    def unsupported_reason(self, media_type, aspect_ratio, duration_seconds=None, generation_mode=None, **requirements):
        del requirements
        capabilities = PROVIDER_CAPABILITIES[("xai", "VIDEO")]
        if media_family(media_type) != "VIDEO":
            return f"The xAI video adapter renders VIDEO only, not {media_type}."
        if generation_mode and generation_mode not in capabilities["modes"]:
            return f"Generation mode {generation_mode} is not supported by xAI video."
        if aspect_ratio not in capabilities["aspect_ratios"]:
            return (f"xAI video cannot produce aspect ratio {aspect_ratio}; supported: "
                    + ", ".join(capabilities["aspect_ratios"]) + ". Generated video is never cropped or stretched.")
        low, high = capabilities["duration_seconds"]
        if duration_seconds is not None and not low <= float(duration_seconds) <= high:
            return f"xAI video duration must be {low}–{high} seconds; the package requests {duration_seconds}s."
        if self.resolution not in capabilities["resolutions"]:
            return f"Resolution {self.resolution} is not supported by xAI video."
        if generation_mode == "REFERENCE_TO_VIDEO" and self.resolution == "1080p":
            return "Reference-to-video is capped at 720p by xAI."
        return None

    def _headers(self):
        return {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}

    def discover_models(self, *, timeout_seconds):
        """List video models available to this key (a free, read-only call)."""
        if self.discovered_models is not None:
            return self.discovered_models
        status, headers, body = self._transport("GET", XAI_VIDEO_MODELS_URL, headers=self._headers(), timeout_seconds=timeout_seconds)
        _raise_for_provider_status(status, headers, body)
        try:
            models = json.loads(body.decode("utf-8"))["models"]
            self.discovered_models = [
                {
                    "id": item["id"], "aliases": list(item.get("aliases") or []),
                    "input_modalities": list(item.get("input_modalities") or []),
                    "output_modalities": list(item.get("output_modalities") or []), "version": item.get("version"),
                }
                for item in models
            ]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise InvalidRendererResponse("xAI returned a malformed video model listing.") from error
        return self.discovered_models

    def validate_model(self, generation_mode, *, timeout_seconds):
        models = self.discover_models(timeout_seconds=timeout_seconds)
        match = next((item for item in models if self.model == item["id"] or self.model in item["aliases"]), None)
        if not match or "video" not in match["output_modalities"]:
            available = ", ".join(item["id"] for item in models if "video" in item["output_modalities"]) or "none"
            raise RendererInvalidRequestError(
                f"Configured video model {self.model!r} is not available to this API key (available: {available})."
            )
        if generation_mode in ("IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO") and "image" not in match["input_modalities"]:
            raise RendererInvalidRequestError(f"Video model {self.model!r} does not accept image input.")
        return match

    @staticmethod
    def build_prompt(request):
        brief = request.get("media_brief") or {}
        parts = [
            "Create one short, neutral editorial video for an informational news post.",
            "Scene direction: " + (brief.get("generation_prompt") or " ".join(request.get("visual_prompts") or ())),
        ]
        if brief.get("visual_brief"):
            parts.append("Visual brief: " + brief["visual_brief"])
        avoid = list(brief.get("negative_constraints") or ())
        if avoid:
            parts.append("Avoid: " + "; ".join(avoid))
        parts.append(
            "Strict constraints: render no text, letters, numbers, captions, logos, flags, party symbols, maps, "
            "or recognizable real people. Do not depict crowds, rallies, endorsements, or persuasive political "
            "messaging. Do not imply any fact, date, statistic, quotation, or event."
        )
        return "\n".join(parts)[:4000]

    def build_payload(self, request, source=None):
        """Return (provider payload, credential-free snapshot of it)."""
        params = request["generation_parameters"]
        mode = params["generation_mode"]
        reason = self.unsupported_reason(request.get("media_type"), params["aspect_ratio"], params["duration_seconds"], mode)
        if reason:
            raise RendererInvalidRequestError(reason)
        payload = {
            "model": self.model, "prompt": self.build_prompt(request),
            "duration": int(round(float(params["duration_seconds"]))), "resolution": self.resolution,
        }
        snapshot = dict(payload)
        if mode == "TEXT_TO_VIDEO":
            payload["aspect_ratio"] = snapshot["aspect_ratio"] = params["aspect_ratio"]
        else:
            if not source:
                raise RendererInvalidRequestError(f"{mode} requires the exact approved source asset.")
            image = {"url": "data:" + source["mime_type"] + ";base64," + base64.b64encode(source["bytes"]).decode("ascii")}
            described = {"source_asset_id": source["id"], "checksum_sha256": source["checksum_sha256"], "bytes": len(source["bytes"])}
            if mode == "IMAGE_TO_VIDEO":
                # aspect_ratio is omitted on purpose: xAI stretches the source image when it is supplied.
                payload["image"], snapshot["image"] = image, described
            else:
                # Reference images guide content without pinning a frame, so a native aspect ratio never stretches them.
                payload["reference_images"], snapshot["reference_images"] = [image], [described]
                payload["aspect_ratio"] = snapshot["aspect_ratio"] = params["aspect_ratio"]
                payload["prompt"] = snapshot["prompt"] = (
                    "Use <IMAGE_0> only as the visual reference for subject, palette, and lighting; compose a new frame "
                    "natively for the requested aspect ratio without stretching or adding anything.\n" + payload["prompt"]
                )[:4000]
        return payload, snapshot

    def _load_source(self, request):
        source = request.get("source_asset")
        if not source:
            return None
        if not self.asset_loader:
            raise RendererInvalidRequestError("No asset loader is configured for the approved source image.")
        data = self.asset_loader(source["id"], source["checksum_sha256"])
        if hashlib.sha256(data).hexdigest() != source["checksum_sha256"]:
            raise RendererInvalidRequestError("Source image bytes do not match the checksum bound to this render job.")
        if len(data) > self.max_source_bytes:
            raise RendererInvalidRequestError("Source image exceeds the maximum size accepted for image-to-video.")
        mime = sniff_image_mime(data)
        if not mime:
            raise RendererInvalidRequestError("Source asset is not a supported PNG, JPEG, or WebP image.")
        return {"id": source["id"], "checksum_sha256": source["checksum_sha256"], "bytes": data, "mime_type": mime}

    def submit(self, request, *, timeout_seconds):
        if not self._api_key:
            raise MissingRendererConfiguration("Video renderer not configured: no xAI credential is available; no render was started.")
        mode = request["generation_parameters"]["generation_mode"]
        model = self.validate_model(mode, timeout_seconds=timeout_seconds)
        payload, snapshot = self.build_payload(request, self._load_source(request))
        status, headers, body = self._transport(
            "POST", XAI_VIDEO_GENERATIONS_URL, headers=self._headers(),
            body=json.dumps(payload).encode("utf-8"), timeout_seconds=timeout_seconds,
        )
        _raise_for_provider_status(status, headers, body)
        try:
            request_id = json.loads(body.decode("utf-8"))["request_id"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise InvalidRendererResponse("xAI video submission returned no request_id.") from error
        if not isinstance(request_id, str) or not request_id:
            raise InvalidRendererResponse("xAI video submission returned an invalid request_id.")
        return ProviderSubmission(
            provider_job_id=request_id, provider_request_id=headers.get("x-request-id") or request_id, status="QUEUED",
            metadata={"provider_request": snapshot, "validated_model": {"id": model["id"], "version": model.get("version")}},
        )

    def poll(self, provider_job_id, *, timeout_seconds):
        status, headers, body = self._transport(
            "GET", XAI_VIDEO_STATUS_URL.format(provider_job_id), headers=self._headers(), timeout_seconds=timeout_seconds,
        )
        _raise_for_provider_status(status, headers, body)
        try:
            data = json.loads(body.decode("utf-8"))
            state = str(data["status"]).lower()
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise InvalidRendererResponse("xAI returned a malformed video status response.") from error
        if state == "pending":
            return ProviderPollResult(status="PROCESSING", metadata={"progress": data.get("progress")})
        if state == "done":
            video = data.get("video") or {}
            if video.get("respect_moderation") is False:
                return ProviderPollResult(status="CONTENT_POLICY_REJECTED", metadata={"respect_moderation": False})
            return ProviderPollResult(
                status="COMPLETED", output_url=video.get("url"), completed_at=self._utc_now(),
                metadata={"duration": video.get("duration"), "model": data.get("model"), "usage": data.get("usage") or {},
                          "progress": data.get("progress")},
            )
        if state in ("failed", "expired"):
            error = data.get("error") or {}
            return ProviderPollResult(status="FAILED", metadata={
                "failure_code": error.get("code") or state, "failure_message": str(error.get("message") or state)[:200],
            })
        return ProviderPollResult(status=state.upper(), metadata={})

    def download(self, output_url, poll_result, *, timeout_seconds):
        if urlparse(output_url).scheme != "https":
            raise InvalidRendererResponse("xAI returned a non-HTTPS video URL.")
        try:
            # The provider credential is never sent to the video download host.
            status, headers, data = self._transport(
                "GET", output_url, headers={"Accept": "video/*"},
                timeout_seconds=max(timeout_seconds, self.download_timeout_seconds), max_bytes=self.max_video_bytes,
            )
        except RendererError as error:
            raise RendererDownloadError(f"Generated video download failed: {error.code}.") from error
        if status != 200:
            raise RendererDownloadError(f"Generated video download failed with HTTP {status}.")
        if not data:
            raise RendererDownloadError("Generated video download returned no bytes.")
        metadata = poll_result.metadata or {}
        usage = metadata.get("usage") or {}
        ticks = usage.get("cost_in_usd_ticks")
        cost = ticks / XAI_USD_TICKS_PER_DOLLAR if isinstance(ticks, int) and not isinstance(ticks, bool) else None
        mime = "video/mp4" if data[4:8] == b"ftyp" else (headers.get("content-type") or "application/octet-stream")
        return RenderResult(
            asset_bytes=data, mime_type=mime, duration_seconds=metadata.get("duration"),
            original_provider_url=output_url, detected_text=(),
            provider_metadata={
                "fixture": False, "asynchronous_provider": True, "reported_duration_seconds": metadata.get("duration"),
                "reported_model": metadata.get("model"), "cost_in_usd_ticks": ticks,
                "text_overlay_detection": "not performed; no OCR adapter configured", "semantic_qa_performed": False,
            },
            request_count=1, input_units=usage.get("input_tokens"), output_units=usage.get("output_tokens"),
            provider_cost_usd=cost, currency="USD" if cost is not None else None,
            pricing_version="xai-reported-cost-ticks" if cost is not None else None,
        )


LIVE_RENDERERS = {"xai": ("IMAGE", "VIDEO")}
# A provider may reuse its existing platform credential once explicitly selected as the renderer.
PROVIDER_CREDENTIAL_FALLBACKS = {"xai": "XAI_API_KEY"}


def live_renderer_credential(provider):
    """Return (secret, source variable name). Only the name is ever surfaced."""
    key = os.environ.get("LIVE_RENDERER_API_KEY", "").strip()
    if key:
        return key, "LIVE_RENDERER_API_KEY"
    fallback = PROVIDER_CREDENTIAL_FALLBACKS.get(provider)
    key = os.environ.get(fallback, "").strip() if fallback else ""
    return (key, fallback) if key else (None, None)


def media_family(media_type):
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError("unsupported media type")
    if media_type in ("IMAGE", "THUMBNAIL", "CAROUSEL_SLIDE"):
        return "IMAGE"
    return "VIDEO" if media_type in ("VIDEO", "SHORT_FORM_VIDEO", "LONG_FORM_VIDEO") else "AUDIO"


def configured_renderer_name(media_type):
    value = os.environ.get(f"RENDERER_PROVIDER_{media_family(media_type)}", "").strip()
    return value or None


def renderer_configuration(media_type):
    name = configured_renderer_name(media_type)
    if not name:
        return {"configured": False, "live": False, "provider": None, "status": "LIVE_RENDERER_NOT_CONFIGURED"}
    if name == "fixture":
        return {"configured": True, "live": False, "provider": name, "status": "FIXTURE_ONLY"}
    _, credential_source = live_renderer_credential(name)
    if not credential_source:
        return {"configured": False, "live": False, "provider": name, "status": "LIVE_RENDERER_CREDENTIALS_MISSING"}
    if media_family(media_type) not in LIVE_RENDERERS.get(name, ()):
        return {"configured": False, "live": False, "provider": name, "status": "LIVE_RENDERER_PROVIDER_UNSUPPORTED"}
    return {
        "configured": True, "live": True, "provider": name, "status": "LIVE_RENDERER_CONFIGURED",
        "credential_source": credential_source,
    }


def renderer_for(name, media_type):
    if name == "fixture":
        if media_type != "IMAGE":
            raise MissingRendererConfiguration("Fixture rendering currently supports IMAGE only.")
        return DeterministicImageRenderer()
    if name == "xai" and media_family(media_type) == "IMAGE":
        return XAIImageRenderer()
    if name == "xai" and media_family(media_type) == "VIDEO":
        return XAIVideoRenderer()
    raise MissingRendererConfiguration(f"Renderer provider {name!r} is not implemented or configured.")
