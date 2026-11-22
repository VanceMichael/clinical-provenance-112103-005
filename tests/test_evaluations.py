"""Tests for evaluation integrity: version history, withdrawal and review scope."""

import unittest
from pathlib import Path

from clinical_workbench import Workbench, load_document, research_view


ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "fixtures" / "scenarios"


def src(source_id, kind, collected_at, **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


class WithdrawalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.workbench = Workbench()
        self.workbench.ingest(
            load_document(SCENARIOS / "withdrawal-batch-1.json"), label="basis"
        )
        self.baseline = self.workbench.snapshot()

    def test_baseline_evaluation_is_complete(self):
        view = self.baseline.evaluations["EV-WD-1"]
        self.assertEqual(view.integrity, "complete")
        self.assertTrue(view.usable_as_complete_evidence)
        self.assertFalse(view.needs_review)

    def test_withdrawal_is_detected(self):
        self.workbench.ingest(
            load_document(SCENARIOS / "withdrawal-batch-2.json"), label="withdrawal"
        )
        snap = self.workbench.snapshot()
        record = snap.core.research["SUB-WD-1"]
        self.assertTrue(record.withdrawn)
        self.assertEqual(record.conclusion["status"], "insufficient")
        self.assertIn("follow_up_withdrawn", record.withdrawal_reasons)

        view = snap.evaluations["EV-WD-1"]
        self.assertEqual(view.integrity, "withdrawn")
        self.assertTrue(view.needs_review)
        self.assertFalse(view.usable_as_complete_evidence)
        self.assertEqual(view.withdrawn_refs, ["SUB-WD-1"])

    def test_published_conclusion_is_retained_not_rewritten(self):
        original = self.baseline.evaluations["EV-WD-1"].conclusion
        self.workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"))
        snap = self.workbench.snapshot()
        view = snap.evaluations["EV-WD-1"]
        self.assertEqual(view.conclusion, original)
        self.assertEqual(view.declared_status, "published")
        self.assertTrue(view.history_preserved)

    def test_review_scope_names_submission_and_lot(self):
        self.workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"))
        view = self.workbench.snapshot().evaluations["EV-WD-1"]
        self.assertEqual(view.review_scope["submissions"], ["SUB-WD-1"])
        self.assertEqual(view.review_scope["lots"], ["LOT-WD-1"])

    def test_resolving_as_of_decision_restores_complete_basis(self):
        self.workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"))
        frozen = self.workbench.snapshot(cutoff="2026-07-26T00:00:00Z")
        view = frozen.evaluations["EV-WD-1"]
        self.assertEqual(view.integrity, "complete")
        self.assertTrue(view.usable_as_complete_evidence)
        # The follow-up still counted at that time.
        self.assertEqual(
            frozen.core.research["SUB-WD-1"].conclusion["status"], "events_observed"
        )

    def test_draft_evaluation_is_no_longer_complete_evidence_after_withdrawal(self):
        self.workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"))
        snap = self.workbench.snapshot()
        # Simulate the same basis declared as a draft by re-ingesting a draft
        # evaluation with a later collection time.
        self.workbench.ingest(
            {
                "sources": [
                    src(
                        "eval-draft",
                        "evaluation_record",
                        "2026-08-10T00:00:00Z",
                        evaluation_id="EV-WD-1",
                        study_id="ST-WD",
                        revision=2,
                        status="draft",
                        decided_at="2026-08-10",
                        evidence_refs=["SUB-WD-1"],
                        conclusion="pending re-review",
                    )
                ]
            }
        )
        draft = self.workbench.snapshot().evaluations["EV-WD-1"]
        self.assertEqual(draft.declared_status, "draft")
        self.assertFalse(draft.usable_as_complete_evidence)
        self.assertTrue(draft.needs_review)
        self.assertFalse(draft.history_preserved)


class EvaluationChangeDetectionTests(unittest.TestCase):
    def test_correction_changes_integrity_without_withdrawal(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"))
        workbench.ingest(
            {
                "sources": [
                    src(
                        "corr-period",
                        "site_correction",
                        "2026-08-01T00:00:00Z",
                        submission_id="SUB-WD-1",
                        revision=2,
                        change="period",
                        field="period",
                        new_value="2026-07",
                    )
                ]
            }
        )
        snap = workbench.snapshot()
        view = snap.evaluations["EV-WD-1"]
        self.assertEqual(view.integrity, "changed")
        self.assertEqual(view.changed_refs, ["SUB-WD-1"])
        self.assertTrue(view.needs_review)
        self.assertFalse(view.usable_as_complete_evidence)
        # Conclusion text is untouched.
        self.assertEqual(view.conclusion, "one event observed; device lot traceable")

    def test_missing_reference_is_reported(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"))
        workbench.ingest(
            {
                "sources": [
                    src(
                        "eval-bad-ref",
                        "evaluation_record",
                        "2026-08-10T00:00:00Z",
                        evaluation_id="EV-BAD",
                        study_id="ST-WD",
                        revision=1,
                        status="draft",
                        decided_at="2026-08-10",
                        evidence_refs=["SUB-DOES-NOT-EXIST"],
                        conclusion="x",
                    )
                ]
            }
        )
        view = workbench.snapshot().evaluations["EV-BAD"]
        self.assertEqual(view.integrity, "incomplete")
        self.assertEqual(view.missing_refs, ["SUB-DOES-NOT-EXIST"])

    def test_evaluation_history_keeps_all_revisions(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"))
        workbench.ingest(
            {
                "sources": [
                    src(
                        "eval-rev2",
                        "evaluation_record",
                        "2026-08-10T00:00:00Z",
                        evaluation_id="EV-WD-1",
                        study_id="ST-WD",
                        revision=2,
                        status="published",
                        decided_at="2026-08-10",
                        evidence_refs=["SUB-WD-1"],
                        conclusion="re-reviewed and re-published",
                    )
                ]
            }
        )
        snap = workbench.snapshot()
        history = snap.history["EV-WD-1"]
        self.assertEqual(len(history), 2)
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual(history[0]["conclusion"], "one event observed; device lot traceable")
        self.assertEqual(history[1]["conclusion"], "re-reviewed and re-published")


class CorrectionWithdrawalTests(unittest.TestCase):
    def test_withdrawn_correction_stops_applying_but_keeps_impact_visible(self):
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
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
                        "sub-1",
                        "site_submission",
                        "2026-07-01T00:00:00Z",
                        study_id="ST",
                        submission_id="SUB-1",
                        revision=1,
                        period="2026-06",
                        participant_ref="P-ORIGINAL",
                        consent_revision=1,
                    ),
                    src(
                        "corr-1",
                        "site_correction",
                        "2026-08-01T00:00:00Z",
                        submission_id="SUB-1",
                        revision=2,
                        change="participant-ref",
                        field="participant_ref",
                        new_value="P-CORRECTED",
                    ),
                ]
            }
        )
        self.assertEqual(
            workbench.snapshot().core.submissions["SUB-1"].participant_ref,
            "P-CORRECTED",
        )
        workbench.ingest(
            {
                "sources": [
                    src(
                        "wd-corr",
                        "source_withdrawal",
                        "2026-08-05T00:00:00Z",
                        target_source_id="corr-1",
                        reason_code="invalid-correction",
                    )
                ]
            }
        )
        snap = workbench.snapshot()
        state = snap.core.submissions["SUB-1"]
        # The correction no longer applies, but the submission is not withdrawn.
        self.assertEqual(state.participant_ref, "P-ORIGINAL")
        self.assertFalse(state.withdrawn)
        # The withdrawal still names the impacted submission for audit.
        self.assertIn("SUB-1", snap.core.withdrawals[0].affected_submissions)
        self.assertEqual(
            snap.core.research["SUB-1"].conclusion["status"], "insufficient"
        )


class CrossViewConsistencyTests(unittest.TestCase):
    def test_all_views_agree_on_the_same_source_change(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"))
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-2.json"))
        snap = workbench.snapshot()
        research = research_view(snap)

        # The research record, the evaluation review scope, and the withdrawal
        # report must name the same submission.
        record = next(
            r for r in research["records"] if r["submission_id"] == "SUB-WD-1"
        )
        self.assertTrue(record["withdrawn"])
        evaluation = research["evaluations"]["EV-WD-1"]
        self.assertIn("SUB-WD-1", evaluation["review_scope"]["submissions"])

        production = __import__("clinical_workbench").production_view(snap)
        lot = next(
            trace
            for trace in production["lots"]
            if trace["lot"]["effective_lot_ref"] == "LOT-WD-1"
        )
        self.assertEqual(lot["evidence"][0]["submission_id"], "SUB-WD-1")
        self.assertFalse(lot["evidence"][0]["complete"])
        self.assertIn("follow_up_withdrawn", lot["evidence"][0]["reasons"])
        self.assertEqual(lot["evidence_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
