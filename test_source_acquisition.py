import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from source_acquisition import (
    CompositeDiscoveryProvider, DeterministicDiscoveryProvider, ExtractedDocument,
    OfficialSourceRegistry, RetrievedResponse, SourceRetriever, build_discovery_plan,
    build_evidence_packet, classify_source, extract_search_hints, family_for_candidate,
    match_claims,
)
from verification import VerificationProviderResult


TOBACCO_CLAIM_A = (
    "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh "
    "during the 2025-26 crop season."
)
TOBACCO_CLAIM_B = (
    "A Union Commerce Ministry notification permitted registered and unregistered growers "
    "to sell excess FCV tobacco at all Tobacco Board-authorised auction platforms."
)
OFFICIAL_TEXT = (
    "The Union government permitted sale of excess FCV tobacco produced in Andhra Pradesh during "
    "the 2025-26 crop season. A Union Commerce Ministry notification permitted registered and "
    "unregistered growers to sell excess FCV tobacco at all Tobacco Board-authorised auction platforms."
)
GAZETTE_PASSAGE = (
    "MINISTRY OF COMMERCE AND INDUSTRY (Department of Commerce) NOTIFICATION. "
    "The Central Government considers it necessary in the public interest to dispose of the excess flue cured "
    "virginia tobacco of registered growers and unauthorised flue cured virginia tobacco of unregistered growers "
    "at the authorised auction platforms of the Tobacco Board in the State of Andhra Pradesh. The Central "
    "Government permits the sale of excess flue cured virginia tobacco of the registered growers and unauthorised "
    "flue cured virginia tobacco of the unregistered growers at the auction platforms authorised by the Tobacco "
    "Board in the State of Andhra Pradesh for the auctions during the crop season 2025-2026."
)


def response(url, body, content_type="text/html; charset=utf-8", final_url=None, status=200):
    return RetrievedResponse(url, final_url or url, status, content_type, body if isinstance(body, bytes) else body.encode(), {})


def candidate(identifier, url, source_class, text, family_id=None, pages=()):
    return {
        "id": identifier, "final_url": url, "title": identifier, "source_class": source_class,
        "classification_reason": "fixture classification", "family_id": family_id or "EF-" + identifier,
        "text": text, "pages": list(pages), "document_type": "PDF" if pages else "HTML",
        "publication_date": "2026-09-30", "authority": "Fixture authority" if source_class == "OFFICIAL_PRIMARY" else None,
    }


class FakePDFExtractor:
    def __init__(self, pages=None):
        self.pages = pages or ({"page": 3, "text": OFFICIAL_TEXT},)

    def extract(self, body):
        if not body.startswith(b"%PDF"):
            raise ValueError("not pdf")
        return tuple(self.pages), {"title": "Commerce notification", "author": "Department of Commerce"}


class CountingProvider:
    name = "counting"
    mode = "live"
    model = "counting-v1"

    def __init__(self):
        self.calls = 0

    def find_corroboration(self, *args, **kwargs):
        self.calls += 1
        return VerificationProviderResult(result={"search_summary": "fixture", "unresolved_gaps": [], "leads": []})


class DeferredExecutor:
    def submit(self, *args, **kwargs):
        return None


class SourceAcquisitionUnitTests(unittest.TestCase):
    def setUp(self):
        self.registry = OfficialSourceRegistry.from_file("config/official_sources.json")
        self.claims = [
            {"claim_id": "CL-A", "text": TOBACCO_CLAIM_A},
            {"claim_id": "CL-B", "text": TOBACCO_CLAIM_B},
        ]

    def test_official_registry_is_configurable_and_prioritized(self):
        self.assertGreaterEqual(len(self.registry.enabled()), 5)
        tobacco = self.registry.match("https://www.tobaccoboard.commerce.gov.in/circular.pdf")
        self.assertEqual(tobacco.id, "tobacco-board-india")
        self.assertIn("PDF", tobacco.document_types)
        self.assertIsNone(self.registry.match("https://reporter.example/story"))

    def test_targeted_plan_contains_all_six_strategies(self):
        source = "The report cited a Commerce Ministry notification and a Tobacco Board auction notice."
        plan = build_discovery_plan("Andhra Pradesh excess FCV tobacco", self.claims, source, self.registry)
        self.assertEqual({item.strategy for item in plan}, {
            "A_AUTHORITATIVE_DOMAIN", "B_EXACT_PHRASE", "C_TITLE_NOTIFICATION_FRAGMENT",
            "D_ENTITY_DATE_RANGE", "E_SECONDARY_CORROBORATION", "F_DIRECT_DOCUMENT_LINK",
        })
        official = next(item for item in plan if item.strategy == "A_AUTHORITATIVE_DOMAIN")
        self.assertIn("tobaccoboard.commerce.gov.in", official.domains)
        self.assertIn("2025-26", official.query)

    def test_reference_hint_and_exact_phrase_strategies(self):
        text = "Officials cited Office Memorandum 14/2026. The ministry notification covers growers."
        hints = extract_search_hints(text, "https://reporter.example/story")
        self.assertEqual(len(hints), 2)
        plan = build_discovery_plan("FCV sale", self.claims[:1], text, self.registry)
        exact = next(item for item in plan if item.strategy == "B_EXACT_PHRASE")
        fragment = next(item for item in plan if item.strategy == "C_TITLE_NOTIFICATION_FRAGMENT")
        secondary = next(item for item in plan if item.strategy == "E_SECONDARY_CORROBORATION")
        self.assertTrue(exact.query.startswith('"'))
        self.assertIn("Office Memorandum", fragment.query)
        self.assertIn("FCV sale", secondary.query)

    def test_provider_abstraction_survives_one_empty_provider(self):
        plan = build_discovery_plan("FCV", self.claims[:1], "notification", self.registry)
        empty = DeterministicDiscoveryProvider()
        fixture = DeterministicDiscoveryProvider({"E_SECONDARY_CORROBORATION": [{"url": "https://reporter.example/a"}]})
        result = CompositeDiscoveryProvider([empty, fixture]).search(plan)
        self.assertEqual([lead.url for lead in result.leads], ["https://reporter.example/a"])
        self.assertEqual(result.cost_usd, 0.0)

    def test_official_text_directly_supports_claim_semantics(self):
        result = app.classify_claim_evidence(TOBACCO_CLAIM_B, GAZETTE_PASSAGE)
        self.assertEqual(result["classification"], "DIRECT_SUPPORT")
        self.assertEqual(result["missing_facets"], [])

    def test_official_text_partially_supports_claim_when_condition_is_missing(self):
        passage = "The Central Government permitted the sale of excess FCV tobacco in Andhra Pradesh."
        result = app.classify_claim_evidence(TOBACCO_CLAIM_A, passage)
        self.assertEqual(result["classification"], "PARTIAL_SUPPORT")
        self.assertIn("crop season/date", result["missing_facets"])

    def test_official_text_that_only_mentions_subject_does_not_support_action(self):
        passage = "The Central Government reviewed excess FCV tobacco in Andhra Pradesh during 2025-2026."
        result = app.classify_claim_evidence(TOBACCO_CLAIM_A, passage)
        self.assertEqual(result["classification"], "MENTIONS_ONLY")

    def test_html_retrieval_preserves_redirect_metadata_and_direct_pdf(self):
        html = """<html><head><title>FCV order</title><meta name="date" content="2026-09-30">
        <link rel="canonical" href="https://commerce.gov.in/final"></head><body>
        <article><p>Union government permitted excess FCV tobacco sale in Andhra Pradesh.</p>
        <a href="/files/order.pdf">Notification PDF</a></article></body></html>"""
        retriever = SourceRetriever(lambda url: response(url, html, final_url="https://commerce.gov.in/final"))
        document = retriever.retrieve("https://commerce.gov.in/start")
        self.assertEqual(document.final_url, "https://commerce.gov.in/final")
        self.assertEqual(document.metadata["canonical_url"], "https://commerce.gov.in/final")
        self.assertEqual(document.direct_document_urls, ("https://commerce.gov.in/files/order.pdf",))
        self.assertEqual(document.checksum, hashlib.sha256(html.encode()).hexdigest())

    def test_inaccessible_and_unsupported_urls_fail_without_evidence(self):
        bad_status = SourceRetriever(lambda url: response(url, b"", status=503))
        with self.assertRaisesRegex(ValueError, "HTTP 503"):
            bad_status.retrieve("https://commerce.gov.in/down")
        unsupported = SourceRetriever(lambda url: response(url, b"zip", "application/zip"))
        with self.assertRaisesRegex(ValueError, "unsupported evidence content type"):
            unsupported.retrieve("https://commerce.gov.in/archive.zip")

    def test_pdf_extraction_is_page_addressable(self):
        retriever = SourceRetriever(
            lambda url: response(url, b"%PDF-fixture", "application/pdf", "https://commerce.gov.in/order.pdf"),
            pdf_extractor=FakePDFExtractor(),
        )
        document = retriever.retrieve("https://commerce.gov.in/order.pdf")
        self.assertEqual(document.document_type, "PDF")
        self.assertEqual(document.pages[0]["page"], 3)
        self.assertIn("[Page 3]", document.text)
        self.assertEqual(document.metadata["author"], "Department of Commerce")

    def test_unreadable_pdf_is_rejected(self):
        extractor = FakePDFExtractor(pages=())
        extractor.extract = lambda body: (_ for _ in ()).throw(ValueError("PDF is unreadable; no extracted text is available"))
        retriever = SourceRetriever(lambda url: response(url, b"%PDF-empty", "application/pdf"), pdf_extractor=extractor)
        with self.assertRaisesRegex(ValueError, "unreadable"):
            retriever.retrieve("https://commerce.gov.in/empty.pdf")

    def test_source_quality_classification(self):
        official = ExtractedDocument("u", "https://commerce.gov.in/n", 200, "text/html", "t", None,
                                     "commerce.gov.in", "now", "hash", OFFICIAL_TEXT, "HTML", {}, (), ())
        independent = ExtractedDocument("u", "https://reporter.example/n", 200, "text/html", "t", None,
                                        "reporter.example", "now", "hash", OFFICIAL_TEXT, "HTML", {}, (), ())
        self.assertEqual(classify_source(official, self.registry)[0], "OFFICIAL_PRIMARY")
        self.assertEqual(classify_source(independent, self.registry, ("reporter.example",))[0], "INDEPENDENT_REPORTING")

    def test_syndicated_copy_stays_in_one_family(self):
        first = candidate("A", "https://wire-one.example/a", "SYNDICATED_REPORTING", OFFICIAL_TEXT, "EF-WIRE")
        second = candidate("B", "https://wire-two.example/b", "SYNDICATED_REPORTING", OFFICIAL_TEXT)
        family, reason, relationship, score = family_for_candidate(second, [first])
        self.assertEqual(family, "EF-WIRE")
        self.assertEqual(relationship, "SAME_FAMILY")
        self.assertIn("overlap", reason)
        self.assertGreaterEqual(score, 0.72)

    def test_claim_candidate_matrix_and_packet_scenarios_a_to_f(self):
        official = candidate("OFF", "https://commerce.gov.in/order", "OFFICIAL_PRIMARY", OFFICIAL_TEXT, "EF-OFF")
        report1 = candidate("R1", "https://one.example/story", "INDEPENDENT_REPORTING", OFFICIAL_TEXT, "EF-R1")
        report2 = candidate("R2", "https://two.example/story", "INDEPENDENT_REPORTING", OFFICIAL_TEXT, "EF-R2")
        syndication = candidate("R3", "https://mirror.example/story", "SYNDICATED_REPORTING", OFFICIAL_TEXT, "EF-R1")
        absent = candidate("ABS", "https://commerce.gov.in/other", "OFFICIAL_PRIMARY", "A board meeting discussed crop quality.", "EF-ABS")
        pdf = candidate("PDF", "https://commerce.gov.in/order.pdf", "OFFICIAL_PRIMARY", OFFICIAL_TEXT, "EF-PDF",
                        ({"page": 7, "text": OFFICIAL_TEXT},))

        official_rows = match_claims(self.claims[:1], official)
        packet_a = build_evidence_packet(self.claims[0], official_rows, [official])
        self.assertTrue(packet_a["deterministically_sufficient"])

        reports = [report1, report2]
        report_rows = sum((match_claims(self.claims[:1], item) for item in reports), [])
        packet_b = build_evidence_packet(self.claims[0], report_rows, reports)
        self.assertEqual(packet_b["independent_family_count"], 2)
        self.assertTrue(packet_b["deterministically_sufficient"])

        syndicated_rows = sum((match_claims(self.claims[:1], item) for item in [report1, syndication]), [])
        packet_c = build_evidence_packet(self.claims[0], syndicated_rows, [report1, syndication])
        self.assertEqual(packet_c["independent_family_count"], 1)
        self.assertFalse(packet_c["deterministically_sufficient"])

        absent_rows = match_claims(self.claims[:1], absent)
        packet_d = build_evidence_packet(self.claims[0], absent_rows, [absent])
        self.assertFalse(packet_d["official_primary_found"])

        pdf_rows = match_claims(self.claims[:1], pdf)
        packet_e = build_evidence_packet(self.claims[0], pdf_rows, [pdf])
        self.assertEqual(packet_e["candidates"][0]["page_number"], 7)

        packet_f = build_evidence_packet(self.claims[0], [], [])
        self.assertFalse(packet_f["deterministically_sufficient"])
        self.assertEqual(packet_f["candidates"], [])

    def test_migration_016_is_additive_and_forward_only(self):
        sql = Path("migrations/016_source_acquisition.sql").read_text().upper()
        for forbidden in ("DROP TABLE", "DELETE FROM", "ALTER TABLE", "UPDATE EVENTS", "UPDATE CLAIMS"):
            self.assertNotIn(forbidden, sql)


class SourceAcquisitionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = app.DB
        app.DB = Path(self.temp.name) / "test.sqlite3"
        app.init()

    def tearDown(self):
        app.DB = self.original_db
        self.temp.cleanup()

    def verification_fixture(self, source_class="independent_reporting"):
        signal = app.ingest_signal(
            url="https://reporter.example/tobacco", title="Excess FCV tobacco sale in Andhra Pradesh",
            text=OFFICIAL_TEXT, source_name="The New Indian Express", source_class=source_class,
            publication_time="2026-09-30T08:00:00Z", content_role="item", item_type="news",
        )
        research = app.enqueue_research(signal["event_id"], "test", background=False)["run"]
        verification = app.enqueue_verification(research["id"], "test", background=False)["run"]
        return signal, research, verification

    @staticmethod
    def html_transport(text=OFFICIAL_TEXT, *, url="https://commerce.gov.in/order", link=None):
        anchor = f'<a href="{link}">PDF</a>' if link else ""
        body = f"<html><head><title>Commerce notification</title></head><body><article><p>{text}</p>{anchor}</article></body></html>"
        return lambda requested: response(requested, body, final_url=url)

    def test_manual_evidence_url_persists_packet_without_auto_approval(self):
        signal, _, verification = self.verification_fixture()
        before = app.event_room(signal["event_id"])
        before_status = before["event"]["verification_status"]
        before_decisions = [(item["id"], item["decision"]) for item in before["verification_runs"][0]["decisions"]]
        with patch.object(app, "_validate_public_url", lambda value: None):
            result = app.add_evidence_url(
                verification["id"], "https://commerce.gov.in/order", transport=self.html_transport(),
            )
        self.assertFalse(result["adjudication_changed"])
        self.assertEqual(result["candidates"][0]["source_class"], "OFFICIAL_PRIMARY")
        self.assertTrue(any(packet["deterministically_sufficient"] for packet in result["packets"]))
        after = app.event_room(signal["event_id"])
        self.assertEqual(after["event"]["verification_status"], before_status)
        self.assertEqual([(item["id"], item["decision"]) for item in after["verification_runs"][0]["decisions"]], before_decisions)
        self.assertEqual(len(after["source_acquisition_runs"][0]["candidates"]), 1)

    def test_manual_direct_document_pdf_is_retrieved_and_page_stored(self):
        _, _, verification = self.verification_fixture()
        html = self.html_transport(link="https://commerce.gov.in/files/order.pdf")

        def transport(url):
            if url.endswith(".pdf"):
                return response(url, b"%PDF-fixture", "application/pdf")
            return html(url)

        with patch.object(app, "_validate_public_url", lambda value: None):
            result = app.add_evidence_url(
                verification["id"], "https://commerce.gov.in/order", transport=transport,
                pdf_extractor=FakePDFExtractor(),
            )
        self.assertEqual(len(result["candidates"]), 2)
        pdf = next(item for item in result["candidates"] if item["document_type"] == "PDF")
        with app.connect() as connection:
            page = connection.execute("SELECT * FROM source_candidate_pages WHERE candidate_id=?", (pdf["id"],)).fetchone()
        self.assertEqual(page["page_number"], 3)

    def test_planned_discovery_urls_execute_as_one_acquisition_pass(self):
        _, _, verification = self.verification_fixture()
        with app.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM source_acquisition_runs WHERE verification_run_id=? "
                "AND trigger_kind='PLANNED_DISCOVERY'", (verification["id"],),
            ).fetchone()

        def transport(url):
            return response(url, f"<html><article><p>{OFFICIAL_TEXT}</p></article></html>")

        leads = [
            {"url": "https://commerce.gov.in/order", "strategy": "A_AUTHORITATIVE_DOMAIN"},
            {"url": "https://pib.gov.in/release", "strategy": "E_SECONDARY_CORROBORATION"},
        ]
        with patch.object(app, "_validate_public_url", lambda value: None):
            result = app.run_source_acquisition_pass(
                verification["id"], leads, acquisition_run_id=plan["id"],
                provider="test-search", search_provider_calls=1, search_cost_status="not_billed",
                transport=transport,
            )
        self.assertEqual(result["run"]["id"], plan["id"])
        self.assertEqual(result["run"]["status"], "COMPLETED")
        self.assertEqual(result["run"]["direct_http_retrievals"], 2)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(all(item["source_class"] == "OFFICIAL_PRIMARY" for item in result["candidates"]))
        with app.connect() as connection:
            live_runs = connection.execute(
                "SELECT COUNT(*) FROM source_acquisition_runs WHERE verification_run_id=?",
                (verification["id"],),
            ).fetchone()[0]
        self.assertEqual(live_runs, 1)

    def test_unrelated_unreadable_candidate_does_not_poison_sufficient_evidence(self):
        _, _, verification = self.verification_fixture()
        with app.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM source_acquisition_runs WHERE verification_run_id=? "
                "AND trigger_kind='PLANNED_DISCOVERY'", (verification["id"],),
            ).fetchone()

        def transport(url):
            if url.endswith("unrelated.pdf"):
                raise ValueError("unreadable unrelated candidate")
            return response(url, f"<html><article><p>{OFFICIAL_TEXT}</p></article></html>")

        leads = [
            {"url": "https://commerce.gov.in/order", "strategy": "A_AUTHORITATIVE_DOMAIN"},
            {"url": "https://commerce.gov.in/unrelated.pdf", "strategy": "F_DIRECT_DOCUMENT_LINK"},
        ]
        with patch.object(app, "_validate_public_url", lambda value: None):
            result = app.run_source_acquisition_pass(
                verification["id"], leads, acquisition_run_id=plan["id"],
                provider="test-search", search_provider_calls=1, search_cost_status="not_billed",
                transport=transport,
            )
        self.assertEqual(result["run"]["status"], "COMPLETED")
        self.assertTrue(any(item["state"] == "UNAVAILABLE" for item in result["candidates"]))
        self.assertTrue(all(packet["deterministically_sufficient"] for packet in result["packets"][:2]))

    def test_inaccessible_manual_url_is_audited_and_remains_review_required(self):
        signal, _, verification = self.verification_fixture()
        with patch.object(app, "_validate_public_url", lambda value: None):
            result = app.add_evidence_url(
                verification["id"], "https://commerce.gov.in/down",
                transport=lambda url: response(url, b"", status=503),
            )
        self.assertEqual(result["run"]["status"], "FAILED")
        self.assertEqual(result["candidates"][0]["state"], "UNAVAILABLE")
        room = app.event_room(signal["event_id"])
        self.assertIn(room["event"]["verification_status"], ("TEST_ONLY", "REVIEW_REQUIRED"))
        self.assertEqual(room["production_jobs"], [])
        self.assertEqual(room["publishing_history"], [])

    def test_acquisition_cost_channels_never_turn_unknown_into_zero(self):
        _, _, verification = self.verification_fixture()
        with app.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM source_acquisition_runs WHERE verification_run_id=? AND trigger_kind='PLANNED_DISCOVERY'",
                (verification["id"],),
            ).fetchone()
        self.assertEqual(plan["search_cost_status"], "unknown")
        self.assertIsNone(plan["search_cost_usd"])
        self.assertEqual(plan["retrieval_cost_status"], "not_billed")
        self.assertEqual(plan["llm_adjudication_calls"], 0)

    def test_discovery_plan_is_persisted_before_any_provider_call(self):
        _, _, verification = self.verification_fixture()
        with app.connect() as connection:
            strategies = {row["strategy"] for row in connection.execute(
                "SELECT sda.strategy FROM source_discovery_attempts sda "
                "JOIN source_acquisition_runs sar ON sar.id=sda.acquisition_run_id "
                "WHERE sar.verification_run_id=? AND sar.trigger_kind='PLANNED_DISCOVERY'",
                (verification["id"],),
            )}
        self.assertEqual(strategies, {
            "A_AUTHORITATIVE_DOMAIN", "B_EXACT_PHRASE", "C_TITLE_NOTIFICATION_FRAGMENT", "D_ENTITY_DATE_RANGE",
            "E_SECONDARY_CORROBORATION", "F_DIRECT_DOCUMENT_LINK",
        })

    def test_fresh_verification_skips_provider_search_when_official_candidate_is_sufficient(self):
        signal, research, verification = self.verification_fixture()
        with patch.object(app, "_validate_public_url", lambda value: None):
            app.add_evidence_url(verification["id"], "https://commerce.gov.in/order", transport=self.html_transport())
        with app.connect() as connection:
            connection.execute("UPDATE research_runs SET mode='live' WHERE id=?", (research["id"],))
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_verification(research["id"], "grok", background=True)
        newest = queued["run"]
        provider = CountingProvider()
        app.run_verification_job(newest["id"], provider)
        with app.connect() as connection:
            checkpoint = connection.execute(
                "SELECT payload_json FROM verification_checkpoints WHERE verification_run_id=? "
                "AND phase='CORROBORATION_DISCOVERY' AND status='COMPLETED'", (newest["id"],),
            ).fetchone()
        payload = json.loads(checkpoint["payload_json"])
        self.assertTrue(payload.get("provider_search_skipped"))
        self.assertEqual(provider.calls, 0)

    def test_exhausted_live_discovery_remains_review_required_with_no_downstream_work(self):
        signal = app.ingest_signal(
            url="https://reporter.example/tobacco-unresolved", title="Excess FCV tobacco sale in Andhra Pradesh",
            text=OFFICIAL_TEXT, source_name="The New Indian Express", source_class="independent_reporting",
            publication_time="2026-09-30T08:00:00Z", content_role="item", item_type="news",
        )
        research = app.enqueue_research(signal["event_id"], "test", background=False)["run"]
        with app.connect() as connection:
            connection.execute("UPDATE research_runs SET mode='live' WHERE id=?", (research["id"],))
        with patch.object(app, "VERIFICATION_EXECUTOR", DeferredExecutor()):
            queued = app.enqueue_verification(research["id"], "grok", background=True)["run"]
        provider = CountingProvider()
        app.run_verification_job(queued["id"], provider)
        room = app.event_room(signal["event_id"])
        self.assertEqual(room["event"]["verification_status"], "REVIEW_REQUIRED")
        self.assertEqual(room["verification_runs"][0]["summary"]["claim_set_status"], "REVIEW_REQUIRED")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(room["production_jobs"], [])
        self.assertEqual(room["render_jobs"], [])
        self.assertEqual(room["publishing_history"], [])

    def test_ui_exposes_discovery_and_manual_url_without_publish_action(self):
        # Architecture 07 simplified the UI; evidence discovery and manual evidence URLs remain
        # as backend endpoints, and the shell still states that publishing does not exist.
        html = Path("index.html").read_text(encoding="utf-8")
        javascript = Path("app.js").read_text(encoding="utf-8")
        self.assertIn("No publishing actions exist", html)
        self.assertIn("/api/events/", javascript)
        self.assertNotIn("Publish now", javascript)


if __name__ == "__main__":
    unittest.main()
