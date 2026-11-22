"""Typed view over the raw source envelopes.

The workbench never trusts the shape of an incoming file.  ``parse_sources``
turns raw documents into :class:`SourceRecord` objects, validating the
envelope and normalising the fields each ``record_kind`` is allowed to carry.
A record kind the workbench does not understand is still *retained* (so the
ingestion view can report it) but contributes nothing to any judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from .util import (
    ValidationError,
    content_hash,
    parse_date,
    parse_datetime,
    parse_period,
)

#: Kinds this build understands.  Anything else is retained, flagged and
#: ignored by scoring.  This keeps forward-compatible feeds ingestible.
KNOWN_KINDS = frozenset(
    {
        "study_scope",
        "site_submission",
        "site_follow_up",
        "device_use",
        "production_lot",
        "lot_correction",
        "site_correction",
        "source_withdrawal",
        "evaluation_record",
    }
)

#: Envelope keys allowed on every record.  ``supersedes`` and ``replaced_by``
#: are optional linkage hints; the resolver does not need them but surfaces
#: them when present.
ENVELOPE_KEYS = frozenset(
    {
        "source_id",
        "record_kind",
        "collected_at",
        "payload",
        "supersedes",
        "replaced_by",
    }
)

REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    "study_scope": ("study_id", "revision", "consent_scope", "valid_from", "valid_to"),
    "site_submission": ("study_id", "submission_id", "revision"),
    "site_follow_up": ("study_id", "submission_id"),
    "device_use": ("study_id", "submission_id", "device_ref", "lot_ref"),
    "production_lot": ("lot_ref", "device_ref", "revision"),
    "lot_correction": ("lot_ref", "revision"),
    "site_correction": ("submission_id", "revision"),
    "source_withdrawal": ("target_source_id",),
    "evaluation_record": ("evaluation_id", "study_id", "revision"),
}


@dataclass(frozen=True)
class SourceRecord:
    """One validated envelope from a hospital or production feed."""

    source_id: str
    record_kind: str
    collected_at: datetime
    payload: dict[str, Any]
    raw_collected_at: str
    supersedes: str | None = None
    replaced_by: str | None = None
    warnings: tuple[str, ...] = ()
    known_kind: bool = True

    @property
    def fingerprint(self) -> str:
        """Hash of the *substantive* content, ignoring collection time.

        Two resubmissions of the same payload hash identically; that is how
        the ingestion view can tell "nothing changed" from "revised data".
        """

        return content_hash({"kind": self.record_kind, "payload": self.payload})

    @property
    def envelope_fingerprint(self) -> str:
        return content_hash(
            {
                "source_id": self.source_id,
                "kind": self.record_kind,
                "collected_at": self.raw_collected_at,
                "payload": self.payload,
            }
        )

    # Convenience accessors used throughout the resolver.  They never raise;
    # missing keys yield ``None`` so callers decide how to treat gaps.
    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def require(self, key: str) -> Any:
        value = self.payload.get(key)
        if value is None:
            raise ValidationError(
                f"{self.source_id} ({self.record_kind}) is missing required field {key!r}"
            )
        return value

    def revision(self) -> int | None:
        value = self.payload.get("revision")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"{self.source_id}: revision must be an integer, got {value!r}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "record_kind": self.record_kind,
            "collected_at": self.raw_collected_at,
            "payload": self.payload,
            "supersedes": self.supersedes,
            "replaced_by": self.replaced_by,
            "fingerprint": self.fingerprint,
            "known_kind": self.known_kind,
            "warnings": list(self.warnings),
        }


@dataclass
class ParseOutcome:
    records: list[SourceRecord] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def _payload_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    return bool(value)


def _validate_envelope(index: int, raw: Any) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, Mapping):
        raise ValidationError(f"source at index {index} is not an object")
    for key in ("source_id", "record_kind", "collected_at", "payload"):
        if key not in raw:
            raise ValidationError(f"source at index {index} is missing {key!r}")
    source_id = raw["source_id"]
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValidationError(f"source at index {index} has an invalid source_id")
    kind = raw["record_kind"]
    if not isinstance(kind, str) or not kind.strip():
        raise ValidationError(f"{source_id}: record_kind must be a non-empty string")
    payload = raw["payload"]
    if not isinstance(payload, Mapping):
        raise ValidationError(f"{source_id}: payload must be an object")
    unknown = sorted(set(raw) - ENVELOPE_KEYS)
    if unknown:
        warnings.append(f"unknown envelope keys ignored: {', '.join(unknown)}")
    return dict(payload), warnings


def _validate_payload(record: SourceRecord) -> None:
    if not record.known_kind:
        return
    required = REQUIRED_PAYLOAD.get(record.record_kind, ())
    missing = [key for key in required if record.payload.get(key) in (None, "")]
    if missing:
        raise ValidationError(
            f"{record.source_id} ({record.record_kind}): missing required "
            f"field(s) {', '.join(missing)}"
        )
    if record.record_kind in {
        "study_scope",
        "site_submission",
        "site_correction",
        "device_use",
        "site_follow_up",
    }:
        study = record.payload.get("study_id")
        if "study_id" in REQUIRED_PAYLOAD[record.record_kind] and not isinstance(study, str):
            raise ValidationError(f"{record.source_id}: study_id must be a string")
    if record.record_kind == "study_scope":
        start = parse_date(record.get("valid_from"))
        end = parse_date(record.get("valid_to"))
        if end < start:
            raise ValidationError(
                f"{record.source_id}: valid_to {end} precedes valid_from {start}"
            )
        record.revision()
    if record.record_kind in {
        "site_submission",
        "site_correction",
        "production_lot",
        "lot_correction",
        "device_use",
    }:
        record.revision()
    if record.record_kind == "device_use":
        used_at = record.get("used_at")
        if used_at is not None:
            try:
                parse_date(used_at)
            except ValidationError as exc:
                raise ValidationError(
                    f"{record.source_id}: used_at is not a date ({used_at!r})"
                ) from exc
    if record.record_kind == "site_submission":
        period = record.get("period")
        if period is not None:
            parse_period(period)
        if record.get("consent_revision") is not None:
            try:
                int(record.get("consent_revision"))
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"{record.source_id}: consent_revision must be an integer"
                ) from exc
    if record.record_kind == "lot_correction":
        field_name = record.get("field")
        if field_name is not None and not isinstance(field_name, str):
            raise ValidationError(f"{record.source_id}: field must be a string")
    if record.record_kind == "source_withdrawal":
        scope = record.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise ValidationError(f"{record.source_id}: scope must be a string")


def parse_sources(document: Any) -> ParseOutcome:
    """Validate a raw document into ordered :class:`SourceRecord`s.

    Records are returned in ``(collected_at, source_id)`` order.  Exact
    duplicate envelopes (same source id *and* same fingerprint) are collapsed
    and reported so a double delivery does not look like new information.
    """

    outcome = parse_sources_uncollapsed(document)
    staged = outcome.records
    latest: dict[str, SourceRecord] = {}
    seen_fingerprints: dict[str, set[str]] = {}
    duplicate_count: dict[str, int] = {}
    for record in staged:
        fingerprints = seen_fingerprints.setdefault(record.source_id, set())
        prior = latest.get(record.source_id)
        if record.fingerprint in fingerprints:
            duplicate_count[record.source_id] = duplicate_count.get(record.source_id, 0) + 1
            outcome.duplicates.append(
                {
                    "source_id": record.source_id,
                    "record_kind": record.record_kind,
                    "note": "identical re-delivery collapsed",
                }
            )
            # An identical re-delivery does not replace a later distinct one.
            continue
        if prior is not None:
            record = _with_warning(
                record,
                f"source_id {record.source_id!r} was resubmitted with different "
                "content; the later collected_at wins",
            )
        fingerprints.add(record.fingerprint)
        latest[record.source_id] = record
    outcome.records = sorted(latest.values(), key=lambda r: (r.collected_at, r.source_id))
    return outcome


def parse_sources_uncollapsed(document: Any) -> ParseOutcome:
    """Validate every delivered envelope, keeping repeated ``source_id``s.

    Use this for version history, where a resubmission is a distinct event
    rather than something to collapse.
    """

    outcome = ParseOutcome()
    if isinstance(document, Mapping):
        raw_sources = document.get("sources")
    else:
        raw_sources = document
    if not isinstance(raw_sources, list):
        raise ValidationError("document must contain a 'sources' list")

    for index, raw in enumerate(raw_sources):
        try:
            record = _record_from_raw(index, raw)
        except ValidationError as exc:
            outcome.rejected.append(
                {
                    "index": index,
                    "source_id": raw.get("source_id") if isinstance(raw, Mapping) else None,
                    "record_kind": raw.get("record_kind") if isinstance(raw, Mapping) else None,
                    "error": str(exc),
                }
            )
            continue
        outcome.records.append(record)
    outcome.records.sort(key=lambda r: (r.collected_at, r.source_id))
    return outcome


def _record_from_raw(index: int, raw: Any) -> SourceRecord:
    payload, warnings = _validate_envelope(index, raw)
    source_id = raw["source_id"].strip()
    kind = raw["record_kind"].strip()
    collected_raw = raw["collected_at"]
    collected_at = parse_datetime(collected_raw)
    supersedes = raw.get("supersedes")
    replaced_by = raw.get("replaced_by")
    if supersedes is not None and not isinstance(supersedes, str):
        raise ValidationError(f"{source_id}: supersedes must be a string")
    if replaced_by is not None and not isinstance(replaced_by, str):
        raise ValidationError(f"{source_id}: replaced_by must be a string")
    known = kind in KNOWN_KINDS
    if not known:
        warnings.append(f"unrecognised record_kind {kind!r}; retained but ignored")
    record = SourceRecord(
        source_id=source_id,
        record_kind=kind,
        collected_at=collected_at,
        payload=payload,
        raw_collected_at=str(collected_raw),
        supersedes=supersedes,
        replaced_by=replaced_by,
        warnings=tuple(warnings),
        known_kind=known,
    )
    _validate_payload(record)
    return record


def _with_warning(record: SourceRecord, message: str) -> SourceRecord:
    return SourceRecord(
        source_id=record.source_id,
        record_kind=record.record_kind,
        collected_at=record.collected_at,
        payload=record.payload,
        raw_collected_at=record.raw_collected_at,
        supersedes=record.supersedes,
        replaced_by=record.replaced_by,
        warnings=record.warnings + (message,),
        known_kind=record.known_kind,
    )


def merge_documents(documents: list[Any]) -> dict[str, Any]:
    """Concatenate several ``{"sources": [...]}`` documents into one."""

    combined: list[Any] = []
    for document in documents:
        if isinstance(document, Mapping):
            raw_sources = document.get("sources", [])
        else:
            raw_sources = document
        if not isinstance(raw_sources, list):
            raise ValidationError("each document must contain a 'sources' list")
        combined.extend(raw_sources)
    return {"sources": combined}


def index_by_id(records: list[SourceRecord]) -> dict[str, SourceRecord]:
    return {record.source_id: record for record in records}


def describe(record_date: date | None) -> str:
    return record_date.isoformat() if record_date else "unknown"


__all__ = [
    "KNOWN_KINDS",
    "ParseOutcome",
    "SourceRecord",
    "ValidationError",
    "describe",
    "index_by_id",
    "merge_documents",
    "parse_sources",
    "parse_sources_uncollapsed",
]
