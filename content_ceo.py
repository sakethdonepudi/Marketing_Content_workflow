"""Replaceable Content CEO providers.

Providers only rank an already-approved evidence package. Eligibility is
enforced in app.py before this interface is called, and provider output cannot
add claims or enqueue production work.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from http.client import HTTPSConnection
import json
import os
import socket
import time
from urllib.parse import urlparse

from research import (
    ConnectionTimeoutError,
    DEFAULT_XAI_MODEL,
    InvalidProviderResponse,
    MissingAPIKeyError,
    ProviderNetworkError,
    ResearchProviderError,
    ResponseTimeoutError,
    USD_TICKS_PER_DOLLAR,
    XAI_RESPONSES_URL,
)


DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["CREATE", "HOLD", "SKIP"]},
        "media_source_strategy": {"type": "string", "enum": [
            "GENERATE_ORIGINAL", "USE_APPROVED_OWNED_MEDIA", "USE_APPROVED_LICENSED_MEDIA", "NONE",
        ]},
        "recommended_format": {"type": "string", "enum": ["REEL", "STORY", "CAROUSEL", "IMAGE"]},
        "language": {"type": "string"},
        "proposed_duration_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        "priority": {"type": "string", "enum": ["BREAKING", "HIGH", "NORMAL", "LOW"]},
        "factual_rationale": {"type": "string"},
        "missing_evidence_or_media": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "decision", "media_source_strategy", "recommended_format", "language", "proposed_duration_seconds",
        "priority", "factual_rationale", "missing_evidence_or_media",
    ],
}


@dataclass(frozen=True)
class ContentProviderResult:
    decision: dict
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_usd_ticks: int | None = None
    elapsed_seconds: float | None = None


class ContentDecisionProvider(ABC):
    name = "provider"
    mode = "test"

    @property
    @abstractmethod
    def model(self):
        raise NotImplementedError

    @abstractmethod
    def decide(self, decision_bundle, *, token_limit, timeout_seconds):
        raise NotImplementedError


def _media_format(media):
    available = [item for item in media if item["availability_status"] == "available" and item["rights_status"] == "verified"]
    videos = [item for item in available if item["media_type"] == "video"]
    images = [item for item in available if item["media_type"] == "image"]
    if videos:
        return "REEL", 45
    if len(images) >= 3:
        return "CAROUSEL", 45
    if images:
        return "IMAGE", 15
    return "IMAGE", 15


def _approved_media_strategy(media):
    available = [
        item for item in media
        if item["availability_status"] == "available" and item["rights_status"] == "verified"
    ]
    if not available:
        return "GENERATE_ORIGINAL"
    owned = any((item.get("metadata") or {}).get("rights_basis") == "owned" for item in available)
    return "USE_APPROVED_OWNED_MEDIA" if owned else "USE_APPROVED_LICENSED_MEDIA"


class DeterministicContentAdapter(ContentDecisionProvider):
    name = "deterministic-content-ceo"
    mode = "test"

    @property
    def model(self):
        return "deterministic-content-policy-v1"

    def decide(self, decision_bundle, **kwargs):
        del kwargs
        recommended_format, duration = _media_format(decision_bundle["media"])
        recent_duplicate = decision_bundle.get("recent_duplicate")
        age_days = decision_bundle.get("event_age_days")
        if recent_duplicate:
            decision = "SKIP"
            media_strategy = "NONE"
            rationale = "Recent publishing history already covers this event and approved claim-set version."
            missing = []
        elif age_days is not None and age_days > decision_bundle["freshness_days"]:
            decision = "HOLD"
            media_strategy = "NONE"
            rationale = "The event is older than the configured freshness window; wait for a material update."
            missing = ["A fresh, verified development is required before creating content."]
        elif not decision_bundle.get("original_generation_allowed", False) and not any(
            item["availability_status"] == "available" and item["rights_status"] == "verified"
            for item in decision_bundle["media"]
        ):
            decision = "HOLD"
            media_strategy = "NONE"
            rationale = "The story requires media that is not approved, and original generation is not safe for this story."
            missing = ["Required approved media or human input is missing."]
        else:
            decision = "CREATE"
            media_strategy = _approved_media_strategy(decision_bundle["media"])
            rationale = (
                "The event is fresh, not recently duplicated, and can be represented using "
                + ("original non-documentary generated visuals." if media_strategy == "GENERATE_ORIGINAL"
                   else "explicitly approved media.")
            )
            missing = []
        return ContentProviderResult(
            decision={
                "decision": decision,
                "media_source_strategy": media_strategy,
                "recommended_format": recommended_format,
                "language": decision_bundle.get("default_language", "English"),
                "proposed_duration_seconds": duration,
                "priority": decision_bundle["event"]["priority"],
                "factual_rationale": rationale,
                "missing_evidence_or_media": missing,
            },
            input_tokens=0, output_tokens=0, total_tokens=0,
            cost_usd=0.0, cost_usd_ticks=0, elapsed_seconds=0.0,
        )


class GrokContentAdapter(ContentDecisionProvider):
    name = "grok"
    mode = "live"

    def __init__(self, api_key=None, model=None):
        self._api_key = api_key if api_key is not None else os.environ.get("XAI_API_KEY")
        self._model = model or os.environ.get("XAI_MODEL", DEFAULT_XAI_MODEL)

    @property
    def model(self):
        return self._model

    def decide(self, decision_bundle, *, token_limit, timeout_seconds):
        if not self._api_key:
            raise MissingAPIKeyError("XAI_API_KEY is not configured; live Content CEO was not started.")
        system = (
            "You are a Content CEO selecting whether verified public-information evidence deserves content and which "
            "safe media-source strategy supports it. Missing source media is not by itself a reason to HOLD: choose "
            "GENERATE_ORIGINAL when neutral, non-deceptive visuals can be generated safely. Evidence-source media is "
            "evidence only and must never be selected for reuse. For public-affairs stories, prefer maps, diagrams, "
            "objects, processes, environments, and neutral contextual illustrations; do not depict identifiable people "
            "unless explicitly justified by approved inputs and human review. Generated visuals are not documentary "
            "evidence. Use only the supplied approved claims. Do not add facts, write a "
            "script, propose persuasion, or target political or demographic groups. Assess public-information relevance, "
            "freshness, duplication, and media fit. Return only the required structured decision."
        )
        payload = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "low"},
            "max_output_tokens": token_limit,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": "APPROVED_DECISION_INPUT:\n" + json.dumps(decision_bundle, ensure_ascii=False)},
            ],
            "text": {"format": {"type": "json_schema", "name": "content_ceo_decision", "strict": True, "schema": DECISION_SCHEMA}},
        }
        encoded = json.dumps(payload).encode("utf-8")
        endpoint = urlparse(XAI_RESPONSES_URL)
        connection = HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=min(timeout_seconds, 10))
        started = time.monotonic()
        try:
            try:
                connection.connect()
            except (socket.timeout, TimeoutError) as error:
                raise ConnectionTimeoutError("xAI connection timed out before TLS completed.") from error
            except (socket.gaierror, OSError) as error:
                raise ProviderNetworkError("xAI could not be reached during Content CEO connection setup.") from error
            if connection.sock:
                connection.sock.settimeout(timeout_seconds)
            try:
                connection.request(
                    "POST", endpoint.path, body=encoded,
                    headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                raw = response.read()
            except (socket.timeout, TimeoutError) as error:
                raise ResponseTimeoutError("xAI connected, but the Content CEO response timed out.") from error
            except OSError as error:
                raise ProviderNetworkError("xAI disconnected before the Content CEO response completed.") from error
        finally:
            connection.close()
        elapsed = time.monotonic() - started
        if response.status >= 400:
            error = ResearchProviderError(f"xAI Content CEO request failed with HTTP {response.status}.")
            error.retryable = response.status in (408, 409, 429) or response.status >= 500
            error.code = f"http_{response.status}"
            raise error
        try:
            data = json.loads(raw.decode("utf-8"))
            output_text = next(
                content["text"]
                for item in data.get("output", []) if item.get("type") == "message"
                for content in item.get("content", []) if content.get("type") == "output_text"
            )
            decision = json.loads(output_text)
        except (UnicodeDecodeError, json.JSONDecodeError, StopIteration, KeyError, TypeError) as error:
            raise InvalidProviderResponse("xAI returned no valid structured Content CEO decision.") from error
        usage = data.get("usage") or {}
        ticks = usage.get("cost_in_usd_ticks")
        cost = usage.get("cost_in_usd")
        if cost is None and isinstance(ticks, int):
            cost = ticks / USD_TICKS_PER_DOLLAR
        return ContentProviderResult(
            decision=decision, provider_request_id=data.get("id"), input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"), total_tokens=usage.get("total_tokens"),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            cost_usd_ticks=ticks, elapsed_seconds=elapsed,
        )


def content_provider_for(name):
    if name == "test":
        return DeterministicContentAdapter()
    if name == "grok":
        return GrokContentAdapter()
    raise ValueError("Content CEO provider must be 'test' or 'grok'")
