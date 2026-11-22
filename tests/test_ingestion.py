"""Tests for envelope parsing and continuous ingestion."""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from clinical_workbench import ValidationError, Workbench, load_document, parse_sources


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "sources.json"
SCENARIOS = ROOT / "fixtures" / "scenarios"


def source(source_id, kind, collected_at, **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


class ParseEnvelopeTests(unittest.TestCase):
    def test_fixture_parses_cleanly(self):
        outcome = parse_sources(load_document(FIXTURE))
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.records), 5)

    def test_missing_envelope_field_is_rejected(self):
        outcome = parse_sources(
            {
                "sources": [
                    {"source_id": "x", "record_kind": "study_scope", "payload": {}}
                ]
            }
        )
        self.assertFalse(outcome.ok)
        self.assertIn("collected_at", outcome.rejected[0]["error"])

    def test_missing_payload_field_is_rejected(self):
        outcome = parse_sources(
            {
                "sources": [
                    source(
                        "s1",
                        "study_scope",
                        "2026-06-01T00:00:00Z",
                        study_id="ST",
                        revision=1,
                        consent_scope="x",
                        valid_from="2026-01-01",
                    )
                ]
            }
        )
        self.assertFalse(outcome.ok)
        self.assertIn("valid_to", outcome.rejected[0]["error"])

    def test_unknown_kind_is_retained_but_flagged(self):
        outcome = parse_sources(
            {
                "sources": [
                    source("s1", "mystery_feed", "2026-06-01T00:00:00Z", foo="bar")
                ]
            }
        )
        self.assertTrue(outcome.ok)
        record = outcome.records[0]
        self.assertFalse(record.known_kind)
        self.assertTrue(any("unrecognised" in w for w in record.warnings))

    def test_bad_timestamp_rejected(self):
        outcome = parse_sources(
            {"sources": [source("s1", "unknown", "not-a-date", foo=1)]}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("ISO-8601", outcome.rejected[0]["error"])

    def test_inverted_consent_window_rejected(self):
        outcome = parse_sources(
            {
                "sources": [
                    source(
                        "s1",
                        "study_scope",
                        "2026-06-01T00:00:00Z",
                        study_id="ST",
                        revision=1,
                        consent_scope="x",
                        valid_from="2027-01-01",
                        valid_to="2026-01-01",
                    )
                ]
            }
        )
        self.assertFalse(outcome.ok)
        self.assertIn("precedes", outcome.rejected[0]["error"])

    def test_ordering_is_by_collected_at_not_file_order(self):
        late = source(
            "late",
            "study_scope",
            "2026-07-01T00:00:00Z",
            study_id="ST",
            revision=1,
            consent_scope="x",
            valid_from="2026-01-01",
            valid_to="2027-01-01",
        )
        early = source(
            "early",
            "study_scope",
            "2026-06-01T00:00:00Z",
            study_id="ST",
            revision=1,
            consent_scope="x",
            valid_from="2026-01-01",
            valid_to="2027-01-01",
        )
        outcome = parse_sources({"sources": [late, early]})
        self.assertEqual([r.source_id for r in outcome.records], ["early", "late"])

    def test_timezone_offsets_normalised(self):
        outcome = parse_sources(
            {
                "sources": [
                    source("a", "unknown", "2026-06-01T09:00:00+08:00", x=1),
                    source("b", "unknown", "2026-06-01T01:00:00Z", x=1),
                ]
            }
        )
        first, second = outcome.records
        self.assertEqual(first.collected_at, second.collected_at)
        self.assertEqual(first.collected_at.tzinfo, timezone.utc)

    def test_identical_redelivery_is_collapsed_and_reported(self):
        raw = source("dup", "unknown", "2026-06-01T00:00:00Z", x=1)
        outcome = parse_sources({"sources": [raw, dict(raw)]})
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(len(outcome.duplicates), 1)

    def test_resubmission_with_new_content_keeps_latest(self):
        first = source("dup", "unknown", "2026-06-01T00:00:00Z", x=1)
        second = source("dup", "unknown", "2026-06-02T00:00:00Z", x=2)
        outcome = parse_sources({"sources": [first, second]})
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.records[0].payload["x"], 2)
        self.assertTrue(any("resubmitted" in w for w in outcome.records[0].warnings))


class IngestionTests(unittest.TestCase):
    def test_batches_accumulate_without_losing_prior_sources(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"), label="one")
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"), label="two")
        snapshot = workbench.snapshot()
        self.assertEqual(len(snapshot.batches), 2)
        self.assertEqual([b.batch_id for b in snapshot.batches], ["batch-001", "batch-002"])
        ids = {record.source_id for record in snapshot.records}
        self.assertIn("submission-wd", ids)
        self.assertIn("withdrawal-wd", ids)

    def test_rejected_entries_do_not_break_later_batches(self):
        workbench = Workbench()
        bad = {"sources": [{"source_id": "bad"}]}
        batch = workbench.ingest(bad, label="bad")
        self.assertEqual(batch.rejected, 1)
        workbench.ingest(load_document(SCENARIOS / "complete-chain.json"), label="good")
        snapshot = workbench.snapshot()
        self.assertGreaterEqual(len(snapshot.records), 5)
        self.assertEqual(len(snapshot.rejected_entries), 1)

    def test_source_history_records_each_version(self):
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
                    source("scope-x", "unknown", "2026-06-01T00:00:00Z", v=1),
                ]
            }
        )
        workbench.ingest(
            {
                "sources": [
                    source("scope-x", "unknown", "2026-06-05T00:00:00Z", v=2),
                ]
            }
        )
        snapshot = workbench.snapshot()
        history = snapshot.source_history["scope-x"]
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0]["fingerprint"], history[1]["fingerprint"])

    def test_from_documents_helper(self):
        workbench = Workbench.from_documents(
            [load_document(SCENARIOS / "complete-chain.json")]
        )
        self.assertEqual(len(workbench.snapshot().records), 5)

    def test_document_must_contain_sources(self):
        with self.assertRaises(ValidationError):
            Workbench().ingest({"not_sources": []})

    def test_unknown_envelope_keys_warn(self):
        outcome = parse_sources(
            {
                "sources": [
                    {
                        **source("s1", "unknown", "2026-06-01T00:00:00Z", x=1),
                        "extra_field": "ignored",
                    }
                ]
            }
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(any("unknown envelope" in w for w in outcome.records[0].warnings))


if __name__ == "__main__":
    unittest.main()
