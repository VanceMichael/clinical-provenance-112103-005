"""End-to-end lifecycle: opening, publication, correction, withdrawal.

This exercises the requirement that ingestion, research judgement and
production traceability stay consistent across one real source change.
"""

import unittest

from clinical_workbench import (
    Workbench,
    production_view,
    research_view,
    summary_view,
)


def src(source_id, kind, collected_at, **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


OPENING = {
    "sources": [
        src(
            "scope-1",
            "study_scope",
            "2026-06-01T00:00:00Z",
            study_id="ST",
            revision=1,
            consent_scope="device-follow-up",
            valid_from="2026-06-01",
            valid_to="2027-05-31",
        ),
        src(
            "lot-1",
            "production_lot",
            "2026-06-02T00:00:00Z",
            lot_ref="LOT-1",
            device_ref="DEV-1",
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
            consent_revision=1,
            participant_ref="P-1",
        ),
        src(
            "dev-1",
            "device_use",
            "2026-07-01T01:00:00Z",
            study_id="ST",
            submission_id="SUB-1",
            device_ref="DEV-1",
            lot_ref="LOT-1",
            used_at="2026-06-10",
        ),
        src(
            "fu-1",
            "site_follow_up",
            "2026-07-15T00:00:00Z",
            study_id="ST",
            submission_id="SUB-1",
            window="2026-06",
            events_observed=0,
            data_complete=True,
        ),
        src(
            "sub-2",
            "site_submission",
            "2026-07-02T00:00:00Z",
            study_id="ST",
            submission_id="SUB-2",
            revision=1,
            period="2026-06",
            consent_revision=1,
            participant_ref="P-2",
        ),
        src(
            "dev-2",
            "device_use",
            "2026-07-02T01:00:00Z",
            study_id="ST",
            submission_id="SUB-2",
            device_ref="DEV-1",
            lot_ref="LOT-1",
            used_at="2026-06-11",
        ),
        src(
            "fu-2",
            "site_follow_up",
            "2026-07-16T00:00:00Z",
            study_id="ST",
            submission_id="SUB-2",
            window="2026-06",
            events_observed=1,
            data_complete=True,
        ),
    ]
}

PUBLICATION = {
    "sources": [
        src(
            "eval-1",
            "evaluation_record",
            "2026-07-20T00:00:00Z",
            evaluation_id="EV-1",
            study_id="ST",
            revision=1,
            status="published",
            decided_at="2026-07-20",
            evidence_refs=["SUB-1", "SUB-2"],
            conclusion="0 and 1 events, both traceable to LOT-1",
        )
    ]
}

CHANGES = {
    "sources": [
        src(
            "corr-1",
            "site_correction",
            "2026-08-01T00:00:00Z",
            submission_id="SUB-1",
            revision=2,
            change="participant-ref",
            field="participant_ref",
            new_value="P-1-FIXED",
            reason_code="transcription",
        ),
        src(
            "wd-fu2",
            "source_withdrawal",
            "2026-08-02T00:00:00Z",
            target_source_id="fu-2",
            reason_code="source-review",
        ),
    ]
}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.workbench = Workbench()
        self.workbench.ingest(OPENING, label="opening", ingested_at="2026-07-17T00:00:00Z")
        self.workbench.ingest(PUBLICATION, label="publication", ingested_at="2026-07-21T00:00:00Z")

    def test_opening_produces_complete_evidence_for_the_lot(self):
        snapshot = self.workbench.snapshot()
        view = production_view(snapshot)
        trace = view["lots"][0]
        self.assertEqual(trace["evidence_status"], "complete")
        self.assertEqual(
            sorted(item["submission_id"] for item in trace["evidence"]),
            ["SUB-1", "SUB-2"],
        )
        summary = research_view(snapshot)["summary"]
        self.assertEqual(summary["submissions_with_no_events_observed"], 1)
        self.assertEqual(summary["submissions_with_events_observed"], 1)
        self.assertEqual(summary["submissions_with_insufficient_data"], 0)

    def test_publication_is_complete_until_something_changes(self):
        view = self.workbench.snapshot().evaluations["EV-1"]
        self.assertEqual(view.integrity, "complete")
        self.assertFalse(view.needs_review)

    def test_one_change_is_consistent_across_all_views(self):
        self.workbench.ingest(CHANGES, label="changes", ingested_at="2026-08-03T00:00:00Z")
        snapshot = self.workbench.snapshot()

        # Research: SUB-2 lost its follow-up, SUB-1 was corrected.
        research = {r["submission_id"]: r for r in research_view(snapshot)["records"]}
        self.assertEqual(research["SUB-2"]["conclusion"]["status"], "insufficient")
        self.assertIn("follow_up_withdrawn", research["SUB-2"]["withdrawal_reasons"])
        self.assertEqual(research["SUB-1"]["participant_ref"], "P-1-FIXED")
        self.assertEqual(research["SUB-1"]["conclusion"]["status"], "no_events_observed")

        # Evaluation: published text preserved, both refs flagged, needs review.
        view = snapshot.evaluations["EV-1"]
        self.assertEqual(view.conclusion, "0 and 1 events, both traceable to LOT-1")
        self.assertTrue(view.history_preserved)
        self.assertEqual(view.integrity, "withdrawn")
        self.assertEqual(view.changed_refs, ["SUB-1"])
        self.assertEqual(view.withdrawn_refs, ["SUB-2"])
        self.assertEqual(
            view.review_scope, {"submissions": ["SUB-1", "SUB-2"], "lots": ["LOT-1"]}
        )
        self.assertFalse(view.usable_as_complete_evidence)

        # Production: the lot's evidence is now incomplete, and names SUB-2.
        trace = production_view(snapshot)["lots"][0]
        self.assertEqual(trace["evidence_status"], "incomplete")
        by_sub = {item["submission_id"]: item for item in trace["evidence"]}
        self.assertFalse(by_sub["SUB-2"]["complete"])
        self.assertIn("follow_up_withdrawn", by_sub["SUB-2"]["reasons"])

    def test_history_lets_us_see_the_publication_basis_intact(self):
        self.workbench.ingest(CHANGES, label="changes")
        frozen = self.workbench.snapshot(cutoff="2026-07-21T00:00:00Z")
        self.assertEqual(frozen.evaluations["EV-1"].integrity, "complete")
        self.assertEqual(frozen.core.research["SUB-1"].participant_ref, "P-1")
        self.assertEqual(
            frozen.core.research["SUB-2"].conclusion["status"], "events_observed"
        )
        self.assertEqual(frozen.lot_traces["LOT-1"].evidence_status, "complete")

    def test_summary_reports_the_review_without_losing_counts(self):
        self.workbench.ingest(CHANGES, label="changes")
        summary = summary_view(self.workbench.snapshot())
        self.assertEqual(summary["counts"]["evaluations_needing_review"], 1)
        # The no-event / insufficient distinction survives the change.
        self.assertEqual(summary["conclusions"]["no_events_observed"], 1)
        self.assertEqual(summary["conclusions"]["insufficient"], 1)


if __name__ == "__main__":
    unittest.main()
