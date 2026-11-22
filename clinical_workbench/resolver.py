"""The resolution engine.

A single pass over the ordered source stream produces one coherent state:
study scopes, submissions (with amendments), follow-ups, device/lot links,
withdrawals, and evaluation bases.  The ingestion, research and production
views are *projections* of that state, which is what makes them consistent
about the same source change while still answering their own questions.

Two ideas do most of the work:

``cutoff``
    Resolution is parameterised by a point in time.  Resolving "as of" an
    evaluation's decision time reconstructs exactly what was known then, so a
    later correction can never rewrite an earlier conclusion's basis.

``exact identity``
    Devices and lots are associated only through an explicit ``device_use``
    whose ``lot_ref`` and ``device_ref`` both match the resolved lot.  Nothing
    is ever joined on a similar-looking name or product reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .model import SourceRecord
from .util import (
    canonical_json,
    parse_date,
    parse_datetime,
    parse_period,
    ranges_overlap,
    short_hash,
)

#: Outcomes a research record can carry.  ``insufficient`` is deliberately
#: distinct from ``no_events_observed``: a missing follow-up is not evidence
#: that nothing happened.
EVENTS_OBSERVED = "events_observed"
NO_EVENTS_OBSERVED = "no_events_observed"
INSUFFICIENT = "insufficient"
EXCLUDED = "excluded"

INCLUDED = "included"
OUT_OF_SCOPE = "out_of_scope"
INSUFFICIENT_BASIS = "insufficient_basis"

INTEGRITY_COMPLETE = "complete"
INTEGRITY_CHANGED = "changed"
INTEGRITY_WITHDRAWN = "withdrawn"
INTEGRITY_INCOMPLETE = "incomplete"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ScopeRevision:
    study_id: str
    revision: int
    consent_scopes: tuple[str, ...]
    valid_from: date
    valid_to: date
    source_id: str
    payload: dict[str, Any]
    withdrawn: bool = False

    def covers_scope(self, claimed: Iterable[str]) -> bool:
        owned = set(self.consent_scopes)
        if "*" in owned or "all" in owned:
            return True
        return bool(owned.intersection(claimed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_id": self.study_id,
            "revision": self.revision,
            "consent_scopes": list(self.consent_scopes),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "source_id": self.source_id,
            "withdrawn": self.withdrawn,
        }


@dataclass
class SubmissionState:
    submission_id: str
    study_id: str
    source_id: str
    revision: int
    collected_at: datetime
    payload: dict[str, Any]
    site_ref: str | None
    participant_ref: str | None
    period_raw: str | None
    period: tuple[date, date] | None
    consent_revision: int | None
    claimed_scopes: tuple[str, ...]
    corrections: list[dict[str, Any]] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    correction_source_ids: list[str] = field(default_factory=list)
    withdrawn: bool = False

    @property
    def period_label(self) -> str:
        if self.period is None:
            return "unknown"
        start, end = self.period
        if start == end:
            return start.isoformat()
        return f"{start.isoformat()}..{end.isoformat()}"


@dataclass
class FollowUpState:
    submission_id: str
    source_id: str
    collected_at: datetime
    payload: dict[str, Any]
    events_observed: int | None
    data_complete: bool
    window: tuple[date, date] | None
    withdrawn: bool = False


@dataclass
class DeviceLink:
    submission_id: str
    source_id: str
    device_ref: str
    lot_ref: str
    used_at: date | None
    collected_at: datetime
    withdrawn: bool = False
    provable: bool = False
    issues: list[str] = field(default_factory=list)
    lot_source_id: str | None = None
    lot_revision: int | None = None
    lot_device_ref: str | None = None
    lot_effective_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "device_ref": self.device_ref,
            "lot_ref": self.lot_ref,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "provable": self.provable,
            "issues": list(self.issues),
            "lot_source_id": self.lot_source_id,
            "lot_revision": self.lot_revision,
            "lot_device_ref": self.lot_device_ref,
            "lot_effective_ref": self.lot_effective_ref,
            "withdrawn": self.withdrawn,
        }


@dataclass
class LotState:
    lot_ref: str
    effective_lot_ref: str
    device_ref: str | None
    line_ref: str | None
    state: str | None
    revision: int
    source_id: str
    collected_at: datetime
    payload: dict[str, Any]
    previous_lot_refs: list[str] = field(default_factory=list)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    withdrawn: bool = False
    detached_links: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_ref": self.lot_ref,
            "effective_lot_ref": self.effective_lot_ref,
            "device_ref": self.device_ref,
            "line_ref": self.line_ref,
            "state": self.state,
            "revision": self.revision,
            "source_id": self.source_id,
            "previous_lot_refs": list(self.previous_lot_refs),
            "corrections": list(self.corrections),
            "withdrawn": self.withdrawn,
            "detached_links": list(self.detached_links),
        }


@dataclass
class InclusionBasis:
    status: str
    scope: ScopeRevision | None
    claimed_scopes: tuple[str, ...]
    coverage: str
    checks: list[Check]
    missing: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "consent_revision": self.scope.revision if self.scope else None,
            "consent_scope": list(self.claimed_scopes),
            "valid_window": (
                {
                    "from": self.scope.valid_from.isoformat(),
                    "to": self.scope.valid_to.isoformat(),
                }
                if self.scope
                else None
            ),
            "coverage": self.coverage,
            "checks": [check.to_dict() for check in self.checks],
            "missing": list(self.missing),
        }


@dataclass
class ResearchRecord:
    submission_id: str
    study_id: str
    site_ref: str | None
    participant_ref: str | None
    period: str
    revision: int
    submission_source_id: str
    inclusion: InclusionBasis
    conclusion: dict[str, Any]
    missing: list[str]
    device_links: list[DeviceLink]
    traceable: bool
    withdrawn: bool
    withdrawal_reasons: list[str]
    corrections: list[dict[str, Any]]
    evidence_version: str
    component_source_ids: list[str]
    evidence_bundle: dict[str, Any]

    @property
    def usable(self) -> bool:
        return (
            self.inclusion.status == INCLUDED
            and not self.withdrawn
            and self.conclusion["status"] in {EVENTS_OBSERVED, NO_EVENTS_OBSERVED}
        )

    def to_dict(self, include_bundle: bool = False) -> dict[str, Any]:
        data = {
            "submission_id": self.submission_id,
            "study_id": self.study_id,
            "site_ref": self.site_ref,
            "participant_ref": self.participant_ref,
            "period": self.period,
            "revision": self.revision,
            "submission_source_id": self.submission_source_id,
            "inclusion": self.inclusion.to_dict(),
            "conclusion": self.conclusion,
            "missing": list(self.missing),
            "device_links": [link.to_dict() for link in self.device_links],
            "traceable": self.traceable,
            "withdrawn": self.withdrawn,
            "withdrawal_reasons": list(self.withdrawal_reasons),
            "corrections": list(self.corrections),
            "evidence_version": self.evidence_version,
            "usable": self.usable,
        }
        if include_bundle:
            data["evidence_bundle"] = self.evidence_bundle
        return data


@dataclass
class WithdrawalRecord:
    source_id: str
    target: str
    target_kind: str
    reason_code: str | None
    scope: str | None
    collected_at: datetime
    resolved: bool
    affected_source_ids: list[str]
    affected_submissions: list[str]
    affected_lots: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target": self.target,
            "target_kind": self.target_kind,
            "reason_code": self.reason_code,
            "scope": self.scope,
            "collected_at": self.collected_at.isoformat(),
            "resolved": self.resolved,
            "affected_source_ids": list(self.affected_source_ids),
            "affected_submissions": list(self.affected_submissions),
            "affected_lots": list(self.affected_lots),
        }


@dataclass
class CoreResolution:
    cutoff: datetime | None
    scopes: dict[str, list[ScopeRevision]]
    submissions: dict[str, SubmissionState]
    follow_ups: dict[str, FollowUpState]
    lots: dict[str, LotState]
    research: dict[str, ResearchRecord]
    withdrawals: list[WithdrawalRecord]
    withdrawn_source_ids: set[str]

    def scope_for(self, study_id: str, revision: int | None) -> ScopeRevision | None:
        if revision is None:
            return None
        for scope in self.scopes.get(study_id, []):
            if scope.revision == revision:
                return scope
        return None


# ---------------------------------------------------------------------------
# helpers


def _latest_per_source(
    records: Iterable[SourceRecord], cutoff: datetime | None
) -> list[SourceRecord]:
    latest: dict[str, SourceRecord] = {}
    for record in records:
        if cutoff is not None and record.collected_at > cutoff:
            continue
        prior = latest.get(record.source_id)
        if prior is None or record.collected_at >= prior.collected_at:
            latest[record.source_id] = record
    return list(latest.values())


def _records_of_kind(
    records: Iterable[SourceRecord], kind: str, cutoff: datetime | None
) -> list[SourceRecord]:
    return [
        record
        for record in records
        if record.record_kind == kind
        and (cutoff is None or record.collected_at <= cutoff)
    ]


def _scopes_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item)
    return (str(value),)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scope_requires_device(scope: ScopeRevision) -> bool:
    declared = scope.payload.get("require_device_traceability")
    if declared is not None:
        return bool(declared)
    return bool({"device-follow-up", "device-use", "device"} & set(scope.consent_scopes))


def _scope_requires_follow_up(scope: ScopeRevision) -> bool:
    declared = scope.payload.get("require_follow_up")
    if declared is not None:
        return bool(declared)
    return bool({"device-follow-up", "device-use", "device"} & set(scope.consent_scopes))


def _apply_changes(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        merged[key] = value
    return merged


@dataclass
class Observation:
    """Result of turning a submission + follow-up into a conclusion."""

    conclusion: dict[str, Any]
    missing: list[str]


def assess_inclusion(state: SubmissionState, scope: ScopeRevision | None) -> InclusionBasis:
    """Decide whether consent scope and time allow this record into analysis.

    This is strictly about *inclusion*: consent basis, claimed scope and the
    reporting window.  Whether the record is complete enough to support a
    conclusion is handled separately, so "out of scope" and "not enough data"
    never get conflated.
    """

    claimed = state.claimed_scopes or (scope.consent_scopes if scope else ())
    checks: list[Check] = []
    missing: list[str] = []
    coverage = "none"
    status = INCLUDED

    if state.consent_revision is None:
        missing.append("consent_revision")
        checks.append(
            Check("consent_revision_present", False, "submission cites no consent revision")
        )
    else:
        checks.append(
            Check(
                "consent_revision_present",
                True,
                f"submission cites consent revision {state.consent_revision}",
            )
        )

    if scope is None:
        status = INSUFFICIENT_BASIS
        missing.append("consent_basis")
        checks.append(
            Check(
                "consent_basis_available",
                False,
                "no study scope revision matches the cited consent revision",
            )
        )
        return InclusionBasis(
            status=status,
            scope=None,
            claimed_scopes=tuple(claimed),
            coverage=coverage,
            checks=checks,
            missing=missing,
        )

    checks.append(
        Check(
            "consent_basis_available",
            True,
            f"scope revision {scope.revision} ({scope.source_id})",
        )
    )
    if scope.withdrawn:
        status = INSUFFICIENT_BASIS
        missing.append("consent_basis_withdrawn")
        checks.append(Check("consent_basis_active", False, "consent basis withdrawn"))
    else:
        checks.append(Check("consent_basis_active", True, "consent basis in force"))

    if claimed and not scope.covers_scope(claimed):
        status = OUT_OF_SCOPE
        checks.append(
            Check(
                "consent_scope_covers",
                False,
                f"claimed {sorted(claimed)} not covered by {sorted(scope.consent_scopes)}",
            )
        )
    else:
        checks.append(
            Check("consent_scope_covers", True, f"within {sorted(scope.consent_scopes)}")
        )

    if state.period is None:
        missing.append("reporting_period")
        coverage = "unknown"
        checks.append(Check("consent_window_covers", False, "no reporting period"))
    elif not ranges_overlap(state.period, (scope.valid_from, scope.valid_to)):
        status = OUT_OF_SCOPE
        coverage = "none"
        checks.append(
            Check(
                "consent_window_covers",
                False,
                f"period {state.period_label} outside "
                f"{scope.valid_from}..{scope.valid_to}",
            )
        )
    else:
        fully = state.period[0] >= scope.valid_from and state.period[1] <= scope.valid_to
        coverage = "full" if fully else "partial"
        checks.append(
            Check(
                "consent_window_covers",
                True,
                f"period {state.period_label} ({coverage}) within "
                f"{scope.valid_from}..{scope.valid_to}",
            )
        )
        if not fully:
            missing.append("full_consent_coverage")

    return InclusionBasis(
        status=status,
        scope=scope,
        claimed_scopes=tuple(claimed),
        coverage=coverage,
        checks=checks,
        missing=missing,
    )


def assess_traceability(
    scope: ScopeRevision | None,
    links: list[DeviceLink],
    inclusion: InclusionBasis,
) -> bool:
    """Return whether every required device/lot association is provable.

    A link is provable only when it is active and its ``device_ref`` and
    ``lot_ref`` exactly match the resolved lot.  Similar names never count.
    """

    if scope is not None and not _scope_requires_device(scope):
        if not links:
            inclusion.checks.append(
                Check("device_traceable", True, "device traceability not required by scope")
            )
            return True
    if not links:
        inclusion.missing.append("device_use_record")
        inclusion.checks.append(
            Check("device_traceable", False, "no device use record for required traceability")
        )
        return False
    unprovable = [link for link in links if not link.provable]
    if unprovable:
        inclusion.missing.append("device_lot_link_unproven")
        inclusion.checks.append(
            Check(
                "device_traceable",
                False,
                "; ".join(
                    f"{link.device_ref}/{link.lot_ref}: "
                    f"{','.join(link.issues) or 'unproven'}"
                    for link in unprovable
                ),
            )
        )
        return False
    inclusion.checks.append(
        Check(
            "device_traceable",
            True,
            "; ".join(
                f"{link.device_ref} -> {link.lot_ref} rev {link.lot_revision}"
                for link in links
            ),
        )
    )
    return True


def determine_conclusion(
    state: SubmissionState,
    scope: ScopeRevision | None,
    inclusion: InclusionBasis,
    follow_up: FollowUpState | None,
    links: list[DeviceLink],
    traceable: bool,
) -> tuple[dict[str, Any], Observation]:
    """Derive the observation status, keeping "missing" apart from "zero"."""

    missing: list[str] = []
    requires_follow_up = _scope_requires_follow_up(scope) if scope else True

    observation_source: str | None = None
    events_observed: int | None = None
    data_complete = True
    if follow_up is not None and not follow_up.withdrawn:
        observation_source = follow_up.source_id
        events_observed = follow_up.events_observed
        data_complete = follow_up.data_complete
    elif follow_up is not None and follow_up.withdrawn:
        # A withdrawn follow-up cannot support any conclusion; leave the
        # observation absent rather than defaulting to zero.
        observation_source = follow_up.source_id
    else:
        events_observed = _int_or_none(
            state.payload.get("events_observed", state.payload.get("outcome_events"))
        )
        declared = state.payload.get("data_complete")
        data_complete = True if declared is None else bool(declared)

    withdrawn_reasons = collect_withdrawal_reasons(state, follow_up, links)

    if inclusion.status != INCLUDED:
        conclusion = {
            "status": EXCLUDED,
            "detail": f"record not includable ({inclusion.status})",
            "source": observation_source,
        }
        return conclusion, Observation(conclusion, missing)
    if withdrawn_reasons:
        conclusion = {
            "status": INSUFFICIENT,
            "detail": "critical source withdrawn: " + ", ".join(sorted(withdrawn_reasons)),
            "source": observation_source,
        }
        return conclusion, Observation(conclusion, list(withdrawn_reasons))
    if requires_follow_up and follow_up is None:
        missing.append("follow_up")
        conclusion = {
            "status": INSUFFICIENT,
            "detail": "no follow-up submitted; absence of data is not zero events",
            "source": None,
        }
        return conclusion, Observation(conclusion, missing)
    if not traceable:
        # ``assess_traceability`` already recorded the specific reason
        # (missing record vs. unprovable link) on the inclusion basis.
        conclusion = {
            "status": INSUFFICIENT,
            "detail": "device lot association not provable",
            "source": observation_source,
        }
        return conclusion, Observation(conclusion, missing)
    if not data_complete:
        missing.append("observation_incomplete")
        conclusion = {
            "status": INSUFFICIENT,
            "detail": "observation flagged incomplete",
            "source": observation_source,
        }
        return conclusion, Observation(conclusion, missing)
    if events_observed is None:
        missing.append("outcome_observation")
        conclusion = {
            "status": INSUFFICIENT,
            "detail": "no explicit event observation; not equivalent to no events",
            "source": None,
        }
        return conclusion, Observation(conclusion, missing)
    if events_observed == 0:
        conclusion = {
            "status": NO_EVENTS_OBSERVED,
            "detail": "explicitly zero events observed in the follow-up window",
            "source": observation_source,
        }
        return conclusion, Observation(conclusion, missing)
    conclusion = {
        "status": EVENTS_OBSERVED,
        "detail": f"{events_observed} event(s) observed",
        "source": observation_source,
    }
    return conclusion, Observation(conclusion, missing)


def collect_withdrawal_reasons(
    state: SubmissionState,
    follow_up: FollowUpState | None,
    links: list[DeviceLink],
) -> list[str]:
    reasons: set[str] = set()
    if state.withdrawn:
        reasons.add("submission_withdrawn")
    if follow_up is not None and follow_up.withdrawn:
        reasons.add("follow_up_withdrawn")
    for link in links:
        if link.withdrawn:
            reasons.add("device_use_withdrawn")
        if "lot_withdrawn" in link.issues:
            reasons.add("lot_withdrawn")
    return sorted(reasons)


def build_evidence_bundle(
    state: SubmissionState,
    scope: ScopeRevision | None,
    follow_up: FollowUpState | None,
    links: list[DeviceLink],
    withdrawal_reasons: list[str],
) -> dict[str, Any]:
    return {
        "study_id": state.study_id,
        "submission_id": state.submission_id,
        "submission": {
            "source_id": state.source_id,
            "revision": state.revision,
            "payload": _stable_payload(state.payload),
        },
        "corrections": state.corrections,
        "follow_up": (
            {
                "source_id": follow_up.source_id,
                "events_observed": follow_up.events_observed,
                "data_complete": follow_up.data_complete,
                "window": (
                    [follow_up.window[0].isoformat(), follow_up.window[1].isoformat()]
                    if follow_up.window
                    else None
                ),
                "withdrawn": follow_up.withdrawn,
            }
            if follow_up
            else None
        ),
        "consent": (
            {
                "source_id": scope.source_id,
                "revision": scope.revision,
                "consent_scopes": list(scope.consent_scopes),
                "valid_from": scope.valid_from.isoformat(),
                "valid_to": scope.valid_to.isoformat(),
                "withdrawn": scope.withdrawn,
            }
            if scope
            else None
        ),
        "device_links": [
            {
                "source_id": link.source_id,
                "device_ref": link.device_ref,
                "lot_ref": link.lot_ref,
                "lot_effective_ref": link.lot_effective_ref,
                "lot_revision": link.lot_revision,
                "provable": link.provable,
                "issues": list(link.issues),
                "withdrawn": link.withdrawn,
            }
            for link in links
        ],
        "withdrawals": sorted(withdrawal_reasons),
    }


def _component_source_ids(
    state: SubmissionState,
    scope: ScopeRevision | None,
    follow_up: FollowUpState | None,
    links: list[DeviceLink],
) -> list[str]:
    return sorted(
        {
            state.source_id,
            *state.source_ids,
            *([scope.source_id] if scope else []),
            *([follow_up.source_id] if follow_up else []),
            *[link.source_id for link in links],
            *[link.lot_source_id for link in links if link.lot_source_id],
        }
    )


# ---------------------------------------------------------------------------
# core builder


def build_core(records: list[SourceRecord], cutoff: datetime | None) -> CoreResolution:
    """Resolve the stream into a coherent state as of ``cutoff``."""

    active = _latest_per_source(records, cutoff)

    # Withdrawals that name a source directly can be known before anything is
    # assembled.  Resolving them first matters for *corrections*: a withdrawn
    # correction must not be applied.  Withdrawals that name a submission or a
    # lot are resolved later, once those objects exist.
    known_source_ids = {record.source_id for record in active}
    direct_withdrawn: set[str] = set()
    for record in _records_of_kind(active, "source_withdrawal", cutoff):
        target = str(record.get("target_source_id") or record.get("target") or "")
        if target in known_source_ids:
            direct_withdrawn.add(target)

    def _is_withdrawn(record: SourceRecord) -> bool:
        return record.source_id in direct_withdrawn

    # --- study scope revisions ---------------------------------------------
    scopes: dict[str, list[ScopeRevision]] = {}
    for record in sorted(
        _records_of_kind(active, "study_scope", cutoff),
        key=lambda r: (int(r.get("revision", 0)), r.collected_at),
    ):
        study_id = record.get("study_id")
        revision = _int_or_none(record.get("revision"))
        if study_id is None or revision is None:
            continue
        scope = ScopeRevision(
            study_id=study_id,
            revision=revision,
            consent_scopes=_scopes_text(record.get("consent_scope")),
            valid_from=parse_date(record.get("valid_from")),
            valid_to=parse_date(record.get("valid_to")),
            source_id=record.source_id,
            payload=record.payload,
        )
        bucket = scopes.setdefault(study_id, [])
        replaced = [i for i, existing in enumerate(bucket) if existing.revision == revision]
        if replaced:
            bucket[replaced[0]] = scope
        else:
            bucket.append(scope)
    for bucket in scopes.values():
        bucket.sort(key=lambda item: item.revision)

    # --- submissions and corrections ---------------------------------------
    submission_source_records: dict[str, list[SourceRecord]] = {}
    for record in _records_of_kind(active, "site_submission", cutoff):
        submission_id = record.get("submission_id")
        if submission_id:
            submission_source_records.setdefault(submission_id, []).append(record)

    corrections_by_submission: dict[str, list[SourceRecord]] = {}
    # Remember which submission/lot each correction targets, including
    # withdrawn corrections, so a withdrawal report can name the affected
    # object even though the correction itself is no longer applied.
    correction_targets: dict[str, tuple[str, str]] = {}
    for record in sorted(
        _records_of_kind(active, "site_correction", cutoff),
        key=lambda r: (int(r.get("revision", 0)), r.collected_at),
    ):
        submission_id = record.get("submission_id")
        if not submission_id:
            continue
        correction_targets[record.source_id] = ("submission", submission_id)
        if not _is_withdrawn(record):
            corrections_by_submission.setdefault(submission_id, []).append(record)

    submissions: dict[str, SubmissionState] = {}
    for submission_id, versions in submission_source_records.items():
        versions.sort(key=lambda r: (int(r.get("revision", 0)), r.collected_at))
        base = versions[-1]
        corrections = corrections_by_submission.get(submission_id, [])
        merged_payload = dict(base.payload)
        correction_notes: list[dict[str, Any]] = []
        effective_revision = int(base.get("revision", 0))
        for correction in corrections:
            overwrite = {
                key: value
                for key, value in correction.payload.items()
                if key
                not in {
                    "submission_id",
                    "revision",
                    "change",
                    "reason_code",
                    "field",
                    "old_value",
                    "new_value",
                    "value",
                }
            }
            if correction.get("field") and correction.get("new_value") is not None:
                overwrite[str(correction.get("field"))] = correction.get("new_value")
            merged_payload = _apply_changes(merged_payload, overwrite)
            effective_revision = max(
                effective_revision, int(correction.get("revision", 0) or 0)
            )
            correction_notes.append(
                {
                    "source_id": correction.source_id,
                    "revision": _int_or_none(correction.get("revision")),
                    "change": correction.get("change"),
                    "reason_code": correction.get("reason_code"),
                }
            )
        merged_payload["revision"] = effective_revision

        period_raw = merged_payload.get("period")
        if period_raw is None and merged_payload.get("period_start") and merged_payload.get(
            "period_end"
        ):
            period_raw = f"{merged_payload['period_start']}/{merged_payload['period_end']}"
        period: tuple[date, date] | None
        if period_raw is None:
            period = None
        elif "/" in str(period_raw):
            left, right = str(period_raw).split("/", 1)
            period = (parse_date(left), parse_date(right))
        else:
            period = parse_period(period_raw)

        claimed = _scopes_text(
            merged_payload.get("consent_scope") or merged_payload.get("scope")
        )
        submissions[submission_id] = SubmissionState(
            submission_id=submission_id,
            study_id=merged_payload.get("study_id"),
            source_id=base.source_id,
            revision=effective_revision,
            collected_at=base.collected_at,
            payload=merged_payload,
            site_ref=merged_payload.get("site_ref"),
            participant_ref=merged_payload.get("participant_ref"),
            period_raw=str(period_raw) if period_raw is not None else None,
            period=period,
            consent_revision=_int_or_none(merged_payload.get("consent_revision")),
            claimed_scopes=claimed,
            corrections=correction_notes,
            source_ids=[
                record.source_id for record in versions
            ]
            + [record.source_id for record in corrections],
            correction_source_ids=[record.source_id for record in corrections],
        )

    # --- follow-ups --------------------------------------------------------
    follow_ups: dict[str, FollowUpState] = {}
    for record in sorted(
        _records_of_kind(active, "site_follow_up", cutoff),
        key=lambda r: r.collected_at,
    ):
        submission_id = record.get("submission_id")
        if not submission_id:
            continue
        window = None
        if record.get("window_start") and record.get("window_end"):
            window = (parse_date(record.get("window_start")), parse_date(record.get("window_end")))
        elif record.get("window"):
            window = parse_period(record.get("window"))
        follow_ups[submission_id] = FollowUpState(
            submission_id=submission_id,
            source_id=record.source_id,
            collected_at=record.collected_at,
            payload=record.payload,
            events_observed=_int_or_none(record.get("events_observed")),
            data_complete=bool(record.get("data_complete", True)),
            window=window,
        )

    # --- lots and lot corrections ------------------------------------------
    lots: dict[str, LotState] = {}
    lot_corrections: dict[str, list[SourceRecord]] = {}
    for record in sorted(
        _records_of_kind(active, "lot_correction", cutoff),
        key=lambda r: (int(r.get("revision", 0)), r.collected_at),
    ):
        lot_ref = record.get("lot_ref")
        if not lot_ref:
            continue
        correction_targets[record.source_id] = ("lot", str(lot_ref))
        if not _is_withdrawn(record):
            lot_corrections.setdefault(lot_ref, []).append(record)

    for record in sorted(
        _records_of_kind(active, "production_lot", cutoff),
        key=lambda r: (int(r.get("revision", 0)), r.collected_at),
    ):
        lot_ref = record.get("lot_ref")
        if not lot_ref:
            continue
        payload = dict(record.payload)
        effective_lot_ref = payload.get("lot_ref", lot_ref)
        previous: list[str] = []
        notes: list[dict[str, Any]] = []
        revision = int(record.get("revision", 0))
        for correction in lot_corrections.get(lot_ref, []):
            changes: dict[str, Any] = {}
            if isinstance(correction.get("changes"), Mapping):
                changes.update(correction.get("changes"))
            if correction.get("field") and correction.get("new_value") is not None:
                changes[str(correction.get("field"))] = correction.get("new_value")
            if correction.get("field") and correction.get("value") is not None:
                changes[str(correction.get("field"))] = correction.get("value")
            for key, value in changes.items():
                if key == "lot_ref" and value != effective_lot_ref:
                    previous.append(str(effective_lot_ref))
                    effective_lot_ref = str(value)
            payload = _apply_changes(payload, changes)
            revision = max(revision, int(correction.get("revision", 0) or 0))
            notes.append(
                {
                    "source_id": correction.source_id,
                    "revision": _int_or_none(correction.get("revision")),
                    "changes": changes,
                    "reason_code": correction.get("reason_code"),
                }
            )
        payload["lot_ref"] = effective_lot_ref
        payload["revision"] = revision
        lots[lot_ref] = LotState(
            lot_ref=lot_ref,
            effective_lot_ref=effective_lot_ref,
            device_ref=payload.get("device_ref"),
            line_ref=payload.get("line_ref"),
            state=payload.get("state"),
            revision=revision,
            source_id=record.source_id,
            collected_at=record.collected_at,
            payload=payload,
            previous_lot_refs=previous,
            corrections=notes,
            source_ids=[record.source_id] + [c.source_id for c in lot_corrections.get(lot_ref, [])],
        )

    # Map effective lot ref -> lot state (a corrected lot is only reachable by
    # its *current* identity; old references are not auto-merged).
    lots_by_effective = {lot.effective_lot_ref: lot for lot in lots.values()}

    # --- withdrawals -------------------------------------------------------
    withdrawals: list[WithdrawalRecord] = []
    withdrawn_source_ids: set[str] = set()
    # A submission is fatal only if its *base* or an amendment is withdrawn;
    # withdrawing a correction removes that correction's effect, not the whole
    # submission.
    submission_sources = {
        sid: [state.source_id, *state.source_ids] for sid, state in submissions.items()
    }
    for record in sorted(
        _records_of_kind(active, "source_withdrawal", cutoff),
        key=lambda r: r.collected_at,
    ):
        target = str(record.get("target_source_id") or record.get("target") or "")
        declared_kind = record.get("target_kind")
        affected_sources: list[str] = []
        affected_submissions: list[str] = []
        affected_lots: list[str] = []
        resolved = False
        if target in known_source_ids:
            kind = "source"
            affected_sources = [target]
            resolved = True
            for sid, sources in submission_sources.items():
                if target in sources:
                    affected_submissions.append(sid)
            for lot in lots.values():
                if target in lot.source_ids:
                    affected_lots.append(lot.lot_ref)
            # A withdrawn correction still points at a real object; report it.
            correction_kind, correction_target = correction_targets.get(
                target, (None, None)
            )
            if correction_kind == "submission" and correction_target not in affected_submissions:
                affected_submissions.append(correction_target)
            elif correction_kind == "lot" and correction_target not in affected_lots:
                affected_lots.append(correction_target)
        elif target in submissions:
            kind = declared_kind or "submission"
            affected_submissions = [target]
            affected_sources = sorted(set(submission_sources.get(target, [])))
            resolved = True
        elif target in lots:
            kind = declared_kind or "lot"
            affected_lots = [target]
            affected_sources = sorted(set(lots[target].source_ids))
            resolved = True
        else:
            kind = declared_kind or "unknown"
        if declared_kind:
            kind = str(declared_kind)
        withdrawn_source_ids.update(affected_sources)
        withdrawals.append(
            WithdrawalRecord(
                source_id=record.source_id,
                target=target,
                target_kind=kind,
                reason_code=record.get("reason_code"),
                scope=record.get("scope"),
                collected_at=record.collected_at,
                resolved=resolved,
                affected_source_ids=affected_sources,
                affected_submissions=affected_submissions,
                affected_lots=affected_lots,
            )
        )

    for scope_list in scopes.values():
        for scope in scope_list:
            scope.withdrawn = scope.source_id in withdrawn_source_ids
    for state in submissions.values():
        # Only the submission itself (base or a later submission revision) is
        # fatal; a withdrawn correction is simply not applied.
        primary = set(state.source_ids) - set(state.correction_source_ids)
        state.withdrawn = bool(primary & withdrawn_source_ids)
    for state in follow_ups.values():
        state.withdrawn = state.source_id in withdrawn_source_ids
    for lot in lots.values():
        lot.withdrawn = bool(set(lot.source_ids) & withdrawn_source_ids)

    # --- device links ------------------------------------------------------
    links_by_submission: dict[str, list[DeviceLink]] = {}
    for record in sorted(
        _records_of_kind(active, "device_use", cutoff),
        key=lambda r: r.collected_at,
    ):
        submission_id = record.get("submission_id")
        if not submission_id:
            continue
        lot_ref = record.get("device_ref") and record.get("lot_ref")
        used_at = record.get("used_at")
        link = DeviceLink(
            submission_id=submission_id,
            source_id=record.source_id,
            device_ref=record.get("device_ref"),
            lot_ref=record.get("lot_ref"),
            used_at=parse_date(used_at) if used_at else None,
            collected_at=record.collected_at,
        )
        link.withdrawn = record.source_id in withdrawn_source_ids
        lot = lots_by_effective.get(link.lot_ref)
        if lot is None:
            link.issues.append("lot_not_found")
        else:
            link.lot_source_id = lot.source_id
            link.lot_revision = lot.revision
            link.lot_device_ref = lot.device_ref
            link.lot_effective_ref = lot.effective_lot_ref
            if lot.withdrawn:
                link.issues.append("lot_withdrawn")
            if link.device_ref != lot.device_ref:
                link.issues.append("device_ref_mismatch")
            if link.lot_ref != lot.effective_lot_ref:
                link.issues.append("lot_ref_mismatch")
        link.provable = (
            not link.withdrawn
            and lot is not None
            and not lot.withdrawn
            and link.device_ref == lot.device_ref
            and link.lot_ref == lot.effective_lot_ref
        )
        # A link that names this lot_ref but no longer matches the resolved
        # lot is recorded on the lot so production can see the detachment.
        for candidate in lots.values():
            if candidate.lot_ref == link.lot_ref and not link.provable:
                candidate.detached_links.append(
                    {
                        "source_id": link.source_id,
                        "submission_id": link.submission_id,
                        "device_ref": link.device_ref,
                        "issues": list(link.issues),
                    }
                )
        links_by_submission.setdefault(submission_id, []).append(link)

    # --- assemble research records -----------------------------------------
    research: dict[str, ResearchRecord] = {}
    for submission_id, state in submissions.items():
        scope = next(
            (
                candidate
                for candidate in scopes.get(state.study_id, [])
                if candidate.revision == state.consent_revision
            ),
            None,
        )
        links = links_by_submission.get(submission_id, [])
        follow_up = follow_ups.get(submission_id)
        inclusion = assess_inclusion(state, scope)
        traceable = assess_traceability(scope, links, inclusion)
        conclusion, observation = determine_conclusion(
            state,
            scope,
            inclusion,
            follow_up,
            links,
            traceable,
        )
        withdrawal_reasons = collect_withdrawal_reasons(state, follow_up, links)
        if state.withdrawn:
            withdrawal_reasons = sorted(set(withdrawal_reasons) | {"submission_withdrawn"})

        bundle = build_evidence_bundle(
            state, scope, follow_up, links, withdrawal_reasons
        )
        research[submission_id] = ResearchRecord(
            submission_id=submission_id,
            study_id=state.study_id,
            site_ref=state.site_ref,
            participant_ref=state.participant_ref,
            period=state.period_label,
            revision=state.revision,
            submission_source_id=state.source_id,
            inclusion=inclusion,
            conclusion=conclusion,
            missing=sorted(set(inclusion.missing) | set(observation.missing)),
            device_links=links,
            traceable=traceable,
            withdrawn=bool(withdrawal_reasons),
            withdrawal_reasons=sorted(set(withdrawal_reasons)),
            corrections=list(state.corrections),
            evidence_version=short_hash(bundle),
            component_source_ids=_component_source_ids(state, scope, follow_up, links),
            evidence_bundle=bundle,
        )

    return CoreResolution(
        cutoff=cutoff,
        scopes=scopes,
        submissions=submissions,
        follow_ups=follow_ups,
        lots=lots,
        research=research,
        withdrawals=withdrawals,
        withdrawn_source_ids=withdrawn_source_ids,
    )


def _stable_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-round-tripped payload so hashing never sees live objects."""

    import json

    return json.loads(canonical_json(payload))


def _submission_ids_from_refs(payload: Mapping[str, Any]) -> list[str]:
    raw = (
        payload.get("evidence_refs")
        or payload.get("submission_ids")
        or payload.get("evidence_source_ids")
        or []
    )
    refs: list[str] = []
    for item in raw:
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, Mapping):
            value = (
                item.get("submission_id")
                or item.get("ref")
                or item.get("id")
            )
            if value:
                refs.append(str(value))
    return refs


@dataclass
class EvaluationView:
    evaluation_id: str
    study_id: str
    revision: int
    declared_status: str
    conclusion: Any
    decided_at: str | None
    evidence_refs: list[str]
    basis_versions: dict[str, str | None]
    current_versions: dict[str, str | None]
    changed_refs: list[str]
    withdrawn_refs: list[str]
    missing_refs: list[str]
    integrity: str
    needs_review: bool
    review_scope: dict[str, Any]
    usable_as_complete_evidence: bool
    history_preserved: bool
    source_id: str
    basis_cutoff: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "study_id": self.study_id,
            "revision": self.revision,
            "declared_status": self.declared_status,
            "conclusion": self.conclusion,
            "decided_at": self.decided_at,
            "evidence_refs": list(self.evidence_refs),
            "basis_versions": dict(self.basis_versions),
            "current_versions": dict(self.current_versions),
            "changed_refs": list(self.changed_refs),
            "withdrawn_refs": list(self.withdrawn_refs),
            "missing_refs": list(self.missing_refs),
            "integrity": self.integrity,
            "needs_review": self.needs_review,
            "review_scope": self.review_scope,
            "usable_as_complete_evidence": self.usable_as_complete_evidence,
            "history_preserved": self.history_preserved,
            "source_id": self.source_id,
            "basis_cutoff": self.basis_cutoff,
        }


def _decided_cutoff(record: SourceRecord) -> datetime:
    raw = record.get("decided_at")
    if not raw:
        return record.collected_at
    if isinstance(raw, str) and "T" not in raw:
        # A date-only decision covers that whole day.
        end_of_day = datetime.fromisoformat(f"{raw[:10]}T23:59:59.999999+00:00")
        return end_of_day
    return parse_datetime(raw)


def build_evaluations(
    records: list[SourceRecord],
    cutoff: datetime | None,
    current: CoreResolution | None = None,
) -> dict[str, EvaluationView]:
    """Compute evaluation integrity against a frozen basis and current state."""

    if current is None:
        current = build_core(records, cutoff)
    views: dict[str, EvaluationView] = {}
    for record in sorted(
        _records_of_kind(_latest_per_source(records, cutoff), "evaluation_record", cutoff),
        key=lambda r: (int(r.get("revision", 0)), r.collected_at),
    ):
        evaluation_id = record.get("evaluation_id")
        if not evaluation_id:
            continue
        refs = _submission_ids_from_refs(record.payload)
        basis_cutoff = _decided_cutoff(record)
        basis_core = build_core(records, basis_cutoff)

        declared_fingerprints = record.get("evidence_fingerprints") or {}
        if not isinstance(declared_fingerprints, Mapping):
            declared_fingerprints = {}

        basis_versions: dict[str, str | None] = {}
        current_versions: dict[str, str | None] = {}
        changed: list[str] = []
        withdrawn: list[str] = []
        missing: list[str] = []

        for ref in refs:
            basis_record = basis_core.research.get(ref)
            current_record = current.research.get(ref)
            basis_version = None
            if ref in declared_fingerprints:
                basis_version = str(declared_fingerprints[ref])
            elif basis_record is not None:
                basis_version = basis_record.evidence_version
            current_version = (
                current_record.evidence_version if current_record is not None else None
            )
            basis_versions[ref] = basis_version
            current_versions[ref] = current_version
            if basis_version is None or basis_record is None:
                missing.append(ref)
            if current_record is None:
                if ref not in missing:
                    missing.append(ref)
            elif current_record.withdrawn:
                withdrawn.append(ref)
            elif basis_version is not None and current_version != basis_version:
                changed.append(ref)

        if withdrawn:
            integrity = INTEGRITY_WITHDRAWN
        elif missing:
            integrity = INTEGRITY_INCOMPLETE
        elif changed:
            integrity = INTEGRITY_CHANGED
        else:
            integrity = INTEGRITY_COMPLETE

        declared_status = str(record.get("status") or "draft").lower()
        published = declared_status == "published"
        needs_review = integrity != INTEGRITY_COMPLETE
        # An evaluation whose basis moved is no longer complete evidence for
        # *new* use, regardless of whether it was published.  Publication only
        # governs whether the historical conclusion is retained (it is, and is
        # flagged for review) rather than erased.
        usable = integrity == INTEGRITY_COMPLETE

        review_submissions: list[str] = sorted(set(withdrawn + changed + missing))
        review_lots: list[str] = []
        for ref in review_submissions:
            record_now = current.research.get(ref)
            if record_now is None:
                record_now = basis_core.research.get(ref)
            if record_now is None:
                continue
            for link in record_now.device_links:
                if link.lot_effective_ref and link.lot_effective_ref not in review_lots:
                    review_lots.append(link.lot_effective_ref)
        for ref in withdrawn:
            for withdrawal in current.withdrawals:
                if ref in withdrawal.affected_submissions:
                    review_lots.extend(
                        lot for lot in withdrawal.affected_lots if lot not in review_lots
                    )

        views[evaluation_id] = EvaluationView(
            evaluation_id=evaluation_id,
            study_id=record.get("study_id"),
            revision=int(record.get("revision", 0)),
            declared_status=declared_status,
            conclusion=record.get("conclusion"),
            decided_at=record.get("decided_at") or record.raw_collected_at,
            evidence_refs=refs,
            basis_versions=basis_versions,
            current_versions=current_versions,
            changed_refs=sorted(changed),
            withdrawn_refs=sorted(withdrawn),
            missing_refs=sorted(set(missing)),
            integrity=integrity,
            needs_review=needs_review,
            review_scope={
                "submissions": review_submissions,
                "lots": sorted(set(review_lots)),
            },
            usable_as_complete_evidence=usable,
            history_preserved=published,
            source_id=record.source_id,
            basis_cutoff=basis_cutoff.isoformat(),
        )
    return views


@dataclass
class EventSummary:
    """Aggregate over research records that keeps "missing" separate from zero.

    ``submissions_with_no_events_observed`` counts only records where a
    follow-up explicitly reported zero.  ``submissions_with_insufficient_data``
    counts everything else that cannot support a no-event claim, so the two
    numbers are never conflated.
    """

    total_submissions: int
    analysable: int
    events_observed: int
    no_events_observed: int
    insufficient_data: int
    excluded: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_submissions": self.total_submissions,
            "analysable": self.analysable,
            "submissions_with_events_observed": self.events_observed,
            "submissions_with_no_events_observed": self.no_events_observed,
            "submissions_with_insufficient_data": self.insufficient_data,
            "submissions_excluded": self.excluded,
        }


def build_event_summary(records: Iterable[Mapping[str, Any]]) -> EventSummary:
    total = 0
    analysable = 0
    events = 0
    no_events = 0
    insufficient = 0
    excluded = 0
    for record in records:
        total += 1
        conclusion = record.get("conclusion", {})
        status = conclusion.get("status")
        if status == EVENTS_OBSERVED:
            events += 1
            analysable += 1
        elif status == NO_EVENTS_OBSERVED:
            no_events += 1
            analysable += 1
        elif status == EXCLUDED:
            excluded += 1
        else:
            insufficient += 1
    return EventSummary(
        total_submissions=total,
        analysable=analysable,
        events_observed=events,
        no_events_observed=no_events,
        insufficient_data=insufficient,
        excluded=excluded,
    )


# ---------------------------------------------------------------------------
# production projection


@dataclass
class LotEvidence:
    submission_id: str
    evidence_version: str | None
    complete: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "evidence_version": self.evidence_version,
            "complete": self.complete,
            "reasons": list(self.reasons),
        }


@dataclass
class LotTrace:
    lot: LotState
    evidence: list[LotEvidence]
    evidence_version: str
    evidence_status: str
    all_evidence_complete: bool
    incomplete_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot": self.lot.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "evidence_version": self.evidence_version,
            "evidence_status": self.evidence_status,
            "all_evidence_complete": self.all_evidence_complete,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def build_lot_traces(core: CoreResolution) -> dict[str, LotTrace]:
    traces: dict[str, LotTrace] = {}
    # Group provable links by the lot they resolve to.
    links_by_lot: dict[str, list[DeviceLink]] = {}
    # For withdrawn lots the true link can no longer be provable (the lot is
    # gone), so also index by the lot reference each link *claims*.  This map
    # is used only to report which submissions a withdrawal affects.
    claimed_links_by_lot: dict[str, list[DeviceLink]] = {}
    for research in core.research.values():
        for link in research.device_links:
            if link.provable and link.lot_effective_ref:
                links_by_lot.setdefault(link.lot_effective_ref, []).append(link)
            if link.issues == ["lot_withdrawn"] and link.lot_ref:
                claimed_links_by_lot.setdefault(link.lot_ref, []).append(link)

    for lot_ref, lot in core.lots.items():
        if lot.withdrawn:
            # Keep the linked submissions visible even though the lot is no
            # longer complete evidence: production must be able to tell which
            # records the withdrawal affects.
            withdrawn_evidence: list[LotEvidence] = []
            withdrawn_links = claimed_links_by_lot.get(lot.effective_lot_ref, [])
            for link in sorted(withdrawn_links, key=lambda i: i.submission_id):
                research = core.research.get(link.submission_id)
                withdrawn_evidence.append(
                    LotEvidence(
                        submission_id=link.submission_id,
                        evidence_version=(
                            research.evidence_version if research else None
                        ),
                        complete=False,
                        reasons=["lot_withdrawn"],
                    )
                )
            traces[lot.effective_lot_ref] = LotTrace(
                lot=lot,
                evidence=withdrawn_evidence,
                evidence_version=short_hash(
                    {"lot": lot.payload, "withdrawn": True}
                ),
                evidence_status="withdrawn",
                all_evidence_complete=False,
                incomplete_reasons=["lot_withdrawn"],
            )
            continue
        evidence: list[LotEvidence] = []
        reasons: list[str] = []
        for link in sorted(
            links_by_lot.get(lot.effective_lot_ref, []),
            key=lambda item: item.submission_id,
        ):
            research = core.research.get(link.submission_id)
            if research is None:
                continue
            if research.usable:
                complete = True
                record_reasons: list[str] = []
            else:
                complete = False
                record_reasons = sorted(
                    set(research.missing)
                    | set(research.withdrawal_reasons)
                    | (
                        set()
                        if research.conclusion["status"]
                        in {EVENTS_OBSERVED, NO_EVENTS_OBSERVED}
                        else {research.conclusion["status"]}
                    )
                )
                for reason in record_reasons:
                    if reason not in reasons:
                        reasons.append(reason)
            evidence.append(
                LotEvidence(
                    submission_id=link.submission_id,
                    evidence_version=research.evidence_version,
                    complete=complete,
                    reasons=record_reasons,
                )
            )
        if lot.detached_links:
            for detached in lot.detached_links:
                reason = "association_not_provable"
                if reason not in reasons:
                    reasons.append(reason)
                # Name the evidence version that is no longer reachable so the
                # detached link is as traceable as a live one.
                detached_research = core.research.get(detached.get("submission_id"))
                if detached_research is not None:
                    detached["evidence_version"] = detached_research.evidence_version
                    detached["conclusion_status"] = detached_research.conclusion["status"]
        if not evidence and not lot.detached_links:
            reasons.append("no_linked_evidence")
        bundle = {
            "lot": {
                "lot_ref": lot.lot_ref,
                "effective_lot_ref": lot.effective_lot_ref,
                "device_ref": lot.device_ref,
                "revision": lot.revision,
                "state": lot.state,
            },
            "corrections": lot.corrections,
            "evidence": [
                {
                    "submission_id": item.submission_id,
                    "evidence_version": item.evidence_version,
                    "complete": item.complete,
                }
                for item in evidence
            ],
        }
        all_complete = (
            bool(evidence)
            and all(item.complete for item in evidence)
            and not lot.detached_links
        )
        if not evidence and not lot.detached_links:
            evidence_status = "untracked"
        elif all_complete:
            evidence_status = "complete"
        else:
            evidence_status = "incomplete"
        traces[lot.effective_lot_ref] = LotTrace(
            lot=lot,
            evidence=evidence,
            evidence_version=short_hash(bundle),
            evidence_status=evidence_status,
            all_evidence_complete=all_complete,
            incomplete_reasons=sorted(set(reasons)),
        )
    return traces


__all__ = [
    "Check",
    "CoreResolution",
    "DeviceLink",
    "EvaluationView",
    "EventSummary",
    "FollowUpState",
    "InclusionBasis",
    "INTEGRITY_CHANGED",
    "INTEGRITY_COMPLETE",
    "INTEGRITY_INCOMPLETE",
    "INTEGRITY_WITHDRAWN",
    "LotState",
    "LotTrace",
    "ResearchRecord",
    "ScopeRevision",
    "SubmissionState",
    "WithdrawalRecord",
    "build_core",
    "build_evaluations",
    "build_event_summary",
    "build_lot_traces",
]
