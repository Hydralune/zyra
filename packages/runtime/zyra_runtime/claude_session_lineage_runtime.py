from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso, to_jsonable


class SessionLineageStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class SessionLineageSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class SessionLineageSurface(StrEnum):
    INPUT_PROCESSOR = "input_processor"
    CONTEXT_ASSEMBLY = "context_assembly"
    SESSION_STORE = "session_store"
    SESSION_REPLAY = "session_replay"
    TURN_LIFECYCLE = "turn_lifecycle"
    TRANSCRIPT_MAPPING = "transcript_mapping"
    ACCEPTANCE = "acceptance"
    QUERY_ENGINE = "query_engine"
    WORKER_ENTRY = "worker_entry"


@dataclass(frozen=True, slots=True)
class SessionLineageTarget:
    path: str
    exists: bool
    surface: SessionLineageSurface
    required_for_default_path: bool
    effective_code: bool
    source_paths: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def clean_runtime_safe(self) -> bool:
        normalized = self.path.replace("\\", "/").lower()
        return not (
            normalized.startswith("vendor/")
            or normalized.startswith("vendor-runtimes/")
            or normalized.startswith("source-pool/")
            or normalized.startswith("runtime-sources/")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "surface": str(self.surface),
            "required_for_default_path": self.required_for_default_path,
            "effective_code": self.effective_code,
            "source_paths": list(self.source_paths),
            "clean_runtime_safe": self.clean_runtime_safe,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLineageFinding:
    code: str
    severity: SessionLineageSeverity
    surface: SessionLineageSurface
    message: str
    path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SessionLineageSeverity.BLOCKER

    @property
    def passed(self) -> bool:
        return self.severity == SessionLineageSeverity.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "path": self.path,
            "blocking": self.blocking,
            "passed": self.passed,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLineageReport:
    status: SessionLineageStatus
    project_root: str
    targets: tuple[SessionLineageTarget, ...]
    findings: tuple[SessionLineageFinding, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionLineageStatus.READY, SessionLineageStatus.DEGRADED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SessionLineageSeverity.WARNING)

    @property
    def pass_count(self) -> int:
        return sum(1 for finding in self.findings if finding.passed)

    @property
    def surface_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for target in self.targets:
            counts[str(target.surface)] = counts.get(str(target.surface), 0) + 1
        return counts

    def metadata_values(self) -> dict[str, str]:
        return {
            "session_lineage_ok": str(self.ok).lower(),
            "session_lineage_status": str(self.status),
            "session_lineage_targets": str(len(self.targets)),
            "session_lineage_blockers": str(self.blocker_count),
            "session_lineage_warnings": str(self.warning_count),
            "session_lineage_passes": str(self.pass_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": str(self.status),
            "project_root": self.project_root,
            "targets": [target.to_dict() for target in self.targets],
            "findings": [finding.to_dict() for finding in self.findings],
            "surface_counts": self.surface_counts,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "pass_count": self.pass_count,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class SessionLineageRuntime:
    """Builds source-to-target lineage for query/session lifecycle modules."""

    def __init__(self, *, required_targets: Sequence[str] | None = None) -> None:
        self.required_targets = tuple(required_targets or default_session_lineage_targets())

    def build_report(self, *, project_root: str | Path, contracts: Any | None = None) -> SessionLineageReport:
        root = Path(project_root)
        source_to_target = list(getattr(contracts, "source_to_target", ()) or ())
        targets = list(self._targets_from_contracts(root, source_to_target))
        targets.extend(self._targets_from_required(root, targets))
        findings = list(self._findings(targets))
        status = lineage_status(findings)
        return SessionLineageReport(
            status=status,
            project_root=str(root),
            targets=tuple(sorted(targets, key=lambda item: item.path)),
            findings=tuple(findings),
            metadata={
                "contract_target_count": sum(len(getattr(item, "target_paths", ()) or ()) for item in source_to_target),
                "required_target_count": len(self.required_targets),
                "owner_unit": "M1-02B",
            },
        )

    def _targets_from_contracts(self, root: Path, source_to_target: Sequence[Any]) -> Iterable[SessionLineageTarget]:
        for item in source_to_target:
            source_path = str(getattr(item, "source_path", ""))
            surface = surface_for_target(source_path=source_path, target_path="")
            for target_path in getattr(item, "target_paths", ()) or ():
                target = str(target_path)
                yield SessionLineageTarget(
                    path=target,
                    exists=(root / target).exists(),
                    surface=surface_for_target(source_path=source_path, target_path=target),
                    required_for_default_path=bool(getattr(item, "required_for_default_path", True)),
                    effective_code=bool(getattr(item, "effective_code", True)),
                    source_paths=(source_path,),
                    metadata={
                        "decision": str(getattr(item, "decision", "")),
                        "owner_unit": str(getattr(item, "owner_unit", "")),
                        "capability": str(getattr(item, "capability", "")),
                        "surface": str(getattr(item, "surface", surface)),
                    },
                )

    def _targets_from_required(
        self,
        root: Path,
        existing: Sequence[SessionLineageTarget],
    ) -> Iterable[SessionLineageTarget]:
        existing_paths = {target.path for target in existing}
        for target_path in self.required_targets:
            if target_path in existing_paths:
                continue
            yield SessionLineageTarget(
                path=target_path,
                exists=(root / target_path).exists(),
                surface=surface_for_target(source_path="", target_path=target_path),
                required_for_default_path=True,
                effective_code=True,
                source_paths=(),
                metadata={"source": "session_lineage_required_targets"},
            )

    def _findings(self, targets: Sequence[SessionLineageTarget]) -> Iterable[SessionLineageFinding]:
        seen: set[str] = set()
        for target in targets:
            if target.path in seen:
                yield SessionLineageFinding(
                    code="duplicate_target",
                    severity=SessionLineageSeverity.WARNING,
                    surface=target.surface,
                    message="Target path appears more than once in session lineage.",
                    path=target.path,
                )
            seen.add(target.path)
            if target.required_for_default_path and not target.exists:
                yield SessionLineageFinding(
                    code="required_target_missing",
                    severity=SessionLineageSeverity.BLOCKER,
                    surface=target.surface,
                    message="Required query/session lifecycle target is missing.",
                    path=target.path,
                )
            elif target.exists:
                yield SessionLineageFinding(
                    code="target_exists",
                    severity=SessionLineageSeverity.PASS,
                    surface=target.surface,
                    message="Target exists in Zyra-owned module tree.",
                    path=target.path,
                )
            if not target.clean_runtime_safe:
                yield SessionLineageFinding(
                    code="target_not_clean_runtime_safe",
                    severity=SessionLineageSeverity.BLOCKER,
                    surface=target.surface,
                    message="Target path is vendor-like or source-pool-like and cannot count as internalized runtime code.",
                    path=target.path,
                )
            if not target.effective_code and target.required_for_default_path:
                yield SessionLineageFinding(
                    code="required_target_not_effective_code",
                    severity=SessionLineageSeverity.BLOCKER,
                    surface=target.surface,
                    message="Required default-path target is not marked as effective code.",
                    path=target.path,
                )


def surface_for_target(*, source_path: str, target_path: str) -> SessionLineageSurface:
    combined = f"{source_path} {target_path}".lower()
    if "input_processor" in combined or "processuserinput" in combined:
        return SessionLineageSurface.INPUT_PROCESSOR
    if "context_assembly" in combined or "querycontext" in combined or "context.ts" in combined:
        return SessionLineageSurface.CONTEXT_ASSEMBLY
    if "session_store" in combined or "sessionstorage" in combined:
        return SessionLineageSurface.SESSION_STORE
    if "session_replay" in combined or "sessionrestore" in combined:
        return SessionLineageSurface.SESSION_REPLAY
    if "turn_lifecycle" in combined:
        return SessionLineageSurface.TURN_LIFECYCLE
    if "transcript_event" in combined or "query_session.py" in combined:
        return SessionLineageSurface.TRANSCRIPT_MAPPING
    if "acceptance" in combined:
        return SessionLineageSurface.ACCEPTANCE
    if "query_engine" in combined or "queryengine" in combined:
        return SessionLineageSurface.QUERY_ENGINE
    if "code_worker_runtime" in combined:
        return SessionLineageSurface.WORKER_ENTRY
    return SessionLineageSurface.QUERY_ENGINE


def lineage_status(findings: Sequence[SessionLineageFinding]) -> SessionLineageStatus:
    if any(finding.blocking for finding in findings):
        return SessionLineageStatus.BLOCKED
    if any(finding.severity == SessionLineageSeverity.WARNING for finding in findings):
        return SessionLineageStatus.DEGRADED
    return SessionLineageStatus.READY


def default_session_lineage_targets() -> tuple[str, ...]:
    return (
        "packages/runtime/zyra_runtime/claude_input_processor.py",
        "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
        "packages/runtime/zyra_runtime/claude_session_store.py",
        "packages/runtime/zyra_runtime/claude_session_foundation_audit.py",
        "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
        "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
        "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
        "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
        "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
        "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
        "packages/runtime/zyra_runtime/claude_session_api_projection.py",
        "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
        "packages/workers/zyra_workers/code_worker_runtime.py",
    )


def session_lineage_metadata(report: SessionLineageReport | None) -> dict[str, str]:
    if report is None:
        return {
            "session_lineage_ok": "",
            "session_lineage_status": "",
            "session_lineage_targets": "0",
        }
    return report.metadata_values()


def render_session_lineage_markdown(report: SessionLineageReport) -> str:
    lines = [
        "# Session Lineage Report",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- targets: `{len(report.targets)}`",
        f"- blockers: `{report.blocker_count}`",
        f"- warnings: `{report.warning_count}`",
        "",
        "## Targets",
        "",
    ]
    for target in report.targets:
        lines.append(
            f"- `{target.surface}` `{target.path}` exists=`{str(target.exists).lower()}` "
            f"clean=`{str(target.clean_runtime_safe).lower()}`"
        )
    lines.extend(["", "## Findings", ""])
    for finding in report.findings:
        lines.append(f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.path}")
    return "\n".join(lines) + "\n"


def lineage_report_from_payload(payload: Mapping[str, Any]) -> SessionLineageReport:
    targets = tuple(
        SessionLineageTarget(
            path=str(item.get("path") or ""),
            exists=item.get("exists") is True,
            surface=_enum_or_default(SessionLineageSurface, item.get("surface"), SessionLineageSurface.QUERY_ENGINE),
            required_for_default_path=item.get("required_for_default_path") is not False,
            effective_code=item.get("effective_code") is not False,
            source_paths=tuple(str(source) for source in item.get("source_paths", []) if source),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("targets", [])
        if isinstance(item, Mapping)
    )
    findings = tuple(
        SessionLineageFinding(
            code=str(item.get("code") or ""),
            severity=_enum_or_default(SessionLineageSeverity, item.get("severity"), SessionLineageSeverity.INFO),
            surface=_enum_or_default(SessionLineageSurface, item.get("surface"), SessionLineageSurface.QUERY_ENGINE),
            message=str(item.get("message") or ""),
            path=str(item.get("path") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("findings", [])
        if isinstance(item, Mapping)
    )
    return SessionLineageReport(
        status=_enum_or_default(SessionLineageStatus, payload.get("status"), SessionLineageStatus.BLOCKED),
        project_root=str(payload.get("project_root") or ""),
        targets=targets,
        findings=findings,
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
