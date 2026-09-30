import tempfile
import unittest
import sqlite3
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import app
from research import MissingAPIKeyError, ProviderResult, ResearchProviderError, GrokResearchAdapter
from verification import VerificationProviderResult
from content_ceo import ContentProviderResult
from content_production import DeterministicProductionFixtureAdapter, ProductionProviderResult
from media_rendering import (
    AsyncMediaRenderer, DeterministicImageRenderer, InvalidRendererResponse, ProviderPollResult,
    ProviderSubmission, RenderResult, RendererAuthError, RendererDownloadError, RendererRateLimitError,
    RendererNetworkError, RendererServerError, RendererTimeoutError, renderer_configuration,
)
from media_storage import LocalMediaStorage, MediaStorage
from media_qa import MediaQAResult, VisualQAProvider


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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB = Path(self.temp.name) / "test.sqlite3"
        app.RENDER_STORAGE_ROOT = Path(self.temp.name) / "generated-media"
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

    def production_approved_event(self, *, with_media=False):
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

    def executable_content_decision(self):
        event, _, _ = self.production_approved_event(with_media=True)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider())
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
        self.assertEqual(families, 1)
        self.assertEqual((decision["decision"], decision["independent_family_count"]), ("INSUFFICIENT_EVIDENCE", 1))

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
        self.assertEqual(decision["decision"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(decision["independent_family_count"], 1)
        self.assertIn("mentions_only", relationships)

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
        self.assertEqual(decision["decision"], "CONFLICTED")
        self.assertIn("conflict", decision["rationale"].lower())

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
        self.assertIn("No claim-set", decision["missing_evidence_or_media"][0])

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

    def test_content_provider_failure_persists_human_review(self):
        event, _, _ = self.production_approved_event(with_media=True)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FailingContentProvider())
        room = app.event_room(event["event_id"])
        run = room["content_decision_runs"][0]
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["cost_status"], "unknown")
        self.assertIsNone(run["total_tokens"])
        self.assertEqual(run["decision_record"]["decision"], "HUMAN_REVIEW")
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

    def test_production_blocks_hold_and_test_only_before_job_creation(self):
        event, _, _ = self.production_approved_event(with_media=False)
        with patch.object(app, "CONTENT_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_content_decision(event["event_id"], "grok")
        app.run_content_decision_job(queued["run"]["id"], FixtureContentProvider())
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
            self.assertEqual((asset["mime_type"], asset["width"], asset["height"]), ("image/png", 1200, 1500))
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
        with self.assertRaisesRegex(ValueError, "requires media type IMAGE"):
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
            (ControlledAsyncImageRenderer(statuses=["PROCESSING"], max_poll_attempts=2), "FAILED", "POLL_ATTEMPTS_EXHAUSTED", 2),
            (ControlledAsyncImageRenderer(statuses=["NO_OUTPUT"]), "FAILED", "INVALID_RESPONSE", 1),
            (ControlledAsyncImageRenderer(statuses=["COMPLETED"], download_error=RendererDownloadError("download failed")),
             "FAILED", "DOWNLOAD_FAILED", 1),
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
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 1)

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
        self.assertEqual((result["job"]["status"], result["job"]["failure_code"]), ("FAILED", "RATE_LIMITED"))
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
