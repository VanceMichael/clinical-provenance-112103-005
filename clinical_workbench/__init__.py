"""Clinical evidence workbench for synthetic RWE/production reconciliation.

Public entry points::

    from clinical_workbench import Workbench, ingest_file, research_view

The package is intentionally dependency-free and deterministic so that two
runs over the same sources always yield the same evidence versions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .model import (
    KNOWN_KINDS,
    ParseOutcome,
    SourceRecord,
    ValidationError,
    parse_sources,
)
from .resolver import (
    EVENTS_OBSERVED,
    EXCLUDED,
    INCLUDED,
    INSUFFICIENT,
    INTEGRITY_CHANGED,
    INTEGRITY_COMPLETE,
    INTEGRITY_INCOMPLETE,
    INTEGRITY_WITHDRAWN,
    NO_EVENTS_OBSERVED,
    OUT_OF_SCOPE,
    build_core,
    build_evaluations,
    build_event_summary,
    build_lot_traces,
)
from .util import canonical_json, content_hash, short_hash
from .views import (
    ingestion_view,
    production_view,
    research_view,
    summary_view,
)
from .workbench import Batch, Snapshot, Workbench

__version__ = "0.1.0"

__all__ = [
    "Batch",
    "EVENTS_OBSERVED",
    "EXCLUDED",
    "INCLUDED",
    "INSUFFICIENT",
    "INTEGRITY_CHANGED",
    "INTEGRITY_COMPLETE",
    "INTEGRITY_INCOMPLETE",
    "INTEGRITY_WITHDRAWN",
    "KNOWN_KINDS",
    "NO_EVENTS_OBSERVED",
    "OUT_OF_SCOPE",
    "ParseOutcome",
    "Snapshot",
    "SourceRecord",
    "ValidationError",
    "Workbench",
    "build_core",
    "build_evaluations",
    "build_event_summary",
    "build_lot_traces",
    "canonical_json",
    "content_hash",
    "ingestion_view",
    "load_document",
    "parse_sources",
    "production_view",
    "research_view",
    "short_hash",
    "summary_view",
]


def load_document(path: str | Path) -> dict[str, Any]:
    """Read a ``{"sources": [...]}`` JSON document from disk."""

    text = Path(path).read_text(encoding="utf-8")
    document = json.loads(text)
    if not isinstance(document, (Mapping, list)):
        raise ValidationError(f"{path}: document must be an object or list")
    return document
