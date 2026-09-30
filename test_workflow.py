import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch
import app
from research import MissingAPIKeyError, ProviderResult, ResearchProviderError, GrokResearchAdapter
from verification import VerificationProviderResult


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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB = Path(self.temp.name) / "test.sqlite3"
        app.init()

    def tearDown(self):
        self.temp.cleanup()

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
        for version in (6, 7, 8):
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
        connection.close()


if __name__ == "__main__":
    unittest.main()
