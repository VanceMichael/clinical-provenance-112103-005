"""Continuous ingestion and snapshotting.

The workbench accepts batches over time.  Every batch is retained so that the
same source stream can be re-resolved *as of any point in time* -- that is what
lets a published conclusion keep the basis it was decided on after later
corrections arrive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .model import (
    ParseOutcome,
    SourceRecord,
    merge_documents,
    parse_sources,
    parse_sources_uncollapsed,
)
from .resolver import (
    CoreResolution,
    EvaluationView,
    LotTrace,
    build_core,
    build_evaluations,
    build_lot_traces,
)
from .util import ValidationError, iso_utc, parse_datetime


@dataclass
class Batch:
    batch_id: str
    label: str | None
    ingested_at: datetime
    accepted: int
    rejected: int
    duplicates: int
    unknown_kinds: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "label": self.label,
            "ingested_at": iso_utc(self.ingested_at),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "unknown_kinds": list(self.unknown_kinds),
            "warnings": list(self.warnings),
        }


@dataclass
class Snapshot:
    """One coherent resolution of everything ingested so far."""

    cutoff: datetime | None
    parse: ParseOutcome
    records: list[SourceRecord]
    core: CoreResolution
    evaluations: dict[str, EvaluationView]
    lot_traces: dict[str, LotTrace]
    batches: list[Batch]
    rejected_entries: list[dict[str, Any]]
    source_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def cutoff_text(self) -> str:
        return iso_utc(self.cutoff) if self.cutoff else "latest"


class Workbench:
    """Accumulates source batches and resolves them on demand."""

    def __init__(self) -> None:
        self._raw_sources: list[Any] = []
        self._batches: list[Batch] = []
        self._rejected_entries: list[dict[str, Any]] = []
        self._batch_counter = 0

    # -- ingestion ---------------------------------------------------------
    def ingest(
        self,
        document: Any,
        label: str | None = None,
        ingested_at: datetime | str | None = None,
    ) -> Batch:
        if isinstance(document, (str, Path)):
            document = json.loads(Path(document).read_text(encoding="utf-8"))
        if isinstance(ingested_at, str):
            ingested_at = parse_datetime(ingested_at)
        ingested_at = ingested_at or datetime.now(timezone.utc)

        raw_sources = document.get("sources") if isinstance(document, Mapping) else document
        if not isinstance(raw_sources, list):
            raise ValidationError("document must contain a 'sources' list")

        # Validate this batch in isolation so the batch report is meaningful,
        # then append the accepted raw envelopes to the running stream.
        outcome = parse_sources({"sources": raw_sources})
        self._rejected_entries.extend(
            {**entry, "label": label} for entry in outcome.rejected
        )
        accepted_raw = [
            raw
            for index, raw in enumerate(raw_sources)
            if not any(rejected["index"] == index for rejected in outcome.rejected)
        ]
        self._raw_sources.extend(accepted_raw)
        self._batch_counter += 1
        unknown = sorted(
            {record.record_kind for record in outcome.records if not record.known_kind}
        )
        warnings: list[str] = []
        for record in outcome.records:
            warnings.extend(record.warnings)
        batch = Batch(
            batch_id=f"batch-{self._batch_counter:03d}",
            label=label,
            ingested_at=ingested_at,
            accepted=len(outcome.records),
            rejected=len(outcome.rejected),
            duplicates=len(outcome.duplicates),
            unknown_kinds=unknown,
            warnings=sorted(set(warnings)),
        )
        self._batches.append(batch)
        return batch

    # -- resolution --------------------------------------------------------
    def snapshot(self, cutoff: datetime | str | None = None) -> Snapshot:
        if isinstance(cutoff, str):
            cutoff = parse_datetime(cutoff)
        if cutoff is not None:
            cutoff = parse_datetime(iso_utc(cutoff))
        document = merge_documents([{"sources": self._raw_sources}])
        # Two parses with different jobs:
        #  * ``outcome`` collapses re-deliveries and reports them (ingestion).
        #  * ``uncollapsed`` keeps every delivered version so that resolving
        #    "as of" an earlier time can still see the version that existed
        #    then.  Collapsing first would erase a resubmission's predecessor
        #    and make historical cutoffs impossible.
        outcome = parse_sources(document)
        uncollapsed = parse_sources_uncollapsed(document)
        records = uncollapsed.records
        core = build_core(records, cutoff)
        evaluations = build_evaluations(records, cutoff, current=core)
        traces = build_lot_traces(core)
        history = _evaluation_history(records, cutoff)
        source_history = _source_history(self._raw_sources, cutoff)
        return Snapshot(
            cutoff=cutoff,
            parse=outcome,
            records=outcome.records,
            core=core,
            evaluations=evaluations,
            lot_traces=traces,
            batches=list(self._batches),
            rejected_entries=list(self._rejected_entries)
            + [{**entry, "label": None} for entry in uncollapsed.rejected],
            source_history=source_history,
            history=history,
        )

    # -- convenience -------------------------------------------------------
    @classmethod
    def from_documents(
        cls,
        documents: list[Any],
        labels: list[str] | None = None,
        ingested_at: datetime | str | None = None,
    ) -> "Workbench":
        workbench = cls()
        for index, document in enumerate(documents):
            label = labels[index] if labels and index < len(labels) else f"document-{index + 1}"
            workbench.ingest(document, label=label, ingested_at=ingested_at)
        return workbench

    @classmethod
    def from_path(cls, path: str | Path, label: str | None = None) -> "Workbench":
        workbench = cls()
        workbench.ingest(path, label=label or str(path))
        return workbench


def _source_history(
    raw_sources: list[Any], cutoff: datetime | None
) -> dict[str, list[dict[str, Any]]]:
    """All delivered versions of each source id, oldest first.

    Uses the uncollapsed parse so a resubmission is visible as its own version
    rather than being silently overwritten.
    """

    parsed = parse_sources_uncollapsed({"sources": raw_sources})
    history: dict[str, list[dict[str, Any]]] = {}
    for record in parsed.records:
        if cutoff is not None and record.collected_at > cutoff:
            continue
        versions = history.setdefault(record.source_id, [])
        if versions and versions[-1]["fingerprint"] == record.fingerprint:
            # Identical re-delivery: record it, but do not pretend it is a
            # new content version.
            versions[-1]["redeliveries"] += 1
            continue
        versions.append(
            {
                "source_id": record.source_id,
                "record_kind": record.record_kind,
                "collected_at": iso_utc(record.collected_at),
                "fingerprint": record.fingerprint,
                "payload": record.payload,
                "warnings": list(record.warnings),
                "redeliveries": 0,
            }
        )
    return history


def _evaluation_history(
    records: list[SourceRecord], cutoff: datetime | None
) -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.record_kind != "evaluation_record":
            continue
        if cutoff is not None and record.collected_at > cutoff:
            continue
        evaluation_id = record.get("evaluation_id")
        if not evaluation_id:
            continue
        history.setdefault(evaluation_id, []).append(
            {
                "source_id": record.source_id,
                "revision": int(record.get("revision", 0)),
                "status": record.get("status"),
                "decided_at": record.get("decided_at") or record.raw_collected_at,
                "conclusion": record.get("conclusion"),
                "evidence_refs": sorted(
                    {
                        ref
                        for ref in (
                            record.get("evidence_refs")
                            or record.get("submission_ids")
                            or []
                        )
                        if isinstance(ref, str)
                    }
                ),
                "collected_at": iso_utc(record.collected_at),
            }
        )
    for entries in history.values():
        entries.sort(key=lambda item: (item["revision"], item["collected_at"]))
    return history


__all__ = ["Batch", "Snapshot", "Workbench"]
