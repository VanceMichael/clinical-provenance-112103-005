"""Command-line entry point for the clinical evidence workbench.

Examples::

    python3 -m clinical_workbench summary fixtures/sources.json
    python3 -m clinical_workbench research fixtures/sources.json --json
    python3 -m clinical_workbench production fixtures/sources.json --json
    python3 -m clinical_workbench ingest fixtures/a.json fixtures/b.json --view research
    python3 -m clinical_workbench history fixtures/a.json fixtures/b.json
    python3 -m clinical_workbench as-of 2026-07-25 fixtures/a.json fixtures/b.json

Every command is read-only: it reads JSON documents and prints a projection of
one resolved snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    ValidationError,
    Workbench,
    ingestion_view,
    load_document,
    production_view,
    research_view,
    summary_view,
)
from .util import parse_datetime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clinical_workbench",
        description="Reconcile synthetic clinical research and production lot sources.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the human-readable summary",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("summary", "compact cross-role overview"),
        ("research", "inclusion basis, missing items and conclusions"),
        ("production", "lot identities, corrections and linked evidence"),
        ("ingestion", "batches, rejections and source changes"),
        ("history", "per-source and per-evaluation version history"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("documents", nargs="+", type=Path)
        command.add_argument("--json", action="store_true", help="emit JSON")
        command.add_argument(
            "--cutoff",
            default=None,
            help="resolve as of an ISO-8601 timestamp instead of latest",
        )

    as_of = sub.add_parser("as-of", help="resolve as of a timestamp")
    as_of.add_argument("when", help="ISO-8601 timestamp")
    as_of.add_argument("documents", nargs="+", type=Path)
    as_of.add_argument("--json", action="store_true", help="emit JSON")

    return parser


def _load_all(paths: list[Path]) -> Workbench:
    workbench = Workbench()
    for path in paths:
        workbench.ingest(load_document(path), label=str(path))
    return workbench


def _render_summary(document: dict[str, Any]) -> str:
    counts = document["counts"]
    lines = [
        f"cutoff: {document['cutoff']}",
        f"sources: {counts['sources']}  submissions: {counts['submissions']}  "
        f"lots: {counts['lots']}  evaluations: {counts['evaluations']}",
        f"evaluations needing review: {counts['evaluations_needing_review']}",
        "conclusions: "
        + ", ".join(f"{key}={value}" for key, value in document["conclusions"].items()),
        "inclusion: "
        + ", ".join(f"{key}={value}" for key, value in document["inclusion"].items()),
        "lots by evidence status: "
        + ", ".join(
            f"{key}={value}"
            for key, value in document["production"]["by_evidence_status"].items()
        ),
    ]
    for review in document["reviews"]:
        lines.append(
            f"  ! evaluation {review['evaluation_id']} ({review['integrity']}) "
            f"needs review: submissions={review['review_scope']['submissions']} "
            f"lots={review['review_scope']['lots']}"
        )
    return "\n".join(lines)


def _render_research(document: dict[str, Any]) -> str:
    lines = [f"cutoff: {document['cutoff']}"]
    summary = document["summary"]
    lines.append(
        "analysable={analysable} events={e} no_events={z} "
        "insufficient={i} excluded={x}".format(
            analysable=summary["analysable"],
            e=summary["submissions_with_events_observed"],
            z=summary["submissions_with_no_events_observed"],
            i=summary["submissions_with_insufficient_data"],
            x=summary["submissions_excluded"],
        )
    )
    for record in document["records"]:
        lines.append(
            f"- {record['submission_id']} [{record['period']}] "
            f"inclusion={record['inclusion']['status']} "
            f"conclusion={record['conclusion']['status']} "
            f"traceable={record['traceable']} version={record['evidence_version']}"
        )
        for check in record["inclusion"]["checks"]:
            if not check["ok"]:
                lines.append(f"    x {check['check']}: {check['detail']}")
        if record["missing"]:
            lines.append(f"    missing: {', '.join(record['missing'])}")
    for evaluation_id, view in document["evaluations"].items():
        if view["needs_review"]:
            lines.append(
                f"!! evaluation {evaluation_id} integrity={view['integrity']} "
                f"review={view['review_scope']}"
            )
    return "\n".join(lines)


def _render_production(document: dict[str, Any]) -> str:
    lines = [f"cutoff: {document['cutoff']}"]
    for lot in document["lots"]:
        data = lot["lot"]
        lines.append(
            f"- {data['lot_ref']} -> {data['effective_lot_ref']} rev {data['revision']} "
            f"device={data['device_ref']} state={data['state']} "
            f"evidence={lot['evidence_status']} version={lot['evidence_version']}"
        )
        if data["detached_links"]:
            lines.append(f"    detached links: {len(data['detached_links'])}")
        for item in lot["evidence"]:
            flag = "ok" if item["complete"] else "incomplete"
            lines.append(
                f"    {item['submission_id']} {flag} version={item['evidence_version']}"
                + (f" reasons={item['reasons']}" if item["reasons"] else "")
            )
    totals = document["totals"]
    lines.append(
        "totals: "
        + ", ".join(
            f"{key}={value}" for key, value in totals["by_evidence_status"].items()
        )
    )
    return "\n".join(lines)


def _render_ingestion(document: dict[str, Any]) -> str:
    lines = [f"cutoff: {document['cutoff']}"]
    lines.append(f"accepted: {document['accepted_sources']}")
    for batch in document["batches"]:
        lines.append(
            f"- {batch['batch_id']} ({batch['label']}) accepted={batch['accepted']} "
            f"rejected={batch['rejected']} duplicates={batch['duplicates']}"
        )
    for entry in document["rejected"]:
        lines.append(f"  x rejected {entry.get('source_id')}: {entry['error']}")
    for source in document["changed_sources"]:
        lines.append(
            f"  ~ {source['source_id']} delivered {source['revisions']}x "
            f"({source['distinct_contents']} distinct)"
        )
    for withdrawal in document["withdrawals"]:
        lines.append(
            f"  ! withdrawal {withdrawal['source_id']} -> {withdrawal['target']} "
            f"({withdrawal['target_kind']})"
        )
    return "\n".join(lines)


def _render_history(document: dict[str, Any]) -> str:
    lines = []
    for source_id, versions in sorted(document["source_history"].items()):
        lines.append(f"{source_id}: {len(versions)} version(s)")
        for version in versions:
            lines.append(
                f"    {version['collected_at']} {version['record_kind']} "
                f"{version['fingerprint'][:12]}"
            )
    if document["history"]:
        lines.append("evaluations:")
        for evaluation_id, versions in sorted(document["history"].items()):
            lines.append(f"  {evaluation_id}: {len(versions)} revision(s)")
            for version in versions:
                lines.append(
                    f"    rev {version['revision']} {version['status']} "
                    f"decided={version['decided_at']}"
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        workbench = _load_all(args.documents)
        if args.command == "as-of":
            snapshot = workbench.snapshot(cutoff=parse_datetime(args.when))
            document = summary_view(snapshot)
            as_json = args.json
        else:
            snapshot = workbench.snapshot(cutoff=args.cutoff)
            captures = {
                "summary": summary_view,
                "research": research_view,
                "production": production_view,
                "ingestion": ingestion_view,
                "history": lambda snap: {
                    "cutoff": snap.cutoff_text,
                    "source_history": snap.source_history,
                    "history": snap.history,
                },
            }
            document = captures[args.command](snapshot)
            as_json = args.json or vars(args).get("json", False)
    except (ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    renderer = {
        "summary": _render_summary,
        "research": _render_research,
        "production": _render_production,
        "ingestion": _render_ingestion,
        "history": _render_history,
        "as-of": _render_summary,
    }[args.command]
    print(renderer(document))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
