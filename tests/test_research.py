"""Tests for inclusion basis, conclusions and the missing-vs-zero distinction."""

import unittest
from pathlib import Path

from clinical_workbench import Workbench, load_document, research_view


ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "fixtures" / "scenarios"
FIXTURE = ROOT / "fixtures" / "sources.json"


def src(source_id, kind, collected_at, **payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected_at,
        "payload": payload,
    }


def scope(study="ST", revision=1, consent="device-follow-up", start="2026-01-01", end="2027-12-31", **extra):
    return src(
        f"scope-{study}-{revision}",
        "study_scope",
        "2026-01-01T00:00:00Z",
        study_id=study,
        revision=revision,
        consent_scope=consent,
        valid_from=start,
        valid_to=end,
        **extra,
    )


def submission(
    sub_id="SUB-1",
    study="ST",
    revision=1,
    period="2026-06",
    consent_revision=1,
    collected_at="2026-07-01T00:00:00Z",
    **extra,
):
    return src(
        f"sub-{sub_id}-{revision}",
        "site_submission",
        collected_at,
        study_id=study,
        submission_id=sub_id,
        revision=revision,
        period=period,
        consent_revision=consent_revision,
        participant_ref="SUBJECT-1",
        **extra,
    )


def device(sub_id="SUB-1", device_ref="DEV-1", lot_ref="LOT-1", study="ST"):
    return src(
        f"dev-{sub_id}",
        "device_use",
        "2026-07-02T00:00:00Z",
        study_id=study,
        submission_id=sub_id,
        device_ref=device_ref,
        lot_ref=lot_ref,
        used_at="2026-06-15",
    )


def lot(lot_ref="LOT-1", device_ref="DEV-1", revision=1):
    return src(
        f"lot-{lot_ref}",
        "production_lot",
        "2026-05-01T00:00:00Z",
        lot_ref=lot_ref,
        device_ref=device_ref,
        line_ref="LINE-1",
        state="released",
        revision=revision,
    )


def follow_up(sub_id="SUB-1", events=0, complete=True, window="2026-06"):
    payload = {
        "study_id": "ST",
        "submission_id": sub_id,
        "window": window,
        "data_complete": complete,
    }
    if events is not None:
        payload["events_observed"] = events
    return src(
        f"fu-{sub_id}",
        "site_follow_up",
        "2026-07-15T00:00:00Z",
        **payload,
    )


def snapshot(*sources):
    workbench = Workbench()
    workbench.ingest({"sources": list(sources)})
    return workbench.snapshot()


class InclusionTests(unittest.TestCase):
    def test_clean_chain_is_included_and_analysable(self):
        snap = snapshot(
            scope(), lot(), submission(), device(), follow_up(events=0)
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.inclusion.status, "included")
        self.assertEqual(record.conclusion["status"], "no_events_observed")
        self.assertTrue(record.usable)
        self.assertEqual(record.missing, [])

    def test_period_outside_consent_window_is_out_of_scope(self):
        snap = snapshot(
            scope(start="2026-01-01", end="2026-03-31"),
            lot(),
            submission(period="2026-06"),
            device(),
            follow_up(events=1),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.inclusion.status, "out_of_scope")
        self.assertEqual(record.conclusion["status"], "excluded")

    def test_partial_coverage_is_reported_not_silently_trimmed(self):
        snap = snapshot(
            scope(start="2026-06-15", end="2027-12-31"),
            lot(),
            submission(period="2026-06"),
            device(),
            follow_up(events=0),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.inclusion.coverage, "partial")
        self.assertIn("full_consent_coverage", record.inclusion.missing)
        # Partial coverage still counts as includable, but the gap is visible.
        self.assertEqual(record.inclusion.status, "included")

    def test_claimed_scope_not_covered_is_out_of_scope(self):
        snap = snapshot(
            scope(consent="device-follow-up"),
            lot(),
            submission(),
            device(),
            follow_up(events=0),
        )
        # Now inject a submission claiming a different scope.
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
                    scope(consent="device-follow-up"),
                    lot(),
                    submission(consent_scope="biobank-analysis"),
                    device(),
                    follow_up(events=0),
                ]
            }
        )
        record = workbench.snapshot().core.research["SUB-1"]
        self.assertEqual(record.inclusion.status, "out_of_scope")

    def test_missing_consent_revision_cannot_be_included(self):
        snap = snapshot(
            scope(),
            lot(),
            submission(consent_revision=None),
            device(),
            follow_up(events=0),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.inclusion.status, "insufficient_basis")
        self.assertIn("consent_revision", record.missing)

    def test_consent_revision_with_no_scope_is_insufficient_basis(self):
        snap = snapshot(
            scope(revision=1),
            lot(),
            submission(consent_revision=9),
            device(),
            follow_up(events=0),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.inclusion.status, "insufficient_basis")


class ConclusionTests(unittest.TestCase):
    def test_zero_events_is_distinct_from_missing_follow_up(self):
        zero = snapshot(scope(), lot(), submission(), device(), follow_up(events=0))
        none = snapshot(scope(), lot(), submission(), device())
        self.assertEqual(
            zero.core.research["SUB-1"].conclusion["status"], "no_events_observed"
        )
        self.assertEqual(
            none.core.research["SUB-1"].conclusion["status"], "insufficient"
        )
        summary_zero = research_view(zero)["summary"]
        summary_none = research_view(none)["summary"]
        self.assertEqual(summary_zero["submissions_with_no_events_observed"], 1)
        self.assertEqual(summary_zero["submissions_with_insufficient_data"], 0)
        self.assertEqual(summary_none["submissions_with_no_events_observed"], 0)
        self.assertEqual(summary_none["submissions_with_insufficient_data"], 1)

    def test_follow_up_without_event_count_is_insufficient(self):
        snap = snapshot(scope(), lot(), submission(), device(), follow_up(events=None))
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.conclusion["status"], "insufficient")
        self.assertIn("outcome_observation", record.missing)

    def test_incomplete_follow_up_is_insufficient(self):
        snap = snapshot(
            scope(), lot(), submission(), device(), follow_up(events=0, complete=False)
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.conclusion["status"], "insufficient")
        self.assertIn("observation_incomplete", record.missing)

    def test_events_observed_counts(self):
        snap = snapshot(scope(), lot(), submission(), device(), follow_up(events=3))
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.conclusion["status"], "events_observed")
        self.assertIn("3", record.conclusion["detail"])

    def test_submission_level_event_count_used_without_follow_up(self):
        # A scope may not require a follow-up; then the submission's own count
        # is the observation.
        snap = snapshot(
            scope(consent="registry", require_follow_up=False),
            submission(events_observed=0),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.conclusion["status"], "no_events_observed")

    def test_device_requirement_can_be_waived_by_scope(self):
        snap = snapshot(
            scope(consent="registry", require_follow_up=False, require_device_traceability=False),
            submission(events_observed=1),
        )
        record = snap.core.research["SUB-1"]
        self.assertEqual(record.conclusion["status"], "events_observed")
        self.assertTrue(record.traceable)

    def test_missing_device_link_makes_record_insufficient(self):
        snap = snapshot(scope(), lot(), submission(), follow_up(events=0))
        record = snap.core.research["SUB-1"]
        self.assertFalse(record.traceable)
        self.assertEqual(record.conclusion["status"], "insufficient")
        self.assertIn("device_use_record", record.missing)


class ResearchViewLinkageTests(unittest.TestCase):
    def test_record_links_to_citing_evaluations_and_source_versions(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "withdrawal-batch-1.json"))
        view = research_view(workbench.snapshot())
        record = next(
            r for r in view["records"] if r["submission_id"] == "SUB-WD-1"
        )
        self.assertEqual(record["cited_by_evaluations"], ["EV-WD-1"])
        # Every contributing source is represented, including the lot.
        self.assertIn("submission-wd", record["source_versions"])
        self.assertIn("lot-wd", record["source_versions"])
        self.assertIn("followup-wd", record["source_versions"])


class CorrectionHistoryTests(unittest.TestCase):
    def test_case_correction_is_recorded_but_does_not_erase_prior_basis(self):
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
                    scope(),
                    lot(),
                    submission(),
                    device(),
                    follow_up(events=0),
                ]
            },
            label="initial",
        )
        initial = workbench.snapshot().core.research["SUB-1"]
        initial_version = initial.evidence_version

        workbench.ingest(
            {
                "sources": [
                    src(
                        "corr-1",
                        "site_correction",
                        "2026-08-01T00:00:00Z",
                        submission_id="SUB-1",
                        revision=2,
                        change="participant-ref",
                        field="participant_ref",
                        new_value="SUBJECT-1-CORRECTED",
                        reason_code="transcription",
                    )
                ]
            },
            label="correction",
        )
        snap = workbench.snapshot()
        corrected = snap.core.research["SUB-1"]
        self.assertEqual(corrected.participant_ref, "SUBJECT-1-CORRECTED")
        self.assertNotEqual(corrected.evidence_version, initial_version)
        self.assertEqual(corrected.corrections[0]["change"], "participant-ref")

        # Resolving as of before the correction reproduces the original basis.
        prior = workbench.snapshot(cutoff="2026-07-31T00:00:00Z")
        self.assertEqual(
            prior.core.research["SUB-1"].evidence_version, initial_version
        )
        self.assertEqual(
            prior.core.research["SUB-1"].participant_ref, "SUBJECT-1"
        )

    def test_correction_cannot_change_an_already_decided_evaluation_basis(self):
        # Covered in depth by test_evaluations; here we assert the mechanism:
        # a cutoff before the correction yields the original evidence version.
        workbench = Workbench()
        batch = load_document(SCENARIOS / "withdrawal-batch-1.json")
        workbench.ingest(batch)
        original = workbench.snapshot().core.research["SUB-WD-1"].evidence_version
        workbench.ingest(
            {
                "sources": [
                    src(
                        "corr-wd",
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
        frozen = workbench.snapshot(cutoff="2026-07-25T23:00:00Z")
        self.assertEqual(
            frozen.core.research["SUB-WD-1"].evidence_version, original
        )

    def test_resubmitted_source_keeps_prior_version_for_cutoffs(self):
        workbench = Workbench()
        workbench.ingest(
            {
                "sources": [
                    scope(),
                    lot(),
                    device(),
                    follow_up(events=0),
                    submission(),
                ]
            }
        )
        original = workbench.snapshot().core.research["SUB-1"]
        self.assertEqual(original.participant_ref, "SUBJECT-1")

        # Same source_id, later collection time, changed participant.
        workbench.ingest(
            {
                "sources": [
                    src(
                        "sub-SUB-1-1",
                        "site_submission",
                        "2026-08-01T00:00:00Z",
                        study_id="ST",
                        submission_id="SUB-1",
                        revision=1,
                        period="2026-06",
                        consent_revision=1,
                        participant_ref="SUBJECT-1-RESUBMITTED",
                    )
                ]
            }
        )
        latest = workbench.snapshot().core.research["SUB-1"]
        self.assertEqual(latest.participant_ref, "SUBJECT-1-RESUBMITTED")
        frozen = workbench.snapshot(cutoff="2026-07-20T00:00:00Z")
        self.assertEqual(frozen.core.research["SUB-1"].participant_ref, "SUBJECT-1")
        self.assertEqual(
            frozen.core.research["SUB-1"].evidence_version,
            original.evidence_version,
        )


if __name__ == "__main__":
    unittest.main()
