"""Small, dependency-free helpers shared across the workbench.

Everything here is deterministic: the same inputs must always produce the
same canonical JSON and therefore the same content hashes.  That property is
what lets the three views (ingestion, research, production) point at the very
same evidence version.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Iterator


class WorkbenchError(Exception):
    """Base class for expected, user-facing workbench failures."""


class ValidationError(WorkbenchError):
    """A source envelope is structurally unusable."""


def canonical_json(value: Any) -> str:
    """Stable JSON encoding used for hashing and comparisons."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 16) -> str:
    return content_hash(value)[:length]


def parse_datetime(raw: Any) -> datetime:
    """Parse an ISO-8601 timestamp.

    ``collected_at`` values must carry enough information to be ordered.  A
    date-only value is accepted and interpreted as midnight UTC so that mixed
    feeds remain usable.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("collected_at must be a non-empty ISO-8601 string")
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - message asserted in tests
        raise ValidationError(f"collected_at is not ISO-8601: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # Normalise to UTC so records from hospitals in different zones order
    # correctly regardless of the offset they report.
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_date(raw: Any) -> date:
    """Parse a calendar date, tolerating a full ISO timestamp."""

    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("expected an ISO date, got an empty value")
    text = raw.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValidationError(f"expected an ISO date, got {raw!r}") from exc


def parse_period(raw: Any) -> tuple[date, date]:
    """Return the inclusive ``(start, end)`` a reporting period covers.

    Supports ``YYYY``, ``YYYY-MM``, ``YYYY-Qn`` and ``YYYY-MM-DD``.  A period
    is how hospitals describe the window a summary or follow-up belongs to.
    """

    if isinstance(raw, datetime):
        return raw.date(), raw.date()
    if isinstance(raw, date):
        return raw, raw
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("expected a reporting period, got an empty value")
    text = raw.strip()
    if len(text) >= 10:
        single = parse_date(text)
        return single, single
    if len(text) == 4 and text.isdigit():
        year = int(text)
        return date(year, 1, 1), date(year, 12, 31)
    if len(text) == 7 and text[4] == "-":
        year, month = int(text[:4]), int(text[5:7])
        if not 1 <= month <= 12:
            raise ValidationError(f"invalid month in period {raw!r}")
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if len(text) == 7 and text[4] in "Qq" and text[5:].isdigit():
        year, quarter = int(text[:4]), int(text[5:])
        if not 1 <= quarter <= 4:
            raise ValidationError(f"invalid quarter in period {raw!r}")
        first = (quarter - 1) * 3 + 1
        last = first + 2
        return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])
    raise ValidationError(f"unrecognised reporting period {raw!r}")


def ranges_overlap(
    left: tuple[date, date],
    right: tuple[date, date],
) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def iter_pairs(items: list[Any]) -> Iterator[tuple[Any, Any]]:
    for index, item in enumerate(items):
        yield index, item
