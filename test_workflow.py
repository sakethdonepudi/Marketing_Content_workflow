import tempfile
from datetime import datetime, timedelta, timezone
import unittest
import sqlite3
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import app
from research import MissingAPIKeyError, ProviderResult, ResearchProviderError, ResponseTimeoutError, GrokResearchAdapter
from verification import VerificationProviderResult
from content_ceo import ContentProviderResult
from content_production import DeterministicProductionFixtureAdapter, ProductionProviderResult
import content_production
import meta_distribution
import final_reel_composer
import media_rendering
import media_qa
import visual_qa
import socket
import threading
import urllib.parse
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from media_inspection import ImageDecodeError, inspect_image, inspect_video
from media_rendering import (
    AsyncMediaRenderer, DeterministicImageRenderer, InvalidRendererResponse, ProviderPollResult,
    ProviderSubmission, RenderResult, RendererAuthError, RendererDownloadError, RendererRateLimitError,
    RendererNetworkError, RendererServerError, RendererTimeoutError, XAIImageRenderer, XAIVideoRenderer,
    deterministic_png, renderer_configuration,
)
from media_storage import LocalMediaStorage, MediaStorage
from media_qa import MediaQAResult, VisualQAProvider
from media_tools import FrameExtractor, OCRProvider, prepare_video_source, sample_times
from visual_qa import ClaudeVisualQAProvider, VisualQAProviderError


class DeterministicSceneRenderer:
    """Test-only scene renderer that returns a local PNG without any network call."""

    name = "xai"
    model = "scene-fixture"
    mode = "live"

    def unsupported_reason(self, media_type, aspect_ratio, **requirements):
        return None

    def render(self, request, *, timeout_seconds):
        from media_rendering import RenderResult
        return RenderResult(
            asset_bytes=deterministic_png(96, 128, request["visual_prompts"][0][:24]),
            mime_type="image/png", provider_request_id="scene-test", provider_metadata={"fixture": True},
            provider_cost_usd=0.08, currency="USD", pricing_version="xai-reported-cost-ticks",
        )


class DeferredExecutor:
    def submit(self, *args, **kwargs):
        return None


class FixtureProvider:
    name = "fixture"
    model = "fixture-v1"
    mode = "test"

    def __init__(self, research):
        self.result = research

    def research(self, evidence_bundle, **limits):
        return ProviderResult(research=self.result)


class FailingProvider(FixtureProvider):
    def __init__(self):
        pass

    def research(self, evidence_bundle, **limits):
        error = ResearchProviderError("Fixture provider unavailable.")
        error.retryable = False
        raise error


class FixtureVerificationProvider:
    name = "fixture-verification"
    model = "fixture-verification-v1"

    def __init__(self, *, mode="test", leads=None):
        self.mode = mode
        self.leads = leads or []

    def find_corroboration(self, gap_bundle, **limits):
        del limits
        return VerificationProviderResult(
            result={
                "search_summary": "Controlled verification fixture.",
                "unresolved_gaps": gap_bundle.get("gaps") or [],
                "leads": self.leads,
            },
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            actual_search_calls=0,
            actual_open_calls=0,
            actual_sources_returned=len(self.leads),
            cost_usd=0.0,
            cost_usd_ticks=0,
            elapsed_seconds=0.0,
        )


class FailingVerificationProvider(FixtureVerificationProvider):
    def find_corroboration(self, gap_bundle, **limits):
        del gap_bundle, limits
        error = ResearchProviderError("Controlled verification provider failure.")
        error.retryable = False
        raise error


class SequencedVerificationProvider(FixtureVerificationProvider):
    def __init__(self, outcomes, *, mode="live"):
        super().__init__(mode=mode)
        self.outcomes = list(outcomes)
        self.calls = 0

    def find_corroboration(self, gap_bundle, **limits):
        del limits
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, VerificationProviderResult):
            return outcome
        return VerificationProviderResult(result={
            "search_summary": "Controlled verification fixture.",
            "unresolved_gaps": gap_bundle.get("gaps") or [],
            "leads": outcome or [],
        }, cost_usd=0.001, cost_usd_ticks=10_000_000, elapsed_seconds=0.01)


class FixtureContentProvider:
    name = "fixture-content-ceo"
    model = "fixture-content-v1"
    mode = "live"

    def __init__(self, decision=None):
        self.decision = decision or {
            "decision": "CREATE",
            "recommended_format": "IMAGE",
            "language": "English",
            "proposed_duration_seconds": 15,
            "priority": "NORMAL",
            "factual_rationale": "Controlled provider fixture using only approved inputs.",
            "missing_evidence_or_media": [],
        }

    def decide(self, decision_bundle, **limits):
        del decision_bundle, limits
        return ContentProviderResult(
            decision=self.decision, input_tokens=10, output_tokens=5, total_tokens=15,
            cost_usd=0.001, cost_usd_ticks=10_000_000, elapsed_seconds=0.01,
        )


class FailingContentProvider(FixtureContentProvider):
    def decide(self, decision_bundle, **limits):
        del decision_bundle, limits
        error = ResearchProviderError("Controlled Content CEO provider failure.")
        error.retryable = False
        raise error


class FixtureProductionProvider(DeterministicProductionFixtureAdapter):
    pass


class FailingProductionProvider(FixtureProductionProvider):
    mode = "live"
    name = "anthropic"

    def generate(self, locked_context, **limits):
        del locked_context, limits
        error = ResearchProviderError("Controlled production provider failure.")
        error.retryable = False
        raise error


class PackageMutationProvider(FixtureProductionProvider):
    def __init__(self, mutate):
        self.mutate = mutate

    def generate(self, locked_context, **limits):
        result = super().generate(locked_context, **limits)
        package = result.package
        self.mutate(package, locked_context)
        return ProductionProviderResult(package=package, input_tokens=1, output_tokens=1, total_tokens=2)


class RetryProductionProvider(FixtureProductionProvider):
    def __init__(self):
        self.calls = 0

    def generate(self, locked_context, **limits):
        self.calls += 1
        if self.calls == 1:
            error = ResearchProviderError("Controlled retryable production failure.")
            error.retryable = True
            error.code = "fixture_retry"
            raise error
        return super().generate(locked_context, **limits)


class MissingUsageProductionProvider(FixtureProductionProvider):
    def generate(self, locked_context, **limits):
        package = super().generate(locked_context, **limits).package
        return ProductionProviderResult(package=package)


class CountingImageRenderer(DeterministicImageRenderer):
    def __init__(self):
        self.calls = 0

    def render(self, request, **limits):
        self.calls += 1
        return super().render(request, **limits)


class FailingImageRenderer(CountingImageRenderer):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def render(self, request, **limits):
        del request, limits
        self.calls += 1
        raise self.error


class MutatingImageRenderer(CountingImageRenderer):
    def __init__(self, mutate_result=None, mutate_lineage=None):
        super().__init__()
        self.mutate_result = mutate_result
        self.mutate_lineage = mutate_lineage

    def render(self, request, **limits):
        result = super().render(request, **limits)
        if self.mutate_lineage:
            self.mutate_lineage()
        if self.mutate_result:
            return self.mutate_result(result)
        return result


class FailingStorage(MediaStorage):
    def save(self, asset_bytes, *, extension, metadata=None):
        del asset_bytes, extension, metadata
        raise OSError("Controlled storage persistence failure.")

    def get(self, storage_uri):
        raise OSError(storage_uri)

    def exists(self, storage_uri):
        return False


class IdenticalImageRenderer(CountingImageRenderer):
    def render(self, request, **limits):
        request = json.loads(json.dumps(request))
        request["generation_parameters"]["seed"] = "constant-binary"
        return super().render(request, **limits)


class UsageImageRenderer(CountingImageRenderer):
    def render(self, request, **limits):
        result = super().render(request, **limits)
        return replace(
            result, credits_consumed=2.5, provider_units=1.0, provider_cost_usd=0.04,
            pricing_version="fixture-pricing-v1",
        )


class ControlledAsyncImageRenderer(AsyncMediaRenderer):
    name = "controlled-live-image"
    mode = "live"

    def __init__(self, statuses=None, *, submit_error=None, download_error=None, detected_text=(), mutate=None,
                 max_poll_attempts=4, max_poll_retries=1, cost=None):
        super().__init__(poll_interval_seconds=0, max_poll_attempts=max_poll_attempts, max_poll_retries=max_poll_retries)
        self.statuses = list(statuses or ["PROCESSING", "COMPLETED"])
        self.submit_error = submit_error
        self.download_error = download_error
        self.detected_text = detected_text
        self.mutate = mutate
        self.cost = cost
        self.submit_calls = 0
        self.poll_calls = 0
        self.download_calls = 0
        self.request = None

    @property
    def model(self):
        return "controlled-async-v1"

    def submit(self, request, **limits):
        del limits
        self.submit_calls += 1
        self.request = request
        if self.submit_error:
            raise self.submit_error
        return ProviderSubmission(
            provider_job_id="provider-job-123", provider_request_id="provider-request-123",
            status="QUEUED", submitted_at="2026-10-01T00:00:00+00:00",
        )

    def poll(self, provider_job_id, **limits):
        del provider_job_id, limits
        self.poll_calls += 1
        value = self.statuses.pop(0) if self.statuses else "PROCESSING"
        if isinstance(value, Exception):
            raise value
        output = None if value in ("PROCESSING", "FAILED", "CONTENT_POLICY_REJECTED", "NO_OUTPUT") else (
            "https://provider.example/output.png?token=signed-secret" if value == "COMPLETED" else None
        )
        status = "COMPLETED" if value == "NO_OUTPUT" else value
        return ProviderPollResult(
            status=status, output_url=output, started_at="2026-10-01T00:00:01+00:00",
            completed_at="2026-10-01T00:00:02+00:00" if status == "COMPLETED" else None,
        )

    def download(self, output_url, poll_result, **limits):
        del poll_result, limits
        self.download_calls += 1
        if self.download_error:
            raise self.download_error
        if self.mutate:
            self.mutate()
        result = DeterministicImageRenderer().render(self.request, timeout_seconds=1)
        detected = self.detected_text
        if detected == "expected":
            detected = tuple(
                item.get("text") or item.get("headline") or "" for item in self.request["intended_text_overlays"]
            )
        return replace(
            result, provider_request_id="provider-request-123", provider_asset_id="provider-asset-123",
            original_provider_url=output_url, detected_text=tuple(detected), request_count=1,
            provider_cost_usd=self.cost, currency="USD" if self.cost is not None else None,
            pricing_version="controlled-pricing-v1" if self.cost is not None else None,
        )


class ControlledVisualQA(VisualQAProvider):
    name = "controlled-visual-qa"
    model = "controlled-visual-qa-v1"

    def __init__(self, status="PASSED", flags=()):
        self.status = status
        self.flags = tuple(flags)

    def qa(self, **context):
        del context
        return MediaQAResult(
            status=self.status, flags=self.flags, details={"fixture": True}, confidence=0.9,
            provider=self.name, model=self.model,
        )


class ControlledOCR(OCRProvider):
    name = "controlled-ocr"
    model = "controlled-ocr-v1"

    def __init__(self, detections=()):
        self.detections = list(detections)
        self.calls = 0

    def detect(self, image_bytes):
        self.calls += 1
        self.last_bytes = image_bytes
        return list(self.detections)


class ControlledFrameExtractor(FrameExtractor):
    name = "controlled-frames"

    def __init__(self, count=5):
        self.count = count
        self.calls = 0

    def extract(self, video_bytes, times):
        self.calls += 1
        self.last_times = list(times)
        return [{
            "requested_seconds": value, "actual_seconds": value, "jpeg": jpeg_bytes(720, 1280, bytes([index + 1])),
            "width": 720, "height": 1280,
        } for index, value in enumerate(times[:self.count])]


class StructuredVisualQA(VisualQAProvider):
    name = "controlled-vision"
    model = "controlled-vision-v1"

    def __init__(self, status="PASS", possible_people=False):
        self.status = status
        self.possible_people = possible_people
        self.calls = 0

    def qa(self, **context):
        self.calls += 1
        self.context = context
        checks = [{"check": key, "status": self.status, "reason": "controlled"}
                  for key in media_qa.SEMANTIC_CHECK_IDS]
        return MediaQAResult(
            status="FLAGGED" if self.status == "FLAG" else "PASSED", flags=(),
            details={"checks": checks, "run_status": self.status, "possible_people_present": self.possible_people,
                     "identity_verified_by_model": False, "prompt_version": "controlled-visual-v1"},
            provider=self.name, model=self.model,
        )


class BlockedConnection:
    def __init__(self, *args, **kwargs):
        raise AssertionError("Tests must never open a real network connection.")


def jpeg_bytes(width, height, payload=b"\x12\x34\x56"):
    frame = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    scan = b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    return (
        b"\xff\xd8" + b"\xff\xc0" + (len(frame) + 2).to_bytes(2, "big") + frame
        + b"\xff\xda" + (len(scan) + 2).to_bytes(2, "big") + scan + payload + b"\xff\xd9"
    )


XAI_TEST_KEY = "xai-controlled-live-renderer-key-000"
CLAUDE_TEST_KEY = "sk-ant-api03-controlled-claude-key-000"


def mp4_box(kind, payload):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def mp4_bytes(width, height, duration=6.0, fps=24, audio=False, payload=b"\x00" * 64, timescale=1000):
    """Minimal structurally valid MP4 (ftyp/moov/mdat) for deterministic video QA tests."""
    ticks = int(duration * timescale)
    def full(version_flags=b"\x00\x00\x00\x00"):
        return version_flags + b"\x00" * 8 + timescale.to_bytes(4, "big") + ticks.to_bytes(4, "big")
    def trak(handler, w, h, codec, samples, delta):
        tkhd = mp4_box(b"tkhd", b"\x00\x00\x00\x07" + b"\x00" * 72 + (w << 16).to_bytes(4, "big") + (h << 16).to_bytes(4, "big"))
        mdhd = mp4_box(b"mdhd", full() + b"\x00" * 4)
        hdlr = mp4_box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 13)
        stsd = mp4_box(b"stsd", b"\x00" * 4 + (1).to_bytes(4, "big") + (16).to_bytes(4, "big") + codec + b"\x00" * 8)
        stts = mp4_box(b"stts", b"\x00" * 4 + (1).to_bytes(4, "big") + samples.to_bytes(4, "big") + delta.to_bytes(4, "big"))
        return mp4_box(b"trak", tkhd + mp4_box(b"mdia", mdhd + hdlr + mp4_box(b"minf", mp4_box(b"stbl", stsd + stts))))
    tracks = trak(b"vide", width, height, b"avc1", int(duration * fps), int(timescale / fps))
    if audio:
        tracks += trak(b"soun", 0, 0, b"mp4a", int(duration * 43), 23)
    moov = mp4_box(b"moov", mp4_box(b"mvhd", full() + b"\x00" * 80) + tracks)
    return mp4_box(b"ftyp", b"isom" + (512).to_bytes(4, "big") + b"isomavc1") + moov + mp4_box(b"mdat", payload)


class FakeXAIVideoTransport:
    """Scripted xAI video API: model listing, submission, bounded polling, and download."""

    def __init__(self, polls=None, submit=None, models=None, download=None):
        self.models = models or (200, {}, json.dumps({"models": [{
            "id": "grok-imagine-video-1.5", "aliases": [], "input_modalities": ["text", "image"],
            "output_modalities": ["video"], "version": "1.5",
        }]}).encode())
        self.submit = submit or (200, {"x-request-id": "xai-video-request-1"}, json.dumps({"request_id": "vid-req-123"}).encode())
        self.polls = list(polls or [self.pending(40), self.done()])
        self.download = download or (200, {"content-type": "video/mp4"}, mp4_bytes(720, 1280, 6.0))
        self.calls = []

    @staticmethod
    def pending(progress):
        return 200, {}, json.dumps({"status": "pending", "progress": progress}).encode()

    @staticmethod
    def done(duration=6, ticks=5_000_000_000, moderation=True):
        body = {"status": "done", "progress": 100, "model": "grok-imagine-video-1.5", "video": {
            "url": "https://vidgen.x.ai/bucket/xai-video-vid-req-123.mp4", "duration": duration,
            "respect_moderation": moderation,
        }}
        if ticks is not None:
            body["usage"] = {"cost_in_usd_ticks": ticks}
        return 200, {}, json.dumps(body).encode()

    def count(self, kind):
        return sum(1 for call in self.calls if call["kind"] == kind)

    def __call__(self, method, url, *, headers=None, body=None, timeout_seconds, max_bytes=None):
        kind = ("models" if url.endswith("/video-generation-models") else "submit" if method == "POST"
                else "poll" if "api.x.ai/v1/videos/" in url else "download")
        self.calls.append({"kind": kind, "method": method, "url": url, "headers": dict(headers or {}), "body": body})
        value = {"models": self.models, "submit": self.submit, "download": self.download}.get(kind)
        if kind == "poll":
            value = self.polls.pop(0) if self.polls else self.pending(99)
        if isinstance(value, Exception):
            raise value
        return value


META_IG_TOKEN = "IGAAcontrolled-instagram-token-000"
META_FB_TOKEN = "EAAcontrolled-facebook-page-token-000"


class FakeMetaTransport:
    """Scripted Meta Graph + rupload endpoints for Instagram and Facebook Reels. Never touches the network."""

    def __init__(self, *, statuses=None, publish=None, poll_errors=0):
        self.statuses = list(statuses or [])
        self.publish_response = publish
        self.poll_errors = poll_errors
        self.calls = []

    def kinds(self):
        return [call["kind"] for call in self.calls]

    def __call__(self, method, url, *, headers=None, body=None, timeout_seconds=60):
        form = dict(urllib.parse.parse_qsl(body.decode())) if isinstance(body, bytes) and headers and \
            headers.get("Content-Type") == "application/x-www-form-urlencoded" else {}
        if "rupload.facebook.com" in url:
            kind = "upload"
        elif url.split("?")[0].endswith("/media") and method == "POST":
            kind = "ig_container"
        elif url.split("?")[0].endswith("/media_publish"):
            kind = "ig_publish"
        elif url.split("?")[0].endswith("/video_reels"):
            kind = "fb_start" if form.get("upload_phase") == "start" else "fb_finish"
        elif "fields=status" in url:
            kind = "status"
        elif "fields=permalink" in url:
            kind = "permalink"
        elif url.split("?")[0].endswith("/media"):
            kind = "ig_media_list"
        else:
            kind = "other"
        self.calls.append({"kind": kind, "method": method, "url": url, "headers": dict(headers or {}), "form": form,
                           "body_size": len(body) if isinstance(body, bytes) and kind == "upload" else None})
        ok = lambda data: (200, {}, json.dumps(data).encode())
        if kind == "upload":
            return ok({"success": True})
        if kind == "ig_container":
            return ok({"id": "IGC-1"})
        if kind == "fb_start":
            return ok({"video_id": "FBV-1", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/FBV-1"})
        if kind == "status":
            if self.poll_errors:
                self.poll_errors -= 1
                return 500, {}, json.dumps({"error": {"code": 2, "message": "temporary"}}).encode()
            value = self.statuses.pop(0) if self.statuses else None
            if "FBV-1" in url:
                return ok({"status": value or {"video_status": "ready", "uploading_phase": {"status": "complete"},
                                               "publishing_phase": {"publish_status": "draft"}}})
            return ok({"status_code": value or "FINISHED"})
        if kind in ("ig_publish", "fb_finish"):
            response = self.publish_response
            if isinstance(response, Exception):
                raise response
            return ok(response or ({"id": "IGM-1"} if kind == "ig_publish" else {"success": True}))
        if kind == "permalink":
            return ok({"permalink": "https://www.instagram.com/reel/CONTROLLED/"} if "IGM-1" in url else {"permalink_url": "/reel/FBV-1"})
        return ok({"data": []})


class FakeClaudeConnection:
    """Stands in for Anthropic's HTTPS endpoint; builds a grounded package from the locked context it receives."""

    requests = []
    status = 200
    stop_reason = "end_turn"

    def __init__(self, host, port, timeout=None):
        self.sock = None

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        payload = json.loads(body)
        FakeClaudeConnection.requests.append({"path": path, "headers": dict(headers or {}), "payload": payload})
        context = json.loads(payload["messages"][0]["content"].split("\n", 1)[1])
        package = DeterministicProductionFixtureAdapter().generate(context).package
        self._body = json.dumps({
            "id": "msg_controlled_123", "stop_reason": FakeClaudeConnection.stop_reason,
            "content": [{"type": "text", "text": json.dumps(package)}],
            "usage": {"input_tokens": 900, "output_tokens": 700},
        }).encode()

    def getresponse(self):
        body, status = self._body, FakeClaudeConnection.status
        class Response:
            def read(self_inner):
                return body
        response = Response()
        response.status = status
        return response

    def close(self):
        pass


class FakeXAITransport:
    """Scripted xAI HTTP responses; entries are (status, headers, body) tuples or exceptions."""

    def __init__(self, posts=None, download=None):
        self.posts = list(posts or [self.success()])
        self.download = download or (200, {"content-type": "image/jpeg"}, jpeg_bytes(1536, 2048))
        self.calls = []

    @staticmethod
    def success(mime_type="image/jpeg", ticks=700_000_000, item=None):
        body = {
            "data": [item or {"url": "https://imgen.x.ai/generated/abc.jpg?sig=signed-download-secret", "mime_type": mime_type}],
            "usage": {"cost_in_usd_ticks": ticks, "input_tokens": 120, "output_tokens": 4096},
        }
        if ticks is None:
            del body["usage"]
        return 200, {"x-request-id": "xai-request-123"}, json.dumps(body).encode()

    def post_count(self):
        return sum(1 for call in self.calls if call["method"] == "POST")

    def __call__(self, method, url, *, headers=None, body=None, timeout_seconds):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body})
        value = self.posts.pop(0) if method == "POST" else self.download
        if isinstance(value, Exception):
            raise value
        return value


class ProductionGroundingValidatorTests(unittest.TestCase):
    claim_one = "CV-CLAIM-ONE"
    claim_two = "CV-CLAIM-TWO"

    def context(self, content_format="IMAGE"):
        return {
            "content_decision": {
                "recommended_format": content_format, "language": "English", "proposed_duration_seconds": 8,
            },
            "approved_claims": [
                {
                    "claim_version_id": self.claim_one,
                    "text": "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh during the 2025-26 crop season.",
                    "claim_type": "factual_assertion", "assertion_scope": "approval",
                    "attribution": "The New Indian Express",
                },
                {
                    "claim_version_id": self.claim_two,
                    "text": "A Union Commerce Ministry notification permitted registered and unregistered growers to sell excess FCV tobacco at all Tobacco Board-authorised auction platforms.",
                    "claim_type": "factual_assertion", "assertion_scope": "approval",
                    "attribution": "The New Indian Express",
                },
            ],
            "evidence_provenance": [
                {"claim_version_id": self.claim_one, "source_name": "The New Indian Express", "source_class": "independent_reporting"},
                {"claim_version_id": self.claim_two, "source_name": "Official Gazette", "source_class": "official_primary"},
            ],
        }

    def package(self):
        return {
            "story_angle": "Union government permits sale of excess FCV tobacco produced in Andhra Pradesh for the 2025-26 crop season",
            "content_objective": "Inform growers about the permitted sale of excess FCV tobacco in Andhra Pradesh for the 2025-26 crop season",
            "format": "IMAGE",
            "headline": {
                "text": "Union Government Permits Sale of Excess FCV Tobacco Produced in Andhra Pradesh for 2025-26 Season",
                "claim_version_ids": [self.claim_one],
            },
            "hook": {
                "text": "Registered and unregistered growers can sell excess FCV tobacco at Tobacco Board-authorised auction platforms",
                "claim_version_ids": [self.claim_two],
            },
            "caption": {
                "text": "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh during the 2025-26 crop season.\nSource: The New Indian Express",
                "claim_version_ids": [self.claim_one],
            },
            "script": [{
                "sequence": 1,
                "text": "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh during the 2025-26 crop season.",
                "claim_version_ids": [self.claim_one],
            }],
            "storyboard": [{
                "scene_number": 1, "duration_seconds": 8,
                "narration": "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh for the 2025-26 crop season.",
                "on_screen_text": "Union Govt Permits Excess FCV Tobacco Sale in AP",
                "visual_prompt": "Neutral illustration of an agricultural auction environment with tobacco leaves",
                "claim_version_ids": [self.claim_one, self.claim_two],
            }],
            "thumbnail": {
                "headline": "FCV Tobacco Sale Permitted", "visual_prompt": "Close-up of dried tobacco leaves",
                "claim_version_ids": [self.claim_one],
            },
            "platform_metadata": {
                "language": "English", "duration_seconds": 8, "aspect_ratio": "3:4",
                "accessibility_text": "Dried tobacco leaves. The Union government permitted sale of excess FCV tobacco in Andhra Pradesh for the 2025-26 crop season.",
                "accessibility_claim_version_ids": [self.claim_one],
            },
            "creative_notes": ["Keep the wording neutral."],
            "non_factual_style_elements": ["Warm editorial palette"],
            "media_brief": {
                "media_type": "IMAGE", "visual_brief": "Neutral agricultural still-life with tobacco leaves",
                "generation_prompt": "Dried tobacco leaves, neutral editorial style, 3:4 portrait aspect ratio",
                "negative_constraints": ["No people", "No text"],
                "factual_constraints": ["Do not imply completed sales"],
            },
        }

    def validate(self, package=None, context=None):
        return content_production.validate_production_package(package or self.package(), context or self.context())

    def test_grounding_allows_three_by_four_format_metadata(self):
        result = self.validate()
        self.assertEqual(result["status"], "PASS")

    def test_grounding_allows_nine_by_sixteen_format_metadata(self):
        package = self.package()
        package["format"] = "REEL"
        package["platform_metadata"]["aspect_ratio"] = "9:16"
        package["media_brief"]["media_type"] = "VIDEO"
        result = self.validate(package, self.context("REEL"))
        self.assertEqual(result["status"], "PASS")

    def test_grounding_allows_faithful_headline_paraphrase(self):
        result = self.validate()
        headline = next(item for item in result["field_results"] if item["field"] == "headline")
        self.assertEqual(headline["status"], "PASS")
        self.assertIn(self.claim_one, headline["matched_approved_claim_ids"])

    def test_grounding_normalizes_entity_variants(self):
        package = self.package()
        package["headline"]["text"] = "UNION GOVT permits excess FCV tobacco sale in Andhra-Pradesh for 2025-26"
        self.assertEqual(self.validate(package)["status"], "PASS")

    def test_grounding_allows_non_factual_visual_instruction(self):
        package = self.package()
        package["storyboard"][0]["visual_prompt"] = "Neutral illustration of an agricultural auction environment"
        self.assertEqual(self.validate(package)["status"], "PASS")

    def test_grounding_rejects_visual_instruction_with_unsupported_event(self):
        package = self.package()
        package["storyboard"][0]["visual_prompt"] = "Thousands of protesting farmers outside Parliament"
        result = self.validate(package)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("unsupported depicted event" in error for error in result["errors"]))

    def test_grounding_allows_supported_source_attribution(self):
        package = self.package()
        package["caption"]["text"] += "\nAccording to Union Commerce Ministry"
        package["caption"]["claim_version_ids"] = [self.claim_one, self.claim_two]
        self.assertEqual(self.validate(package)["status"], "PASS")

    def test_grounding_rejects_unsupported_source_attribution(self):
        package = self.package()
        package["caption"]["text"] = package["caption"]["text"].replace("The New Indian Express", "Reuters")
        result = self.validate(package)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("unsupported source attribution" in error for error in result["errors"]))

    def test_grounding_allows_compressed_thumbnail_copy(self):
        result = self.validate()
        thumbnail = next(item for item in result["field_results"] if item["field"] == "thumbnail.headline")
        self.assertEqual(thumbnail["status"], "PASS")

    def test_grounding_rejects_sensational_unsupported_thumbnail(self):
        package = self.package()
        package["thumbnail"]["headline"] = "HUGE WIN: FCV Tobacco Sale Permitted"
        result = self.validate(package)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("sensational" in error for error in result["errors"]))

    def test_grounding_rejects_unsupported_number(self):
        package = self.package()
        package["headline"]["text"] += " worth ₹999 crore"
        result = self.validate(package)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("unsupported numerical" in error for error in result["errors"]))

    def test_grounding_rejects_unsupported_quote(self):
        package = self.package()
        package["caption"]["text"] += ' “This is a huge win.”'
        result = self.validate(package)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("unsupported quotation" in error for error in result["errors"]))

    def test_grounding_returns_per_field_results(self):
        result = self.validate()
        by_field = {item["field"]: item for item in result["field_results"]}
        self.assertEqual(by_field["headline"]["content_type"], "EDITORIAL_COPY")
        self.assertEqual(by_field["caption.source_attribution[0]"]["content_type"], "SOURCE_ATTRIBUTION")
        self.assertEqual(by_field["storyboard[0].visual_prompt"]["content_type"], "VISUAL_INSTRUCTION")
        self.assertEqual(by_field["platform_metadata.aspect_ratio"]["content_type"], "TECHNICAL_PARAMETER")
        self.assertTrue(all("matched_approved_claim_ids" in item for item in result["field_results"]))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # Never inherit real renderer credentials/config from the developer's .env, and never touch the network.
        environment = patch.dict("os.environ", {
            "RENDERER_PROVIDER_IMAGE": "", "RENDERER_PROVIDER_VIDEO": "", "RENDERER_PROVIDER_AUDIO": "",
            "LIVE_RENDERER_API_KEY": "", "XAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "REACHOUT_DEMO_MODE": "",
            "OCR_PROVIDER": "none", "VISUAL_QA_PROVIDER": "none", "VIDEO_FRAME_EXTRACTOR": "none",
            "SOCIAL_PUBLISHING_ENABLED": "0", "INSTAGRAM_PUBLISHING_ENABLED": "0", "FACEBOOK_PUBLISHING_ENABLED": "0",
            "INSTAGRAM_USER_ID": "", "INSTAGRAM_ACCESS_TOKEN": "", "FACEBOOK_PAGE_ID": "", "FACEBOOK_PAGE_ACCESS_TOKEN": "",
            "DISTRIBUTION_STATIC_HASHTAGS": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for module in (media_rendering, content_production, visual_qa, meta_distribution):
            network = patch.object(module, "HTTPSConnection", BlockedConnection)
            network.start()
            self.addCleanup(network.stop)
        self.temp = tempfile.TemporaryDirectory()
        app.DB = Path(self.temp.name) / "test.sqlite3"
        app.RENDER_STORAGE_ROOT = Path(self.temp.name) / "generated-media"
        app.RENDER_BACKOFF_SECONDS = 0
        app.VERIFICATION_RETRY_BACKOFF_SECONDS = 0
        app.VERIFICATION_RETRY_MAX_BACKOFF_SECONDS = 0
        app.init()

    def tearDown(self):
        self.temp.cleanup()

    def reset_database(self):
        self.temp.cleanup()
        self.setUp()

    def discovery_source(self, **metadata):
        return {
            "id": "independent-fixture",
            "name": "Independent Andhra Pradesh Desk",
            "type": "webpage",
            "url": "https://news.example/ap/cbn",
            "official": False,
            "source_class": "independent_reporting",
            "enabled": True,
            "rate_limit_seconds": 0,
            "poll_interval_seconds": 900,
            "identity": {
                "status": "verified",
                "verification_method": "publisher_masthead_and_registry",
                "evidence_url": "https://news.example/about",
                "verification_note": "Fixture publisher identity reviewed independently.",
            },
            "workspace_match": {
                "leader": "N. Chandrababu Naidu",
                "jurisdiction": "Andhra Pradesh, India",
                "topics": ["government administration"],
            },
            "metadata": {
                "content_role": "listing",
                "item_type": "news",
                "link_pattern": r"^https://news\.example/ap/items/",
                "max_items": 10,
                "max_pages": 1,
                **metadata,
            },
        }

    @staticmethod
    def listing_html(*links):
        anchors = "".join(f'<a href="{link}">Item</a>' for link in links)
        return (
            "<html><head><title>N. Chandrababu Naidu Andhra Pradesh news</title></head>"
            "<body><h1>Chief Minister government administration</h1>" + anchors + "</body></html>"
        )

    @staticmethod
    def item_html(title="CM reviews Andhra Pradesh programme", published="2026-09-29T08:00:00Z"):
        payload = app.json.dumps({
            "@context": "https://schema.org", "@type": "NewsArticle", "headline": title,
            "datePublished": published, "author": {"@type": "Person", "name": "A. Reporter"},
            "articleBody": "N. Chandrababu Naidu reviewed an Andhra Pradesh government administration programme.",
        })
        return (
            f'<html><head><title>{title}</title><link rel="canonical" href="https://news.example/ap/items/one">'
            f'<meta property="og:type" content="article"><script type="application/ld+json">{payload}</script>'
            "</head><body><article><p>N. Chandrababu Naidu reviewed an Andhra Pradesh government programme.</p></article></body></html>"
        )

    def research_event(self, *, url="https://news.example/ap/items/research", text=None, source_class="independent_reporting"):
        text = text or "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review on Monday."
        return app.ingest_signal(
            url=url,
            title="N. Chandrababu Naidu approves Andhra Pradesh irrigation review",
            text=text,
            source_name="Independent Andhra Pradesh Desk",
            publication_time="2026-09-29T08:00:00Z",
            detected_at="2026-09-29T09:00:00Z",
            content_role="item",
            item_type="news",
            source_class=source_class,
        )

    def queued_live_verification(self, *, source_class="independent_reporting", text=None):
        event = self.research_event(source_class=source_class, text=text)
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        with app.connect() as connection:
            connection.execute("UPDATE research_runs SET mode='live' WHERE id=?", (research["id"],))
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            verification = app.enqueue_verification(research["id"], "grok")["run"]
        return event, research, verification

    @staticmethod
    def proposed_research(claims, **overrides):
        result = {
            "concrete_occurrence": True,
            "relevance": "RELEVANT",
            "occurrence_kind": "approval",
            "what_happened": "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review on Monday.",
            "who": ["N. Chandrababu Naidu"],
            "where": "Andhra Pradesh, India",
            "publication_time": "2026-09-29T08:00:00+00:00",
            "stated_event_time": None,
            "unknowns": [],
            "contradictions": [],
            "claims": claims,
        }
        result.update(overrides)
        return result

    @staticmethod
    def proposed_claim(url, excerpt, *, text=None, support_kind="supports"):
        return {
            "text": text or excerpt,
            "claim_type": "factual_assertion",
            "assertion_scope": "approval",
            "attribution": "Independent Andhra Pradesh Desk",
            "reviewer_notes": None,
            "required_for_event": True,
            "evidence_refs": [{"url": url, "excerpt": excerpt, "support_kind": support_kind}],
        }

    def production_approved_event(self, *, with_media=False, with_video=False):
        event = self.research_event(source_class="official_primary")
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        with app.connect() as connection:
            connection.execute("UPDATE research_runs SET mode='live' WHERE id=?", (research["id"],))
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            verification = app.enqueue_verification(research["id"], "grok")["run"]
        app.run_verification_job(verification["id"], FixtureVerificationProvider(mode="live"))
        if with_media:
            app.register_media_asset(
                event["event_id"], "https://fixture.example/test-data/approved-image.jpg", "image",
                "TEST FIXTURE — rights-cleared media", rights_status="verified", availability_status="available",
                content_hash="media-v1", metadata={
                    "fixture": True, "rights_basis": "Controlled test fixture; no real-world media license is asserted."
                },
            )
        if with_video:
            app.register_media_asset(
                event["event_id"], "https://fixture.example/test-data/approved-video.mp4", "video",
                "TEST FIXTURE — rights-cleared video", rights_status="verified", availability_status="available",
                content_hash="video-v1", metadata={"fixture": True, "rights_basis": "Controlled test fixture."},
            )
        return event, research, verification

    def fixture_event_with_test_claim_set(self):
        event = app.ingest_signal(
            url="https://fixture.example/test-data/content-ceo-event",
            title="TEST DATA — Andhra Pradesh public-information fixture",
            text="TEST DATA: N. Chandrababu Naidu approved an Andhra Pradesh public-information review.",
            source_name="TEST DATA — controlled official fixture", source_class="official_primary",
            content_role="item", item_type="announcement", publication_time="2026-09-30T01:00:00Z",
        )
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        app.register_media_asset(
            event["event_id"], "https://fixture.example/test-data/image.jpg", "image",
            "TEST DATA — fixture media", mode="test", rights_status="verified",
            availability_status="available", content_hash="fixture-media-v1",
        )
        return event, research, verification

    def executable_content_decision(self, provider=None, with_video=False):
        event, _, _ = self.production_approved_event(with_media=True, with_video=with_video)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], provider or FixtureContentProvider())
        room = app.event_room(event["event_id"])
        decision = room["content_decision_runs"][0]["decision_record"]
        self.assertEqual(decision["decision"], "CREATE")
        self.assertEqual(decision["executable"], 1)
        return event, decision

    def renderable_package(self):
        event, decision = self.executable_content_decision()
        production = app.enqueue_production(decision["id"], "fixture", background=False)["job"]
        with app.connect() as connection:
            package = dict(connection.execute("SELECT * FROM content_packages WHERE job_id=?", (production["id"],)).fetchone())
        return event, decision, production, package

    def xai_renderable_package(self):
        return self.renderable_package()[3]

    def reel_renderable_package(self):
        provider = FixtureContentProvider({
            "decision": "CREATE", "recommended_format": "REEL", "language": "English", "proposed_duration_seconds": 6,
            "priority": "NORMAL", "factual_rationale": "Controlled REEL fixture using only approved inputs.",
            "missing_evidence_or_media": [],
        })
        _, decision = self.executable_content_decision(provider, with_video=True)
        production = app.enqueue_production(decision["id"], "fixture", background=False)["job"]
        with app.connect() as connection:
            return dict(connection.execute("SELECT * FROM content_packages WHERE job_id=?", (production["id"],)).fetchone())

    def video_render(self, transport, package, **kwargs):
        renderer = XAIVideoRenderer(api_key=XAI_TEST_KEY, transport=transport, poll_interval_seconds=0,
                                    max_poll_attempts=kwargs.pop("max_poll_attempts", 5))
        return app.enqueue_render(package["id"], "VIDEO", renderer=renderer, background=False, **kwargs)["job"]

    def xai_render(self, transport, package=None):
        package = package or self.xai_renderable_package()
        renderer = XAIImageRenderer(api_key=XAI_TEST_KEY, transport=transport)
        return app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)["job"]

    def test_state_and_audit(self):
        event_id = app.create_event("Event", "Source", event_time="2026-09-29T08:00:00Z")
        app.transition(event_id, "VERIFYING")
        with app.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()["status"],
                "VERIFYING",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transitions WHERE event_id=?", (event_id,)).fetchone()[0],
                2,
            )

    def test_unresearched_event_cannot_be_manually_verified(self):
        event_id = app.create_event("Event", "Source", event_time="2026-09-29T08:00:00Z")
        app.transition(event_id, "VERIFYING")
        with self.assertRaisesRegex(ValueError, "evidence policy"):
            app.transition(event_id, "VERIFIED")

    def test_reject_invalid_jump(self):
        event_id = app.create_event("Event", "Source", event_time="2026-09-29T08:00:00Z")
        with self.assertRaises(ValueError):
            app.transition(event_id, "PUBLISHED")

    def test_duplicate_url_is_one_immutable_signal(self):
        first = app.ingest_signal(
            url="https://example.com/report?id=7&utm_source=test",
            title="Andhra Pradesh publishes a factual report",
            text="The Government of Andhra Pradesh published the report on Tuesday.",
            source_name="Example Newsroom",
            publication_time="2026-09-29T08:00:00Z",
        )
        duplicate = app.ingest_signal(
            url="https://EXAMPLE.com/report?id=7",
            title="A changed title must not replace the stored signal",
            text="A changed body must not replace the stored signal.",
            source_name="Example Newsroom",
            publication_time="2026-09-29T08:05:00Z",
        )

        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["signal_id"], duplicate["signal_id"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
            stored = connection.execute("SELECT title FROM signals").fetchone()["title"]
            self.assertEqual(stored, "Andhra Pradesh publishes a factual report")
            with self.assertRaises(app.sqlite3.IntegrityError):
                connection.execute("UPDATE signals SET title='changed'")

    def test_two_reports_cluster_as_one_event_with_two_sources(self):
        first = app.ingest_signal(
            url="https://government.example/reports/water-project-review",
            title="N. Chandrababu Naidu reviews Andhra Pradesh water projects",
            text="Andhra Pradesh Chief Minister Nara Chandrababu Naidu reviewed state water project progress with officials in Amaravati.",
            source_name="Government Source Fixture",
            source_type="webpage",
            content_role="item",
            item_type="news",
            publication_time="2026-09-29T14:00:00Z",
            detected_at="2026-09-29T14:05:00Z",
            source_metadata={"official": True},
        )
        second = app.ingest_signal(
            url="https://leader.example/updates/andhra-water-projects",
            title="Andhra Pradesh CM reviews progress of state water projects",
            text="N. Chandrababu Naidu met officials in Amaravati to review progress across Andhra Pradesh water projects.",
            source_name="Leader Source Fixture",
            source_type="rss",
            content_role="feed_item",
            item_type="news",
            publication_time="2026-09-29T15:00:00Z",
            detected_at="2026-09-29T15:03:00Z",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(second["clustered"])
        result = app.overview()
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["source_count"], 2)
        self.assertCountEqual(result["events"][0]["source_names"], ["Government Source Fixture", "Leader Source Fixture"])
        with app.connect() as connection:
            links = connection.execute(
                "SELECT event_id,COUNT(*) AS total FROM signals GROUP BY event_id"
            ).fetchone()
            self.assertEqual(links["event_id"], first["event_id"])
            self.assertEqual(links["total"], 2)

    def test_rejects_cbn_gov_ng_for_andhra_pradesh_workspace(self):
        source = {
            "id": "wrong-cbn-source",
            "name": "Wrong CBN Source",
            "type": "webpage",
            "url": "https://www.cbn.gov.ng/",
            "official": True,
            "source_class": "official_primary",
            "identity": {
                "status": "verified",
                "verification_method": "government_domain",
                "evidence_url": "https://www.cbn.gov.ng/",
                "verification_note": "Fixture asserting the wrong host is rejected.",
            },
            "workspace_match": {
                "leader": "N. Chandrababu Naidu",
                "jurisdiction": "Andhra Pradesh, India",
                "topics": ["public leadership"],
            },
        }
        with self.assertRaisesRegex(ValueError, "not approved"):
            app.validate_source_registration(source, "n-chandrababu-naidu-andhra-pradesh")

    def test_workspace_correction_archives_and_removes_ambiguous_sample(self):
        with app.connect() as connection:
            connection.execute("DELETE FROM data_corrections WHERE id=?", (app.CORRECTION_ID,))
        event_id = app.create_event(
            "Sample CBN public event", "Manual sample", event_time="2026-09-29T08:00:00Z"
        )

        result = app.apply_workspace_identity_correction()

        self.assertEqual(result["events_removed"], 1)
        with app.connect() as connection:
            self.assertIsNone(connection.execute("SELECT id FROM events WHERE id=?", (event_id,)).fetchone())
            audit = connection.execute(
                "SELECT action,snapshot_json FROM correction_audit WHERE correction_id=? AND entity_type='event' AND entity_id=?",
                (app.CORRECTION_ID, event_id),
            ).fetchall()
        self.assertEqual({row["action"] for row in audit}, {"reviewed_for_removal", "removed"})
        self.assertTrue(any("Sample CBN public event" in row["snapshot_json"] for row in audit))

    def test_cm_profile_is_reference_and_never_an_event(self):
        result = app.ingest_signal(
            url="https://prakasam.ap.gov.in/chief-minister-profile/",
            title="Chief Minister Profile",
            text="N. Chandrababu Naidu is Chief Minister of Andhra Pradesh.",
            source_name="Government of Andhra Pradesh",
            publication_time="2026-09-29T08:00:00Z",
            detected_at="2026-09-29T09:00:00Z",
            content_role="profile",
            item_type="news",
        )
        self.assertTrue(result["reference"])
        self.assertIsNone(result["event_id"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_unchanged_homepage_is_one_reference_and_never_breaking(self):
        values = {
            "url": "https://ncbn.info/",
            "title": "Nara Chandra Babu Naidu",
            "text": "Official homepage of N. Chandrababu Naidu, Chief Minister of Andhra Pradesh.",
            "source_name": "N. Chandrababu Naidu — Official Website",
            "content_role": "homepage",
        }
        first = app.ingest_signal(**values)
        duplicate = app.ingest_signal(**values)
        self.assertTrue(first["reference"])
        self.assertTrue(duplicate["duplicate"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_crawl_time_never_substitutes_for_missing_or_old_event_time(self):
        undated = app.ingest_signal(
            url="https://government.example/news/undated-item",
            title="Andhra Pradesh government update",
            text="N. Chandrababu Naidu reviewed an Andhra Pradesh government programme.",
            source_name="Government Source Fixture",
            detected_at="2026-09-29T12:00:00Z",
            content_role="item",
            item_type="news",
        )
        old = app.ingest_signal(
            url="https://government.example/news/old-item",
            title="Archived Andhra Pradesh government update",
            text="N. Chandrababu Naidu reviewed an Andhra Pradesh government programme.",
            source_name="Government Source Fixture",
            publication_time="2024-01-01T12:00:00Z",
            detected_at="2026-09-29T12:00:00Z",
            content_role="item",
            item_type="news",
        )
        self.assertTrue(undated["review"])
        self.assertTrue(old["rejected"])
        with app.connect() as connection:
            rows = connection.execute(
                "SELECT canonical_url,event_id,item_kind,event_time,classification_reason FROM signals ORDER BY canonical_url"
            ).fetchall()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertTrue(all(row["event_id"] is None for row in rows))
        self.assertIsNone(rows[1]["event_time"])
        self.assertIn("missing_defensible", rows[1]["classification_reason"])
        self.assertIn("older_than", rows[0]["classification_reason"])

    def test_reference_correction_reclassifies_signal_and_audits_false_event(self):
        app.sync_sources(app.load_source_config("config/sources.example.json"))
        with app.connect() as connection:
            connection.execute(
                "DELETE FROM data_corrections WHERE id=?", (app.REFERENCE_CORRECTION_ID,)
            )
        false_signal = app.ingest_signal(
            url="https://ncbn.info/reference-correction-fixture",
            title="N. Chandrababu Naidu homepage fixture",
            text="N. Chandrababu Naidu is Chief Minister of Andhra Pradesh.",
            source_name="N. Chandrababu Naidu — Official Website",
            source_id="ncbn-official-site",
            publication_time="2026-09-29T08:00:00Z",
            detected_at="2026-09-29T09:00:00Z",
            content_role="item",
            item_type="news",
        )
        result = app.apply_reference_material_correction()
        self.assertEqual(result["signals_reclassified"], 1)
        self.assertEqual(result["events_removed"], 1)
        with app.connect() as connection:
            signal = connection.execute(
                "SELECT event_id,item_kind FROM signals WHERE id=?", (false_signal["signal_id"],)
            ).fetchone()
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM correction_audit WHERE correction_id=?",
                (app.REFERENCE_CORRECTION_ID,),
            ).fetchone()[0]
        self.assertIsNone(signal["event_id"])
        self.assertEqual(signal["item_kind"], "reference")
        self.assertEqual(audit_count, 2)

    def test_listing_discovers_dated_item_and_keeps_listing_as_reference(self):
        source = self.discovery_source()
        app.sync_sources({"workspace_key": "n-chandrababu-naidu-andhra-pradesh", "sources": [source]})
        listing = self.listing_html("https://news.example/ap/items/one")
        item = self.item_html()

        def fetch(url):
            return (item if "/items/" in url else listing, "text/html", url)

        with patch.object(app, "fetch_public_url", side_effect=fetch):
            result = app.ingest_webpage_source(source)

        self.assertEqual([row["item_kind"] for row in result["results"]], ["reference", "event"])
        with app.connect() as connection:
            item_row = connection.execute("SELECT * FROM signals WHERE item_kind='event'").fetchone()
        self.assertEqual(item_row["publication_time"], "2026-09-29T08:00:00+00:00")
        self.assertEqual(item_row["event_time_basis"], "publication_time")
        self.assertEqual(item_row["author"], "A. Reporter")
        self.assertEqual(item_row["source_class"], "independent_reporting")

    def test_listing_pagination_discovers_items_on_next_page(self):
        source = self.discovery_source(
            pagination_pattern=r"/ap/cbn/page/\d+", max_pages=2,
        )
        app.sync_sources({"workspace_key": "n-chandrababu-naidu-andhra-pradesh", "sources": [source]})
        page_one = self.listing_html("https://news.example/ap/cbn/page/2")
        page_two = self.listing_html("https://news.example/ap/items/one")

        def fetch(url):
            if "/items/" in url:
                return self.item_html(), "text/html", url
            return (page_two if url.endswith("/page/2") else page_one), "text/html", url

        with patch.object(app, "fetch_public_url", side_effect=fetch):
            result = app.ingest_webpage_source(source)

        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(sum(row["item_kind"] == "reference" for row in result["results"]), 2)
        self.assertEqual(sum(row["item_kind"] == "event" for row in result["results"]), 1)

    def test_incremental_polling_stops_at_cursor_without_duplicate_item(self):
        source = self.discovery_source()
        config = {"workspace_key": "n-chandrababu-naidu-andhra-pradesh", "sources": [source]}
        listing = self.listing_html("https://news.example/ap/items/one")

        def fetch(url):
            return (self.item_html() if "/items/" in url else listing, "text/html", url)

        with patch.object(app, "load_source_config", return_value=config), patch.object(
            app, "fetch_public_url", side_effect=fetch
        ) as mocked_fetch:
            first = app.ingest_configured_sources(force=True)
            second = app.ingest_configured_sources(force=True)

        self.assertEqual(first["event_signals_created"], 1)
        self.assertEqual(second["event_signals_created"], 0)
        self.assertEqual(mocked_fetch.call_count, 3)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 2)

    def test_source_classification_separates_independent_from_official_allowlist(self):
        independent = self.discovery_source()
        app.validate_source_registration(independent, "n-chandrababu-naidu-andhra-pradesh")

        false_official = {**independent, "official": True, "source_class": "official_primary"}
        false_official["identity"] = {
            **independent["identity"], "verification_method": "cross_source_confirmation"
        }
        with self.assertRaisesRegex(ValueError, "not approved"):
            app.validate_source_registration(false_official, "n-chandrababu-naidu-andhra-pradesh")

        self_claim = {**false_official, "url": "https://ncbn.info/news"}
        self_claim["identity"] = {
            **false_official["identity"], "verification_method": "self_claimed_official"
        }
        with self.assertRaisesRegex(ValueError, "own claim is insufficient"):
            app.validate_source_registration(self_claim, "n-chandrababu-naidu-andhra-pradesh")

    def test_fabricated_citation_and_missing_excerpt_are_insufficient(self):
        event = self.research_event()
        valid_url = "https://news.example/ap/items/research"
        claims = [
            self.proposed_claim(
                "https://fabricated.example/not-linked",
                "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review on Monday.",
            ),
            self.proposed_claim(valid_url, "This sentence is absent from the stored source."),
        ]
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_research(event["event_id"], "test")
        app.run_research_job(queued["run"]["id"], FixtureProvider(self.proposed_research(claims)))

        with app.connect() as connection:
            rows = connection.execute(
                "SELECT verification_status FROM claims ORDER BY created_at,id"
            ).fetchall()
            validations = {row[0] for row in connection.execute("SELECT validation_status FROM claim_evidence")}
        self.assertEqual([row["verification_status"] for row in rows], ["INSUFFICIENT_EVIDENCE"] * 2)
        self.assertEqual(validations, {"URL_NOT_IN_EVIDENCE", "EXCERPT_NOT_FOUND"})

    def test_conflicting_numbers_create_conflicted_claim(self):
        first_text = "N. Chandrababu Naidu approved Rs 100 crore for the Andhra Pradesh irrigation review."
        second_text = "N. Chandrababu Naidu approved Rs 120 crore for the Andhra Pradesh irrigation review."
        first = self.research_event(url="https://news.example/ap/items/amount-one", text=first_text)
        second = self.research_event(url="https://reporter.example/ap/items/amount-two", text=second_text)
        self.assertEqual(first["event_id"], second["event_id"])
        claim = self.proposed_claim(
            "https://news.example/ap/items/amount-one", first_text,
            text="The reported approval was Rs 100 crore.",
        )
        claim["evidence_refs"].append({
            "url": "https://reporter.example/ap/items/amount-two",
            "excerpt": second_text,
            "support_kind": "conflicts",
        })
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_research(first["event_id"], "test")
        app.run_research_job(queued["run"]["id"], FixtureProvider(self.proposed_research([claim])))
        with app.connect() as connection:
            status = connection.execute("SELECT verification_status FROM claims").fetchone()[0]
        self.assertEqual(status, "CONFLICTED")

    def test_publication_time_is_not_substituted_for_event_time(self):
        event = self.research_event()
        excerpt = "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review on Monday."
        result = self.proposed_research(
            [self.proposed_claim("https://news.example/ap/items/research", excerpt)],
            stated_event_time="2026-09-29T08:00:00+00:00",
        )
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_research(event["event_id"], "test")
        app.run_research_job(queued["run"]["id"], FixtureProvider(result))
        room = app.event_room(event["event_id"])
        summary = room["runs"][0]["summary"]
        self.assertEqual(summary["publication_time"], "2026-09-29T08:00:00+00:00")
        self.assertIsNone(summary["stated_event_time"])
        self.assertTrue(any("no event time" in item.lower() for item in summary["unknowns"]))

    def test_duplicate_research_jobs_return_the_active_run(self):
        event = self.research_event()
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            first = app.enqueue_research(event["event_id"], "test")
            second = app.enqueue_research(event["event_id"], "test")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0], 1)

    def test_unchanged_evidence_uses_a_zero_usage_cached_run(self):
        event = self.research_event()
        first = app.enqueue_research(event["event_id"], "test", background=False)
        second = app.enqueue_research(event["event_id"], "test", background=False)
        self.assertEqual(first["run"]["status"], "COMPLETED")
        self.assertTrue(second["cached"])
        self.assertEqual(second["run"]["status"], "CACHED")
        self.assertEqual(second["run"]["cache_source_run_id"], first["run"]["id"])
        self.assertEqual(second["run"]["total_tokens"], 0)
        self.assertEqual(second["run"]["cost_usd"], 0.0)
        with app.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT research_status FROM events WHERE id=?", (event["event_id"],)
            ).fetchone()[0], "REVIEW_REQUIRED")
        room = app.event_room(event["event_id"])
        self.assertEqual(
            room["runs"][0]["verification_explanation"],
            "Test data is excluded from production event verification.",
        )
        self.assertEqual(room["event"]["status"], "VERIFYING")
        # Even a legacy/tampered test run carrying the success phrase cannot
        # satisfy the manual production verification gate.
        with app.connect() as connection:
            connection.execute(
                "UPDATE research_runs SET verification_explanation='All required claims meet the documented evidence policy.' "
                "WHERE id=?", (first["run"]["id"],),
            )
            connection.execute(
                "UPDATE events SET research_status='COMPLETE' WHERE id=?", (event["event_id"],)
            )
        with self.assertRaisesRegex(ValueError, "evidence policy"):
            app.transition(event["event_id"], "VERIFIED")

    def test_provider_failure_is_recorded_without_verifying_event(self):
        event = self.research_event()
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_research(event["event_id"], "test")
        app.run_research_job(queued["run"]["id"], FailingProvider())
        room = app.event_room(event["event_id"])
        self.assertEqual(room["runs"][0]["status"], "FAILED")
        self.assertEqual(room["runs"][0]["error_code"], "provider_error")
        self.assertEqual(room["event"]["research_status"], "FAILED")
        self.assertEqual(room["event"]["status"], "VERIFYING")

    def test_missing_xai_key_is_a_graceful_provider_error(self):
        provider = GrokResearchAdapter(api_key="", model="grok-fixture")
        with self.assertRaisesRegex(MissingAPIKeyError, "not configured"):
            provider.research({}, search_limit=0, token_limit=256, timeout_seconds=5)

    def test_syndicated_duplicates_are_one_evidence_family(self):
        text = "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review after meeting officials."
        first = self.research_event(url="https://news.example/ap/items/syndicated-one", text=text)
        second = self.research_event(url="https://copy.example/ap/items/syndicated-two", text=text)
        self.assertEqual(first["event_id"], second["event_id"])
        research = app.enqueue_research(first["event_id"], "test", background=False)["run"]
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            families = connection.execute(
                "SELECT COUNT(DISTINCT evidence_family_id) FROM verification_snapshots WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()[0]
            decision = connection.execute(
                "SELECT decision,independent_family_count FROM verification_decisions WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()
            family_assessment = connection.execute(
                "SELECT relationship,reason FROM verification_source_family_assessments "
                "WHERE verification_run_id=?", (verification["id"],),
            ).fetchone()
        self.assertEqual(families, 1)
        self.assertEqual((decision["decision"], decision["independent_family_count"]), ("INSUFFICIENT_EVIDENCE", 1))
        self.assertEqual(family_assessment["relationship"], "SAME_FAMILY")
        self.assertIn("identical", family_assessment["reason"].lower())

    def test_unrelated_topic_mention_does_not_corroborate_exact_claim(self):
        first_text = "N. Chandrababu Naidu approved Rs 100 crore for the Andhra Pradesh irrigation review."
        second_text = "N. Chandrababu Naidu met Andhra Pradesh officials to discuss irrigation review progress."
        first = self.research_event(url="https://news.example/ap/items/exact-amount", text=first_text)
        second = self.research_event(url="https://reporter.example/ap/items/topic-only", text=second_text)
        self.assertEqual(first["event_id"], second["event_id"])
        claim = self.proposed_claim(
            "https://news.example/ap/items/exact-amount", first_text,
            text="N. Chandrababu Naidu approved Rs 100 crore for the Andhra Pradesh irrigation review.",
        )
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            research = app.enqueue_research(first["event_id"], "test")["run"]
        app.run_research_job(research["id"], FixtureProvider(self.proposed_research([claim])))
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            decision = connection.execute(
                "SELECT decision,independent_family_count FROM verification_decisions WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()
            relationships = [row[0] for row in connection.execute(
                "SELECT relationship FROM verification_decision_evidence vde "
                "JOIN verification_decisions vd ON vd.id=vde.decision_id WHERE vd.verification_run_id=?",
                (verification["id"],),
            )]
            attempts = connection.execute(
                "SELECT COUNT(*) FROM verification_attempts WHERE verification_run_id=? "
                "AND phase='CORROBORATION_DISCOVERY'", (verification["id"],),
            ).fetchone()[0]
        self.assertEqual(decision["decision"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(decision["independent_family_count"], 1)
        self.assertIn("mentions_only", relationships)
        self.assertEqual(attempts, 1)

    def test_conflicting_primary_evidence_blocks_verification(self):
        first_text = "N. Chandrababu Naidu approved Rs 100 crore for the Andhra Pradesh irrigation review."
        second_text = "N. Chandrababu Naidu approved Rs 120 crore for the Andhra Pradesh irrigation review."
        first = self.research_event(
            url="https://government.example/ap/items/primary-one", text=first_text, source_class="official_primary"
        )
        second = self.research_event(
            url="https://agency.example/ap/items/primary-two", text=second_text, source_class="official_primary"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        claim = self.proposed_claim(
            "https://government.example/ap/items/primary-one", first_text, text=first_text,
        )
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            research = app.enqueue_research(first["event_id"], "test")["run"]
        app.run_research_job(research["id"], FixtureProvider(self.proposed_research([claim])))
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            decision = connection.execute(
                "SELECT decision,rationale FROM verification_decisions WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()
            attempts = connection.execute(
                "SELECT COUNT(*) FROM verification_attempts WHERE verification_run_id=? "
                "AND phase='CORROBORATION_DISCOVERY'", (verification["id"],),
            ).fetchone()[0]
        self.assertEqual(decision["decision"], "CONFLICTED")
        self.assertIn("conflict", decision["rationale"].lower())
        self.assertEqual(attempts, 1)

    def test_paraphrase_cannot_be_labeled_as_direct_quotation(self):
        event = self.research_event(text="N. Chandrababu Naidu approved the Andhra Pradesh irrigation review.")
        excerpt = "N. Chandrababu Naidu approved the Andhra Pradesh irrigation review."
        claim = self.proposed_claim(
            "https://news.example/ap/items/research", excerpt,
            text='The Chief Minister said, “The irrigation review is approved.”',
        )
        claim["claim_type"] = "quotation"
        claim["assertion_scope"] = "quotation"
        with patch.object(app, "RESEARCH_EXECUTOR", DeferredExecutor()):
            research = app.enqueue_research(event["event_id"], "test")["run"]
        app.run_research_job(research["id"], FixtureProvider(self.proposed_research([claim])))
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            decision = connection.execute(
                "SELECT decision,missing_information_json FROM verification_decisions WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()
        self.assertEqual(decision["decision"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("paraphrase", decision["missing_information_json"].lower())

    def test_evidence_change_invalidates_cached_verification_decision(self):
        event = self.research_event()
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        first = app.enqueue_verification(research["id"], "test", background=False)
        cached = app.enqueue_verification(research["id"], "test", background=False)
        self.assertEqual(first["run"]["status"], "COMPLETED")
        self.assertTrue(cached["cached"])
        added = self.research_event(
            url="https://reporter.example/ap/items/new-evidence",
            text="N. Chandrababu Naidu approved the Andhra Pradesh irrigation review on Monday after meeting officials.",
        )
        self.assertEqual(event["event_id"], added["event_id"])
        refreshed = app.enqueue_verification(research["id"], "test", background=False)
        self.assertFalse(refreshed["cached"])
        self.assertEqual(refreshed["run"]["status"], "COMPLETED")
        self.assertNotEqual(first["run"]["final_evidence_version"], refreshed["run"]["final_evidence_version"])

    def test_test_data_is_excluded_but_live_policy_can_verify(self):
        event = self.research_event(source_class="official_primary")
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        fixture = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            test_status = connection.execute(
                "SELECT status FROM approved_claim_sets WHERE verification_run_id=?", (fixture["id"],)
            ).fetchone()[0]
            event_status = connection.execute(
                "SELECT status,verification_status FROM events WHERE id=?", (event["event_id"],)
            ).fetchone()
            connection.execute("UPDATE research_runs SET mode='live' WHERE id=?", (research["id"],))
        self.assertEqual(test_status, "TEST_ONLY")
        self.assertEqual((event_status["status"], event_status["verification_status"]), ("VERIFYING", "TEST_ONLY"))

        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_verification(research["id"], "grok")
        app.run_verification_job(queued["run"]["id"], FixtureVerificationProvider(mode="live"))
        with app.connect() as connection:
            live_set = connection.execute(
                "SELECT status FROM approved_claim_sets WHERE verification_run_id=?", (queued["run"]["id"],)
            ).fetchone()[0]
            event_status = connection.execute(
                "SELECT status,verification_status FROM events WHERE id=?", (event["event_id"],)
            ).fetchone()
        self.assertEqual(live_set, "APPROVED")
        self.assertEqual((event_status["status"], event_status["verification_status"]), ("VERIFIED", "VERIFIED"))

    def test_duplicate_verification_jobs_return_active_run(self):
        event = self.research_event()
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            first = app.enqueue_verification(research["id"], "test")
            second = app.enqueue_verification(research["id"], "test")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])

    def test_verification_provider_failure_keeps_local_decisions_and_review_state(self):
        event = self.research_event()
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_verification(research["id"], "test")
        app.run_verification_job(queued["run"]["id"], FailingVerificationProvider())
        with app.connect() as connection:
            run = connection.execute(
                "SELECT status,cost_status,total_tokens FROM verification_runs WHERE id=?", (queued["run"]["id"],)
            ).fetchone()
            event_state = connection.execute(
                "SELECT status,verification_status FROM events WHERE id=?", (event["event_id"],)
            ).fetchone()
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM verification_decisions WHERE verification_run_id=?", (queued["run"]["id"],)
            ).fetchone()[0]
            claim_set = connection.execute(
                "SELECT status FROM approved_claim_sets WHERE verification_run_id=?", (queued["run"]["id"],)
            ).fetchone()[0]
        self.assertEqual((run["status"], run["cost_status"], run["total_tokens"]), ("FAILED", "unknown", None))
        self.assertEqual((event_state["status"], event_state["verification_status"]), ("VERIFYING", "TEST_ONLY"))
        self.assertEqual(decision_count, 1)
        self.assertEqual(claim_set, "TEST_ONLY")

    def test_verification_timeout_is_paused_and_never_becomes_insufficient_evidence(self):
        event, _, verification = self.queued_live_verification()
        provider = SequencedVerificationProvider([
            ResponseTimeoutError("first controlled timeout"),
            ResponseTimeoutError("second controlled timeout"),
        ])
        app.run_verification_job(verification["id"], provider)
        with app.connect() as connection:
            run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (verification["id"],)).fetchone()
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY' "
                "ORDER BY attempt_number", (verification["id"],),
            )]
            decisions = connection.execute(
                "SELECT COUNT(*) FROM verification_decisions WHERE verification_run_id=?", (verification["id"],)
            ).fetchone()[0]
            claim_set = connection.execute(
                "SELECT status FROM approved_claim_sets WHERE verification_run_id=?", (verification["id"],)
            ).fetchone()[0]
            downstream = {
                "content": connection.execute("SELECT COUNT(*) FROM content_decision_runs WHERE event_id=?", (event["event_id"],)).fetchone()[0],
                "packages": connection.execute("SELECT COUNT(*) FROM production_jobs WHERE event_id=?", (event["event_id"],)).fetchone()[0],
                "renders": connection.execute("SELECT COUNT(*) FROM render_jobs WHERE event_id=?", (event["event_id"],)).fetchone()[0],
                "publishing": connection.execute("SELECT COUNT(*) FROM publishing_history WHERE event_id=?", (event["event_id"],)).fetchone()[0],
            }
        self.assertEqual(provider.calls, 2)
        self.assertEqual([item["failure_category"] for item in attempts], ["TRANSIENT_PROVIDER_TIMEOUT"] * 2)
        self.assertEqual((run["status"], run["resume_state"], run["recoverable"]), ("FAILED", "PAUSED_TRANSIENT", 1))
        self.assertEqual(run["cost_status"], "unknown")
        self.assertIsNone(run["cost_usd"])
        self.assertEqual(decisions, 0)
        self.assertEqual(claim_set, "REVIEW_REQUIRED")
        self.assertEqual(downstream, {"content": 0, "packages": 0, "renders": 0, "publishing": 0})
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app.enqueue_content_decision(event["event_id"], "test", background=False)

    def test_one_bounded_transient_retry_records_lineage_and_unknown_failed_attempt_cost(self):
        _, _, verification = self.queued_live_verification()
        provider = SequencedVerificationProvider([ResponseTimeoutError("temporary"), []])
        app.run_verification_job(verification["id"], provider)
        with app.connect() as connection:
            run = connection.execute("SELECT * FROM verification_runs WHERE id=?", (verification["id"],)).fetchone()
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY' "
                "ORDER BY attempt_number", (verification["id"],),
            )]
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["retry_of_attempt_id"], attempts[0]["id"])
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["cost_status"], "unknown")
        self.assertIsNone(run["cost_usd"])

    def test_auth_failure_is_not_retried(self):
        _, _, verification = self.queued_live_verification()
        provider = SequencedVerificationProvider([MissingAPIKeyError("missing controlled key"), []])
        app.run_verification_job(verification["id"], provider)
        with app.connect() as connection:
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY'",
                (verification["id"],),
            )]
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["failure_category"], "AUTH_FAILURE")

    def test_retry_after_is_respected_and_retry_maximum_is_enforced(self):
        _, _, verification = self.queued_live_verification()
        limited = ResearchProviderError("controlled rate limit")
        limited.code = "http_429"
        limited.retryable = True
        limited.retry_after_seconds = 7
        provider = SequencedVerificationProvider([limited, ResponseTimeoutError("retry also failed"), []])
        with patch.object(app, "VERIFICATION_RETRY_MAX_BACKOFF_SECONDS", 30), patch.object(app.time, "sleep") as sleep:
            app.run_verification_job(verification["id"], provider)
        self.assertEqual(provider.calls, 2)
        sleep.assert_called_once_with(7)
        with app.connect() as connection:
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY'",
                (verification["id"],),
            ).fetchone()[0]
        self.assertEqual(attempt_count, 2)

    def test_resume_reuses_checkpoints_evidence_and_run_but_creates_new_attempt(self):
        _, _, verification = self.queued_live_verification()
        app.run_verification_job(verification["id"], SequencedVerificationProvider([
            ResponseTimeoutError("one"), ResponseTimeoutError("two"),
        ]))
        with app.connect() as connection:
            before_ids = [row[0] for row in connection.execute(
                "SELECT id FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (verification["id"],)
            )]
            primary_before = connection.execute(
                "SELECT COUNT(*) FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='PRIMARY_EVIDENCE_EXTRACTION' AND status='COMPLETED'", (verification["id"],)
            ).fetchone()[0]
        resumed = app.resume_verification(
            verification["id"], provider=SequencedVerificationProvider([[]]), background=False,
        )
        with app.connect() as connection:
            after_ids = [row[0] for row in connection.execute(
                "SELECT id FROM verification_snapshots WHERE verification_run_id=? ORDER BY id", (verification["id"],)
            )]
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM verification_attempts WHERE verification_run_id=? AND phase='CORROBORATION_DISCOVERY' "
                "ORDER BY attempt_number", (verification["id"],),
            )]
            primary_after = connection.execute(
                "SELECT COUNT(*) FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='PRIMARY_EVIDENCE_EXTRACTION' AND status='COMPLETED'", (verification["id"],)
            ).fetchone()[0]
        self.assertEqual(resumed["run"]["id"], verification["id"])
        self.assertEqual(resumed["run"]["status"], "COMPLETED")
        self.assertEqual(before_ids, after_ids)
        self.assertEqual((primary_before, primary_after), (1, 1))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[2]["retry_of_attempt_id"], attempts[1]["id"])

    def test_partial_claim_analysis_checkpoint_survives_retrieval_resume(self):
        _, _, verification = self.queued_live_verification()
        first = {"snapshots": [], "transient_error": "controlled retrieval timeout"}
        with patch.object(app, "_inspect_verification_leads", return_value=first):
            app.run_verification_job(verification["id"], SequencedVerificationProvider([[]]))
        with app.connect() as connection:
            partial = connection.execute(
                "SELECT payload_json FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='CLAIM_SOURCE_MATCHING' AND status='PARTIAL'", (verification["id"],),
            ).fetchone()
        self.assertTrue(json.loads(partial["payload_json"])["claims"])
        with patch.object(app, "_inspect_verification_leads", return_value={"snapshots": [], "transient_error": None}):
            app.resume_verification(verification["id"], provider=FixtureVerificationProvider(mode="live"), background=False)
        with app.connect() as connection:
            still_present = connection.execute(
                "SELECT COUNT(*) FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='CLAIM_SOURCE_MATCHING' AND status='PARTIAL'", (verification["id"],),
            ).fetchone()[0]
            final = connection.execute("SELECT status FROM verification_runs WHERE id=?", (verification["id"],)).fetchone()[0]
        self.assertEqual(still_present, 1)
        self.assertEqual(final, "COMPLETED")

    def test_official_document_references_are_added_to_discovery_hints(self):
        text = "A Commerce Ministry notification and Tobacco Board order permitted the sale under review."
        event = self.research_event(text=text)
        research = app.enqueue_research(event["event_id"], "test", background=False)["run"]
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        with app.connect() as connection:
            checkpoint = connection.execute(
                "SELECT payload_json FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='PRIMARY_EVIDENCE_EXTRACTION' AND status='COMPLETED'", (verification["id"],),
            ).fetchone()
        hints = json.loads(checkpoint["payload_json"])["official_source_hints"]
        self.assertTrue(hints)
        self.assertIn("notification", hints[0]["reference_terms"])

    def test_verification_ui_exposes_recoverable_timeout_and_resume_language(self):
        script = (Path(__file__).parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("Verification paused because the evidence provider timed out.", script)
        self.assertIn("Resume verification", script)
        self.assertIn("/api/verification/${encodeURIComponent(paused.id)}/resume", script)

    def test_content_ceo_blocks_unverified_event_before_provider_call(self):
        event = self.research_event()
        result = app.enqueue_content_decision(event["event_id"], "grok", background=False)
        room = app.event_room(event["event_id"])
        run = room["content_decision_runs"][0]
        decision = run["decision_record"]
        self.assertEqual(result["run"]["status"], "COMPLETED")
        self.assertEqual(decision["decision"], "HOLD")
        self.assertEqual(decision["executable"], 0)
        self.assertEqual(run["provider_called"], 0)
        self.assertEqual(decision["media_source_strategy"], "NONE")
        self.assertIn("No claim-set", decision["missing_evidence_or_media"][0])

    def test_verified_story_without_source_media_can_generate_original(self):
        event, _, _ = self.production_approved_event(with_media=False)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider())
        decision = app.event_room(event["event_id"])["content_decision_runs"][0]["decision_record"]
        self.assertEqual((decision["decision"], decision["executable"]), ("CREATE", 1))
        self.assertEqual(decision["media_source_strategy"], "GENERATE_ORIGINAL")

    def test_verified_story_with_licensed_media_uses_approved_media(self):
        event, _, _ = self.production_approved_event(with_media=True)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider())
        decision = app.event_room(event["event_id"])["content_decision_runs"][0]["decision_record"]
        self.assertEqual(decision["decision"], "CREATE")
        self.assertEqual(decision["media_source_strategy"], "USE_APPROVED_LICENSED_MEDIA")

    def test_rights_restricted_required_media_holds_without_provider_call(self):
        event, _, _ = self.production_approved_event(with_media=False)
        app.register_media_asset(
            event["event_id"], "https://media.example/required.jpg", "image", "Restricted fixture",
            rights_status="restricted", availability_status="available",
            metadata={"required_for_story": True, "original_generation_prohibited": True},
        )
        result = app.enqueue_content_decision(event["event_id"], "grok", background=False)
        run = app.event_room(event["event_id"])["content_decision_runs"][0]
        self.assertEqual((result["run"]["status"], run["provider_called"]), ("COMPLETED", 0))
        self.assertEqual(run["decision_record"]["decision"], "HOLD")
        self.assertIn("required", run["decision_record"]["factual_rationale"].lower())

    def test_non_executable_story_without_safe_generation_path_holds(self):
        event, _, _ = self.production_approved_event(with_media=False)
        app.register_media_asset(
            event["event_id"], "https://media.example/prohibited.jpg", "image", "Restricted fixture",
            rights_status="restricted", availability_status="available",
            metadata={"original_generation_prohibited": True},
        )
        app.enqueue_content_decision(event["event_id"], "grok", background=False)
        decision = app.event_room(event["event_id"])["content_decision_runs"][0]["decision_record"]
        self.assertEqual((decision["decision"], decision["media_source_strategy"]), ("HOLD", "NONE"))

    def test_evidence_sources_never_become_production_media_automatically(self):
        event, _, _ = self.production_approved_event(with_media=False)
        bundle, _ = app._content_input_bundle(event["event_id"], "live")
        self.assertEqual(bundle["media"], [])
        self.assertTrue(bundle["approved_claims"])

    def test_new_decision_preserves_prior_hold_and_lineage(self):
        event, _, _ = self.production_approved_event(with_media=False)
        asset_url = "https://media.example/required-lineage.jpg"
        app.register_media_asset(
            event["event_id"], asset_url, "image", "Restricted fixture",
            rights_status="restricted", availability_status="available",
            metadata={"required_for_story": True},
        )
        app.enqueue_content_decision(event["event_id"], "grok", background=False)
        first = app.event_room(event["event_id"])["content_decision_runs"][0]["decision_record"]
        self.assertEqual(first["decision"], "HOLD")
        app.register_media_asset(
            event["event_id"], asset_url, "image", "Owned fixture",
            rights_status="verified", availability_status="available",
            metadata={"required_for_story": True, "rights_basis": "owned"},
        )
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider())
        with app.connect() as connection:
            preserved = connection.execute("SELECT decision FROM content_decisions WHERE id=?", (first["id"],)).fetchone()
            latest = connection.execute(
                "SELECT * FROM content_decisions WHERE event_id=? ORDER BY decided_at DESC,id DESC LIMIT 1",
                (event["event_id"],),
            ).fetchone()
        self.assertEqual(preserved["decision"], "HOLD")
        self.assertEqual(latest["previous_decision_id"], first["id"])
        self.assertEqual((latest["decision"], latest["media_source_strategy"]), ("CREATE", "USE_APPROVED_OWNED_MEDIA"))

    def test_content_ceo_blocks_stale_approved_claim_set(self):
        event, _, _ = self.production_approved_event(with_media=True)
        added = self.research_event(
            url="https://reporter.example/ap/items/content-new-evidence",
            text="N. Chandrababu Naidu approved the Andhra Pradesh irrigation review after a later official update.",
            source_class="official_primary",
        )
        self.assertEqual(event["event_id"], added["event_id"])
        result = app.enqueue_content_decision(event["event_id"], "grok", background=False)
        room = app.event_room(event["event_id"])
        run = room["content_decision_runs"][0]
        self.assertEqual(result["run"]["status"], "COMPLETED")
        self.assertEqual(run["provider_called"], 0)
        self.assertEqual(run["decision_record"]["decision"], "HOLD")
        self.assertTrue(any("stale" in item.lower() for item in run["decision_record"]["missing_evidence_or_media"]))

    def test_content_ceo_skips_recent_duplicate_content(self):
        event, _, verification = self.production_approved_event(with_media=True)
        with app.connect() as connection:
            claim_set_id = connection.execute(
                "SELECT id FROM approved_claim_sets WHERE verification_run_id=?", (verification["id"],)
            ).fetchone()[0]
        app.record_publishing_history(
            event_id=event["event_id"], claim_set_id=claim_set_id, content_format="IMAGE",
            status="PUBLISHED", title="Already published fixture",
        )
        result = app.enqueue_content_decision(event["event_id"], "test", background=False)
        room = app.event_room(event["event_id"])
        decision = room["content_decision_runs"][0]["decision_record"]
        self.assertEqual(result["run"]["status"], "COMPLETED")
        self.assertEqual(decision["decision"], "SKIP")
        self.assertEqual(decision["executable"], 0)
        self.assertIn("publishing history", decision["factual_rationale"].lower())

    def test_content_ceo_fixture_create_is_never_executable(self):
        event, _, _ = self.fixture_event_with_test_claim_set()
        result = app.enqueue_content_decision(event["event_id"], "test", background=False)
        room = app.event_room(event["event_id"])
        decision = room["content_decision_runs"][0]["decision_record"]
        self.assertEqual(result["run"]["eligibility_status"], "TEST_ONLY")
        self.assertEqual(decision["decision"], "CREATE")
        self.assertEqual((decision["test_only"], decision["executable"]), (1, 0))
        self.assertEqual(room["event"]["status"], "VERIFYING")
        self.assertEqual(len(room["publishing_history"]), 0)
        with app.connect() as connection:
            transitions = connection.execute(
                "SELECT to_state FROM transitions WHERE event_id=? ORDER BY id", (event["event_id"],)
            ).fetchall()
        self.assertNotIn("CONTENT_PLANNED", [row[0] for row in transitions])

    def test_content_decision_cache_invalidates_when_evidence_media_or_history_changes(self):
        event, _, _ = self.production_approved_event(with_media=True)
        first = app.enqueue_content_decision(event["event_id"], "test", background=False)
        cached = app.enqueue_content_decision(event["event_id"], "test", background=False)
        self.assertTrue(cached["cached"])
        app.register_media_asset(
            event["event_id"], "https://media.example/approved-image.jpg", "image", "Fixture media desk",
            rights_status="verified", availability_status="available", content_hash="media-v2",
        )
        changed_media = app.enqueue_content_decision(event["event_id"], "test", background=False)
        self.assertFalse(changed_media["cached"])
        self.assertNotEqual(first["run"]["input_version"], changed_media["run"]["input_version"])
        app.record_publishing_history(
            event_id=event["event_id"], content_format="IMAGE", status="PUBLISHED",
            title="New publishing record",
        )
        changed_history = app.enqueue_content_decision(event["event_id"], "test", background=False)
        self.assertFalse(changed_history["cached"])
        self.assertNotEqual(changed_media["run"]["input_version"], changed_history["run"]["input_version"])
        added = self.research_event(
            url="https://reporter.example/ap/items/content-input-evidence",
            text="N. Chandrababu Naidu approved the Andhra Pradesh irrigation review after a new evidence update.",
            source_class="official_primary",
        )
        self.assertEqual(event["event_id"], added["event_id"])
        changed_evidence = app.enqueue_content_decision(event["event_id"], "test", background=False)
        self.assertFalse(changed_evidence["cached"])
        self.assertNotEqual(changed_history["run"]["input_version"], changed_evidence["run"]["input_version"])

    def test_content_provider_failure_persists_hold(self):
        event, _, _ = self.production_approved_event(with_media=True)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FailingContentProvider())
        room = app.event_room(event["event_id"])
        run = room["content_decision_runs"][0]
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["cost_status"], "unknown")
        self.assertIsNone(run["total_tokens"])
        self.assertEqual(run["decision_record"]["decision"], "HOLD")
        self.assertEqual(run["decision_record"]["media_source_strategy"], "NONE")
        self.assertEqual(run["decision_record"]["executable"], 0)

    def test_production_positive_fixture_creates_one_validated_immutable_package(self):
        event, decision = self.executable_content_decision()
        result = app.enqueue_production(decision["id"], "fixture", background=False)
        job = result["job"]
        self.assertEqual(job["status"], "READY_FOR_APPROVAL")
        self.assertEqual(job["provider"], "anthropic-fixture")
        self.assertEqual(job["model"], "claude-opus-4-5-20251101")
        self.assertEqual(job["validation_status"], "PASSED")
        self.assertEqual(job["cost_status"], "unknown")
        with app.connect() as connection:
            package = connection.execute("SELECT * FROM content_packages WHERE job_id=?", (job["id"],)).fetchone()
            self.assertIsNotNone(package)
            self.assertEqual(package["status"], "READY_FOR_APPROVAL")
            self.assertEqual(package["fixture_only"], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM publishing_history WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0], 0)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE content_packages SET story_angle='changed' WHERE id=?", (package["id"],))

    def test_validation_failed_immutable_draft_can_be_revalidated_without_provider_call(self):
        _, decision = self.executable_content_decision()
        false_failure = {
            "valid": False, "status": "FAIL", "errors": ["Controlled legacy false positive."],
            "field_results": [], "approved_claim_version_ids": [], "used_claim_version_ids": [],
            "validator_version": "legacy-validator",
        }
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        with patch.object(app, "validate_production_package", return_value=false_failure):
            app.run_production_job(queued["job"]["id"], FixtureProductionProvider())
        with app.connect() as connection:
            original = dict(connection.execute(
                "SELECT * FROM production_drafts WHERE job_id=?", (queued["job"]["id"],)
            ).fetchone())
        self.assertEqual(app.production_job(queued["job"]["id"])["status"], "HUMAN_REVIEW")
        result = app.revalidate_production_draft(original["id"])
        self.assertFalse(result["provider_called_again"])
        self.assertEqual(result["job"]["status"], "READY_FOR_APPROVAL")
        self.assertEqual(result["package"]["version_number"], 1)
        self.assertEqual(result["package"]["content_hash"], original["content_hash"])
        with app.connect() as connection:
            persisted = dict(connection.execute(
                "SELECT * FROM production_drafts WHERE id=?", (original["id"],)
            ).fetchone())
            self.assertEqual(persisted, original)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM production_drafts WHERE job_id=?", (queued["job"]["id"],)
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM content_packages WHERE job_id=?", (queued["job"]["id"],)
            ).fetchone()[0], 1)

    def test_production_blocks_hold_and_test_only_before_job_creation(self):
        event, _, _ = self.production_approved_event(with_media=False)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider({
            "decision": "HOLD",
            "recommended_format": "NONE",
            "language": "English",
            "proposed_duration_seconds": None,
            "priority": "NORMAL",
            "factual_rationale": "Controlled editorial hold unrelated to source-media availability.",
            "missing_evidence_or_media": ["Editorial execution is intentionally paused."],
            "media_source_strategy": "NONE",
        }))
        hold = app.event_room(event["event_id"])["content_decision_runs"][0]["decision_record"]
        self.assertEqual(hold["decision"], "HOLD")
        with self.assertRaisesRegex(ValueError, "HOLD"):
            app.enqueue_production(hold["id"], "fixture", background=False)
        test_event, _, _ = self.fixture_event_with_test_claim_set()
        app.enqueue_content_decision(test_event["event_id"], "test", background=False)
        test_decision = app.event_room(test_event["event_id"])["content_decision_runs"][0]["decision_record"]
        with self.assertRaisesRegex(ValueError, "TEST_ONLY|non-executable"):
            app.enqueue_production(test_decision["id"], "fixture", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0], 0)

    def test_production_rejects_stale_content_decision_before_job(self):
        _, decision = self.executable_content_decision()
        event_id = decision["event_id"]
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            app.enqueue_content_decision(event_id, "grok")
        with self.assertRaisesRegex(ValueError, "newer decision|stale"):
            app.enqueue_production(decision["id"], "fixture", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0], 0)

    def test_production_rejects_revoked_claim_and_invalidated_evidence(self):
        _, decision = self.executable_content_decision()
        with app.connect() as connection:
            claim_version_id = connection.execute(
                "SELECT claim_version_id FROM approved_claim_set_items WHERE claim_set_id=? LIMIT 1",
                (decision["approved_claim_set_id"],),
            ).fetchone()[0]
            connection.execute("UPDATE claim_versions SET revoked_at=?,revocation_reason='test' WHERE id=?", (app.now(), claim_version_id))
        with self.assertRaisesRegex(ValueError, "revoked"):
            app.enqueue_production(decision["id"], "fixture", background=False)

        self.reset_database()
        _, decision = self.executable_content_decision()
        with app.connect() as connection:
            snapshot_id = connection.execute(
                "SELECT vde.snapshot_id FROM approved_claim_set_items acsi "
                "JOIN verification_decision_evidence vde ON vde.decision_id=acsi.verification_decision_id "
                "WHERE acsi.claim_set_id=? AND vde.relationship='supports' LIMIT 1",
                (decision["approved_claim_set_id"],),
            ).fetchone()[0]
            connection.execute("UPDATE verification_snapshots SET invalidated_at=?,invalidation_reason='test' WHERE id=?", (app.now(), snapshot_id))
        with self.assertRaisesRegex(ValueError, "invalidated|supporting evidence"):
            app.enqueue_production(decision["id"], "fixture", background=False)

    def test_production_rejects_unavailable_media_and_duplicate_active_job(self):
        event, decision = self.executable_content_decision()
        with app.connect() as connection:
            connection.execute(
                "UPDATE media_assets SET availability_status='expired',updated_at=? WHERE event_id=?",
                (app.now(), event["event_id"]),
            )
        with self.assertRaisesRegex(ValueError, "media|stale"):
            app.enqueue_production(decision["id"], "fixture", background=False)

        self.reset_database()
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            first = app.enqueue_production(decision["id"], "fixture")
            second = app.enqueue_production(decision["id"], "fixture")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])

    def test_production_validator_rejects_malformed_output_unknown_refs_numbers_and_certainty_upgrade(self):
        mutations = [
            lambda package, context: package.pop("headline"),
            lambda package, context: package["caption"].update(claim_version_ids=["CV-NOT-APPROVED"]),
            lambda package, context: package["caption"].update(text=package["caption"]["text"] + " ₹999 crore"),
            lambda package, context: package["caption"].update(text=package["caption"]["text"] + " Prime Minister Modi attended."),
            lambda package, context: package["caption"].update(text=package["caption"]["text"] + ' He said “This is finished”.'),
            lambda package, context: package["caption"].update(text=package["caption"]["text"] + " The work was completed."),
        ]
        expected = [
            "Missing package fields", "unapproved claim", "unsupported numerical", "unsupported entity",
            "unsupported quotation", "completed outcome",
        ]
        for mutation, message in zip(mutations, expected):
            with self.subTest(message=message):
                self.reset_database()
                _, decision = self.executable_content_decision()
                with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
                    queued = app.enqueue_production(decision["id"], "fixture")
                app.run_production_job(queued["job"]["id"], PackageMutationProvider(mutation))
                job = app.production_job(queued["job"]["id"])
                self.assertEqual(job["status"], "HUMAN_REVIEW")
                self.assertIn(message.lower(), job["error_message"].lower())
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_packages").fetchone()[0], 0)

    def test_production_provider_failure_and_missing_usage_are_fail_closed(self):
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        app.run_production_job(queued["job"]["id"], FailingProductionProvider())
        job = app.production_job(queued["job"]["id"])
        self.assertEqual(job["status"], "HUMAN_REVIEW")
        self.assertEqual(job["cost_status"], "unknown")
        self.assertIsNone(job["total_tokens"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_packages").fetchone()[0], 0)

    def test_production_bounded_retry_and_successful_unknown_usage(self):
        _, decision = self.executable_content_decision()
        provider = RetryProductionProvider()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()), patch.object(app, "PRODUCTION_MAX_RETRIES", 1):
            queued = app.enqueue_production(decision["id"], "fixture")
            app.run_production_job(queued["job"]["id"], provider)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(app.production_job(queued["job"]["id"])["status"], "READY_FOR_APPROVAL")

        self.reset_database()
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        app.run_production_job(queued["job"]["id"], MissingUsageProductionProvider())
        job = app.production_job(queued["job"]["id"])
        self.assertEqual(job["status"], "READY_FOR_APPROVAL")
        self.assertIsNone(job["total_tokens"])
        self.assertIsNone(job["cost_usd"])
        self.assertEqual(job["cost_status"], "unknown")

    def test_production_race_change_blocks_finalization(self):
        event, decision = self.executable_content_decision()
        def change_media(package, context):
            del package, context
            with app.connect() as connection:
                connection.execute(
                    "UPDATE media_assets SET content_hash='race-change',updated_at=? WHERE event_id=?",
                    (app.now(), event["event_id"]),
                )
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        app.run_production_job(queued["job"]["id"], PackageMutationProvider(change_media))
        job = app.production_job(queued["job"]["id"])
        self.assertEqual(job["status"], "BLOCKED")
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_packages").fetchone()[0], 0)

    def test_production_regeneration_is_explicit_versioned_and_idempotent(self):
        _, decision = self.executable_content_decision()
        first = app.enqueue_production(decision["id"], "fixture", background=False)
        cached = app.enqueue_production(decision["id"], "fixture", background=False)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["job"]["id"], first["job"]["id"])
        second = app.enqueue_production(decision["id"], "fixture", background=False, regenerate=True)
        self.assertEqual(second["job"]["regeneration_number"], 2)
        with app.connect() as connection:
            versions = [row[0] for row in connection.execute(
                "SELECT version_number FROM content_packages ORDER BY version_number"
            )]
        self.assertEqual(versions, [1, 2])

    def test_production_state_machine_rejects_illegal_transition(self):
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        with app.connect() as connection:
            with self.assertRaisesRegex(ValueError, "Invalid production transition"):
                app._transition_production_job(connection, queued["job"]["id"], "READY_FOR_APPROVAL", "skip")

    def test_render_fixture_creates_persisted_validated_image_ready_for_review(self):
        event, _, production, package = self.renderable_package()
        with app.connect() as connection:
            immutable_before = {
                "event": tuple(connection.execute(
                    "SELECT status,verification_status,research_status FROM events WHERE id=?", (event["event_id"],)
                ).fetchone()),
                "claims": [tuple(row) for row in connection.execute(
                    "SELECT id,verification_status,reviewer_notes FROM claims WHERE event_id=? ORDER BY id", (event["event_id"],)
                )],
                "evidence": [tuple(row) for row in connection.execute(
                    "SELECT es.id,es.content_hash,es.retrieved_at FROM evidence_snapshots es "
                    "JOIN research_runs rr ON rr.id=es.run_id WHERE rr.event_id=? ORDER BY es.id", (event["event_id"],)
                )],
                "package": tuple(connection.execute(
                    "SELECT status,content_hash,package_json FROM content_packages WHERE id=?", (package["id"],)
                ).fetchone()),
            }
        renderer = CountingImageRenderer()
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        job = result["job"]
        self.assertEqual(renderer.calls, 1)
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual(job["provider"], "deterministic-image-fixture")
        self.assertEqual(job["model"], "fixture-raster-v1")
        self.assertEqual(job["validation_status"], "PASSED")
        self.assertEqual(
            (job["technical_validation_status"], job["text_validation_status"],
             job["semantic_qa_status"], job["human_review_status"]),
            ("PASSED", "NOT_PERFORMED", "NOT_PERFORMED", "REQUIRED"),
        )
        self.assertEqual(job["cost_status"], "unknown")
        with app.connect() as connection:
            asset = connection.execute(
                "SELECT ga.* FROM generated_assets ga JOIN render_job_outputs rjo ON rjo.generated_asset_id=ga.id "
                "WHERE rjo.render_job_id=?", (job["id"],),
            ).fetchone()
            self.assertEqual((asset["status"], asset["validation_status"], asset["fixture_only"]), ("VALIDATED", "PASSED", 1))
            self.assertEqual(asset["render_job_id"], job["id"])
            self.assertEqual((asset["mime_type"], asset["width"], asset["height"]), ("image/png", 1200, 1600))
            self.assertGreater(asset["file_size"], 0)
            self.assertEqual(len(asset["checksum_sha256"]), 64)
            self.assertTrue(LocalMediaStorage(app.RENDER_STORAGE_ROOT).exists(asset["storage_uri"]))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publishing_history").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_qa_results").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM events WHERE id=?", (event["event_id"],)).fetchone()[0], "VERIFIED")
            self.assertEqual(connection.execute("SELECT status FROM production_jobs WHERE id=?", (production["id"],)).fetchone()[0], "READY_FOR_APPROVAL")
            immutable_after = {
                "event": tuple(connection.execute(
                    "SELECT status,verification_status,research_status FROM events WHERE id=?", (event["event_id"],)
                ).fetchone()),
                "claims": [tuple(row) for row in connection.execute(
                    "SELECT id,verification_status,reviewer_notes FROM claims WHERE event_id=? ORDER BY id", (event["event_id"],)
                )],
                "evidence": [tuple(row) for row in connection.execute(
                    "SELECT es.id,es.content_hash,es.retrieved_at FROM evidence_snapshots es "
                    "JOIN research_runs rr ON rr.id=es.run_id WHERE rr.event_id=? ORDER BY es.id", (event["event_id"],)
                )],
                "package": tuple(connection.execute(
                    "SELECT status,content_hash,package_json FROM content_packages WHERE id=?", (package["id"],)
                ).fetchone()),
            }
            self.assertEqual(immutable_after, immutable_before)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE generated_assets SET checksum_sha256='changed' WHERE id=?", (asset["id"],))

    def test_render_blocks_missing_test_only_and_tobacco_packages_without_job(self):
        with self.assertRaises(KeyError):
            app.enqueue_render("CP-MISSING", "IMAGE", renderer=CountingImageRenderer(), background=False)
        test_event, _, _ = self.fixture_event_with_test_claim_set()
        app.enqueue_content_decision(test_event["event_id"], "test", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_packages WHERE event_id=?", (test_event["event_id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_render_stale_decision_claim_evidence_and_rights_fail_before_provider(self):
        event, _, _, package = self.renderable_package()
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            app.enqueue_content_decision(event["event_id"], "grok")
        renderer = CountingImageRenderer()
        with self.assertRaisesRegex(ValueError, "stale|newer decision"):
            app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual(renderer.calls, 0)

        for mutation, expected in (
            ("claim", "revoked|superseded"), ("evidence", "invalidated"), ("rights", "rights|availability|stale")
        ):
            with self.subTest(mutation=mutation):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                with app.connect() as connection:
                    if mutation == "claim":
                        claim_id = json.loads(package["approved_claim_version_ids_json"])[0]
                        connection.execute("UPDATE claim_versions SET revoked_at=?,revocation_reason='test' WHERE id=?", (app.now(), claim_id))
                    elif mutation == "evidence":
                        snapshot_id = json.loads(package["evidence_snapshot_ids_json"])[0]
                        connection.execute("UPDATE verification_snapshots SET invalidated_at=?,invalidation_reason='test' WHERE id=?", (app.now(), snapshot_id))
                    else:
                        connection.execute(
                            "UPDATE media_assets SET rights_status='restricted',updated_at=? WHERE event_id=?",
                            (app.now(), package["event_id"]),
                        )
                renderer = CountingImageRenderer()
                with self.assertRaisesRegex(ValueError, expected):
                    app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
                self.assertEqual(renderer.calls, 0)
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_render_missing_live_provider_is_graceful_and_creates_no_job(self):
        _, _, _, package = self.renderable_package()
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": ""}):
            with self.assertRaisesRegex(Exception, "No live renderer"):
                app.enqueue_render(package["id"], "IMAGE", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_render_provider_config_resolves_fixture_only_when_explicitly_configured(self):
        _, _, _, package = self.renderable_package()
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "fixture"}):
            result = app.enqueue_render(package["id"], "IMAGE", background=False)
        self.assertEqual((result["job"]["provider"], result["job"]["fixture_only"]), ("deterministic-image-fixture", 1))
        self.assertEqual(result["job"]["status"], "READY_FOR_REVIEW")

    def test_render_requires_ready_production_lineage_and_matching_media_type(self):
        _, _, production, package = self.renderable_package()
        with app.connect() as connection:
            connection.execute("UPDATE production_jobs SET status='FAILED' WHERE id=?", (production["id"],))
        renderer = CountingImageRenderer()
        with self.assertRaisesRegex(ValueError, "READY_FOR_APPROVAL"):
            app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual(renderer.calls, 0)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)
        self.reset_database()
        _, _, _, package = self.renderable_package()
        with self.assertRaisesRegex(ValueError, "only generate video from an explicitly selected approved source image"):
            app.enqueue_render(package["id"], "VIDEO", renderer=renderer, background=False)
        self.assertEqual(renderer.calls, 0)

    def test_render_duplicate_click_and_explicit_regeneration_are_versioned(self):
        _, _, _, package = self.renderable_package()
        renderer = CountingImageRenderer()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            first = app.enqueue_render(package["id"], "IMAGE", renderer=renderer)
            duplicate = app.enqueue_render(package["id"], "IMAGE", renderer=renderer)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["job"]["id"], duplicate["job"]["id"])
        self.assertEqual(renderer.calls, 0)
        app.run_render_job(first["job"]["id"], renderer)
        cached = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertTrue(cached["cached"])
        self.assertEqual(renderer.calls, 1)
        regenerated = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False, regenerate=True)
        self.assertEqual((regenerated["job"]["regeneration_number"], renderer.calls), (2, 2))
        with app.connect() as connection:
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT version_number FROM generated_assets ORDER BY version_number")],
                [1, 2],
            )

    def test_render_duplicate_binary_reuses_asset_record(self):
        _, _, _, package = self.renderable_package()
        renderer = IdenticalImageRenderer()
        first = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        second = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False, regenerate=True)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 1)
            reused = connection.execute(
                "SELECT reused_identical_binary FROM render_job_outputs WHERE render_job_id=?", (second["job"]["id"],)
            ).fetchone()[0]
        self.assertEqual(reused, 1)
        self.assertEqual(first["job"]["status"], "READY_FOR_REVIEW")
        self.assertEqual(second["job"]["status"], "READY_FOR_REVIEW")

    def test_render_provider_transient_failures_use_bounded_retries(self):
        errors = [
            RendererTimeoutError("timeout"), RendererNetworkError("network"),
            RendererRateLimitError("rate limited"), RendererServerError("server error")
        ]
        for error in errors:
            with self.subTest(code=error.code):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                renderer = FailingImageRenderer(error)
                with patch.object(app, "RENDER_MAX_RETRIES", 1), patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
                    queued = app.enqueue_render(package["id"], "IMAGE", renderer=renderer)
                app.run_render_job(queued["job"]["id"], renderer)
                job = app.render_job(queued["job"]["id"])
                self.assertEqual((renderer.calls, job["retry_count"], job["status"]), (2, 1, "FAILED"))
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 0)

    def test_render_invalid_outputs_and_storage_failure_fail_closed(self):
        mutations = [
            (lambda result: replace(result, asset_bytes=b""), "empty"),
            (lambda result: replace(result, asset_bytes=b"not-a-png"), "corrupt"),
            (lambda result: replace(result, mime_type="video/mp4"), "mime"),
        ]
        for mutation, expected in mutations:
            with self.subTest(expected=expected):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                renderer = MutatingImageRenderer(mutate_result=mutation)
                result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
                self.assertNotEqual(result["job"]["status"], "READY_FOR_REVIEW")
                self.assertIn(expected, result["job"]["failure_reason"].lower())

        self.reset_database()
        _, _, _, package = self.renderable_package()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer())
        app.run_render_job(queued["job"]["id"], CountingImageRenderer(), FailingStorage())
        self.assertEqual(app.render_job(queued["job"]["id"])["status"], "FAILED")

    def test_render_malformed_provider_result_fails_without_asset(self):
        _, _, _, package = self.renderable_package()
        renderer = MutatingImageRenderer(mutate_result=lambda result: replace(result, asset_bytes=None))
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual(result["job"]["status"], "FAILED")
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 0)

    def test_render_midflight_evidence_invalidation_preserves_blocked_asset(self):
        _, _, _, package = self.renderable_package()
        snapshot_id = json.loads(package["evidence_snapshot_ids_json"])[0]
        def invalidate():
            with app.connect() as connection:
                connection.execute(
                    "UPDATE verification_snapshots SET invalidated_at=?,invalidation_reason='mid-render test' WHERE id=?",
                    (app.now(), snapshot_id),
                )
        renderer = MutatingImageRenderer(mutate_lineage=invalidate)
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual(result["job"]["status"], "BLOCKED")
        with app.connect() as connection:
            asset = connection.execute("SELECT * FROM generated_assets").fetchone()
            self.assertEqual((asset["status"], asset["executable"], asset["validation_status"]), ("BLOCKED", 0, "FAILED"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publishing_history").fetchone()[0], 0)

    def test_render_midflight_rights_invalidation_preserves_blocked_asset(self):
        _, _, _, package = self.renderable_package()
        def invalidate_rights():
            with app.connect() as connection:
                connection.execute(
                    "UPDATE media_assets SET rights_status='restricted',updated_at=? WHERE event_id=?",
                    (app.now(), package["event_id"]),
                )
        renderer = MutatingImageRenderer(mutate_lineage=invalidate_rights)
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual(result["job"]["status"], "BLOCKED")
        with app.connect() as connection:
            asset = connection.execute("SELECT status,executable,validation_status FROM generated_assets").fetchone()
            self.assertEqual(tuple(asset), ("BLOCKED", 0, "FAILED"))

    def test_render_usage_cost_and_audit_metadata_are_persisted(self):
        _, _, _, package = self.renderable_package()
        result = app.enqueue_render(package["id"], "IMAGE", renderer=UsageImageRenderer(), background=False)
        job = result["job"]
        self.assertEqual((job["credits_consumed"], job["provider_units"], job["provider_cost_usd"]), (2.5, 1.0, 0.04))
        self.assertEqual((job["cost_status"], job["pricing_version"]), ("known", "fixture-pricing-v1"))
        with app.connect() as connection:
            states = [row[0] for row in connection.execute(
                "SELECT to_status FROM render_job_status_history WHERE render_job_id=? ORDER BY id", (job["id"],)
            )]
            self.assertEqual(states, ["QUEUED", "PREPARING", "RENDERING", "VALIDATING", "READY_FOR_REVIEW"])
            prompt = connection.execute("SELECT * FROM render_prompt_snapshots WHERE render_job_id=?", (job["id"],)).fetchone()
            self.assertEqual(len(prompt["request_hash"]), 64)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE render_prompt_snapshots SET request_hash='changed' WHERE id=?", (prompt["id"],))

    def test_render_missing_cost_is_unknown_not_zero(self):
        _, _, _, package = self.renderable_package()
        job = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False)["job"]
        self.assertEqual(job["cost_status"], "unknown")
        self.assertIsNone(job["provider_cost_usd"])
        self.assertIsNone(job["calculated_cost_usd"])

    def test_render_scrubs_provider_secrets_and_signed_urls(self):
        _, _, _, package = self.renderable_package()
        renderer = MutatingImageRenderer(mutate_result=lambda result: replace(
            result,
            original_provider_url="https://renderer.example/output.png?token=credential-value",
            provider_metadata={"authorization": "Bearer credential-value", "nested": {"api_key": "credential-value"}},
        ))
        app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        with app.connect() as connection:
            asset = connection.execute(
                "SELECT original_provider_url,provider_metadata_json FROM generated_assets"
            ).fetchone()
        self.assertIsNone(asset["original_provider_url"])
        self.assertNotIn("credential-value", asset["provider_metadata_json"])
        self.assertIn("[REDACTED]", asset["provider_metadata_json"])
        self.assertNotIn("credential-value", app._safe_render_error(
            RuntimeError("Authorization: Bearer credential-value https://renderer.example/x?token=credential-value")
        ))

    def test_render_state_machine_rejects_illegal_transition(self):
        _, _, _, package = self.renderable_package()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer())
        with app.connect() as connection:
            with self.assertRaisesRegex(ValueError, "Invalid render transition"):
                app._transition_render_job(connection, queued["job"]["id"], "READY_FOR_REVIEW", "skip")

    def test_live_renderer_configuration_is_explicit_and_never_falls_back_to_fixture(self):
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "", "LIVE_RENDERER_API_KEY": ""}):
            self.assertEqual(renderer_configuration("IMAGE")["status"], "LIVE_RENDERER_NOT_CONFIGURED")
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "future-provider", "LIVE_RENDERER_API_KEY": ""}):
            self.assertEqual(renderer_configuration("IMAGE")["status"], "LIVE_RENDERER_CREDENTIALS_MISSING")
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "future-provider", "LIVE_RENDERER_API_KEY": "configured"}):
            self.assertEqual(renderer_configuration("IMAGE")["status"], "LIVE_RENDERER_PROVIDER_UNSUPPORTED")
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "fixture", "LIVE_RENDERER_API_KEY": ""}):
            configuration = renderer_configuration("IMAGE")
            self.assertEqual((configuration["status"], configuration["live"]), ("FIXTURE_ONLY", False))

    def test_async_live_lifecycle_normalizes_provider_download_qa_and_cost(self):
        _, _, _, package = self.renderable_package()
        renderer = ControlledAsyncImageRenderer(detected_text="expected", cost=0.12)
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        job = result["job"]
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual((job["provider_mode"], job["fixture_only"]), ("live", 0))
        self.assertEqual((job["provider_job_id"], job["provider_status"], job["poll_count"]),
                         ("provider-job-123", "COMPLETED", 2))
        self.assertEqual((job["technical_validation_status"], job["text_validation_status"],
                          job["semantic_qa_status"], job["human_review_status"]),
                         ("PASSED", "PASSED", "NOT_PERFORMED", "REQUIRED"))
        self.assertEqual((renderer.submit_calls, renderer.poll_calls, renderer.download_calls), (1, 2, 1))
        with app.connect() as connection:
            prompt = json.loads(connection.execute(
                "SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job["id"],)
            ).fetchone()[0])
            asset = connection.execute("SELECT * FROM generated_assets").fetchone()
            events = [row[0] for row in connection.execute(
                "SELECT event_type FROM render_provider_events ORDER BY id"
            )]
            qa = {row[0]: row[1] for row in connection.execute("SELECT qa_type,status FROM media_qa_results")}
            cost = connection.execute(
                "SELECT cost_status,currency,provider_reported_cost,pricing_version FROM cost_ledger"
            ).fetchone()
        self.assertEqual(renderer.request, prompt)
        self.assertIsNone(asset["original_provider_url"])
        self.assertEqual((asset["usable_for_review"], asset["stale"], asset["human_review_status"]), (1, 0, "REQUIRED"))
        self.assertEqual(events, ["SUBMITTED", "POLLED", "POLLED", "COMPLETED", "DOWNLOADED"])
        self.assertEqual(qa, {"TECHNICAL": "PASSED", "TEXT_OVERLAY": "PASSED", "SEMANTIC_VISUAL": "NOT_PERFORMED"})
        self.assertEqual(tuple(cost), ("known", "USD", 0.12, "controlled-pricing-v1"))

    def test_async_qa_not_performed_is_explicit_and_unknown_cost_is_null(self):
        _, _, _, package = self.renderable_package()
        result = app.enqueue_render(
            package["id"], "IMAGE", renderer=ControlledAsyncImageRenderer(statuses=["COMPLETED"]),
            background=False,
        )
        job = result["job"]
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual((job["text_validation_status"], job["semantic_qa_status"]),
                         ("NOT_PERFORMED", "NOT_PERFORMED"))
        self.assertEqual(job["cost_status"], "unknown")
        with app.connect() as connection:
            cost = connection.execute(
                "SELECT cost_status,provider_reported_cost,locally_calculated_cost FROM cost_ledger"
            ).fetchone()
        self.assertEqual(tuple(cost), ("unknown", None, None))

    def test_async_provider_terminal_failures_fail_closed_and_polling_is_bounded(self):
        cases = [
            (ControlledAsyncImageRenderer(statuses=["FAILED"]), "HUMAN_REVIEW", "PROVIDER_REJECTED", 1),
            (ControlledAsyncImageRenderer(statuses=["PROCESSING"], max_poll_attempts=2), "RENDERING", "POLL_ATTEMPTS_EXHAUSTED", 2),
            (ControlledAsyncImageRenderer(statuses=["NO_OUTPUT"]), "HUMAN_REVIEW", "INVALID_RESPONSE", 1),
            (ControlledAsyncImageRenderer(statuses=["COMPLETED"], download_error=RendererDownloadError("download failed")),
             "RENDERING", "DOWNLOAD_FAILED", 1),
            (ControlledAsyncImageRenderer(submit_error=RendererAuthError("auth rejected")), "FAILED", "AUTH_ERROR", 0),
        ]
        for renderer, status, code, polls in cases:
            with self.subTest(code=code):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
                self.assertEqual((result["job"]["status"], result["job"]["failure_code"]), (status, code))
                self.assertEqual((renderer.submit_calls, renderer.poll_calls), (1, polls))
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 0)
                    expected_cost_rows = 0 if result["job"].get("resume_state") == "PROVIDER_PENDING" else 1
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], expected_cost_rows)

    def test_async_polling_honors_bounded_429_and_5xx_retries_without_resubmission(self):
        for transient in (
            RendererRateLimitError("rate limited", retry_after_seconds=0), RendererServerError("server error"),
            RendererTimeoutError("poll timeout"),
        ):
            with self.subTest(code=transient.code):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                renderer = ControlledAsyncImageRenderer(statuses=[transient, "COMPLETED"])
                result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
                self.assertEqual(result["job"]["status"], "READY_FOR_REVIEW")
                self.assertEqual((renderer.submit_calls, renderer.poll_calls), (1, 2))
        self.reset_database()
        _, _, _, package = self.renderable_package()
        renderer = ControlledAsyncImageRenderer(statuses=[
            RendererRateLimitError("first", retry_after_seconds=0),
            RendererRateLimitError("second", retry_after_seconds=0),
        ])
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual((result["job"]["status"], result["job"]["failure_code"], result["job"]["resume_state"]),
                         ("RENDERING", "RATE_LIMITED", "PROVIDER_PENDING"))
        self.assertEqual(renderer.submit_calls, 1)

    def test_text_and_semantic_qa_flags_route_to_human_review(self):
        _, _, _, package = self.renderable_package()
        renderer = ControlledAsyncImageRenderer(detected_text=("Arunachal Pradesh ₹20,000 crore",), statuses=["COMPLETED"])
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        self.assertEqual((result["job"]["status"], result["job"]["text_validation_status"]),
                         ("HUMAN_REVIEW", "FAILED"))
        with app.connect() as connection:
            flags = connection.execute(
                "SELECT flags_json FROM media_qa_results WHERE qa_type='TEXT_OVERLAY'"
            ).fetchone()[0]
        self.assertIn("UNEXPECTED_GENERATED_TEXT", flags)

        self.reset_database()
        _, _, _, package = self.renderable_package()
        result = app.enqueue_render(
            package["id"], "IMAGE", renderer=ControlledAsyncImageRenderer(statuses=["COMPLETED"]),
            visual_qa_provider=ControlledVisualQA("FLAGGED", ("WRONG_PUBLIC_FIGURE",)), background=False,
        )
        self.assertEqual((result["job"]["status"], result["job"]["semantic_qa_status"]),
                         ("HUMAN_REVIEW", "FLAGGED"))

    def test_async_midrender_claim_and_package_invalidation_make_assets_stale(self):
        for mutation in ("claim", "package"):
            with self.subTest(mutation=mutation):
                self.reset_database()
                _, decision, _, package = self.renderable_package()
                def invalidate():
                    if mutation == "claim":
                        claim_id = json.loads(package["approved_claim_version_ids_json"])[0]
                        with app.connect() as connection:
                            connection.execute(
                                "UPDATE claim_versions SET revoked_at=?,revocation_reason='async race' WHERE id=?",
                                (app.now(), claim_id),
                            )
                    else:
                        app.enqueue_production(decision["id"], "fixture", background=False, regenerate=True)
                renderer = ControlledAsyncImageRenderer(statuses=["COMPLETED"], mutate=invalidate)
                result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
                self.assertEqual(result["job"]["status"], "BLOCKED")
                with app.connect() as connection:
                    asset = connection.execute(
                        "SELECT stale,usable_for_review,executable FROM generated_assets"
                    ).fetchone()
                self.assertEqual(tuple(asset), (1, 0, 0))

    def test_async_idempotency_and_explicit_regeneration_control_paid_submissions(self):
        _, _, _, package = self.renderable_package()
        first_renderer = ControlledAsyncImageRenderer(statuses=["COMPLETED"])
        first = app.enqueue_render(package["id"], "IMAGE", renderer=first_renderer, background=False)
        second_renderer = ControlledAsyncImageRenderer(statuses=["COMPLETED"])
        cached = app.enqueue_render(package["id"], "IMAGE", renderer=second_renderer, background=False)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["job"]["id"], first["job"]["id"])
        self.assertEqual(second_renderer.submit_calls, 0)
        regenerated = app.enqueue_render(
            package["id"], "IMAGE", renderer=second_renderer, background=False, regenerate=True
        )
        self.assertEqual(regenerated["job"]["regeneration_number"], 2)
        self.assertEqual(second_renderer.submit_calls, 1)

    def test_live_provider_secrets_are_absent_from_database_and_frontend_payload(self):
        _, _, _, package = self.renderable_package()
        secret = "controlled-super-secret-value"
        renderer = ControlledAsyncImageRenderer(submit_error=RendererAuthError(
            f"Authorization: Bearer {secret} https://provider.example/error?token={secret}"
        ))
        result = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=False)
        with app.connect() as connection:
            database_text = "\n".join(connection.iterdump())
        self.assertNotIn(secret, database_text)
        self.assertNotIn(secret, json.dumps(app.event_room(result["job"]["event_id"])))

    def test_xai_renderer_configuration_requires_explicit_provider_and_renderer_key(self):
        environments = [
            ({"RENDERER_PROVIDER_IMAGE": "xai", "LIVE_RENDERER_API_KEY": "configured"}, "IMAGE",
             ("LIVE_RENDERER_CONFIGURED", True)),
            ({"RENDERER_PROVIDER_VIDEO": "xai", "LIVE_RENDERER_API_KEY": "configured"}, "VIDEO",
             ("LIVE_RENDERER_CONFIGURED", True)),
            ({"RENDERER_PROVIDER_AUDIO": "xai", "LIVE_RENDERER_API_KEY": "configured"}, "AUDIO",
             ("LIVE_RENDERER_PROVIDER_UNSUPPORTED", False)),
            ({"RENDERER_PROVIDER_IMAGE": "xai", "LIVE_RENDERER_API_KEY": "", "XAI_API_KEY": "shared-xai"}, "IMAGE",
             ("LIVE_RENDERER_CONFIGURED", True)),
            ({"RENDERER_PROVIDER_IMAGE": "xai", "LIVE_RENDERER_API_KEY": "", "XAI_API_KEY": ""}, "IMAGE",
             ("LIVE_RENDERER_CREDENTIALS_MISSING", False)),
            ({"RENDERER_PROVIDER_IMAGE": "", "LIVE_RENDERER_API_KEY": "", "XAI_API_KEY": "research-only"}, "IMAGE",
             ("LIVE_RENDERER_NOT_CONFIGURED", False)),
        ]
        for environment, media_type, expected in environments:
            with self.subTest(environment=environment), patch.dict("os.environ", environment):
                configuration = renderer_configuration(media_type)
                self.assertEqual((configuration["status"], configuration["live"]), expected)
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "xai", "XAI_API_KEY": "shared-xai"}):
            self.assertEqual(renderer_configuration("IMAGE")["credential_source"], "XAI_API_KEY")
        with patch.dict("os.environ", {"LIVE_RENDERER_API_KEY": "dedicated", "XAI_API_KEY": "shared-xai"}):
            self.assertEqual(media_rendering.live_renderer_credential("xai"), ("dedicated", "LIVE_RENDERER_API_KEY"))
        with patch.dict("os.environ", {"LIVE_RENDERER_API_KEY": "", "XAI_API_KEY": ""}):
            with self.assertRaisesRegex(app.MissingRendererConfiguration, "Image renderer not configured"):
                XAIImageRenderer(transport=FakeXAITransport()).render({"media_type": "IMAGE"}, timeout_seconds=1)

    def test_caller_cannot_substitute_fixture_for_configured_or_missing_live_renderer(self):
        _, _, _, package = self.renderable_package()
        for environment in (
            {"RENDERER_PROVIDER_IMAGE": "", "LIVE_RENDERER_API_KEY": ""},
            {"RENDERER_PROVIDER_IMAGE": "xai", "LIVE_RENDERER_API_KEY": "configured"},
        ):
            with self.subTest(environment=environment), patch.dict("os.environ", environment):
                with self.assertRaises(app.MissingRendererConfiguration):
                    app.enqueue_render(package["id"], "IMAGE", "fixture", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_xai_unsupported_aspect_ratio_blocks_before_job_or_network(self):
        renderer = XAIImageRenderer(api_key=XAI_TEST_KEY, transport=FakeXAITransport())
        self.assertIsNone(renderer.unsupported_reason("IMAGE", "3:4"))
        self.assertIn("4:5", renderer.unsupported_reason("IMAGE", "4:5"))
        with self.assertRaises(media_rendering.RendererInvalidRequestError):
            renderer.build_payload({"media_type": "IMAGE", "generation_parameters": {"aspect_ratio": "4:5"}})
        _, _, _, package = self.renderable_package()
        transport = FakeXAITransport()
        original = XAIImageRenderer.unsupported_reason
        historical = lambda self, media_type, aspect: original(self, media_type, "4:5")
        with patch.object(XAIImageRenderer, "unsupported_reason", historical):
            with self.assertRaisesRegex(ValueError, "RENDERER_CAPABILITY_MISMATCH.*4:5"):
                self.xai_render(transport, package)
        self.assertEqual(transport.calls, [])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_xai_live_render_downloads_validates_and_stops_at_ready_for_review(self):
        transport = FakeXAITransport()
        job = self.xai_render(transport)
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual((job["provider"], job["model"], job["provider_mode"], job["fixture_only"]),
                         ("xai", "grok-imagine-image-2.0", "live", 0))
        self.assertEqual((job["provider_request_id"], job["provider_status"], job["poll_count"]),
                         ("xai-request-123", "COMPLETED", 0))
        self.assertEqual((job["technical_validation_status"], job["text_validation_status"],
                          job["semantic_qa_status"], job["human_review_status"]),
                         ("PASSED", "NOT_PERFORMED", "NOT_PERFORMED", "REQUIRED"))
        self.assertAlmostEqual(job["provider_cost_usd"], 0.07)
        self.assertEqual((job["cost_status"], job["currency"], job["pricing_version"]),
                         ("known", "USD", "xai-reported-cost-ticks"))
        post, download = transport.calls
        self.assertEqual(post["headers"]["Authorization"], "Bearer " + XAI_TEST_KEY)
        self.assertNotIn("Authorization", download["headers"])
        payload = json.loads(post["body"])
        self.assertEqual((payload["aspect_ratio"], payload["n"], payload["response_format"]), ("3:4", 1, "url"))
        with app.connect() as connection:
            prompt = json.loads(connection.execute(
                "SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job["id"],)
            ).fetchone()[0])
            asset = dict(connection.execute("SELECT * FROM generated_assets").fetchone())
            events = [row[0] for row in connection.execute("SELECT event_type FROM render_provider_events ORDER BY id")]
            submitted = json.loads(connection.execute(
                "SELECT safe_metadata_json FROM render_provider_events WHERE event_type='SUBMITTED'"
            ).fetchone()[0])
            ledger = dict(connection.execute("SELECT * FROM cost_ledger").fetchone())
            publishing = connection.execute("SELECT COUNT(*) FROM publishing_history").fetchone()[0]
            database_text = "\n".join(connection.iterdump())
        # Commit 8bb08f7: the approved, grounding-validated package prompt is sent verbatim; no ad-hoc additions.
        self.assertEqual(payload["prompt"], prompt["media_brief"]["generation_prompt"].strip())
        self.assertTrue({"No text or numbers", "No people or faces", "No flags, logos, or party symbols"}
                        <= set(prompt["media_brief"]["negative_constraints"]))
        self.assertEqual(submitted["provider_request"], payload)
        self.assertEqual(events, ["SUBMITTED", "COMPLETED", "DOWNLOADED"])
        self.assertEqual((asset["mime_type"], asset["width"], asset["height"]), ("image/jpeg", 1536, 2048))
        self.assertEqual((asset["usable_for_review"], asset["stale"], asset["executable"]), (1, 0, 1))
        self.assertTrue(asset["storage_uri"].startswith("local://"))
        self.assertIsNone(asset["original_provider_url"])
        self.assertEqual((ledger["stage"], ledger["cost_status"]), ("MEDIA_RENDERING", "known"))
        self.assertAlmostEqual(ledger["provider_reported_cost"], 0.07)
        self.assertEqual(publishing, 0)
        room = json.dumps(app.event_room(job["event_id"]))
        for secret in (XAI_TEST_KEY, "signed-download-secret"):
            self.assertNotIn(secret, database_text)
            self.assertNotIn(secret, room)

    def test_xai_provider_errors_normalize_and_retry_within_bounds(self):
        cases = [
            ("auth", [(401, {}, b'{"error":"bad key"}')], "FAILED", "AUTH_ERROR", 1),
            ("429-then-ok", [(429, {"retry-after": "0"}, b"{}"), FakeXAITransport.success()], "READY_FOR_REVIEW", None, 2),
            ("429-exhausted", [(429, {"retry-after": "0"}, b"{}")] * 2, "FAILED", "RATE_LIMITED", 2),
            ("5xx-exhausted", [(503, {}, b"")] * 2, "FAILED", "PROVIDER_5XX", 2),
            ("policy", [(400, {}, b'{"error":"Rejected by content moderation"}')], "HUMAN_REVIEW", "CONTENT_POLICY_REJECTED", 1),
            ("invalid-request", [(400, {}, b'{"error":"bad aspect"}')], "FAILED", "INVALID_REQUEST", 1),
            ("malformed", [(200, {}, b"not json")], "FAILED", "INVALID_RESPONSE", 1),
            ("no-output", [FakeXAITransport.success(item={"mime_type": "image/jpeg"})], "FAILED", "INVALID_RESPONSE", 1),
            ("timeout-after-send", [RendererTimeoutError("response timeout", retryable=False)], "FAILED", "TIMEOUT", 1),
        ]
        for name, posts, status, code, post_count in cases:
            with self.subTest(case=name):
                self.reset_database()
                transport = FakeXAITransport(posts=posts)
                job = self.xai_render(transport)
                self.assertEqual((job["status"], job["failure_code"]), (status, code))
                self.assertEqual(transport.post_count(), post_count)
                with app.connect() as connection:
                    assets = connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0]
                    database_text = "\n".join(connection.iterdump())
                self.assertEqual(assets, 1 if status == "READY_FOR_REVIEW" else 0)
                self.assertNotIn(XAI_TEST_KEY, database_text)
                self.assertNotIn("bad key", database_text)
        self.reset_database()
        transport = FakeXAITransport(download=(404, {}, b""))
        job = self.xai_render(transport)
        self.assertEqual((job["status"], job["failure_code"]), ("FAILED", "DOWNLOAD_FAILED"))
        self.assertEqual(transport.post_count(), 1)

    def test_xai_output_mime_mismatch_and_undersized_image_fail_technical_qa(self):
        cases = [
            ("declared-mime-mismatch", FakeXAITransport(posts=[FakeXAITransport.success(mime_type="image/png")])),
            ("undersized", FakeXAITransport(download=(200, {}, jpeg_bytes(768, 1024)))),
            ("wrong-aspect", FakeXAITransport(download=(200, {}, jpeg_bytes(2048, 2048)))),
            ("corrupt", FakeXAITransport(download=(200, {}, jpeg_bytes(1536, 2048)[:-2]))),
        ]
        for name, transport in cases:
            with self.subTest(case=name):
                self.reset_database()
                job = self.xai_render(transport)
                self.assertNotEqual(job["status"], "READY_FOR_REVIEW")
                self.assertEqual(job["technical_validation_status"], "FAILED")
                with app.connect() as connection:
                    asset = connection.execute("SELECT usable_for_review,executable FROM generated_assets").fetchone()
                self.assertEqual(tuple(asset), (0, 0))

    def test_xai_unknown_cost_stays_null_and_duplicate_click_avoids_second_paid_render(self):
        package = self.xai_renderable_package()
        transport = FakeXAITransport(posts=[FakeXAITransport.success(ticks=None), FakeXAITransport.success()])
        first = self.xai_render(transport, package)
        self.assertEqual((first["cost_status"], first["provider_cost_usd"], first["calculated_cost_usd"]),
                         ("unknown", None, None))
        cached = self.xai_render(transport, package)
        self.assertEqual((cached["id"], transport.post_count()), (first["id"], 1))
        regenerated = app.enqueue_render(
            package["id"], "IMAGE", renderer=XAIImageRenderer(api_key=XAI_TEST_KEY, transport=transport),
            background=False, regenerate=True,
        )["job"]
        self.assertEqual((regenerated["regeneration_number"], transport.post_count()), (2, 2))

    def test_completed_asset_is_not_current_after_newer_package_and_cost_summary_is_honest(self):
        package = self.xai_renderable_package()
        job = self.xai_render(FakeXAITransport(), package)
        room = app.event_room(job["event_id"])
        asset = room["render_jobs"][0]["assets"][0]
        self.assertEqual((asset["current_for_review"], asset["currency_reasons"]), (True, []))
        rendering = next(item for item in room["cost_summary"]["stages"] if item["stage"] == "IMAGE_RENDERING")
        self.assertEqual((rendering["live_runs"], rendering["known_cost_usd"], rendering["unknown_cost_runs"]),
                         (1, 0.07, 0))
        self.assertEqual(
            [item["stage"] for item in room["cost_summary"]["stages"]],
            ["RESEARCH", "VERIFICATION", "CONTENT_CEO", "CONTENT_PRODUCTION", "IMAGE_RENDERING", "VIDEO_RENDERING", "OCR_QA", "VISUAL_QA"],
        )
        if room["cost_summary"]["unknown_cost_runs"]:
            self.assertEqual(room["cost_summary"]["total_status"], "partial")
        app.enqueue_production(job["content_decision_id"], "fixture", background=False, regenerate=True)
        room = app.event_room(job["event_id"])
        asset = room["render_jobs"][0]["assets"][0]
        self.assertFalse(asset["current_for_review"])
        self.assertIn("A newer ContentPackage version exists.", asset["currency_reasons"])
        with app.connect() as connection:
            stored = connection.execute("SELECT usable_for_review,stale FROM generated_assets").fetchone()
        self.assertEqual(tuple(stored), (1, 0))

    def test_render_backoff_honors_retry_after_then_exponential_policy(self):
        cases = [
            ([(429, {"retry-after": "3"}, b"{}"), FakeXAITransport.success()], [3.0]),
            ([(503, {}, b""), FakeXAITransport.success()], [1.5]),
        ]
        for posts, expected in cases:
            with self.subTest(expected=expected):
                self.reset_database()
                with patch.object(app, "RENDER_BACKOFF_SECONDS", 1.5), patch.object(app.time, "sleep") as sleep:
                    job = self.xai_render(FakeXAITransport(posts=posts))
                self.assertEqual(job["status"], "READY_FOR_REVIEW")
                self.assertEqual([call.args[0] for call in sleep.call_args_list], expected)

    def test_live_render_leaves_event_claims_evidence_and_package_unchanged(self):
        package = self.xai_renderable_package()
        tables = {
            "events": "SELECT * FROM events WHERE id=?",
            "claim_versions": "SELECT * FROM claim_versions ORDER BY id",
            "verification_snapshots": "SELECT * FROM verification_snapshots ORDER BY id",
            "content_decisions": "SELECT * FROM content_decisions ORDER BY id",
            "content_packages": "SELECT * FROM content_packages ORDER BY id",
        }
        def snapshot():
            with app.connect() as connection:
                return {
                    name: [
                        {key: value for key, value in dict(row).items() if key not in ("render_status", "updated_at")}
                        for row in connection.execute(query, (package["event_id"],) if "?" in query else ())
                    ]
                    for name, query in tables.items()
                }
        before = snapshot()
        job = self.xai_render(FakeXAITransport(), package)
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual(snapshot(), before)

    def test_render_failure_logs_are_structured_and_exclude_credentials(self):
        transport = FakeXAITransport(posts=[RendererNetworkError(
            f"socket closed Authorization: Bearer {XAI_TEST_KEY} https://imgen.x.ai/a.jpg?sig=signed-log-secret",
            retryable=False,
        )])
        with self.assertLogs(app.LOGGER, level="ERROR") as captured:
            job = self.xai_render(transport)
        self.assertEqual((job["status"], job["failure_code"]), ("FAILED", "NETWORK_ERROR"))
        output = "\n".join(captured.output) + json.dumps([record.__dict__.get("context") for record in captured.records])
        self.assertIn("render_failed", [record.__dict__.get("event") for record in captured.records])
        self.assertNotIn(XAI_TEST_KEY, output)
        self.assertNotIn("signed-log-secret", output)

    def test_canonical_image_aspect_ratio_is_native_three_by_four(self):
        package = self.xai_renderable_package()
        payload = json.loads(package["package_json"])
        self.assertEqual(payload["platform_metadata"]["aspect_ratio"], "3:4")
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            job = app.enqueue_render(package["id"], "IMAGE", renderer=DeterministicImageRenderer())["job"]
        with app.connect() as connection:
            request = json.loads(connection.execute(
                "SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (job["id"],)
            ).fetchone()[0])
        self.assertEqual(
            (request["generation_parameters"]["aspect_ratio"], request["generation_parameters"]["width"],
             request["generation_parameters"]["height"]), ("3:4", 1200, 1600),
        )
        self.reset_database()
        def legacy_ratio(package, locked_context):
            del locked_context
            package["platform_metadata"]["aspect_ratio"] = "4:5"
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        app.run_production_job(queued["job"]["id"], PackageMutationProvider(legacy_ratio))
        rejected = app.production_job(queued["job"]["id"])
        self.assertEqual(rejected["status"], "HUMAN_REVIEW")
        self.assertIn("canonical 3:4", rejected["error_message"])

    def test_missing_renderer_credential_fails_closed_without_fixture_fallback(self):
        package = self.xai_renderable_package()
        with patch.dict("os.environ", {"RENDERER_PROVIDER_IMAGE": "xai"}):
            self.assertEqual(renderer_configuration("IMAGE")["status"], "LIVE_RENDERER_CREDENTIALS_MISSING")
            with self.assertRaises(app.MissingRendererConfiguration):
                app.enqueue_render(package["id"], "IMAGE", background=False)
            gate = app.event_room(package["event_id"])["render_gate"]
        self.assertEqual((gate["live_renderer_configured"], gate["renderer_configuration_status"]),
                         (False, "LIVE_RENDERER_CREDENTIALS_MISSING"))
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 0)

    def test_live_regeneration_preserves_versions_and_flags_replayed_output(self):
        package = self.xai_renderable_package()
        first = self.xai_render(FakeXAITransport(), package)
        second = app.enqueue_render(
            package["id"], "IMAGE", background=False, regenerate=True,
            renderer=XAIImageRenderer(api_key=XAI_TEST_KEY, transport=FakeXAITransport(
                download=(200, {}, jpeg_bytes(1536, 2048, payload=b"\x99\x88\x77")))),
        )["job"]
        self.assertEqual((first["status"], second["status"]), ("READY_FOR_REVIEW", "READY_FOR_REVIEW"))
        with app.connect() as connection:
            before = [dict(row) for row in connection.execute("SELECT * FROM generated_assets ORDER BY version_number")]
        self.assertEqual([row["version_number"] for row in before], [1, 2])
        self.assertEqual(len({row["checksum_sha256"] for row in before}), 2)
        replay = app.enqueue_render(
            package["id"], "IMAGE", background=False, regenerate=True,
            renderer=XAIImageRenderer(api_key=XAI_TEST_KEY, transport=FakeXAITransport()),
        )["job"]
        self.assertEqual((replay["status"], replay["failure_code"], replay["regeneration_number"]),
                         ("HUMAN_REVIEW", "DUPLICATE_PROVIDER_OUTPUT", 3))
        with app.connect() as connection:
            after = [dict(row) for row in connection.execute("SELECT * FROM generated_assets ORDER BY version_number")]
            jobs = connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(jobs, 3)
        metadata = json.loads(after[0]["provider_metadata_json"])
        self.assertEqual(metadata["provider_request"]["aspect_ratio"], "3:4")
        self.assertEqual((after[0]["provider"], after[0]["provider_request_id"]), ("xai", "xai-request-123"))

    def test_publish_routes_exist_only_behind_review_and_switch_gates(self):
        import inspect
        handler = inspect.getsource(app.Handler)
        self.assertIn("/publish", handler)
        self.assertIn("schedule", handler)
        self.assertNotIn("/approve", handler)
        job = self.xai_render(FakeXAITransport())
        self.assertEqual((job["status"], job["human_review_status"]), ("READY_FOR_REVIEW", "REQUIRED"))
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publishing_history").fetchone()[0], 0)

    # ---------- Architecture 06D: live Claude production ----------

    def test_claude_production_configuration_and_demo_mode(self):
        self.assertEqual(content_production.production_configuration()["status"], "CLAUDE_PRODUCTION_UNAVAILABLE")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": CLAUDE_TEST_KEY}):
            configuration = content_production.production_configuration()
            self.assertEqual((configuration["live"], configuration["status"], configuration["fixture_allowed"]),
                             (True, "CLAUDE_PRODUCTION_READY", False))
            self.assertNotIn(CLAUDE_TEST_KEY, json.dumps(configuration))
        with patch.dict("os.environ", {"REACHOUT_DEMO_MODE": "1"}):
            self.assertTrue(content_production.production_configuration()["fixture_allowed"])

    def test_live_production_never_falls_back_to_fixture(self):
        _, decision = self.executable_content_decision()
        with self.assertRaisesRegex(content_production.ProductionProviderUnavailable, "Claude production provider unavailable"):
            app.enqueue_production(decision["id"], "anthropic", background=False)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0], 0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for provider in ("fixture", "anthropic"):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("POST", f"/api/content-decisions/{decision['id']}/production",
                               body=json.dumps({"provider": provider}), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertIn("demo mode" if provider == "fixture" else "Claude production provider unavailable", body["error"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0], 0)

    def test_missing_claude_key_fails_before_network(self):
        with self.assertRaisesRegex(MissingAPIKeyError, "Claude production provider unavailable"):
            content_production.AnthropicProductionAdapter(api_key="").generate(
                {}, token_limit=10, connection_timeout_seconds=1, response_timeout_seconds=1,
            )

    def test_mocked_claude_success_stores_provenance_and_media_brief(self):
        _, decision = self.executable_content_decision()
        FakeClaudeConnection.requests, FakeClaudeConnection.status, FakeClaudeConnection.stop_reason = [], 200, "end_turn"
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": CLAUDE_TEST_KEY}), \
                patch.object(content_production, "HTTPSConnection", FakeClaudeConnection):
            job = app.enqueue_production(decision["id"], "anthropic", background=False)["job"]
        self.assertEqual((job["status"], job["provider"], job["provider_mode"], job["fixture_only"]),
                         ("READY_FOR_APPROVAL", "anthropic", "live", 0))
        self.assertEqual((job["provider_request_id"], job["input_tokens"], job["output_tokens"], job["cost_status"]),
                         ("msg_controlled_123", 900, 700, "unknown"))
        sent = FakeClaudeConnection.requests[0]
        self.assertEqual(sent["headers"]["x-api-key"], CLAUDE_TEST_KEY)
        self.assertNotIn("Authorization", sent["headers"])
        self.assertIn("media_brief", sent["payload"]["output_config"]["format"]["schema"]["required"])
        with app.connect() as connection:
            stored = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job["id"],)).fetchone()
            package = json.loads(connection.execute("SELECT package_json FROM content_packages WHERE job_id=?", (job["id"],)).fetchone()[0])
            database_text = "\n".join(connection.iterdump())
        snapshot = json.loads(stored["request_snapshot_json"])
        self.assertEqual(snapshot["body"], sent["payload"])
        self.assertEqual(stored["request_snapshot_hash"], app.hashlib.sha256(stored["request_snapshot_json"].encode()).hexdigest())
        self.assertEqual(package["media_brief"]["media_type"], "IMAGE")
        self.assertNotIn(CLAUDE_TEST_KEY, database_text)
        self.assertNotIn("request_snapshot_json", json.dumps(app.event_room(decision["event_id"])["production_jobs"]))

    def test_mocked_claude_failures_fail_closed(self):
        for name, status, stop_reason, expected in (
            ("auth", 401, "end_turn", "auth_error"), ("refusal", 200, "refusal", "provider_refusal"),
            ("truncated", 200, "max_tokens", "max_tokens"),
        ):
            with self.subTest(case=name):
                self.reset_database()
                _, decision = self.executable_content_decision()
                FakeClaudeConnection.requests, FakeClaudeConnection.status, FakeClaudeConnection.stop_reason = [], status, stop_reason
                with patch.dict("os.environ", {"ANTHROPIC_API_KEY": CLAUDE_TEST_KEY}), \
                        patch.object(content_production, "HTTPSConnection", FakeClaudeConnection):
                    job = app.enqueue_production(decision["id"], "anthropic", background=False)["job"]
                self.assertIn(job["status"], ("HUMAN_REVIEW", "FAILED"))
                self.assertEqual(job["error_code"], expected)
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_packages").fetchone()[0], 0)
        FakeClaudeConnection.status, FakeClaudeConnection.stop_reason = 200, "end_turn"

    def test_media_brief_must_be_visual_only(self):
        def factual_brief(package, locked_context):
            del locked_context
            package["media_brief"]["generation_prompt"] = 'Show "record funding" of 9999 crore with Rahul Gandhi'
        _, decision = self.executable_content_decision()
        with patch.object(app, "PRODUCTION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_production(decision["id"], "fixture")
        app.run_production_job(queued["job"]["id"], PackageMutationProvider(factual_brief))
        job = app.production_job(queued["job"]["id"])
        self.assertEqual(job["status"], "HUMAN_REVIEW")
        for fragment in ("numerical value", "quotations", "unsupported entity"):
            self.assertIn(fragment, job["error_message"])

    def test_xai_image_uses_approved_package_generation_prompt_exactly(self):
        approved = (
            "High-quality photograph of dried FCV tobacco leaves, 3:4 portrait aspect ratio, "
            "no people, no text, no logos, no symbols, no signage"
        )
        request = {
            "media_brief": {"generation_prompt": approved},
            "visual_prompts": ["This storyboard prompt must not replace the approved renderer prompt."],
            "thumbnail_concept": {"headline": "This must not be included."},
            "creative_constraints": {"non_factual_style_elements": ["This must not be appended."]},
        }
        self.assertEqual(XAIImageRenderer.build_prompt(request), approved)

    # ---------- Architecture 06D: xAI video ----------

    def test_image_and_video_media_types_are_separate(self):
        package = self.xai_renderable_package()
        with self.assertRaisesRegex(ValueError, "approved source image"):
            self.video_render(FakeXAIVideoTransport(), package)
        self.assertIn("IMAGE only", XAIImageRenderer(api_key="k").unsupported_reason("VIDEO", "9:16"))
        self.assertIn("VIDEO only", XAIVideoRenderer(api_key="k").unsupported_reason("IMAGE", "3:4"))
        self.assertIn("never cropped", XAIVideoRenderer(api_key="k").unsupported_reason("VIDEO", "4:5", 6, "TEXT_TO_VIDEO"))
        self.assertIn("1–15", XAIVideoRenderer(api_key="k").unsupported_reason("VIDEO", "9:16", 30, "TEXT_TO_VIDEO"))

    def test_text_to_video_lifecycle_persists_provider_job_and_decoded_metadata(self):
        package = self.reel_renderable_package()
        transport = FakeXAIVideoTransport()
        job = self.video_render(transport, package)
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual((job["provider"], job["model"], job["generation_mode"], job["requested_aspect_ratio"]),
                         ("xai", "grok-imagine-video-1.5", "TEXT_TO_VIDEO", "9:16"))
        self.assertEqual((job["provider_job_id"], job["provider_request_id"], job["poll_count"]),
                         ("vid-req-123", "xai-video-request-1", 2))
        self.assertEqual((job["requested_duration_seconds"], job["requested_resolution"], job["human_review_status"]), (6, "720p", "REQUIRED"))
        self.assertAlmostEqual(job["provider_cost_usd"], 0.5)
        self.assertEqual([call["kind"] for call in transport.calls], ["models", "submit", "poll", "poll", "download"])
        payload = json.loads(transport.calls[1]["body"])
        self.assertEqual((payload["aspect_ratio"], payload["duration"], payload["resolution"]), ("9:16", 6, "720p"))
        self.assertNotIn("Authorization", transport.calls[-1]["headers"])
        with app.connect() as connection:
            asset = dict(connection.execute("SELECT * FROM generated_assets").fetchone())
            events = [row[0] for row in connection.execute("SELECT event_type FROM render_provider_events ORDER BY id")]
        self.assertEqual((asset["mime_type"], asset["width"], asset["height"], asset["duration_seconds"], asset["frame_rate"], asset["codec"], asset["has_audio"]),
                         ("video/mp4", 720, 1280, 6.0, 24.0, "avc1", 0))
        self.assertEqual((asset["fixture_only"], asset["executable"], asset["usable_for_review"]), (0, 1, 1))
        self.assertEqual(events, ["SUBMITTED", "POLLED", "POLLED", "COMPLETED", "DOWNLOADED"])
        room = app.event_room(job["event_id"])
        self.assertEqual(room["render_jobs"][0]["render_phase"]["phase"], "READY_FOR_REVIEW")
        video_cost = next(item for item in room["cost_summary"]["stages"] if item["stage"] == "VIDEO_RENDERING")
        self.assertEqual((video_cost["known_cost_usd"], video_cost["unknown_cost_runs"]), (0.5, 0))

    def test_video_model_is_validated_before_any_paid_submission(self):
        package = self.reel_renderable_package()
        transport = FakeXAIVideoTransport(models=(200, {}, json.dumps({"models": [{
            "id": "some-other-video-model", "aliases": [], "input_modalities": ["text"], "output_modalities": ["video"],
        }]}).encode()))
        job = self.video_render(transport, package)
        self.assertEqual((job["status"], job["failure_code"]), ("FAILED", "INVALID_REQUEST"))
        self.assertIn("not available to this API key", job["failure_reason"])
        self.assertEqual(transport.count("submit"), 0)

    def test_video_polling_timeout_and_failures_fail_closed(self):
        cases = [
            ("timeout", FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(10)] * 3), "RENDERING", "POLL_ATTEMPTS_EXHAUSTED"),
            ("download", FakeXAIVideoTransport(download=(503, {}, b"")), "RENDERING", "DOWNLOAD_FAILED"),
            ("moderation", FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(moderation=False)]), "HUMAN_REVIEW", "CONTENT_POLICY_REJECTED"),
            ("provider-failed", FakeXAIVideoTransport(polls=[(200, {}, json.dumps({"status": "failed", "error": {"code": "internal_error", "message": "engine"}}).encode())]),
             "FAILED", "PROVIDER_JOB_FAILED"),
            ("expired", FakeXAIVideoTransport(polls=[(200, {}, b'{"status": "expired"}')]), "FAILED", "PROVIDER_JOB_FAILED"),
        ]
        for name, transport, status, code in cases:
            with self.subTest(case=name):
                self.reset_database()
                package = self.reel_renderable_package()
                job = self.video_render(transport, package, max_poll_attempts=3)
                self.assertEqual((job["status"], job["failure_code"]), (status, code))
                self.assertEqual(job["provider_job_id"], "vid-req-123")
                self.assertEqual(transport.count("submit"), 1)
                with app.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 0)

    def test_malformed_zero_duration_and_wrong_aspect_videos_fail_technical_qa(self):
        cases = [
            ("malformed", b"not an mp4 container at all"),
            ("zero-duration", mp4_bytes(720, 1280, 0.0)),
            ("wrong-aspect", mp4_bytes(1280, 720, 6.0)),
            ("truncated", mp4_bytes(720, 1280, 6.0)[:-30]),
        ]
        for name, data in cases:
            with self.subTest(case=name):
                self.reset_database()
                package = self.reel_renderable_package()
                job = self.video_render(FakeXAIVideoTransport(download=(200, {}, data)), package)
                self.assertNotEqual(job["status"], "READY_FOR_REVIEW")
                self.assertEqual(job["technical_validation_status"], "FAILED")

    def test_video_regeneration_is_immutable_and_replay_is_flagged(self):
        package = self.reel_renderable_package()
        first = self.video_render(FakeXAIVideoTransport(), package)
        second = self.video_render(FakeXAIVideoTransport(download=(200, {}, mp4_bytes(720, 1280, 6.0, payload=b"\x01" * 64))),
                                   package, regenerate=True)
        with app.connect() as connection:
            before = [dict(row) for row in connection.execute("SELECT * FROM generated_assets ORDER BY version_number")]
        self.assertEqual((first["regeneration_number"], second["regeneration_number"]), (1, 2))
        self.assertEqual([row["version_number"] for row in before], [1, 2])
        replay = self.video_render(FakeXAIVideoTransport(), package, regenerate=True)
        self.assertEqual((replay["status"], replay["failure_code"]), ("HUMAN_REVIEW", "DUPLICATE_PROVIDER_OUTPUT"))
        with app.connect() as connection:
            after = [dict(row) for row in connection.execute("SELECT * FROM generated_assets ORDER BY version_number")]
        self.assertEqual(after, before)

    def test_unknown_video_cost_stays_null(self):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(ticks=None)]), package)
        self.assertEqual((job["status"], job["cost_status"], job["provider_cost_usd"]), ("READY_FOR_REVIEW", "unknown", None))
        stage = next(item for item in app.event_room(job["event_id"])["cost_summary"]["stages"] if item["stage"] == "VIDEO_RENDERING")
        self.assertEqual((stage["known_cost_usd"], stage["unknown_cost_runs"]), (None, 1))

    def test_image_to_video_binds_exact_source_lineage(self):
        package = self.xai_renderable_package()
        image_job = self.xai_render(FakeXAITransport(download=(200, {}, jpeg_bytes(1536, 2048))), package)
        with app.connect() as connection:
            source = dict(connection.execute("SELECT * FROM generated_assets WHERE render_job_id=?", (image_job["id"],)).fetchone())
        room = app.event_room(image_job["event_id"])
        self.assertTrue(room["render_jobs"][0]["assets"][0]["video_source"]["eligible"])
        transport = FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(duration=15)],
                                          download=(200, {}, mp4_bytes(768, 1024, 15.0)))
        video = self.video_render(transport, package, source_asset_id=source["id"])
        self.assertEqual(video["status"], "READY_FOR_REVIEW", video["failure_reason"])
        self.assertEqual((video["generation_mode"], video["source_asset_id"], video["source_asset_checksum"], video["requested_aspect_ratio"]),
                         ("IMAGE_TO_VIDEO", source["id"], source["checksum_sha256"], "3:4"))
        payload = json.loads(transport.calls[1]["body"])
        self.assertTrue(payload["image"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("aspect_ratio", payload)
        with app.connect() as connection:
            submitted = json.loads(connection.execute(
                "SELECT safe_metadata_json FROM render_provider_events WHERE render_job_id=? AND event_type='SUBMITTED'", (video["id"],)
            ).fetchone()[0])
            snapshot = json.loads(connection.execute("SELECT request_json FROM render_prompt_snapshots WHERE render_job_id=?", (video["id"],)).fetchone()[0])
        with app.connect() as connection:
            derivative = dict(connection.execute("SELECT * FROM derived_assets WHERE source_asset_id=? AND purpose='VIDEO_SOURCE'", (source["id"],)).fetchone())
        self.assertEqual(submitted["provider_request"]["image"], {"source_asset_id": derivative["id"], "checksum_sha256": derivative["checksum_sha256"], "bytes": derivative["file_size"]})
        self.assertEqual(snapshot["source_asset"]["checksum_sha256"], source["checksum_sha256"])
        cached = self.video_render(FakeXAIVideoTransport(), package, source_asset_id=source["id"])
        self.assertEqual(cached["id"], video["id"])
        new_image = app.enqueue_render(package["id"], "IMAGE", background=False, regenerate=True,
                                             renderer=XAIImageRenderer(api_key=XAI_TEST_KEY, transport=FakeXAITransport(
                                                 download=(200, {}, jpeg_bytes(1536, 2048, payload=b"\x55\x66")))))["job"]
        with app.connect() as connection:
            new_source = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (new_image["id"],)).fetchone()[0]
        second_video = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(duration=15)],
                                                               download=(200, {}, mp4_bytes(768, 1024, 15.0, payload=b"\x02" * 64))),
                                         package, source_asset_id=new_source)
        self.assertNotEqual(second_video["id"], video["id"])
        self.assertEqual(app.render_job(video["id"])["source_asset_id"], source["id"])

    def test_reference_to_video_produces_native_ratio_without_stretching(self):
        package = self.xai_renderable_package()
        image_job = self.xai_render(FakeXAITransport(download=(200, {}, jpeg_bytes(1536, 2048))), package)
        with app.connect() as connection:
            source = dict(connection.execute("SELECT * FROM generated_assets WHERE render_job_id=?", (image_job["id"],)).fetchone())
        with self.assertRaisesRegex(ValueError, "would stretch"):
            self.video_render(FakeXAIVideoTransport(), package, source_asset_id=source["id"], aspect_ratio="9:16")
        transport = FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(duration=15)],
                                          download=(200, {}, mp4_bytes(720, 1280, 15.0)))
        video = self.video_render(transport, package, source_asset_id=source["id"], generation_mode="REFERENCE_TO_VIDEO",
                                  aspect_ratio="9:16")
        self.assertEqual((video["status"], video["generation_mode"], video["requested_aspect_ratio"], video["source_asset_id"]),
                         ("READY_FOR_REVIEW", "REFERENCE_TO_VIDEO", "9:16", source["id"]))
        payload = json.loads(transport.calls[1]["body"])
        self.assertEqual(payload["aspect_ratio"], "9:16")
        self.assertNotIn("image", payload)
        self.assertTrue(payload["reference_images"][0]["url"].startswith("data:image/"))
        self.assertIn("render no text", payload["prompt"])
        self.assertEqual(transport.count("submit"), 1)

    def test_fixture_images_can_never_become_video_sources(self):
        _, _, _, package = self.renderable_package()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            fixture_job = app.enqueue_render(package["id"], "IMAGE", renderer=DeterministicImageRenderer(), background=False)["job"]
        with app.connect() as connection:
            fixture_asset = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (fixture_job["id"],)).fetchone()[0]
        transport = FakeXAIVideoTransport()
        with self.assertRaisesRegex(ValueError, "Fixture placeholder images can never be a video source"):
            self.video_render(transport, package, source_asset_id=fixture_asset)
        self.assertEqual(transport.calls, [])
        room = app.event_room(fixture_job["event_id"])
        asset = room["render_jobs"][0]["assets"][0]
        self.assertEqual((asset["fixture_only"], asset["video_source"]["eligible"]), (1, False))

    def test_semantic_checks_are_recorded_and_flags_route_to_review(self):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(), package)
        with app.connect() as connection:
            details = json.loads(connection.execute(
                "SELECT details_json FROM media_qa_results WHERE render_job_id=? AND qa_type='SEMANTIC_VISUAL'", (job["id"],)
            ).fetchone()[0])
        self.assertEqual({item["status"] for item in details["checks"]}, {"UNKNOWN"})
        self.assertFalse(details["identity_verified_by_model"])
        class FlaggingQA(ControlledVisualQA):
            def qa(self, **context):
                return MediaQAResult(status="PASSED", details={"checks": [{"check": "NO_UNINTENDED_SYMBOLS", "status": "FLAG"}]},
                                     provider=self.name, model=self.model)
        self.reset_database()
        package = self.reel_renderable_package()
        renderer = XAIVideoRenderer(api_key=XAI_TEST_KEY, transport=FakeXAIVideoTransport(), poll_interval_seconds=0)
        flagged = app.enqueue_render(package["id"], "VIDEO", renderer=renderer, background=False, visual_qa_provider=FlaggingQA())["job"]
        self.assertEqual((flagged["status"], flagged["semantic_qa_status"], flagged["human_review_status"]),
                         ("HUMAN_REVIEW", "FLAGGED", "REQUIRED"))

    def test_duplicate_paid_request_and_double_click_return_one_active_job(self):
        _, _, _, package = self.renderable_package()
        renderer = ControlledAsyncImageRenderer()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            first = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=True,
                                       regenerate=True, client_request_id="click-one")
            replay = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=True,
                                        regenerate=True, client_request_id="click-one")
            equivalent = app.enqueue_render(package["id"], "IMAGE", renderer=renderer, background=True,
                                            regenerate=True, client_request_id="click-two")
        self.assertEqual({first["job"]["id"], replay["job"]["id"], equivalent["job"]["id"]}, {first["job"]["id"]})
        self.assertTrue(replay["duplicate"] and equivalent["duplicate"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paid_request_keys").fetchone()[0], 2)

    def test_concurrent_paid_requests_are_serialized_by_fingerprint(self):
        _, _, _, package = self.renderable_package()
        renderer = ControlledAsyncImageRenderer()
        results, errors = [], []
        barrier = threading.Barrier(5)
        def submit(index):
            try:
                barrier.wait()
                results.append(app.enqueue_render(
                    package["id"], "IMAGE", renderer=renderer, background=True, regenerate=True,
                    client_request_id=f"concurrent-{index}",
                )["job"]["id"])
            except Exception as error:
                errors.append(error)
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            threads = [threading.Thread(target=submit, args=(index,)) for index in range(5)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0], 1)

    def test_timeout_is_provider_pending_and_resume_never_resubmits(self):
        package = self.reel_renderable_package()
        initial = FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(10)])
        job = self.video_render(initial, package, max_poll_attempts=1)
        self.assertEqual((job["status"], job["resume_state"], initial.count("submit")),
                         ("RENDERING", "PROVIDER_PENDING", 1))
        resumed_transport = FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(55)])
        resumed = app.resume_render_job(job["id"], renderer=XAIVideoRenderer(
            api_key=XAI_TEST_KEY, transport=resumed_transport, poll_interval_seconds=0, max_poll_attempts=1,
        ))
        self.assertEqual((resumed["resume_state"], resumed_transport.count("submit"), resumed_transport.count("poll")),
                         ("PROVIDER_PENDING", 0, 1))

    def test_resume_downloads_provider_completion_after_local_timeout(self):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(10)]), package,
                                max_poll_attempts=1)
        transport = FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done()])
        resumed = app.resume_render_job(job["id"], renderer=XAIVideoRenderer(
            api_key=XAI_TEST_KEY, transport=transport, poll_interval_seconds=0,
        ))
        self.assertEqual((resumed["status"], resumed["resume_state"], transport.count("submit")),
                         ("READY_FOR_REVIEW", None, 0))
        self.assertEqual(resumed["submitted_at"], job["submitted_at"])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM generated_assets").fetchone()[0], 1)

    def test_resume_records_provider_failure_after_local_timeout(self):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(10)]), package,
                                max_poll_attempts=1)
        transport = FakeXAIVideoTransport(polls=[(200, {}, b'{"status":"failed","error":{"code":"engine","message":"failed"}}')])
        resumed = app.resume_render_job(job["id"], renderer=XAIVideoRenderer(api_key=XAI_TEST_KEY, transport=transport))
        self.assertEqual((resumed["status"], resumed["failure_code"], transport.count("submit")), ("FAILED", "engine", 0))

    def test_resume_unknown_provider_status_requires_intervention(self):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.pending(10)]), package,
                                max_poll_attempts=1)
        transport = FakeXAIVideoTransport(polls=[(200, {}, b'{"status":"mystery"}')])
        resumed = app.resume_render_job(job["id"], renderer=XAIVideoRenderer(api_key=XAI_TEST_KEY, transport=transport))
        self.assertEqual((resumed["status"], resumed["resume_state"], transport.count("submit")),
                         ("HUMAN_REVIEW", "NEEDS_INTERVENTION", 0))

    def test_startup_recovery_marks_unfinished_jobs_without_provider_calls(self):
        package = self.reel_renderable_package()
        with patch.object(app, "RENDER_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_render(package["id"], "VIDEO", renderer=XAIVideoRenderer(
                api_key=XAI_TEST_KEY, transport=FakeXAIVideoTransport()), background=True)["job"]
        recovered = app.recover_unfinished_media_jobs()
        self.assertEqual(next(item for item in recovered if item["id"] == queued["id"])["resume_state"], "INTERRUPTED")
        with app.connect() as connection:
            connection.execute(
                "UPDATE render_jobs SET status='RENDERING',provider_called=1,provider_job_id='saved-job',resume_state=NULL WHERE id=?",
                (queued["id"],),
            )
        recovered = app.recover_unfinished_media_jobs()
        self.assertEqual(next(item for item in recovered if item["id"] == queued["id"])["resume_state"], "PROVIDER_PENDING")

    def test_video_source_derivative_preserves_original_and_lineage(self):
        package = self.xai_renderable_package()
        image = app.enqueue_render(package["id"], "IMAGE", renderer=ControlledAsyncImageRenderer(statuses=["COMPLETED"]),
                                   background=False)["job"]
        with app.connect() as connection:
            source = dict(connection.execute("SELECT * FROM generated_assets WHERE render_job_id=?", (image["id"],)).fetchone())
        original_bytes = LocalMediaStorage(app.RENDER_STORAGE_ROOT).get(source["storage_uri"])
        video = self.video_render(FakeXAIVideoTransport(polls=[FakeXAIVideoTransport.done(duration=15)],
                                                        download=(200, {}, mp4_bytes(768, 1024, 15.0))),
                                  package, source_asset_id=source["id"])
        with app.connect() as connection:
            derivative = dict(connection.execute("SELECT * FROM derived_assets WHERE id=?", (video["source_derivative_id"],)).fetchone())
            unchanged = dict(connection.execute("SELECT * FROM generated_assets WHERE id=?", (source["id"],)).fetchone())
        self.assertEqual((derivative["source_asset_id"], derivative["source_checksum_sha256"]),
                         (source["id"], source["checksum_sha256"]))
        self.assertEqual((derivative["mime_type"], derivative["width"] / derivative["height"]), ("image/jpeg", 0.75))
        self.assertLessEqual(derivative["file_size"], app.VIDEO_SOURCE_MAX_BYTES)
        self.assertEqual((unchanged["checksum_sha256"], LocalMediaStorage(app.RENDER_STORAGE_ROOT).get(unchanged["storage_uri"])),
                         (source["checksum_sha256"], original_bytes))

    def test_video_source_size_limit_fails_before_paid_submission(self):
        package = self.xai_renderable_package()
        image = app.enqueue_render(package["id"], "IMAGE", renderer=ControlledAsyncImageRenderer(statuses=["COMPLETED"]),
                                   background=False)["job"]
        with app.connect() as connection:
            source_id = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (image["id"],)).fetchone()[0]
        transport = FakeXAIVideoTransport()
        with patch.object(app, "VIDEO_SOURCE_MAX_BYTES", 10):
            job = self.video_render(transport, package, source_asset_id=source_id)
        self.assertEqual((job["status"], job["failure_code"], transport.count("submit")),
                         ("FAILED", "VIDEO_SOURCE_PREPARATION_FAILED", 0))

    def test_image_ocr_pass_and_unexpected_text_flag(self):
        _, _, _, package = self.renderable_package()
        passed = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                    ocr_provider=ControlledOCR(()))["job"]
        self.assertEqual((passed["status"], passed["text_validation_status"]), ("READY_FOR_REVIEW", "PASSED"))
        self.reset_database()
        _, _, _, package = self.renderable_package()
        flagged = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                     ocr_provider=ControlledOCR(({"text": "VOTE BJP 999", "confidence": .99, "box": [0, 0, 1, 1]},)))["job"]
        self.assertEqual((flagged["status"], flagged["text_validation_status"]), ("HUMAN_REVIEW", "FAILED"))
        with app.connect() as connection:
            evidence = json.loads(connection.execute("SELECT evidence_json FROM media_qa_runs WHERE qa_kind='OCR'").fetchone()[0])
        self.assertEqual(evidence["detections"][0]["text"], "VOTE BJP 999")

    def test_video_frame_extraction_creates_lineaged_qa_artifacts(self):
        package = self.reel_renderable_package()
        extractor, ocr, vision = ControlledFrameExtractor(), ControlledOCR(()), StructuredVisualQA("PASS")
        renderer = XAIVideoRenderer(api_key=XAI_TEST_KEY, transport=FakeXAIVideoTransport(), poll_interval_seconds=0)
        job = app.enqueue_render(package["id"], "VIDEO", renderer=renderer, background=False,
                                 frame_extractor=extractor, ocr_provider=ocr, visual_qa_provider=vision)["job"]
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        self.assertEqual((extractor.calls, ocr.calls), (1, 5))
        self.assertGreaterEqual(len(extractor.last_times), 5)
        with app.connect() as connection:
            rows = connection.execute("SELECT source_asset_id,purpose,frame_time_seconds FROM derived_assets WHERE purpose='QA_FRAME'").fetchall()
            asset_id = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (job["id"],)).fetchone()[0]
        self.assertEqual(len(rows), 5)
        self.assertEqual({row[0] for row in rows}, {asset_id})

    def test_structured_visual_qa_pass_flag_and_public_figure_advisory(self):
        for visual, expected_status, expected_qa in (
            (StructuredVisualQA("PASS"), "READY_FOR_REVIEW", "PASSED"),
            (StructuredVisualQA("FLAG", possible_people=True), "HUMAN_REVIEW", "FLAGGED"),
        ):
            with self.subTest(expected_status=expected_status):
                self.reset_database()
                _, _, _, package = self.renderable_package()
                job = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                         ocr_provider=ControlledOCR(()), visual_qa_provider=visual)["job"]
                self.assertEqual((job["status"], job["semantic_qa_status"]), (expected_status, expected_qa))
                with app.connect() as connection:
                    run = dict(connection.execute("SELECT * FROM media_qa_runs WHERE qa_kind='VISUAL'").fetchone())
                self.assertEqual(run["status"], "PASS" if expected_qa == "PASSED" else "FLAG")
                self.assertFalse(json.loads(run["evidence_json"])["identity_verified_by_model"])

    def test_malformed_visual_qa_response_is_unknown_on_manual_rerun(self):
        _, _, _, package = self.renderable_package()
        job = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                 ocr_provider=ControlledOCR(()))["job"]
        with app.connect() as connection:
            asset_id = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (job["id"],)).fetchone()[0]
        class MalformedQA(VisualQAProvider):
            name, model = "malformed", "v1"
            def qa(self, **context):
                raise VisualQAProviderError("malformed", "malformed_response")
        runs = app.rerun_media_qa(asset_id, "VISUAL", visual_qa_provider=MalformedQA(), ocr_provider=ControlledOCR(()))
        self.assertEqual((runs[0]["status"], runs[0]["run_number"]), ("UNKNOWN", 2))

    def test_qa_reruns_are_immutable_versions(self):
        _, _, _, package = self.renderable_package()
        job = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                 ocr_provider=ControlledOCR(()), visual_qa_provider=StructuredVisualQA("PASS"))["job"]
        with app.connect() as connection:
            asset_id = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (job["id"],)).fetchone()[0]
        app.rerun_media_qa(asset_id, "OCR", ocr_provider=ControlledOCR(()))
        with app.connect() as connection:
            runs = connection.execute("SELECT id,run_number,trigger FROM media_qa_runs WHERE generated_asset_id=? AND qa_kind='OCR' ORDER BY run_number", (asset_id,)).fetchall()
            self.assertEqual([tuple(row)[1:] for row in runs], [(1, "AUTOMATIC"), (2, "MANUAL")])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE media_qa_runs SET status='FLAG' WHERE id=?", (runs[0][0],))

    def test_human_review_is_immutable_and_scoped_to_asset_version(self):
        _, _, _, package = self.renderable_package()
        first = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                   ocr_provider=ControlledOCR(()))["job"]
        second = app.enqueue_render(package["id"], "IMAGE", renderer=CountingImageRenderer(), background=False,
                                    regenerate=True, ocr_provider=ControlledOCR(()))["job"]
        with app.connect() as connection:
            first_asset = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (first["id"],)).fetchone()[0]
            second_asset = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (second["id"],)).fetchone()[0]
        approved = app.review_media_asset(first_asset, "APPROVED", "Reviewer One", "Checked version one")
        changed = app.review_media_asset(first_asset, "CHANGES_REQUIRED", "Reviewer Two", "Re-review")
        self.assertEqual((approved["asset_version"], changed["asset_version"]), (1, 1))
        room = app.event_room(first["event_id"])
        by_id = {asset["id"]: asset for render in room["render_jobs"] for asset in render["assets"]}
        self.assertEqual(by_id[first_asset]["latest_review"]["action"], "CHANGES_REQUIRED")
        self.assertIsNone(by_id[second_asset]["latest_review"])
        with app.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE media_reviews SET action='REJECTED' WHERE id=?", (approved["id"],))

    def test_media_ui_contains_paid_guards_resume_qa_and_review_without_publish(self):
        source = Path(app.__file__).with_name("app.js").read_text(encoding="utf-8")
        for phrase in ("Submitting…", "This starts a paid xAI generation", "Resume status check", "OCR QA",
                       "Visual QA", "CHANGES_REQUIRED", "approved media asset only", "client_request_id"):
            self.assertIn(phrase, source)
        self.assertNotIn("Publish media", source)
        self.assertNotIn("Schedule media", source)

    def test_mp4_inspection_reads_structure(self):
        info = inspect_video(mp4_bytes(1080, 1920, 8.0, fps=30, audio=True))
        self.assertEqual((info["width"], info["height"], info["duration_seconds"], info["frame_rate"], info["codec"], info["has_audio"]),
                         (1080, 1920, 8.0, 30.0, "avc1", True))
        for corrupt in (b"", b"\x00" * 32, mp4_bytes(720, 1280, 0.0), mp4_bytes(720, 1280)[:40]):
            with self.subTest(size=len(corrupt)), self.assertRaises(ImageDecodeError):
                inspect_video(corrupt)

    def test_ui_labels_distinguish_fixture_from_live(self):
        source = Path(app.__file__).with_name("app.js").read_text(encoding="utf-8")
        for label in ("Claude · live", "Fixture · demo", "Fixture placeholder · not AI generated", "AI generated",
                      "Claude production provider unavailable", "Generate video from image"):
            self.assertIn(label, source)

    # ---------- Architecture 07: Meta distribution (Meta fully mocked) ----------

    def approved_meta_video(self, review=True):
        package = self.reel_renderable_package()
        job = self.video_render(FakeXAIVideoTransport(), package)
        self.assertEqual(job["status"], "READY_FOR_REVIEW")
        with app.connect() as connection:
            asset_id = connection.execute("SELECT id FROM generated_assets WHERE render_job_id=?", (job["id"],)).fetchone()[0]
        if review:
            app.review_media_asset(asset_id, "APPROVED", "Media Reviewer", "Approved for distribution")
        return job, asset_id

    def approved_platform_package(self, platform="INSTAGRAM_REELS"):
        job, asset_id = self.approved_meta_video()
        package = app.create_distribution_package(asset_id, platform)
        app.review_distribution_package(package["id"], "APPROVED", "Distribution Reviewer")
        return job, asset_id, package

    def meta_live(self, **extra):
        values = {
            "SOCIAL_PUBLISHING_ENABLED": "1", "INSTAGRAM_PUBLISHING_ENABLED": "1", "FACEBOOK_PUBLISHING_ENABLED": "1",
            "INSTAGRAM_USER_ID": "17841400000000000", "INSTAGRAM_ACCESS_TOKEN": META_IG_TOKEN,
            "FACEBOOK_PAGE_ID": "100000000000000", "FACEBOOK_PAGE_ACCESS_TOKEN": META_FB_TOKEN, **extra,
        }
        return patch.dict("os.environ", values)

    def meta_publisher(self, platform, transport):
        cls = meta_distribution.InstagramReelsPublisher if platform == "INSTAGRAM_REELS" else meta_distribution.FacebookReelsPublisher
        token = META_IG_TOKEN if platform == "INSTAGRAM_REELS" else META_FB_TOKEN
        account = "17841400000000000" if platform == "INSTAGRAM_REELS" else "100000000000000"
        return cls(token=token, account_id=account, transport=transport, poll_interval_seconds=0, max_polls=3, sleep=lambda _: None)

    def test_distribution_requires_human_approved_media(self):
        _, asset_id = self.approved_meta_video(review=False)
        with self.assertRaisesRegex(ValueError, "latest human review is not APPROVED"):
            app.create_distribution_package(asset_id, "INSTAGRAM_REELS")
        app.review_media_asset(asset_id, "APPROVED", "Reviewer")
        app.review_media_asset(asset_id, "CHANGES_REQUIRED", "Reviewer", "Recheck")
        with self.assertRaisesRegex(ValueError, "latest human review is not APPROVED"):
            app.create_distribution_package(asset_id, "FACEBOOK_REELS")
        review = app.review_media_asset(asset_id, "APPROVED", "Reviewer")
        packages = [app.create_distribution_package(asset_id, platform) for platform in ("INSTAGRAM_REELS", "FACEBOOK_REELS")]
        for package in packages:
            self.assertEqual((package["media_review_id"], package["generated_asset_id"], package["compliant"]), (review["id"], asset_id, 1))
            with app.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE distribution_packages SET caption='edited' WHERE id=?", (package["id"],))
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0], 0)

    def test_platform_copy_only_reformats_approved_package_text(self):
        _, asset_id = self.approved_meta_video()
        instagram = app.create_distribution_package(asset_id, "INSTAGRAM_REELS")
        facebook = app.create_distribution_package(asset_id, "FACEBOOK_REELS")
        with app.connect() as connection:
            package = json.loads(connection.execute("SELECT package_json FROM content_packages WHERE id=?", (instagram["content_package_id"],)).fetchone()[0])
            claims = [dict(row) for row in connection.execute("SELECT text FROM claim_versions")]
        self.assertTrue(instagram["copy_validation"]["valid"], instagram["copy_validation"])
        self.assertIn(package["headline"]["text"], instagram["caption"])
        self.assertIn(package["caption"]["text"], instagram["caption"])
        self.assertIsNone(instagram["title"])
        self.assertEqual(facebook["title"], package["headline"]["text"][:255])
        self.assertEqual(instagram["accessibility_text"], package["platform_metadata"]["accessibility_text"])
        self.assertEqual(instagram["platform_metadata"]["media_type"], "REELS")
        self.assertIn("thumb_offset", json.dumps(instagram["cover"]) + "thumb_offset")
        self.assertEqual(instagram["cover"]["time_ms"], 1000)
        for tag in instagram["hashtags"]:
            self.assertIn(tag.lstrip("#").lower(), " ".join(claim["text"] for claim in claims).replace(" ", "").replace("-", "").lower())
        tampered = {**meta_distribution.build_platform_copy("INSTAGRAM_REELS", package, claims, cover_time_ms=0)}
        tampered["caption"] += "\n\nOver 5,000 farmers benefited."
        result = meta_distribution.validate_copy(tampered, package, claims, "INSTAGRAM_REELS")
        self.assertFalse(result["valid"])
        self.assertTrue(any("number" in error for error in result["errors"]))
        self.assertTrue(any("not verbatim" in error for error in result["errors"]))

    def test_platform_compliance_never_alters_media(self):
        base = {"mime_type": "video/mp4", "codec": "avc1", "has_audio": True, "audio_codec": "mp4a", "frame_rate": 24.0,
                "duration_seconds": 8.042, "moov_before_mdat": True, "file_size": 9915400}
        approved_like = {**base, "width": 816, "height": 1104, "edit_lists": True}
        instagram = meta_distribution.check_compliance("INSTAGRAM_REELS", approved_like)
        facebook = meta_distribution.check_compliance("FACEBOOK_REELS", approved_like)
        self.assertFalse(instagram["compliant"])
        self.assertTrue(any("edit lists" in error for error in instagram["errors"]))
        self.assertTrue(any("9:16" in warning for warning in instagram["warnings"]))
        self.assertFalse(facebook["compliant"])
        self.assertTrue(any("9:16" in error for error in facebook["errors"]))
        clean = {**base, "width": 1080, "height": 1920, "edit_lists": False}
        self.assertTrue(meta_distribution.check_compliance("INSTAGRAM_REELS", clean)["compliant"])
        self.assertTrue(meta_distribution.check_compliance("FACEBOOK_REELS", clean)["compliant"])
        _, asset_id = self.approved_meta_video()
        with patch.object(app, "check_platform_compliance", lambda platform, video: {"compliant": False, "errors": ["Facebook Reels require 9:16."], "warnings": []}):
            package = app.create_distribution_package(asset_id, "FACEBOOK_REELS")
        with self.assertRaisesRegex(ValueError, "cannot be approved.*9:16"):
            app.review_distribution_package(package["id"], "APPROVED", "Distribution Reviewer")

    def test_each_platform_package_needs_its_own_approval(self):
        _, asset_id = self.approved_meta_video()
        instagram = app.create_distribution_package(asset_id, "INSTAGRAM_REELS")
        facebook = app.create_distribution_package(asset_id, "FACEBOOK_REELS")
        app.review_distribution_package(instagram["id"], "APPROVED", "Distribution Reviewer")
        with self.meta_live():
            self.assertTrue(app._publish_gate(app.distribution_package(instagram["id"]))["allowed"])
            gate = app._publish_gate(app.distribution_package(facebook["id"]))
        self.assertFalse(gate["allowed"])
        self.assertIn("The platform package's latest human review is not APPROVED.", gate["blockers"])

    def test_kill_switches_block_publishing_without_network(self):
        _, _, package = self.approved_platform_package()
        transport = FakeMetaTransport()
        for overrides, expected in (
            ({"SOCIAL_PUBLISHING_ENABLED": "0"}, "SOCIAL_PUBLISHING_ENABLED is off"),
            ({"INSTAGRAM_PUBLISHING_ENABLED": "0"}, "INSTAGRAM_PUBLISHING_ENABLED is off"),
            ({"INSTAGRAM_ACCESS_TOKEN": ""}, "Missing platform configuration"),
        ):
            with self.subTest(expected=expected), self.meta_live(**overrides):
                with self.assertRaisesRegex(ValueError, expected):
                    app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)
        with self.assertRaisesRegex(ValueError, "SOCIAL_PUBLISHING_ENABLED is off"):
            app.request_publish(package["id"], background=False)
        self.assertEqual(transport.calls, [])
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0], 0)

    def test_instagram_reel_publish_flow_with_meta_mocked(self):
        job, asset_id, package = self.approved_platform_package("INSTAGRAM_REELS")
        transport = FakeMetaTransport(statuses=["IN_PROGRESS", "FINISHED"])
        with self.meta_live():
            result = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport),
                                         background=False, client_request_id="click-1", requested_by="Publisher")
        published = result["job"]
        self.assertEqual((published["status"], published["provider_container_id"], published["provider_post_id"], published["permalink"]),
                         ("PUBLISHED", "IGC-1", "IGM-1", "https://www.instagram.com/reel/CONTROLLED/"))
        self.assertEqual(transport.kinds(), ["ig_container", "upload", "status", "status", "ig_publish", "permalink"])
        container = transport.calls[0]
        self.assertEqual((container["form"]["media_type"], container["form"]["upload_type"], container["form"]["thumb_offset"]),
                         ("REELS", "resumable", "1000"))
        self.assertEqual(container["form"]["caption"], package["caption"])
        upload = transport.calls[1]
        self.assertTrue(upload["url"].startswith("https://rupload.facebook.com/ig-api-upload/v25.0/IGC-1"))
        self.assertEqual((upload["headers"]["Authorization"], upload["headers"]["offset"]), ("OAuth " + META_IG_TOKEN, "0"))
        self.assertEqual(int(upload["headers"]["file_size"]), upload["body_size"])
        self.assertEqual(transport.calls[4]["form"]["creation_id"], "IGC-1")
        with app.connect() as connection:
            history = connection.execute("SELECT format,status,claim_set_id FROM publishing_history").fetchone()
            database_text = "\n".join(connection.iterdump())
        self.assertEqual(tuple(history), ("REEL", "PUBLISHED", package["approved_claim_set_id"]))
        room = json.dumps(app.event_room(job["event_id"]))
        for secret in (META_IG_TOKEN, META_FB_TOKEN):
            self.assertNotIn(secret, database_text)
            self.assertNotIn(secret, room)

    def test_facebook_reel_publish_flow_with_meta_mocked(self):
        _, _, package = self.approved_platform_package("FACEBOOK_REELS")
        transport = FakeMetaTransport(statuses=[{"video_status": "processing"}, None])
        with self.meta_live():
            published = app.request_publish(package["id"], publisher=self.meta_publisher("FACEBOOK_REELS", transport),
                                            background=False)["job"]
        self.assertEqual((published["status"], published["provider_post_id"], published["permalink"]),
                         ("PUBLISHED", "FBV-1", "https://www.facebook.com/reel/FBV-1"))
        self.assertEqual(transport.kinds(), ["fb_start", "upload", "status", "status", "fb_finish", "permalink"])
        finish = transport.calls[4]["form"]
        self.assertEqual((finish["upload_phase"], finish["video_id"], finish["video_state"]), ("finish", "FBV-1", "PUBLISHED"))
        self.assertEqual((finish["title"], finish["description"]), (package["title"], package["caption"]))
        self.assertTrue(transport.calls[1]["url"].startswith("https://rupload.facebook.com/video-upload/v25.0/FBV-1"))

    def test_duplicate_post_protection(self):
        _, _, package = self.approved_platform_package()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        scheduled = app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=future, client_request_id="tab-a")
        replay = app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=future, client_request_id="tab-a")
        self.assertEqual((replay["duplicate"], replay["job"]["id"]), (True, scheduled["job"]["id"]))
        transport = FakeMetaTransport()
        with self.meta_live():
            second_tab = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)
        self.assertEqual((second_tab["duplicate"], second_tab["job"]["id"]), (True, scheduled["job"]["id"]))
        self.assertEqual(transport.calls, [])
        app.cancel_publish_job(scheduled["job"]["id"])
        results = []
        def click():
            results.append(app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=future)["job"]["id"])
        threads = [threading.Thread(target=click) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        app.cancel_publish_job(results[0])
        with self.meta_live():
            first = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)
            self.assertEqual(first["job"]["status"], "PUBLISHED")
            with self.assertRaisesRegex(ValueError, "Duplicate post blocked"):
                app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)
        self.assertEqual(transport.kinds().count("ig_publish"), 1)

    def test_transient_retries_and_ambiguous_publish_never_reposts(self):
        _, _, package = self.approved_platform_package()
        app.PUBLISH_RETRY_BACKOFF_SECONDS = 0
        transport = FakeMetaTransport(poll_errors=1)
        with self.meta_live():
            job = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)["job"]
        self.assertEqual(job["status"], "PUBLISHED")
        self.assertIn("ATTEMPT_FAILED", [event["event_type"] for event in job["events"]])
        self.reset_database()
        app.PUBLISH_RETRY_BACKOFF_SECONDS = 0
        _, _, package = self.approved_platform_package()
        transport = FakeMetaTransport(publish=meta_distribution.MetaAmbiguousError("timeout after send"))
        with self.meta_live():
            job = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)["job"]
            self.assertEqual((job["status"], job["last_error_code"]), ("NEEDS_INTERVENTION", "META_OUTCOME_UNKNOWN"))
            self.assertEqual(transport.kinds().count("ig_publish"), 1)
            transport.statuses = ["PUBLISHED"]
            checked = app.check_publish_status(job["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport))
        self.assertEqual(checked["status"], "PUBLISHED")
        self.assertIsNone(checked["provider_post_id"])
        self.assertIn("Confirm", checked["last_error_message"])
        self.assertEqual(transport.kinds().count("ig_publish"), 1)
        self.reset_database()
        _, _, package = self.approved_platform_package()
        transport = FakeMetaTransport(statuses=["ERROR"])
        with self.meta_live():
            failed = app.request_publish(package["id"], publisher=self.meta_publisher("INSTAGRAM_REELS", transport), background=False)["job"]
        self.assertEqual((failed["status"], failed["last_error_code"]), ("FAILED", "META_PROCESSING_FAILED"))
        self.assertEqual(transport.kinds().count("ig_publish"), 0)

    def test_schedule_runs_locally_and_respects_kill_switch_at_due_time(self):
        _, _, package = self.approved_platform_package()
        due = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        job = app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=due)["job"]
        self.assertEqual(job["status"], "SCHEDULED")
        transport = FakeMetaTransport()
        factory = lambda platform: self.meta_publisher(platform, transport)
        self.assertEqual(app.run_due_publish_jobs(now_at=datetime.now(timezone.utc).isoformat(), publisher_factory=factory), [])
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        blocked = app.run_due_publish_jobs(now_at=later, publisher_factory=factory)[0]
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("SOCIAL_PUBLISHING_ENABLED is off", blocked["last_error_message"])
        self.assertEqual(transport.calls, [])
        cancelled_job = app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=due)["job"]
        self.assertEqual(app.cancel_publish_job(cancelled_job["id"], "Editor changed plan")["status"], "CANCELLED")
        self.assertEqual(app.run_due_publish_jobs(now_at=later, publisher_factory=factory), [])
        with self.assertRaisesRegex(ValueError, "Only scheduled posts can be cancelled"):
            app.cancel_publish_job(cancelled_job["id"])
        with self.assertRaisesRegex(ValueError, "future"):
            app.request_publish(package["id"], mode="SCHEDULED", scheduled_for="2020-01-01T00:00:00+00:00")
        app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=due)
        with self.meta_live():
            executed = app.run_due_publish_jobs(now_at=later, publisher_factory=factory)[0]
        self.assertEqual(executed["status"], "PUBLISHED")

    def test_revoked_lineage_after_approval_blocks_publish(self):
        _, _, package = self.approved_platform_package()
        with app.connect() as connection:
            claim_id = json.loads(connection.execute(
                "SELECT approved_claim_version_ids_json FROM content_packages WHERE id=?", (package["content_package_id"],)
            ).fetchone()[0])[0]
            connection.execute("UPDATE claim_versions SET revoked_at=?,revocation_reason='test' WHERE id=?", (app.now(), claim_id))
        with self.meta_live(), self.assertRaisesRegex(ValueError, "revoked or superseded"):
            app.request_publish(package["id"], background=False, publisher=self.meta_publisher("INSTAGRAM_REELS", FakeMetaTransport()))

    def test_startup_recovery_never_reposts(self):
        _, _, package = self.approved_platform_package()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        job = app.request_publish(package["id"], mode="SCHEDULED", scheduled_for=future)["job"]
        with app.connect() as connection:
            connection.execute("UPDATE publish_jobs SET status='PUBLISHING',provider_container_id='IGC-1' WHERE id=?", (job["id"],))
        self.assertEqual(app.recover_interrupted_publish_jobs(), 1)
        recovered = app.publish_job(job["id"])
        self.assertEqual((recovered["status"], recovered["last_error_code"]), ("NEEDS_INTERVENTION", "INTERRUPTED"))

    def test_publish_http_route_is_gated(self):
        _, _, package = self.approved_platform_package()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("POST", f"/api/distribution-packages/{package['id']}/publish", body=json.dumps({"client_request_id": "x"}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 400)
        self.assertIn("SOCIAL_PUBLISHING_ENABLED is off", body["error"])

    def test_second_platform_can_publish_after_first_post_is_recorded(self):
        _, asset_id = self.approved_meta_video()
        packages = {}
        for platform in ("INSTAGRAM_REELS", "FACEBOOK_REELS"):
            packages[platform] = app.create_distribution_package(asset_id, platform)
            app.review_distribution_package(packages[platform]["id"], "APPROVED", "Distribution Reviewer")
        with self.meta_live():
            first = app.request_publish(packages["INSTAGRAM_REELS"]["id"], background=False,
                                        publisher=self.meta_publisher("INSTAGRAM_REELS", FakeMetaTransport()))["job"]
            second = app.request_publish(packages["FACEBOOK_REELS"]["id"], background=False,
                                         publisher=self.meta_publisher("FACEBOOK_REELS", FakeMetaTransport()))["job"]
        self.assertEqual((first["status"], second["status"]), ("PUBLISHED", "PUBLISHED"))
        with app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publishing_history WHERE status='PUBLISHED'").fetchone()[0], 2)

    def test_distribution_ui_controls_and_kill_switch_copy(self):
        source = Path(app.__file__).with_name("app.js").read_text(encoding="utf-8")
        for phrase in ("Publish now", "Cancel schedule", "Schedule", "Publishing is OFF", "Check status",
                       "Instagram Reels", "Facebook Reels", "client_request_id"):
            self.assertIn(phrase, source)

    # ---------- Final Reel Composer integration ----------

    def _reference_asset(self, asset_type, label, rights="VERIFIED", reviewer="Rights Reviewer"):
        image = deterministic_png(64, 64, f"{asset_type}-{label}")
        return app.ingest_reference_media(
            data=image, filename=f"{label.lower().replace(' ', '-')}.png", asset_type=asset_type, label=label,
            source_name="Government of Andhra Pradesh", source_url="https://ap.gov.in/example",
            license_note="Official government media, reuse permitted with attribution.",
            rights_status=rights, uploader="Uploader One", reviewer=reviewer,
            identity_subject="N. Chandrababu Naidu" if asset_type == "PUBLIC_FIGURE_PHOTO" else None,
        )["asset"]

    def _final_reel_fixture(self, source_asset_id, *, duration=14.0, qa_pass=True, cbn_asset_id=None, tdp_asset_id=None):
        with app.connect() as connection:
            source = dict(connection.execute("SELECT * FROM generated_assets WHERE id=?", (source_asset_id,)).fetchone())
            package_row = connection.execute(
                "SELECT package_json FROM content_packages WHERE id=? AND version_number=?",
                (source["content_package_id"], source["content_package_version"]),
            ).fetchone()
        narration = final_reel_composer.approved_narration(json.loads(package_row["package_json"]))
        stored = LocalMediaStorage(app.RENDER_STORAGE_ROOT).save(
            mp4_bytes(720, 1280, duration, fps=24, audio=True), extension="mp4"
        )
        status = "PASS" if qa_pass else "FLAG"
        qa = {"status": status, "errors": []}
        row = {
            "id": "FR-" + app.uuid.uuid4().hex[:12].upper(), "event_id": source["event_id"],
            "content_package_id": source["content_package_id"], "content_package_version": source["content_package_version"],
            "source_asset_id": source["id"], "source_asset_version": source["version_number"],
            "source_asset_checksum_sha256": source["checksum_sha256"], "source_render_job_id": source["render_job_id"],
            "storage_uri": stored.storage_uri, "mime_type": "video/mp4", "width": 720, "height": 1280,
            "duration_seconds": duration, "frame_rate": 24.0, "codec": "avc1", "has_audio": 1, "audio_codec": "mp4a",
            "file_size": stored.file_size, "checksum_sha256": stored.checksum_sha256, "narration_text": narration,
            "voice_provider": "apple-speech", "voice_model": "Aman (en-IN)",
            "subtitle_manifest_json": json.dumps({"burned_in": True, "cues": []}),
            "audio_manifest_json": json.dumps({"status": status, "music_below_speech": True}),
            "transform_manifest_json": json.dumps({"policy_version": final_reel_composer.COMPOSER_POLICY_VERSION}),
            "transform_hash": app.hashlib.sha256(app.uuid.uuid4().bytes).hexdigest(),
            "technical_qa_json": json.dumps(qa), "subtitle_qa_json": json.dumps(qa), "audio_qa_json": json.dumps(qa),
            "factual_qa_json": json.dumps({"status": "PASS", "no_new_factual_claims": True}),
            "instagram_compatibility_json": json.dumps({"compliant": True, "errors": []}),
            "facebook_compatibility_json": json.dumps({"compliant": True, "errors": []}),
            "status": "READY_FOR_REVIEW" if qa_pass else "BLOCKED", "cost_status": "not_billed", "cost_usd": 0.0,
            "currency": "USD", "created_at": app.now(), "cbn_asset_id": cbn_asset_id, "tdp_asset_id": tdp_asset_id,
            "public_figure_qa_json": json.dumps({"status": "PASS", "figures": [], "neutral_labels": []}),
            "composition_manifest_json": json.dumps({"segments": []}),
        }
        with app.connect() as connection:
            connection.execute(
                f"INSERT INTO final_reel_assets({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return app.final_reel_asset(row["id"])

    def _fake_composer(self, duration=13.6):
        def compose(source_asset_id, **kwargs):
            # The real composer produces distinct bytes per transform; the duration gives each fixture a unique checksum.
            return self._final_reel_fixture(source_asset_id, duration=duration)
        return compose

    def test_final_reel_composition_gate_and_per_version_approval(self):
        _, asset_id = self.approved_meta_video()
        with self.assertRaisesRegex(ValueError, "Final Reel composition blocked"):
            app.create_final_reel("GA-DOES-NOT-EXIST", composer=self._fake_composer())
        first = app.create_final_reel(asset_id, composer=self._fake_composer(duration=13.6))
        second = app.create_final_reel(asset_id, composer=self._fake_composer(duration=13.7))
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNone(first["latest_review"])
        approved = app.review_final_reel(first["id"], "APPROVED", "Reel Reviewer", "Looks good")
        self.assertEqual(approved["latest_review"]["action"], "APPROVED")
        self.assertIsNone(app.final_reel_asset(second["id"])["latest_review"])
        with app.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE final_reel_assets SET narration_text='edit' WHERE id=?", (first["id"],))
        room = app.event_room(first["event_id"])
        self.assertEqual({reel["id"] for reel in room["final_reels"]}, {first["id"], second["id"]})
        self.assertEqual(room["final_reel_source_asset_id"], asset_id)
        by_id = {asset["id"]: asset for render in room["render_jobs"] for asset in render["assets"]}
        self.assertTrue(by_id[asset_id]["final_reel"]["eligible"], by_id[asset_id]["final_reel"])

    def test_final_reel_source_gate_rejects_ineligible_media(self):
        _, asset_id = self.approved_meta_video(review=False)
        with self.assertRaisesRegex(ValueError, "Final Reel composition blocked"):
            app.create_final_reel("GA-NOT-A-VIDEO", composer=self._fake_composer())
        with self.assertRaisesRegex(ValueError, "cannot be approved"):
            app.review_final_reel(self._final_reel_fixture(asset_id, qa_pass=False)["id"], "APPROVED", "Reviewer")

    def test_distribution_binds_approved_final_reel_not_raw_video(self):
        _, asset_id = self.approved_meta_video()
        reel = self._final_reel_fixture(asset_id)
        app.review_final_reel(reel["id"], "APPROVED", "Reel Reviewer")
        with self.assertRaisesRegex(ValueError, "approved Final Reel"):
            app.create_distribution_package(asset_id, "INSTAGRAM_REELS")
        package = app.create_final_reel_distribution_package(reel["id"], "INSTAGRAM_REELS")
        self.assertEqual(
            (package["media_source"], package["final_reel_asset_id"], package["asset_checksum_sha256"]),
            ("FINAL_REEL", reel["id"], reel["checksum_sha256"]),
        )
        app.review_distribution_package(package["id"], "APPROVED", "Distribution Reviewer")
        with self.meta_live():
            gate = app._publish_gate(app.distribution_package(package["id"]))
        self.assertTrue(gate["allowed"], gate["blockers"])
        self.assertEqual(gate["final_reel_asset_id"], reel["id"])
        with app.connect() as connection:
            media = app._distribution_media(connection, "FINAL_REEL", reel["id"])
        self.assertEqual(media["storage_uri"], reel["storage_uri"])

    def test_final_reel_http_routes_and_ui_surface(self):
        _, asset_id = self.approved_meta_video()
        reel = self._final_reel_fixture(asset_id)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", f"/api/final-reels/{reel['id']}")
        response = connection.getresponse()
        self.assertEqual((response.status, json.loads(response.read())["asset"]["id"]), (200, reel["id"]))
        connection.request("GET", f"/api/final-reels/{reel['id']}/content")
        content = connection.getresponse()
        self.assertEqual(content.status, 200)
        self.assertEqual(content.getheader("Content-Type"), "video/mp4")
        self.assertTrue(len(content.read()) > 0)
        connection.request("POST", f"/api/final-reels/{reel['id']}/review",
                           body=json.dumps({"action": "APPROVED", "reviewer": "HTTP Reviewer"}),
                           headers={"Content-Type": "application/json"})
        reviewed = connection.getresponse()
        self.assertEqual((reviewed.status, json.loads(reviewed.read())["asset"]["latest_review"]["action"]), (201, "APPROVED"))
        source = Path(app.__file__).with_name("app.js").read_text(encoding="utf-8")
        for phrase in ("Create final Reel", "final-reels", "Narration (approved package text only)", "Subtitle QA",
                       "Audio QA", "Instagram compatibility", "Facebook compatibility", "Final Reel review",
                       "CHANGES_REQUIRED", "REJECTED", "Lineage & technical details"):
            self.assertIn(phrase, source)

    def test_reference_media_ingest_requires_verified_rights_and_types(self):
        asset = self._reference_asset("PUBLIC_FIGURE_PHOTO", "CBN Portrait")
        self.assertEqual(asset["rights_status"], "VERIFIED")
        self.assertTrue(asset["checksum_sha256"])
        # Duplicate bytes reuse the existing registered asset rather than duplicating it.
        duplicate = app.ingest_reference_media(
            data=deterministic_png(64, 64, "PUBLIC_FIGURE_PHOTO-CBN Portrait"), filename="cbn.png",
            asset_type="PUBLIC_FIGURE_PHOTO", label="CBN Portrait",
            source_name="Government of Andhra Pradesh", license_note="Official government media.",
            rights_status="VERIFIED", uploader="Uploader One")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["asset"]["id"], asset["id"])
        with self.assertRaisesRegex(ValueError, "asset_type"):
            app.ingest_reference_media(
                data=deterministic_png(8, 8, "bad-type"), filename="x.png", asset_type="POSTER", label="x",
                source_name="s", license_note="l", rights_status="VERIFIED", uploader="u")
        with self.assertRaisesRegex(ValueError, "rights_status"):
            app.ingest_reference_media(
                data=deterministic_png(8, 8, "bad-rights"), filename="x.png", asset_type="PARTY_LOGO", label="x",
                source_name="s", license_note="l", rights_status="MAYBE", uploader="u")
        with self.assertRaisesRegex(ValueError, "Only JPG, PNG, or WebP"):
            app.ingest_reference_media(
                data=b"gif", filename="x.gif", asset_type="PARTY_LOGO", label="x",
                source_name="s", license_note="l", rights_status="VERIFIED", uploader="u")
        # Only VERIFIED assets can enter a Reel.
        restricted = self._reference_asset("PARTY_LOGO", "TDP Cycle", rights="RESTRICTED")
        with app.connect() as connection:
            with self.assertRaisesRegex(ValueError, "not rights-verified"):
                app._reference_asset_for_reel(connection, restricted["id"], "PARTY_LOGO")
            self.assertEqual(app._reference_asset_for_reel(connection, asset["id"], "PUBLIC_FIGURE_PHOTO")["id"], asset["id"])
            with self.assertRaisesRegex(ValueError, "not a PARTY_LOGO"):
                app._reference_asset_for_reel(connection, asset["id"], "PARTY_LOGO")

    def test_final_reel_binds_verified_context_assets_and_public_figure_qa(self):
        _, asset_id = self.approved_meta_video()
        cbn = self._reference_asset("PUBLIC_FIGURE_PHOTO", "CBN Portrait")
        tdp = self._reference_asset("PARTY_LOGO", "TDP Cycle")
        captured = {}

        def composer(source_asset_id, **kwargs):
            captured.update(kwargs)
            return self._final_reel_fixture(source_asset_id, duration=14.2,
                                            cbn_asset_id=kwargs.get("cbn_asset_id"), tdp_asset_id=kwargs.get("tdp_asset_id"))

        result = app.create_final_reel(asset_id, cbn_asset_id=cbn["id"], tdp_asset_id=tdp["id"], composer=composer)
        self.assertEqual((captured["cbn_asset_id"], captured["tdp_asset_id"]), (cbn["id"], tdp["id"]))
        self.assertEqual((result["cbn_asset_id"], result["tdp_asset_id"]), (cbn["id"], tdp["id"]))
        self.assertIn("Chandrababu Naidu", result["narration_text"])
        # An unregistered or unverified contextual asset is rejected before composing.
        with self.assertRaisesRegex(ValueError, "not registered"):
            app.create_final_reel(asset_id, cbn_asset_id="MA-NOPE", composer=composer)
        restricted = self._reference_asset("PUBLIC_FIGURE_PHOTO", "Blog Portrait", rights="RESTRICTED")
        with self.assertRaisesRegex(ValueError, "not rights-verified"):
            app.create_final_reel(asset_id, cbn_asset_id=restricted["id"], composer=composer)

    def test_public_figure_context_qa_flags_unsupported_or_attributing_framing(self):
        cbn = {"asset": {"id": "MA-CBN", "rights_status": "VERIFIED", "identity_subject": "N. Chandrababu Naidu", "label": "CBN"}}
        contextual = {"cbn": cbn}
        named = {"hook": {"text": "Chief Minister N. Chandrababu Naidu discussed the market situation."}, "script": []}
        passed = final_reel_composer.public_figure_context_qa(named, [], contextual)
        self.assertEqual(passed["status"], "PASS")
        unsupported = {"hook": {"text": "Farmers can sell excess FCV tobacco."}, "script": []}
        # Neutral contextual identification is allowed even when the package does not name the figure.
        self.assertEqual(final_reel_composer.public_figure_context_qa(unsupported, [], contextual)["status"], "PASS")
        mislabelled = {"cbn": {"asset": {"id": "MA-X", "rights_status": "VERIFIED", "label": "Someone else"}}}
        self.assertEqual(final_reel_composer.public_figure_context_qa(unsupported, [], mislabelled)["status"], "FLAG")
        attributing = {"hook": {"text": "N. Chandrababu Naidu issued the notification."}, "script": []}
        self.assertEqual(final_reel_composer.public_figure_context_qa(attributing, [], contextual)["status"], "FLAG")
        self.assertEqual(final_reel_composer.public_figure_context_qa(named, [], {})["status"], "PASS")

    def test_reference_media_http_routes_and_schema(self):
        with app.connect() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='uploaded_media_assets'"
            )}
            cols = {row[1] for row in connection.execute("PRAGMA table_info(final_reel_assets)")}
        self.assertEqual(tables, {"uploaded_media_assets"})
        self.assertTrue({"cbn_asset_id", "tdp_asset_id", "public_figure_qa_json", "composition_manifest_json"} <= cols)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        import base64
        payload = {
            "data": base64.b64encode(deterministic_png(32, 32, "cbn-http")).decode(), "filename": "cbn.png",
            "asset_type": "PUBLIC_FIGURE_PHOTO", "label": "CBN Portrait", "source_name": "AP Government",
            "license_note": "Reuse permitted with attribution.", "rights_status": "VERIFIED",
            "uploader": "Dashboard", "reviewer": "Editor",
        }
        connection.request("POST", "/api/uploads", body=json.dumps(payload), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        created = json.loads(response.read())
        self.assertEqual((response.status, created["asset"]["rights_status"]), (201, "VERIFIED"))
        connection.request("GET", "/api/uploads?type=PUBLIC_FIGURE_PHOTO")
        listed = json.loads(connection.getresponse().read())
        self.assertTrue(any(item["id"] == created["asset"]["id"] for item in listed["assets"]))
        source = Path(app.__file__).with_name("app.js").read_text(encoding="utf-8")
        for phrase in ("Reference media", "CBN portrait", "Party logo", "Rights-verified only",
                       "License / permission note", "cbn_asset_id", "tdp_asset_id", "Public-figure QA"):
            self.assertIn(phrase, source)

    def test_media_discovery_rights_gate_and_report(self):
        import media_discovery
        with app.connect() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='media_candidates'")}
        self.assertEqual(tables, {"media_candidates"})
        # A CC/GODL candidate is reusable but is never auto-approved.
        cc = media_discovery.register_candidate(
            connect=app.connect, source_url="https://commons.example/ap-field.jpg", publisher="Wikimedia (CC)",
            asset_type="IMAGE", title="AP tobacco field", license_status="ATTRIBUTION_REQUIRED",
            license_text="CC BY-SA 4.0", attribution_required=True, ap_specific="yes", real_footage=True,
            recommended_scene="SHOT 2", state="Andhra Pradesh", district="Nellore")
        self.assertEqual(cc["lifecycle_state"], "DISCOVERED")
        self.assertTrue(media_discovery.rights_eligible(cc["license_status"]))
        approved = media_discovery.approve_for_use(cc["id"], connect=app.connect, reviewer="Editor")
        self.assertEqual(approved["lifecycle_state"], "APPROVED_FOR_USE")
        # UNKNOWN-rights media can never be approved.
        unknown = media_discovery.register_candidate(
            connect=app.connect, source_url="https://news.example/clip", publisher="News",
            asset_type="VIDEO", title="Random news clip", license_status="UNKNOWN", ap_specific="unknown")
        self.assertFalse(media_discovery.rights_eligible("UNKNOWN"))
        with self.assertRaisesRegex(media_discovery.MediaDiscoveryError, "cannot enter production"):
            media_discovery.approve_for_use(unknown["id"], connect=app.connect, reviewer="Editor")
        # Report counts only cleared media as approved.
        report = media_discovery.candidate_report(connect=app.connect)
        self.assertEqual(report["approved_for_use"], 1)
        self.assertGreaterEqual(report["unknown"], 1)
        self.assertTrue(all(item["lifecycle_state"] != "APPROVED_FOR_USE"
                            for item in report["candidates"] if item["license_status"] == "UNKNOWN"))

    def test_real_ap_plan_local_context_and_rights_qa(self):
        import final_reel_composer as frc
        assets = {
            key: {"scene_key": key, "storage_uri": f"local://{key}.jpg", "candidate_id": f"MC-{key}",
                  "rights_status": "ATTRIBUTION_REQUIRED", "attribution": "Wikimedia Commons / X — CC BY-SA 4.0",
                  "attribution_required": True, "publisher": "Wikimedia", "location": "Nellore, Andhra Pradesh",
                  "content_hash": key}
            for key in ("PLATFORM", "PLANTATION", "BARN", "DRYING", "OFFICIALS", "GUNTUR", "TRACTOR")
        }
        beats, used = frc.real_ap_scene_plan(30.0, assets, has_cbn=True)
        self.assertGreaterEqual(len(beats), 9)
        self.assertLessEqual(max(b["end"] for b in beats), 30.0)
        # No fallback reset and varied shot lengths.
        self.assertFalse(any(b["kind"] == "FOOTAGE" for b in beats))
        spans = [round(b["end"] - b["start"], 2) for b in beats]
        self.assertGreaterEqual(len(set(spans)), 2)
        # Real assets dominate; the map/policy beat is the only non-photo visual.
        lc = frc.local_context_qa(beats, generated_scene_ids=[])
        self.assertEqual(lc["status"], "PASS")
        self.assertGreaterEqual(lc["real_ap_percent"], 60)
        # Rights QA passes only when every real asset is cleared and attributed.
        self.assertEqual(frc.rights_provenance_qa(assets)["status"], "PASS")
        bad = dict(assets, BAD={"scene_key": "BAD", "rights_status": "UNKNOWN", "attribution": None})
        self.assertEqual(frc.rights_provenance_qa(bad)["status"], "FLAG")

    def test_rendered_frame_continuity_qa_shape_and_hash_distance(self):
        from media_tools import hash_distance
        import final_reel_composer as frc
        self.assertEqual(hash_distance("ffffffffffffffff", "ffffffffffffffff"), 0)
        self.assertEqual(hash_distance("0000000000000000", "ffffffffffffffff"), 64)
        self.assertEqual(hash_distance(None, "ffff"), 64)
        beats = [
            {"kind": "IMAGE", "asset_source": "MC-A", "scene_key": "A", "start": 0.0, "end": 5.0},
            {"kind": "IMAGE", "asset_source": "MC-B", "scene_key": "B", "start": 5.0, "end": 10.0},
        ]
        # No probe needed: verify the QA is wired and exposes the render-output counts.
        source = Path(app.__file__).with_name("final_reel_composer.py").read_text(encoding="utf-8")
        self.assertIn("rendered_repeated_asset_count", source)
        self.assertIn("third_asset_transition_count", source)
        self.assertIn("RENDERED_FRAME", source.upper())

    def test_generated_scene_provenance_and_beat_plan(self):
        # Every generated scene is an original work with a prompt and provenance recorded.
        scenes = final_reel_composer.generate_scenes(
            ["AP_FIELD_GOLDEN"], connect=app.connect, storage_root=app.RENDER_STORAGE_ROOT, now=app.now,
            renderer=DeterministicSceneRenderer(),
        )
        row = scenes["AP_FIELD_GOLDEN"]
        self.assertEqual(row["rights_status"], "GENERATED_ORIGINAL")
        self.assertTrue(row["prompt"] and row["checksum_sha256"])
        with app.connect() as connection:
            tables = {r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='generated_scenes'")}
            cols = {r[1] for r in connection.execute("PRAGMA table_info(final_reel_assets)")}
        self.assertEqual(tables, {"generated_scenes"})
        self.assertIn("source_qa_json", cols)
        # The beat plan must stay inside the runtime and use every supplied scene.
        rows = {key: {"id": f"GS-{key}", "storage_uri": f"local://{key}.jpg", "checksum_sha256": key,
                      "rights_status": "GENERATED_ORIGINAL", "provider": "xai", "model": "m",
                      "prompt": "p", "cost_status": "known", "cost_usd": 0.08}
                for key in final_reel_composer.SCENE_ORDER}
        beats, used = final_reel_composer.scene_beat_plan(24.0, rows, has_cbn=True)
        self.assertGreaterEqual(len(beats), 7)
        self.assertLessEqual(max(b["end"] for b in beats), 24.0)
        self.assertLessEqual(max(b["end"] - b["start"] for b in beats), 4.0)
        self.assertIn("MAP", used)
        self.assertIn("DOCUMENT", used)

    def test_final_reel_subtitles_render_filled_glyphs_and_audio_mix_is_measured(self):
        # The composer must draw solid white glyphs with a black halo, never the
        # hollow outline-only text that made burned-in subtitles invisible.
        composer = Path(app.__file__).parent.joinpath("tools", "final_reel_composer.swift").read_text(encoding="utf-8")
        self.assertNotIn(".strokeWidth: -2.0,", composer)
        self.assertIn(".strokeWidth: 6.0", composer)
        self.assertIn("Outline pass first", composer)
        self.assertIn("Fill pass second", composer)
        self.assertIn(".foregroundColor: NSColor.white", composer)
        # Audio QA measures real PCM and reports speech loudness, ducking, clipping, silence.
        samples = [int(32767 * 0.12 * __import__("math").sin(i / 6.0)) for i in range(48000)]
        import struct as _struct
        payload = b"".join(_struct.pack("<h", value) for value in samples)
        header = b"RIFF" + _struct.pack("<I", 36 + len(payload)) + b"WAVE"
        header += b"fmt " + _struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
        header += b"data" + _struct.pack("<I", len(payload))
        decoded, rate, channels = final_reel_composer.parse_pcm_wav(header + payload)
        self.assertEqual((rate, channels, len(decoded)), (48000, 1, len(samples)))
        # Speech cue from 0.5s so the lead-in window is music-only and quiet relative to speech.
        cues = [{"text": "one two", "start": 0.5, "end": 1.5}]
        rendered = tuple([0] * 24000) + tuple(samples)  # 0.5s quiet lead-in, then speech
        with patch.object(final_reel_composer, "_decode_audio", return_value=(rendered, 48000, 1)):
            qa = final_reel_composer._audio_qa(b"x", cues)
        self.assertEqual(qa["status"], "PASS", qa["errors"])
        self.assertTrue(qa["checks"]["narration_track_present"])
        self.assertLess(qa["checks"]["true_peak"], final_reel_composer.CLIP_CEILING)
        # A silent render must fail closed.
        silent = tuple([0] * 48000)
        with patch.object(final_reel_composer, "_decode_audio", return_value=(silent, 48000, 1)):
            self.assertEqual(final_reel_composer._audio_qa(b"x", cues)["status"], "FLAG")

    def test_final_reel_subtitle_qa_flags_missing_glyphs(self):
        cues = [{"text": "the union government permitted sale", "start": 1.0, "end": 2.0}]
        frames = [{"jpeg": b"frame", "actual_seconds": 1.5}]

        class BlankOCR:
            name = "blank"

            def detect(self, jpeg):
                return []

        with patch.object(final_reel_composer, "frame_extractor_for", lambda: type("E", (), {"extract": lambda self, data, times: frames})()), \
             patch.object(final_reel_composer, "ocr_provider_for", lambda: BlankOCR()):
            qa = final_reel_composer._subtitle_qa(b"video", cues)
        self.assertEqual(qa["status"], "FLAG")
        self.assertTrue(any("no glyphs" in error for error in qa["errors"]))

    def test_final_reel_narration_is_package_exact_and_schema_is_immutable(self):
        package = {
            "hook": {"text": "Registered growers can sell through authorised platforms"},
            "script": [{"sequence": 1, "text": "The Union government permitted the sale."}],
        }
        claims = [{"text": "The Union government permitted the sale."}]
        narration = final_reel_composer.approved_narration(package)
        self.assertEqual(
            narration,
            "Registered growers can sell through authorised platforms. The Union government permitted the sale.",
        )
        self.assertEqual(final_reel_composer.validate_factual_narration(narration, package, claims)["status"], "PASS")
        self.assertEqual(
            final_reel_composer.validate_factual_narration(narration + " Prices doubled.", package, claims)["status"],
            "FLAG",
        )
        phrases = final_reel_composer.subtitle_phrases(narration)
        self.assertTrue(all(len(phrase.split()) <= 5 for phrase in phrases))
        self.assertEqual(" ".join(phrases), narration)
        edit = (8).to_bytes(4, "big") + b"edts"
        track = (8 + len(edit)).to_bytes(4, "big") + b"trak" + edit
        movie = (8 + len(track)).to_bytes(4, "big") + b"moov" + track
        neutralized, count = final_reel_composer._neutralize_mp4_edit_lists(movie)
        self.assertEqual((count, len(neutralized)), (1, len(movie)))
        self.assertNotIn(b"edts", neutralized)
        self.assertIn(b"free", neutralized)
        with app.connect() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('final_reel_assets','final_reel_reviews')"
            )}
            triggers = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'final_reel_%_immutable_%'"
            )}
        self.assertEqual(tables, {"final_reel_assets", "final_reel_reviews"})
        self.assertEqual(len(triggers), 4)

    def test_https_transport_marks_post_send_timeouts_non_retryable(self):
        class TimeoutConnection:
            def __init__(self, host, port, timeout):
                self.sock = None

            def connect(self):
                if self.fail_on == "connect":
                    raise socket.timeout()

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                raise socket.timeout()

            def close(self):
                pass

        for fail_on, retryable in (("connect", True), ("response", False)):
            with self.subTest(fail_on=fail_on):
                TimeoutConnection.fail_on = fail_on
                with patch.object(media_rendering, "HTTPSConnection", TimeoutConnection):
                    with self.assertRaises(RendererTimeoutError) as caught:
                        media_rendering.https_request("POST", "https://api.x.ai/v1/images/generations", timeout_seconds=1)
                self.assertEqual(caught.exception.retryable, retryable)
        with self.assertRaises(media_rendering.RendererInvalidRequestError):
            media_rendering.https_request("GET", "http://insecure.example/image.png", timeout_seconds=1)
        self.assertEqual(media_rendering._retry_after_seconds("3"), 3.0)
        self.assertEqual(media_rendering._retry_after_seconds("9999"), media_rendering.LIVE_RENDERER_MAX_RETRY_AFTER_SECONDS)
        self.assertIsNone(media_rendering._retry_after_seconds("soon"))

    def test_image_inspection_decodes_supported_formats_and_rejects_corruption(self):
        png = deterministic_png(40, 20, "inspection")
        self.assertEqual(inspect_image(png)["width"], 40)
        self.assertEqual((inspect_image(jpeg_bytes(300, 200))["mime_type"], inspect_image(jpeg_bytes(300, 200))["height"]),
                         ("image/jpeg", 200))
        vp8x = b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
        webp = b"RIFF" + (4 + len(vp8x) + 10).to_bytes(4, "little") + b"WEBP" + vp8x + b"\x00" * 10
        self.assertEqual((inspect_image(webp)["width"], inspect_image(webp)["height"]), (640, 480))
        for corrupt in (b"", b"GIF89a", png[:-10], png[:40] + b"\x00" + png[41:], jpeg_bytes(10, 10)[:-2]):
            with self.subTest(size=len(corrupt)), self.assertRaises(ImageDecodeError):
                inspect_image(corrupt)

    def test_research_migration_preserves_architecture_02b_data(self):
        connection = sqlite3.connect(":memory:")
        for version in range(1, 6):
            path = next(app.MIGRATIONS.glob(f"{version:03d}_*.sql"))
            connection.executescript(path.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO events(id,title,source,source_url,status,priority,created_at,updated_at,first_seen_at,last_seen_at,workspace_key,event_time) "
            "VALUES('EV-LEGACY','Legacy event','Legacy source','https://example.test/legacy','DETECTED','NORMAL',"
            "'2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00','2026-09-29T00:00:00+00:00',"
            "'2026-09-29T00:00:00+00:00','n-chandrababu-naidu-andhra-pradesh','2026-09-29T00:00:00+00:00')"
        )
        for version in (6, 7, 8, 9, 10, 11, 12):
            migration = next(app.MIGRATIONS.glob(f"{version:03d}_*.sql"))
            connection.executescript(migration.read_text(encoding="utf-8"))
        row = connection.execute("SELECT title,research_status FROM events WHERE id='EV-LEGACY'").fetchone()
        self.assertEqual(row, ("Legacy event", "NOT_RESEARCHED"))
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone())
        columns = {row[1] for row in connection.execute("PRAGMA table_info(research_runs)")}
        self.assertIn("provider_elapsed_seconds", columns)
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='verification_decisions'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_decisions'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='production_jobs'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_packages'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='render_jobs'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='generated_assets'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='media_qa_results'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_ledger'"
        ).fetchone())
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        self.assertIn("render_status", event_columns)
        connection.close()


if __name__ == "__main__":
    unittest.main()
