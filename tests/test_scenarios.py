"""Tests that pin the shipped synthetic scenarios to their intended outcomes.

These guard the fixtures themselves: if the resolver's semantics drift, the
scenario expectations fail rather than silently changing meaning.
"""

import unittest
from pathlib import Path

from clinical_workbench import Workbench, load_document, production_view, research_view


ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "fixtures" / "scenarios"


def first_record(snapshot):
    return next(iter(snapshot.core.research.values()))


class CompleteChainScenarioTests(unittest.TestCase):
    def setUp(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "complete-chain.json"))
        self.snapshot = workbench.snapshot()

    def test_record_is_analysable_with_an_explicit_zero(self):
        record = first_record(self.snapshot)
        self.assertEqual(record.inclusion.status, "included")
        self.assertEqual(record.conclusion["status"], "no_events_observed")
        self.assertTrue(record.traceable)
        self.assertTrue(record.usable)

    def test_lot_evidence_is_complete(self):
        trace = production_view(self.snapshot)["lots"][0]
        self.assertEqual(trace["evidence_status"], "complete")


class OutOfWindowScenarioTests(unittest.TestCase):
    def setUp(self):
        workbench = Workbench()
        workbench.ingest(load_document(SCENARIOS / "out-of-window.json"))
        self.snapshot = workbench.snapshot()

    def test_record_is_excluded_not_merely_insufficient(self):
        record = first_record(self.snapshot)
        self.assertEqual(record.inclusion.status, "out_of_scope")
        self.assertEqual(record.conclusion["status"], "excluded")

    def test_summary_counts_it_as_excluded(self):
        summary = research_view(self.snapshot)["summary"]
        self.assertEqual(summary["submissions_excluded"], 1)
        self.assertEqual(summary["submissions_with_insufficient_data"], 0)
        self.assertEqual(summary["submissions_with_events_observed"], 0)


class ShippedFixtureTests(unittest.TestCase):
    def test_base_fixture_is_honest_about_missing_follow_up(self):
        workbench = Workbench()
        workbench.ingest(load_document(ROOT / "fixtures" / "sources.json"))
        snapshot = workbench.snapshot()
        record = snapshot.core.research["SUB-88"]
        self.assertEqual(record.inclusion.status, "included")
        # No follow-up was supplied, so it must be insufficient, never zero.
        self.assertEqual(record.conclusion["status"], "insufficient")
        self.assertIn("follow_up", record.missing)
        self.assertTrue(record.traceable)
        # The correction is applied but leaves the consent basis intact.
        self.assertEqual(record.corrections[0]["change"], "follow-up-window")


if __name__ == "__main__":
    unittest.main()
