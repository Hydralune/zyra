from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..models import SkillRevision, SkillSourceKind, utc_now


class SkillSourceState(StrEnum):
    CONFIGURED = "configured"
    SCANNING = "scanning"
    STAGED = "staged"
    ACTIVE = "active"
    DISABLED = "disabled"
    RELOAD_REJECTED = "reload_rejected"


@dataclass(frozen=True, slots=True)
class SkillSourceScan:
    source_id: str
    source_kind: SkillSourceKind
    state: SkillSourceState
    revisions: tuple[SkillRevision, ...]
    errors: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    scanned_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.state in {SkillSourceState.STAGED, SkillSourceState.ACTIVE} and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": str(self.source_kind),
            "state": str(self.state),
            "revisions": [item.to_dict() for item in self.revisions],
            "errors": [dict(item) for item in self.errors],
            "warnings": [dict(item) for item in self.warnings],
            "metadata": dict(self.metadata),
            "scanned_at": self.scanned_at,
        }


@runtime_checkable
class SkillSource(Protocol):
    source_id: str
    source_kind: SkillSourceKind

    def scan(self, *, generation: int) -> SkillSourceScan:
        """Return a complete staged snapshot or a rejected scan."""


def rejected_scan(
    source: SkillSource,
    error: Exception,
    *,
    metadata: dict[str, Any] | None = None,
) -> SkillSourceScan:
    code = str(getattr(error, "code", "skill_source_error"))
    detail = dict(getattr(error, "detail", {}) or {})
    return SkillSourceScan(
        source_id=source.source_id,
        source_kind=source.source_kind,
        state=SkillSourceState.RELOAD_REJECTED,
        revisions=(),
        errors=({"code": code, "message": str(error), "detail": detail},),
        metadata=dict(metadata or {}),
    )
