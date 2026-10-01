"""Deterministic source acquisition for verification candidates.

Acquisition deliberately stops before factual adjudication.  It finds, retrieves,
classifies, groups, and matches documents so the existing verification policy can
evaluate a structured evidence packet.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urljoin, urlparse


SOURCE_CLASSES = {
    "OFFICIAL_PRIMARY",
    "INDEPENDENT_REPORTING",
    "SYNDICATED_REPORTING",
    "AGGREGATOR",
    "PRESS_RELEASE_REPRINT",
    "UNKNOWN",
}
DOCUMENT_TERMS = (
    "notification", "circular", "order", "ministry notification", "board notice",
    "press release", "gazette", "office memorandum", "auction notice",
)
DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".odt")
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "their", "this", "to", "was", "were", "with",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalized_host(url):
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def normalized_text(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))


def content_tokens(value):
    return {token for token in normalized_text(value).split() if len(token) > 2 and token not in STOPWORDS}


@dataclass(frozen=True)
class OfficialAuthority:
    id: str
    name: str
    domain: str
    authority_type: str
    priority: int
    document_types: tuple[str, ...]
    enabled: bool = True


class OfficialSourceRegistry:
    def __init__(self, authorities):
        self.authorities = tuple(authorities)
        seen = set()
        for authority in self.authorities:
            if not authority.id or not authority.name or not authority.domain:
                raise ValueError("official authority requires id, name, and domain")
            if authority.id in seen:
                raise ValueError(f"duplicate official authority id: {authority.id}")
            if authority.priority < 1:
                raise ValueError("official authority priority must be positive")
            if not authority.document_types:
                raise ValueError("official authority requires supported document types")
            seen.add(authority.id)

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload.get("authorities"), list):
            raise ValueError("official registry must contain an authorities array")
        def registry_domain(value):
            raw = str(value).strip()
            return normalized_host(raw if "://" in raw else "https://" + raw)

        return cls(OfficialAuthority(
            id=str(item["id"]).strip(), name=str(item["name"]).strip(),
            domain=registry_domain(item["domain"]),
            authority_type=str(item["authority_type"]).strip(), priority=int(item["priority"]),
            document_types=tuple(str(value).upper() for value in item["document_types"]),
            enabled=bool(item.get("enabled", True)),
        ) for item in payload["authorities"])

    @classmethod
    def from_file(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def enabled(self):
        return tuple(sorted((item for item in self.authorities if item.enabled), key=lambda item: item.priority))

    def match(self, url):
        host = normalized_host(url)
        matches = [item for item in self.enabled() if host == item.domain or host.endswith("." + item.domain)]
        return min(matches, key=lambda item: (host != item.domain, -len(item.domain), item.priority)) if matches else None


@dataclass(frozen=True)
class SearchHint:
    phrase: str
    document_terms: tuple[str, ...]
    source_url: str | None = None


@dataclass(frozen=True)
class DiscoveryQuery:
    strategy: str
    query: str
    target_claim_ids: tuple[str, ...]
    domains: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class DiscoveryLead:
    url: str
    title: str
    target_claim_ids: tuple[str, ...]
    provider: str
    strategy: str
    snippet: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    leads: tuple[DiscoveryLead, ...]
    request_id: str | None = None
    search_calls: int | None = None
    cost_usd: float | None = None
    cost_status: str = "unknown"


class DiscoveryProvider(ABC):
    name = "provider"

    @abstractmethod
    def search(self, queries):
        raise NotImplementedError


class DeterministicDiscoveryProvider(DiscoveryProvider):
    """Fixture/provider adapter that never performs I/O."""

    name = "deterministic"

    def __init__(self, leads_by_strategy=None):
        self.leads_by_strategy = leads_by_strategy or {}
        self.calls = 0

    def search(self, queries):
        self.calls += 1
        leads = []
        for query in queries:
            for item in self.leads_by_strategy.get(query.strategy, []):
                leads.append(DiscoveryLead(
                    url=item["url"], title=item.get("title") or item["url"],
                    target_claim_ids=tuple(item.get("target_claim_ids") or query.target_claim_ids),
                    provider=self.name, strategy=query.strategy, snippet=item.get("snippet"),
                ))
        unique = {lead.url: lead for lead in leads}
        return DiscoveryResult(tuple(unique.values()), search_calls=0, cost_usd=0.0, cost_status="not_billed")


class CompositeDiscoveryProvider(DiscoveryProvider):
    name = "composite"

    def __init__(self, providers):
        self.providers = tuple(providers)

    def search(self, queries):
        leads, searches, costs, unknown = [], 0, 0.0, False
        for provider in self.providers:
            result = provider.search(queries)
            leads.extend(result.leads)
            searches += result.search_calls or 0
            costs += result.cost_usd or 0.0
            unknown = unknown or result.cost_status == "unknown"
        unique = {lead.url: lead for lead in leads}
        return DiscoveryResult(
            tuple(unique.values()), search_calls=searches,
            cost_usd=None if unknown else costs, cost_status="unknown" if unknown else "known",
        )


def extract_search_hints(text, source_url=None):
    hints = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        terms = tuple(term for term in DOCUMENT_TERMS if re.search(r"\b" + re.escape(term) + r"\b", sentence, re.I))
        if terms:
            hints.append(SearchHint(" ".join(sentence.split())[:700], terms, source_url))
    return tuple(hints[:12])


def _quoted_fragment(value, words=16):
    tokens = str(value).split()
    return '"' + " ".join(tokens[:words]).strip('"“”') + '"'


def build_discovery_plan(event_title, claims, source_text, registry):
    """Build all six required strategies without contacting a provider."""
    authorities = registry.enabled()
    domains = tuple(item.domain for item in authorities)
    hints = extract_search_hints(source_text)
    queries = []
    for claim in claims:
        claim_id, text = claim["claim_id"], claim["text"]
        target = (claim_id,)
        core = " ".join(word for word in text.split() if normalized_text(word) not in STOPWORDS)
        queries.append(DiscoveryQuery("A_AUTHORITATIVE_DOMAIN", core, target, domains,
                                      "Search enabled official authorities in registry priority order."))
        queries.append(DiscoveryQuery("B_EXACT_PHRASE", _quoted_fragment(text), target, (),
                                      "Find exact wording from the originating report."))
        for hint in hints[:3]:
            queries.append(DiscoveryQuery("C_TITLE_NOTIFICATION_FRAGMENT", _quoted_fragment(hint.phrase, 12), target,
                                          domains, "Search the referenced official-document fragment."))
        entities = re.findall(r"\b(?:[A-Z][A-Za-z&.-]+(?:\s+[A-Z][A-Za-z&.-]+){0,4}|20\d{2}(?:-\d{2})?)\b", text)
        date_terms = re.findall(r"\b20\d{2}(?:-\d{2})?\b", text)
        entity_query = " ".join(dict.fromkeys(entities + date_terms)) or event_title
        queries.append(DiscoveryQuery("D_ENTITY_DATE_RANGE", entity_query, target, domains,
                                      "Search named entities together with explicit date or crop-season terms."))
        queries.append(DiscoveryQuery("E_SECONDARY_CORROBORATION", f"{event_title} {core}", target, (),
                                      "Find a genuinely independent reporting family."))
    # Strategy F is deterministic link extraction and is recorded even before pages exist.
    all_ids = tuple(claim["claim_id"] for claim in claims)
    queries.append(DiscoveryQuery("F_DIRECT_DOCUMENT_LINK", "Extract document links from retrieved pages", all_ids,
                                  domains, "Inspect retrieved pages for direct official documents."))
    unique = {}
    for query in queries:
        unique[(query.strategy, query.query, query.target_claim_ids, query.domains)] = query
    return tuple(unique.values())


class _DocumentHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.title = []
        self.meta = {}
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"title", "h1", "h2", "h3", "p", "li", "article", "time"}:
            self.stack.append(tag)
        if tag == "meta" and attributes.get("content"):
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key:
                self.meta[key] = attributes["content"]
        if tag == "link" and "canonical" in str(attributes.get("rel", "")).lower() and attributes.get("href"):
            self.meta["canonical"] = attributes["href"]
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        cleaned = " ".join(data.split())
        if cleaned and self.stack:
            self.text.append(cleaned)
            if self.stack[-1] == "title":
                self.title.append(cleaned)


@dataclass(frozen=True)
class RetrievedResponse:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDocument:
    original_url: str
    final_url: str
    status: int
    content_type: str
    title: str
    publication_date: str | None
    publisher: str
    retrieved_at: str
    checksum: str
    text: str
    document_type: str
    metadata: dict
    pages: tuple[dict, ...]
    direct_document_urls: tuple[str, ...]


class PDFExtractor:
    def __init__(self, helper_path=None, runner=None):
        self.helper_path = Path(helper_path) if helper_path else Path(__file__).parent / "tools" / "pdf_extract.swift"
        self.runner = runner or subprocess.run

    def extract(self, body):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(body)
            handle.flush()
            completed = self.runner(
                ["/usr/bin/swift", str(self.helper_path), handle.name], capture_output=True,
                text=True, timeout=45, check=False,
            )
        if completed.returncode:
            raise ValueError("PDF text extraction failed")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("PDF extractor returned invalid output") from error
        pages = tuple({"page": int(item["page"]), "text": " ".join(str(item["text"]).split())}
                      for item in payload.get("pages", []) if str(item.get("text", "")).strip())
        if not pages:
            raise ValueError("PDF is unreadable; no extracted text is available")
        return pages, payload.get("metadata") or {}


class SourceRetriever:
    def __init__(self, transport, *, pdf_extractor=None, url_validator=None):
        self.transport = transport
        self.pdf_extractor = pdf_extractor or PDFExtractor()
        self.url_validator = url_validator

    def retrieve(self, url):
        if self.url_validator:
            self.url_validator(url)
        response = self.transport(url)
        if self.url_validator:
            self.url_validator(response.final_url)
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"source returned HTTP {response.status}")
        checksum = sha256(response.body).hexdigest()
        content_type = response.content_type.split(";", 1)[0].strip().lower()
        publisher = normalized_host(response.final_url)
        if content_type == "application/pdf" or response.final_url.lower().endswith(".pdf"):
            pages, metadata = self.pdf_extractor.extract(response.body)
            text = "\n".join(f"[Page {item['page']}] {item['text']}" for item in pages)
            metadata_lookup = {normalized_text(key).replace(" ", ""): value for key, value in metadata.items()}
            title = str(metadata_lookup.get("title") or Path(urlparse(response.final_url).path).name or "Official PDF")
            publication = metadata_lookup.get("creationdate") or metadata_lookup.get("modificationdate")
            return ExtractedDocument(url, response.final_url, response.status, content_type, title, publication, publisher,
                                     utc_now(), checksum, text, "PDF", metadata, pages, ())
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"unsupported evidence content type: {content_type or 'unknown'}")
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", response.content_type, re.I)
        if match:
            charset = match.group(1)
        document = response.body.decode(charset, errors="replace")
        parser = _DocumentHTMLParser()
        parser.feed(document)
        text = unescape(" ".join(dict.fromkeys(parser.text))).strip()
        if not text:
            raise ValueError("no factual page text could be extracted")
        title = unescape(parser.meta.get("og:title") or " ".join(parser.title) or response.final_url).strip()
        publication = next((parser.meta[key] for key in (
            "article:published_time", "date", "datepublished", "dc.date", "pubdate"
        ) if parser.meta.get(key)), None)
        links = tuple(dict.fromkeys(urljoin(response.final_url, item) for item in parser.links
                                    if urlparse(urljoin(response.final_url, item)).path.lower().endswith(DOCUMENT_SUFFIXES)))
        metadata = {"canonical_url": urljoin(response.final_url, parser.meta.get("canonical", response.final_url))}
        return ExtractedDocument(url, response.final_url, response.status, content_type, title, publication, publisher,
                                 utc_now(), checksum, text, "HTML", metadata, (), links)


def classify_source(document, registry, independent_domains=()):
    authority = registry.match(document.final_url)
    if authority:
        return "OFFICIAL_PRIMARY", f"Host matches enabled official authority {authority.name}; content still requires claim adjudication."
    host = normalized_host(document.final_url)
    lowered = normalized_text(document.text[:8000])
    if any(host == normalized_host("https://" + item) or host.endswith("." + normalized_host("https://" + item))
           for item in independent_domains):
        if re.search(r"\b(ani|pti|reuters|associated press)\b", lowered):
            return "SYNDICATED_REPORTING", "Registered reporting host carries an explicit wire-service marker."
        if re.search(r"\b(press release|pib release|official release)\b", lowered):
            return "PRESS_RELEASE_REPRINT", "Reporting host identifies the text as an official release or press-release reprint."
        return "INDEPENDENT_REPORTING", "Host matches the configured independent-reporting registry."
    if re.search(r"\b(aggregated from|news aggregator)\b", lowered):
        return "AGGREGATOR", "Page identifies itself as aggregated content."
    return "UNKNOWN", "No enabled official or independent source registration matches this host."


def family_for_candidate(candidate, existing=()):
    """Return stable family id and an auditable relationship explanation."""
    host = normalized_host(candidate["final_url"])
    text = normalized_text(candidate.get("text", ""))
    for other in existing:
        other_text = normalized_text(other.get("text", ""))
        similarity = SequenceMatcher(None, text[:20000], other_text[:20000]).ratio()
        same_host = host == normalized_host(other["final_url"])
        syndication = candidate["source_class"] in {"SYNDICATED_REPORTING", "PRESS_RELEASE_REPRINT"}
        syndication = syndication or other["source_class"] in {"SYNDICATED_REPORTING", "PRESS_RELEASE_REPRINT"}
        if same_host or similarity >= 0.72 or (syndication and similarity >= 0.45):
            reason = "Same normalized publisher host." if same_host else (
                "High text overlap indicates a copied or syndicated document."
                if similarity >= 0.72 else "Syndication marker and substantial text overlap indicate one reporting family."
            )
            return other["family_id"], reason, "SAME_FAMILY", similarity
    material = host or sha256(text.encode()).hexdigest()
    return "EF-" + sha256(material.encode()).hexdigest()[:12].upper(), "Distinct host with no syndication-level text overlap.", "NEW_FAMILY", 0.0


def match_claims(claims, candidate):
    rows = []
    for claim in claims:
        score, passage, page = best_passage(claim["text"], candidate.get("text", ""), candidate.get("pages") or ())
        rows.append({
            "claim_id": claim["claim_id"], "candidate_id": candidate["id"], "match_score": round(score, 6),
            "matched_passage": passage if score >= 0.2 else "", "page_number": page,
            "relationship": "CANDIDATE" if score >= 0.28 else "TEXT_ABSENT",
            "reason": "Candidate wording overlaps the claim; adjudication is still required." if score >= 0.28
                      else "Retrieved text does not sufficiently address this claim.",
        })
    return rows


def best_passage(claim_text, document_text, pages=()):
    claim = content_tokens(claim_text)
    best = (0.0, "", None)
    sources = [(item.get("text", ""), int(item["page"])) for item in pages] if pages else [(document_text, None)]
    for text, page in sources:
        for passage in re.split(r"(?<=[.!?])\s+|\n+", text):
            tokens = content_tokens(passage)
            score = len(claim & tokens) / len(claim) if claim else 0.0
            if score > best[0]:
                best = (score, passage.strip()[:1800], page)
    return best


def build_evidence_packet(claim, matrix_rows, candidates):
    by_id = {item["id"]: item for item in candidates}
    matched = []
    for row in matrix_rows:
        if row["claim_id"] != claim["claim_id"] or row["relationship"] == "TEXT_ABSENT":
            continue
        candidate = by_id[row["candidate_id"]]
        matched.append({
            "candidate_id": candidate["id"], "url": candidate["final_url"], "title": candidate["title"],
            "source_class": candidate["source_class"], "family_id": candidate["family_id"],
            "publication_date": candidate.get("publication_date"), "passage": row["matched_passage"],
            "page_number": row.get("page_number"), "document_type": candidate["document_type"],
            "authority": candidate.get("authority"), "classification_reason": candidate["classification_reason"],
        })
    families = {item["family_id"] for item in matched if item["source_class"] == "INDEPENDENT_REPORTING"}
    official = any(item["source_class"] == "OFFICIAL_PRIMARY" for item in matched)
    sufficient = official or len(families) >= 2
    return {
        "claim_id": claim["claim_id"], "claim": claim["text"], "candidates": matched,
        "contradictions": [], "official_primary_found": official,
        "independent_family_count": len(families), "deterministically_sufficient": sufficient,
        "required_condition": "Explicit official primary support or two independent reporting families.",
        "note": "Sufficiency is a routing signal only; the unchanged verification policy makes the decision.",
    }
