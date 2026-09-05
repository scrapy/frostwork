"""Optional checks on extracted items; no framework imports and no changes to matching."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, Tuple

if TYPE_CHECKING:
    from .page import Item


class FieldProcessingError(ValueError):
    """A transform failed; ``__cause__`` is the original exception."""

    def __init__(self, field: str, selector: str, cause: Optional[Exception] = None) -> None:
        self.field = field
        self.selector = selector
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(f"Frostwork transform failed for field {field!r}, selector {selector!r}{detail}")


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str


class StatsCollector(Protocol):
    def inc_value(self, key: str, count: int = 1) -> None: ...


class ItemValidationError(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__("; ".join(f"{i.field}: {i.message}" for i in report.issues))


@dataclass
class ValidationReport:
    """Processed item and checks. Reuse ``item`` after checking ``ok`` to avoid rerunning transforms.

    States distinguish ``no_match``, ``matched_empty``, ``processed_empty``, ``processing_failed``
    and ``filled``. Required fields check the final value: a transform may supply a missing default.
    Whitespace is a value; use ``map=str.strip`` if your schema treats it as empty.
    """
    item: dict[str, Any]
    states: dict[str, str]
    issues: list[ValidationIssue]

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_for_status(self) -> None:
        if not self.ok:
            raise ItemValidationError(self)

    def record_stats(self, stats: StatsCollector, prefix: str = "frostwork") -> None:
        """Increment Scrapy-compatible counters, bounded by schema names (never row indices)."""
        stats.inc_value(f"{prefix}/items")
        stats.inc_value(f"{prefix}/{'valid' if self.ok else 'invalid'}")
        for name, state in self.states.items():
            stats.inc_value(f"{prefix}/fields/{name}/{state}")
        # Aggregate row issues by code: a long listing must not create unbounded stats keys.
        for issue in self.issues:
            stats.inc_value(f"{prefix}/issues/{issue.code}")


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return all(_empty(v) for v in value)
    if isinstance(value, (str, bytes, dict, set, frozenset)):
        return len(value) == 0
    return False  # zero and False are real values


def validate_item(
    item: Item, *, required: Iterable[str],
    counts: Optional[Mapping[str, Tuple[int, Optional[int]]]],
    group_required: Optional[Mapping[str, Iterable[str]]],
) -> ValidationReport:
    if isinstance(required, str):
        raise TypeError("required must be an iterable of field names, not a string")
    required = set(required)
    counts = counts or {}
    groups = {}
    for name, subfields in (group_required or {}).items():
        if isinstance(subfields, str):
            raise TypeError("group_required values must be iterables of subfield names, not strings")
        groups[name] = tuple(subfields)
    names = set(item._fields) | set(item._grouped)
    unknown = (required | set(counts) | set(groups)) - names
    if unknown:
        raise ValueError(f"unknown validation fields: {sorted(unknown)}")
    for name, bounds in counts.items():
        low, high = bounds
        if (type(low) is not int or low < 0 or
                (high is not None and (type(high) is not int or high < low))):
            raise ValueError(f"invalid count bounds for {name!r}: {bounds!r}")
        field = item._fields.get(name)
        full_column = (field.card[0] != "first" if field is not None else
                       name in item._group_specs and not item._group_specs[name][0])
        if not full_column:
            raise ValueError(f"count validation for {name!r} needs field_all, field_join or many; "
                             "a first-only declaration does not retain every match")
    for name, subfields in groups.items():
        if name not in item._group_specs:
            raise ValueError(f"group_required needs a Page.many/one group: {name!r}")
        missing = set(subfields) - set(item._group_specs[name][1])
        if missing:
            raise ValueError(f"unknown subfields of {name!r}: {sorted(missing)}")

    report = ValidationReport({}, {}, [])
    for name in [*item._fields, *item._grouped]:
        raw = item.get_all(name)
        try:
            value = item.value(name)
        except FieldProcessingError as exc:
            report.states[name] = "processing_failed"
            report.issues.append(ValidationIssue(name, "processing_failed", str(exc)))
            continue
        report.item[name] = value
        is_group = name in item._grouped
        state = ("no_match" if not raw else "filled" if is_group else "matched_empty" if _empty(raw) else
                 "processed_empty" if _empty(value) else "filled")
        report.states[name] = state
        if name in required and (not raw if is_group else _empty(value)):
            report.issues.append(ValidationIssue(name, "required", f"required value is empty ({state})"))
        if name in counts:
            low, high = counts[name]
            if len(raw) < low or (high is not None and len(raw) > high):
                report.issues.append(ValidationIssue(name, "count", f"matched {len(raw)}; expected "
                                                    f"{low}..{high if high is not None else 'unbounded'}"))
        for row_index, row in enumerate(raw if name in groups else []):
            for subfield in groups[name]:
                if _empty(row[subfield]):
                    report.issues.append(ValidationIssue(f"{name}[{row_index}].{subfield}", "required",
                                                        "required group value is empty"))
    return report
