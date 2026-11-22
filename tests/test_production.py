"""Tests for production lot traceability and evidence versions."""

import unittest
from pathlib import Path

from clinical_workbench import Workbench, load_document, production_view


ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "fixtures" / "scenarios"


def src(source_id, kind, collected_at, **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


def base_sources(lot_ref="LOT-1", device_ref="DEV-1", events=0):
    return [
        src(
            "scope-1",
            "study_scope",
            "2026-01-01T00:00:00Z",
            study_id="ST",
            revision=1,
            consent_scope="device-follow-up",
            valid_from="2026-01-01",
            valid_to="2027-12-31",
        ),
        src(
            f"lot-{lot_ref}",
            "production_lot",
            "2026-05-01T00:00:00Z",
            lot_ref=lot_ref,
            device_ref=device_ref,
            line_ref="LINE-1",
            state="released",
            revision=1,
        ),
        src(
            "sub-1",
            "site_submission",
            "2026-07-01T00:00:00Z",
            study_id="ST",
            submission_id="SUB-1",
            revision=1,
            period="2026-06",
            participant_ref="SUBJECT-1",
            consent_revision=1,
        ),
        src(
            "dev-1",
            "device_use",
            "2026-07-01T12:00:00Z",
            study_id="ST",
            submission_id="SUB-1",
            device_ref=device_ref,
            lot_ref=lot_ref,
            used_at="2026-06-15",
        ),
        src(
            "fu-1",
            "site_follow_up",
            "2026-07-15T00:00:00Z",
            study_id="ST",
            submission_id="SUB-1",
            window="2026-06",
            events_observed=events,
            data_complete=True,
        ),
    ]


def build(**kwargs):
    workbench = Workbench()
    workbench.ingest({"sources": base_sources(**kwargs)})
    return workbench


class TraceabilityTests(unittest.TestCase):
    def test_complete_chain_is_traced_to_the_lot(self):
        snap = build().snapshot()
        view = production_view(snap)
        self.assertEqual(len(view["lots"]), 1)
        trace = view["lots"][0]
        self.assertEqual(trace["evidence_status"], "complete")
        self.assertTrue(trace["all_evidence_complete"])
        self.assertEqual(trace["evidence"][0]["submission_id"], "SUB-1")
        self.assertEqual(
            trace["evidence"][0]["evidence_version"],
            snap.core.research["SUB-1"].evidence_version,
        )

    def test_evidence_version_matches_the_research_view(self):
        workbench = build()
        snap = workbench.snapshot()
        research = {
            record["submission_id"]: record
            for record in __import__("clinical_workbench").research_view(snap)["records"]
        }
        production = production_view(snap)
        for trace in production["lots"]:
            for item in trace["evidence"]:
                self.assertEqual(
                    item["evidence_version"],
                    research[item["submission_id"]]["evidence_version"],
                )

    def test_lot_with_no_linked_evidence_is_untracked_not_incomplete(self):
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
                    src(
                        "lot-orphan",
                        "production_lot",
                        "2026-05-01T00:00:00Z",
                        lot_ref="LOT-ORPHAN",
                        device_ref="DEV-ORPHAN",
                        line_ref="LINE-1",
                        state="released",
                        revision=1,
                    )
                ]
            }
        )
        view = production_view(workbench.snapshot())
        self.assertEqual(view["lots"][0]["evidence_status"], "untracked")
        self.assertEqual(view["totals"]["untracked_lots"], 1)
        self.assertEqual(view["totals"]["incomplete_lots"], 0)

    def test_incomplete_evidence_is_flagged_with_reasons(self):
        workbench = Workbench()
        sources = base_sources()
        sources = [s for s in sources if s["source_id"] != "fu-1"]
        workbench.ingest({"sources": sources})
        view = production_view(workbench.snapshot())
        trace = view["lots"][0]
        self.assertEqual(trace["evidence_status"], "incomplete")
        self.assertIn("follow_up", trace["incomplete_reasons"])


class NoFuzzyMatchingTests(unittest.TestCase):
    def test_similar_product_name_is_never_auto_merged(self):
        # DEV-1 belongs to LOT-1; DEV-1-SIMILAR belongs to LOT-SIMILAR. A
        # device_use that cites DEV-1 with LOT-SIMILAR must not be treated as
        # traceable, even though the names are near-identical.
        workbench = Workbench()
        workbench.ingest({"sources": base_sources()})
        workbench.ingest(
            {
                "sources": [
                    src(
                        "lot-similar",
                        "production_lot",
                        "2026-05-01T00:00:00Z",
                        lot_ref="LOT-SIMILAR",
                        device_ref="DEV-1-SIMILAR",
                        line_ref="LINE-2",
                        state="released",
                        revision=1,
                    )
                ]
            }
        )
        view = production_view(workbench.snapshot())
        lots = {trace["lot"]["effective_lot_ref"]: trace for trace in view["lots"]}
        self.assertIn("LOT-1", lots)
        self.assertIn("LOT-SIMILAR", lots)
        self.assertEqual(lots["LOT-1"]["evidence"][0]["submission_id"], "SUB-1")
        # The similar lot has no evidence of its own.
        self.assertEqual(lots["LOT-SIMILAR"]["evidence_status"], "untracked")

    def test_mismatched_device_ref_on_same_lot_is_not_provable(self):
        workbench = Workbench()
        sources = base_sources()
        for source in sources:
            if source["source_id"] == "dev-1":
                source["payload"]["device_ref"] = "DEV-OTHER"
        workbench.ingest({"sources": sources})
        snap = workbench.snapshot()
        record = snap.core.research["SUB-1"]
        self.assertFalse(record.traceable)
        self.assertEqual(record.device_links[0].issues, ["device_ref_mismatch"])

    def test_missing_lot_is_not_provable(self):
        workbench = Workbench()
        sources = base_sources()
        sources = [s for s in sources if s["source_id"] != "lot-LOT-1"]
        workbench.ingest({"sources": sources})
        record = workbench.snapshot().core.research["SUB-1"]
        self.assertFalse(record.traceable)
        self.assertIn("lot_not_found", record.device_links[0].issues)


class LotCorrectionTests(unittest.TestCase):
    def test_renaming_a_lot_detaches_old_links(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-1.json"))
        before = workbench.snapshot()
        self.assertEqual(before.core.research["SUB-LOT-1"].conclusion["status"], "no_events_observed")

        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-2.json"))
        snap = workbench.snapshot()
        record = snap.core.research["SUB-LOT-1"]
        self.assertFalse(record.traceable)
        self.assertEqual(record.conclusion["status"], "insufficient")
        view = production_view(snap)
        renamed = next(
            trace for trace in view["lots"] if trace["lot"]["effective_lot_ref"] == "LOT-A-RENAMED"
        )
        self.assertEqual(renamed["lot"]["previous_lot_refs"], ["LOT-A"])
        self.assertEqual(renamed["evidence_status"], "incomplete")
        self.assertIn("association_not_provable", renamed["incomplete_reasons"])
        self.assertEqual(len(renamed["lot"]["detached_links"]), 1)

    def test_similar_lot_is_not_substituted_after_correction(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-1.json"))
        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-2.json"))
        view = production_view(workbench.snapshot())
        similar = next(
            trace for trace in view["lots"] if trace["lot"]["effective_lot_ref"] == "LOT-A-SIMILAR"
        )
        self.assertEqual(similar["evidence"], [])
        self.assertEqual(similar["evidence_status"], "untracked")

    def test_lot_correction_preserves_prior_resolution(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-1.json"))
        original = workbench.snapshot().lot_traces["LOT-A"].evidence_version
        workbench.ingest(load_document(SCENARIOS / "lot-chain-batch-2.json"))
        frozen = workbench.snapshot(cutoff="2026-08-01T00:00:00Z")
        self.assertIn("LOT-A", frozen.lot_traces)
        self.assertEqual(frozen.lot_traces["LOT-A"].evidence_version, original)
        self.assertEqual(frozen.lot_traces["LOT-A"].evidence_status, "complete")

    def test_lot_withdrawal_marks_evidence_withdrawn(self):
        workbench = Workbench()
        workbench.ingest({"sources": base_sources()})
        workbench.ingest(
            {
                "sources": [
                    src(
                        "wd-lot",
                        "source_withdrawal",
                        "2026-08-01T00:00:00Z",
                        target_source_id="lot-LOT-1",
                        reason_code="label-recall",
                    )
                ]
            }
        )
        snap = workbench.snapshot()
        view = production_view(snap)
        trace = view["lots"][0]
        self.assertEqual(trace["evidence_status"], "withdrawn")
        self.assertEqual(trace["incomplete_reasons"], ["lot_withdrawn"])
        # The affected submission remains visible so production can act on it.
        self.assertEqual([item["submission_id"] for item in trace["evidence"]], ["SUB-1"])
        self.assertEqual(trace["evidence"][0]["reasons"], ["lot_withdrawn"])
        self.assertEqual(
            snap.core.research["SUB-1"].conclusion["status"], "insufficient"
        )

    def test_multiple_submissions_share_one_lot_with_distinct_versions(self):
        workbench = Workbench()
        sources = base_sources() + [
            src(
                "sub-2",
                "site_submission",
                "2026-07-01T00:00:00Z",
                study_id="ST",
                submission_id="SUB-2",
                revision=1,
                period="2026-06",
                participant_ref="SUBJECT-2",
                consent_revision=1,
            ),
            src(
                "dev-2",
                "device_use",
                "2026-07-01T12:00:00Z",
                study_id="ST",
                submission_id="SUB-2",
                device_ref="DEV-1",
                lot_ref="LOT-1",
                used_at="2026-06-16",
            ),
            src(
                "fu-2",
                "site_follow_up",
                "2026-07-15T00:00:00Z",
                study_id="ST",
                submission_id="SUB-2",
                window="2026-06",
                events_observed=2,
                data_complete=True,
            ),
        ]
        workbench.ingest({"sources": sources})
        view = production_view(workbench.snapshot())
        trace = view["lots"][0]
        self.assertEqual(trace["evidence_status"], "complete")
        self.assertEqual(
            sorted(item["submission_id"] for item in trace["evidence"]),
            ["SUB-1", "SUB-2"],
        )
        versions = {item["evidence_version"] for item in trace["evidence"]}
        self.assertEqual(len(versions), 2)


if __name__ == "__main__":
    unittest.main()
