"""Role-specific projections of one resolved snapshot.

All three views read the same :class:`~clinical_workbench.workbench.Snapshot`;
they differ only in what they surface.  That shared origin is the point: a
single source change is reflected consistently, yet each role sees the facet
it needs.
"""

from __future__ import annotations

from typing import Any

from .resolver import build_event_summary
from .workbench import Snapshot

__all__ = [
    "ingestion_view",
    "research_view",
    "production_view",
    "summary_view",
]


def ingestion_view(snapshot: Snapshot) -> dict[str, Any]:
    """What arrived, what was rejected, and what changed per source."""

    rejected = list(snapshot.rejected_entries)
    warnings: list[dict[str, Any]] = []
    for record in snapshot.records:
        if record.warnings:
            warnings.append(
                {
                    "source_id": record.source_id,
                    "record_kind": record.record_kind,
                    "warnings": list(record.warnings),
                }
            )
    changed = []
    for source_id, versions in sorted(snapshot.source_history.items()):
        fingerprints = {version["fingerprint"] for version in versions}
        if len(versions) > 1:
            changed.append(
                {
                    "source_id": source_id,
                    "revisions": len(versions),
                    "distinct_contents": len(fingerprints),
                    "revisions_detail": versions,
                }
            )
    return {
        "view": "ingestion",
        "cutoff": snapshot.cutoff_text,
        "batches": [batch.to_dict() for batch in snapshot.batches],
        "accepted_sources": len(snapshot.records),
        "rejected": rejected,
        "rejected_count": len(rejected),
        "unknown_kinds": sorted(
            {record.record_kind for record in snapshot.records if not record.known_kind}
        ),
        "warnings": warnings,
        "changed_sources": changed,
        "source_history": snapshot.source_history,
        "withdrawals": [
            withdrawal.to_dict() for withdrawal in snapshot.core.withdrawals
        ],
    }


def research_view(snapshot: Snapshot) -> dict[str, Any]:
    """Inclusion basis, missing items, conclusion status and evidence history."""

    records = []
    for submission_id in sorted(snapshot.core.research):
        research = snapshot.core.research[submission_id]
        data = research.to_dict(include_bundle=True)
        # Evaluations that cite this submission, with their own revision
        # history -- distinct from the raw source versions below.
        data["cited_by_evaluations"] = [
            evaluation_id
            for evaluation_id, view in sorted(snapshot.evaluations.items())
            if submission_id in view.evidence_refs
        ]
        data["source_versions"] = {
            source_id: snapshot.source_history.get(source_id, [])
            for source_id in research.component_source_ids
            if source_id in snapshot.source_history
        }
        records.append(data)

    study_scopes: dict[str, list[dict[str, Any]]] = {}
    for study_id, scopes in sorted(snapshot.core.scopes.items()):
        study_scopes[study_id] = [scope.to_dict() for scope in scopes]

    summary = build_event_summary(records)
    return {
        "view": "research",
        "cutoff": snapshot.cutoff_text,
        "study_scopes": study_scopes,
        "records": records,
        "summary": summary.to_dict(),
        "evaluations": {
            evaluation_id: view.to_dict()
            for evaluation_id, view in sorted(snapshot.evaluations.items())
        },
        "evaluation_history": snapshot.history,
    }


def production_view(snapshot: Snapshot) -> dict[str, Any]:
    """Lot identities, corrections, linked evidence and evidence versions."""

    traces = []
    for lot_ref in sorted(snapshot.lot_traces):
        trace = snapshot.lot_traces[lot_ref]
        data = trace.to_dict()
        # Attach the research side's own view of each linked submission so
        # production can see *why* evidence is or is not complete.
        for item in data["evidence"]:
            research = snapshot.core.research.get(item["submission_id"])
            if research is not None:
                item["conclusion"] = research.conclusion
                item["inclusion_status"] = research.inclusion.status
                item["traceable"] = research.traceable
                item["withdrawn"] = research.withdrawn
        traces.append(data)

    lots_by_device: dict[str, list[str]] = {}
    for lot in snapshot.core.lots.values():
        if lot.device_ref:
            lots_by_device.setdefault(lot.device_ref, []).append(lot.effective_lot_ref)
    for device_ref in lots_by_device:
        lots_by_device[device_ref] = sorted(set(lots_by_device[device_ref]))

    by_status: dict[str, int] = {}
    for trace in traces:
        status = trace["evidence_status"]
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "view": "production",
        "cutoff": snapshot.cutoff_text,
        "lots": traces,
        "lots_by_device": dict(sorted(lots_by_device.items())),
        "withdrawals": [
            withdrawal.to_dict() for withdrawal in snapshot.core.withdrawals
        ],
        "totals": {
            "lots": len(traces),
            "by_evidence_status": dict(sorted(by_status.items())),
            "complete_lots": by_status.get("complete", 0),
            "incomplete_lots": by_status.get("incomplete", 0),
            "untracked_lots": by_status.get("untracked", 0),
            "withdrawn_lots": by_status.get("withdrawn", 0),
        },
    }


def summary_view(snapshot: Snapshot) -> dict[str, Any]:
    """A compact, cross-role overview used by the CLI ``summary`` command."""

    records = [research.to_dict() for research in snapshot.core.research.values()]
    status_counts: dict[str, int] = {}
    for record in records:
        status = record["conclusion"]["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    inclusion_counts: dict[str, int] = {}
    for record in records:
        status = record["inclusion"]["status"]
        inclusion_counts[status] = inclusion_counts.get(status, 0) + 1
    reviews = [
        view.to_dict()
        for view in snapshot.evaluations.values()
        if view.needs_review
    ]
    return {
        "view": "summary",
        "cutoff": snapshot.cutoff_text,
        "counts": {
            "sources": len(snapshot.records),
            "submissions": len(records),
            "lots": len(snapshot.lot_traces),
            "evaluations": len(snapshot.evaluations),
            "evaluations_needing_review": len(reviews),
        },
        "conclusions": dict(sorted(status_counts.items())),
        "inclusion": dict(sorted(inclusion_counts.items())),
        "reviews": reviews,
        "production": production_view(snapshot)["totals"],
    }
