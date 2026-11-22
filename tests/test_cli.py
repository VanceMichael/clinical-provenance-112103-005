"""Tests for the CLI and for deterministic, reproducible resolution."""

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from clinical_workbench import Workbench, load_document
from clinical_workbench.cli import main


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "sources.json"
SCENARIOS = ROOT / "fixtures" / "scenarios"


def run_cli(*args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def test_summary_text(self):
        code, out, err = run_cli("summary", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("sources: 5", out)
        self.assertEqual(err, "")

    def test_research_json(self):
        code, out, err = run_cli("research", str(FIXTURE), "--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["view"], "research")
        self.assertEqual(document["records"][0]["submission_id"], "SUB-88")

    def test_production_json(self):
        code, out, _ = run_cli("production", str(FIXTURE), "--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["lots"][0]["lot"]["lot_ref"], "LOT-SYN-4")

    def test_ingestion_view(self):
        code, out, _ = run_cli("ingestion", str(FIXTURE), "--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["view"], "ingestion")
        self.assertEqual(document["accepted_sources"], 5)

    def test_history_view(self):
        code, out, _ = run_cli("history", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("study-scope-2", out)

    def test_multiple_documents(self):
        code, out, _ = run_cli(
            "production",
            str(SCENARIOS / "lot-chain-batch-1.json"),
            str(SCENARIOS / "lot-chain-batch-2.json"),
            "--json",
        )
        self.assertEqual(code, 0)
        document = json.loads(out)
        refs = {lot["lot"]["effective_lot_ref"] for lot in document["lots"]}
        self.assertIn("LOT-A-RENAMED", refs)

    def test_as_of_restores_prior_state(self):
        code, out, _ = run_cli(
            "as-of",
            "2026-07-26T00:00:00Z",
            str(SCENARIOS / "withdrawal-batch-1.json"),
            str(SCENARIOS / "withdrawal-batch-2.json"),
            "--json",
        )
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["cutoff"], "2026-07-26T00:00:00Z")
        self.assertEqual(document["counts"]["evaluations_needing_review"], 0)

    def test_invalid_document_returns_error(self):
        code, out, err = run_cli("summary", "/nonexistent/path.json")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_cutoff_flag(self):
        code, out, _ = run_cli(
            "summary",
            str(SCENARIOS / "withdrawal-batch-1.json"),
            str(SCENARIOS / "withdrawal-batch-2.json"),
            "--cutoff",
            "2026-07-26T00:00:00Z",
            "--json",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["cutoff"], "2026-07-26T00:00:00Z")


class DeterminismTests(unittest.TestCase):
    def test_evidence_versions_are_stable_across_runs(self):
        first = Workbench.from_path(FIXTURE).snapshot()
        second = Workbench.from_path(FIXTURE).snapshot()
        self.assertEqual(
            first.core.research["SUB-88"].evidence_version,
            second.core.research["SUB-88"].evidence_version,
        )
        self.assertEqual(
            first.lot_traces["LOT-SYN-4"].evidence_version,
            second.lot_traces["LOT-SYN-4"].evidence_version,
        )

    def test_ingestion_order_does_not_change_versions(self):
        sources = load_document(FIXTURE)["sources"]
        forward = Workbench()
        forward.ingest({"sources": sources})
        backward = Workbench()
        backward.ingest({"sources": list(reversed(sources))})
        self.assertEqual(
            forward.snapshot().core.research["SUB-88"].evidence_version,
            backward.snapshot().core.research["SUB-88"].evidence_version,
        )

    def test_json_views_are_serialisable(self):
        snapshot = Workbench.from_path(FIXTURE).snapshot()
        from clinical_workbench import (
            ingestion_view,
            production_view,
            research_view,
            summary_view,
        )

        for view in (ingestion_view, research_view, production_view, summary_view):
            text = json.dumps(view(snapshot), ensure_ascii=False)
            self.assertIsInstance(text, str)
            self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
