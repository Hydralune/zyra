from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_execution_timeline import ToolExecutionTimelineReport
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_session import ToolPermissionSessionReport
from .tool_runtime_result_context import ToolResultContextReport
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolSemanticEffectKind(StrEnum):
    FILE_READ_CONTEXT = "file_read_context"
    FILE_WRITE_WORKSPACE = "file_write_workspace"
    FILE_EDIT_WORKSPACE = "file_edit_workspace"
    SHELL_PERMISSION_PENDING = "shell_permission_pending"
    SCHEMA_ERROR_NO_SIDE_EFFECT = "schema_error_no_side_effect"
    BUDGET_EXTERNALIZED_CONTEXT = "budget_externalized_context"
    SESSION_TOOL_USE_EXECUTED = "session_tool_use_executed"
    RESULT_VISIBLE_TO_NEXT_TURN = "result_visible_to_next_turn"


class ToolSemanticEffectStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class ToolSemanticReportStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolSemanticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolSemanticSurface(StrEnum):
    WORKSPACE = "workspace"
    RECEIPT = "receipt"
    CONTEXT = "context"
    PERMISSION = "permission"
    BUDGET = "budget"
    SESSION_BRIDGE = "session_bridge"
    TIMELINE = "timeline"
    RESULT_CONTEXT = "result_context"


@dataclass(frozen=True, slots=True)
class ToolSemanticEffect:
    effect_id: str
    kind: ToolSemanticEffectKind
    status: ToolSemanticEffectStatus
    tool_call_id: str = ""
    tool_name: str = ""
    path: str = ""
    expected: str = ""
    observed: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status in {ToolSemanticEffectStatus.PASS, ToolSemanticEffectStatus.NOT_APPLICABLE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "kind": str(self.kind),
            "status": str(self.status),
            "ok": self.ok,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "path": self.path,
            "expected": self.expected,
            "observed": self.observed,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolSemanticFinding:
    code: str
    severity: ToolSemanticSeverity
    surface: ToolSemanticSurface
    message: str
    effect_id: str = ""
    tool_call_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolSemanticSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "effect_id": self.effect_id,
            "tool_call_id": self.tool_call_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSemanticEffectReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    workspace_root: str
    effects: tuple[ToolSemanticEffect, ...]
    findings: tuple[ToolSemanticFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolSemanticReportStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolSemanticReportStatus.BLOCKED
        if not self.effects:
            return ToolSemanticReportStatus.EMPTY
        if self.findings:
            return ToolSemanticReportStatus.DEGRADED
        return ToolSemanticReportStatus.READY

    @property
    def pass_count(self) -> int:
        return sum(1 for effect in self.effects if effect.status == ToolSemanticEffectStatus.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for effect in self.effects if effect.status == ToolSemanticEffectStatus.FAIL)

    @property
    def applicable_count(self) -> int:
        return sum(1 for effect in self.effects if effect.status != ToolSemanticEffectStatus.NOT_APPLICABLE)

    @property
    def effect_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for effect in self.effects:
            counts[str(effect.kind)] = counts.get(str(effect.kind), 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_semantic_effects.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "workspace_root": self.workspace_root,
            "ok": self.ok,
            "status": str(self.status),
            "effect_count": len(self.effects),
            "applicable_count": self.applicable_count,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "effect_counts": self.effect_counts,
            "effects": [effect.to_dict() for effect in self.effects],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_semantic_effect_report_id": self.report_id,
            "tool_semantic_effect_owner_unit": self.owner_unit,
            "tool_semantic_effect_runtime_id": self.runtime_id,
            "tool_semantic_effect_ok": str(self.ok).lower(),
            "tool_semantic_effect_status": str(self.status),
            "tool_semantic_effect_total": str(len(self.effects)),
            "tool_semantic_effect_applicable": str(self.applicable_count),
            "tool_semantic_effect_passed": str(self.pass_count),
            "tool_semantic_effect_failed": str(self.fail_count),
            "tool_semantic_effect_findings": str(len(self.findings)),
            **{f"tool_semantic_effect_count_{key.split('.')[-1]}": str(value) for key, value in self.effect_counts.items()},
        }


class ToolSemanticEffectRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        workspace_root: str | Path,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
        result_context_report: ToolResultContextReport | None,
        permission_session_report: ToolPermissionSessionReport | None,
        session_bridge_report: ToolSessionBridgeReport | None,
        timeline_report: ToolExecutionTimelineReport | None,
    ) -> ToolSemanticEffectReport:
        root = Path(workspace_root).resolve()
        context_index = _context_index(context_snapshots)
        result_index = _result_projection_index(result_context_report)
        permission_index = _permission_tool_ids(permission_session_report)
        effects: list[ToolSemanticEffect] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            effects.extend(self._effects_for_receipt(root, receipt, context_index, result_index, permission_index))
        effects.extend(self._session_bridge_effects(session_bridge_report, receipts))
        effects.extend(self._timeline_effects(timeline_report, receipts))
        findings = self._findings(effects)
        return ToolSemanticEffectReport(
            report_id=new_id("toolsemantic"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            workspace_root=str(root),
            effects=tuple(effects),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolSemanticEffectReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "tool_semantic_effects",
                    "tool_semantic_effects": report.to_dict(),
                }
            },
        )

    def _effects_for_receipt(
        self,
        workspace_root: Path,
        receipt: Mapping[str, Any],
        context_index: Mapping[str, Any],
        result_index: Mapping[str, Any],
        permission_tool_ids: set[str],
    ) -> list[ToolSemanticEffect]:
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        raw_result = receipt.get("raw_result") if isinstance(receipt.get("raw_result"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        tool_name = str(request.get("tool_name") or "")
        tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
        args = request.get("arguments") if isinstance(request.get("arguments"), Mapping) else {}
        ok = result.get("ok") is True
        error = str(result.get("error") or "")
        output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
        effects: list[ToolSemanticEffect] = []
        if tool_name == "file_read" and ok:
            path = str(raw_result.get("output", {}).get("relative_path") if isinstance(raw_result.get("output"), Mapping) else args.get("path") or "")
            context_hit = path in context_index.get("read_file_state", {}) or str(args.get("path") or "") in context_index.get("read_file_state", {})
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.FILE_READ_CONTEXT,
                    status=ToolSemanticEffectStatus.PASS if context_hit else ToolSemanticEffectStatus.FAIL,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    path=path or str(args.get("path") or ""),
                    expected="read_file_state updated for file_read",
                    observed="read_file_state updated" if context_hit else "read_file_state missing",
                    metadata={"content_chars": str(output.get("chars") or "")},
                )
            )
        if tool_name in {"file_write", "file_edit"}:
            requested_path = str(args.get("path") or "")
            resolved = _safe_workspace_path(workspace_root, requested_path)
            exists = resolved.exists() if resolved is not None else False
            expected_content = str(args.get("content") or args.get("new") or "")
            content_match = True
            if exists and expected_content and tool_name == "file_write":
                try:
                    content_match = resolved.read_text(encoding="utf-8") == expected_content
                except OSError:
                    content_match = False
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.FILE_WRITE_WORKSPACE if tool_name == "file_write" else ToolSemanticEffectKind.FILE_EDIT_WORKSPACE,
                    status=ToolSemanticEffectStatus.PASS if ok and exists and content_match else ToolSemanticEffectStatus.NOT_APPLICABLE if not ok else ToolSemanticEffectStatus.FAIL,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    path=str(resolved or requested_path),
                    expected="workspace mutation visible on disk",
                    observed=f"exists={str(exists).lower()}; content_match={str(content_match).lower()}",
                    metadata={"requested_path": requested_path},
                )
            )
        if tool_name == "shell" and error == "permission_required":
            blocked_paths = _shell_redirect_targets(workspace_root, str(args.get("command") or ""))
            blocked = all(not path.exists() for path in blocked_paths) if blocked_paths else True
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.SHELL_PERMISSION_PENDING,
                    status=ToolSemanticEffectStatus.PASS if tool_call_id in permission_tool_ids and blocked else ToolSemanticEffectStatus.FAIL,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    path=";".join(str(path) for path in blocked_paths),
                    expected="permission pending prevents shell side effect",
                    observed=f"permission_question={str(tool_call_id in permission_tool_ids).lower()}; redirected_files_absent={str(blocked).lower()}",
                    metadata={"command": str(args.get("command") or "")[:240]},
                )
            )
        if error == "schema_error":
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.SCHEMA_ERROR_NO_SIDE_EFFECT,
                    status=ToolSemanticEffectStatus.PASS,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    expected="schema error returns ToolResult before side effects",
                    observed="schema_error ToolResult emitted",
                    metadata={"schema_errors": str(len(output.get("schema_errors") or [])) if isinstance(output.get("schema_errors"), Sequence) else "0"},
                )
            )
        if decision.get("applied") is True:
            projection = result_index.get(tool_call_id)
            budget_ok = bool(projection and projection.get("raw_output_blocked") is True and projection.get("artifact_ids"))
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.BUDGET_EXTERNALIZED_CONTEXT,
                    status=ToolSemanticEffectStatus.PASS if budget_ok else ToolSemanticEffectStatus.FAIL,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    expected="budgeted result has context projection with artifact ref",
                    observed="budget projection ready" if budget_ok else "budget projection missing artifact/raw-output block",
                    metadata={"artifact_id": str(decision.get("artifact_id") or "")},
                )
            )
        projection = result_index.get(tool_call_id)
        if projection is not None:
            effects.append(
                ToolSemanticEffect(
                    effect_id=new_id("toolsemeffect"),
                    kind=ToolSemanticEffectKind.RESULT_VISIBLE_TO_NEXT_TURN,
                    status=ToolSemanticEffectStatus.PASS if projection.get("visible_to_next_turn") is True else ToolSemanticEffectStatus.FAIL,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    expected="tool result visible as next-turn session message",
                    observed=str(projection.get("visible_to_next_turn")),
                    metadata={"projection_id": str(projection.get("projection_id") or "")},
                )
            )
        return effects

    def _session_bridge_effects(
        self,
        session_bridge_report: ToolSessionBridgeReport | None,
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolSemanticEffect]:
        if session_bridge_report is None or session_bridge_report.valid_tool_use_count == 0:
            return []
        receipt_ids = {
            str((receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}).get("tool_call_id") or "")
            for receipt in receipts
            if isinstance(receipt, Mapping)
        }
        output: list[ToolSemanticEffect] = []
        for turn in session_bridge_report.turns:
            for tool_use in turn.valid_tool_uses:
                tool_call_id = tool_use.upstream_tool_use_id or tool_use.bridge_tool_use_id
                matched = tool_call_id in receipt_ids
                output.append(
                    ToolSemanticEffect(
                        effect_id=new_id("toolsemeffect"),
                        kind=ToolSemanticEffectKind.SESSION_TOOL_USE_EXECUTED,
                        status=ToolSemanticEffectStatus.PASS if matched else ToolSemanticEffectStatus.FAIL,
                        tool_call_id=tool_call_id,
                        tool_name=tool_use.tool_name,
                        expected="assistant tool_use reaches ToolExecutionRuntime receipt",
                        observed="receipt found" if matched else "receipt missing",
                        metadata={"bridge_tool_use_id": tool_use.bridge_tool_use_id, "origin": str(tool_use.origin)},
                    )
                )
        return output

    def _timeline_effects(
        self,
        timeline_report: ToolExecutionTimelineReport | None,
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolSemanticEffect]:
        if timeline_report is None:
            return []
        if not receipts:
            return []
        return [
            ToolSemanticEffect(
                effect_id=new_id("toolsemeffect"),
                kind=ToolSemanticEffectKind.RESULT_VISIBLE_TO_NEXT_TURN,
                status=ToolSemanticEffectStatus.PASS if timeline_report.ok and timeline_report.tool_call_count >= len(receipts) else ToolSemanticEffectStatus.FAIL,
                expected="timeline reconstructs all receipt tool calls",
                observed=f"timeline_tool_calls={timeline_report.tool_call_count}; receipts={len(receipts)}",
                metadata={"timeline_report_id": timeline_report.report_id},
            )
        ]

    def _findings(self, effects: Sequence[ToolSemanticEffect]) -> list[ToolSemanticFinding]:
        findings: list[ToolSemanticFinding] = []
        for effect in effects:
            if effect.status == ToolSemanticEffectStatus.FAIL:
                findings.append(
                    ToolSemanticFinding(
                        code="TOOL_SEMANTIC_EFFECT_FAILED",
                        severity=ToolSemanticSeverity.BLOCKER,
                        surface=_surface_for_effect(effect.kind),
                        message=f"Tool semantic effect failed: {effect.kind}.",
                        effect_id=effect.effect_id,
                        tool_call_id=effect.tool_call_id,
                        metadata={"expected": effect.expected, "observed": effect.observed, **effect.metadata},
                    )
                )
            elif effect.status == ToolSemanticEffectStatus.WARN:
                findings.append(
                    ToolSemanticFinding(
                        code="TOOL_SEMANTIC_EFFECT_WARN",
                        severity=ToolSemanticSeverity.WARNING,
                        surface=_surface_for_effect(effect.kind),
                        message=f"Tool semantic effect is degraded: {effect.kind}.",
                        effect_id=effect.effect_id,
                        tool_call_id=effect.tool_call_id,
                        metadata=effect.metadata,
                    )
                )
        return findings


def tool_semantic_effect_metadata(report: ToolSemanticEffectReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_semantic_effect_ok": "false", "tool_semantic_effect_total": "0"}
    return report.metadata()


def assert_tool_semantic_effects_ready(report: ToolSemanticEffectReport) -> None:
    if report.ok:
        return
    failures = ", ".join(f"{finding.code}:{finding.tool_call_id}" for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool semantic effects blocked: {failures or 'unknown'}")


def render_tool_semantic_effects_markdown(report: ToolSemanticEffectReport) -> str:
    lines = [
        "## Tool Semantic Effects",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- effects: `{len(report.effects)}`",
        f"- passed: `{report.pass_count}`",
        f"- failed: `{report.fail_count}`",
        "",
        "### Effects",
        "",
    ]
    for effect in report.effects:
        lines.append(f"- `{effect.kind}` `{effect.status}` tool `{effect.tool_name}` call `{effect.tool_call_id}`")
    lines.extend(["", "### Findings", ""])
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _context_index(context_snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    read_file_state: dict[str, Any] = {}
    content_replacements: dict[str, Any] = {}
    artifact_refs: set[str] = set()
    budget_ledger: list[Any] = []
    for snapshot in context_snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        if isinstance(snapshot.get("read_file_state"), Mapping):
            read_file_state.update(snapshot["read_file_state"])
        if isinstance(snapshot.get("content_replacements"), Mapping):
            content_replacements.update(snapshot["content_replacements"])
        refs = snapshot.get("artifact_refs") if isinstance(snapshot.get("artifact_refs"), Sequence) else ()
        artifact_refs.update(str(ref) for ref in refs if ref)
        ledger = snapshot.get("budget_ledger") if isinstance(snapshot.get("budget_ledger"), Sequence) else ()
        budget_ledger.extend(ledger)
    return {
        "read_file_state": read_file_state,
        "content_replacements": content_replacements,
        "artifact_refs": artifact_refs,
        "budget_ledger": budget_ledger,
    }


def _result_projection_index(report: ToolResultContextReport | None) -> dict[str, Mapping[str, Any]]:
    if report is None:
        return {}
    return {projection.tool_call_id: projection.to_dict() for projection in report.projections}


def _permission_tool_ids(report: ToolPermissionSessionReport | None) -> set[str]:
    if report is None:
        return set()
    return {record.tool_call_id for record in report.records if record.tool_call_id}


def _safe_workspace_path(root: Path, raw: str) -> Path | None:
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return resolved
    except (OSError, ValueError):
        return None


def _shell_redirect_targets(root: Path, command: str) -> list[Path]:
    targets: list[Path] = []
    tokens = command.replace("2>", ">").replace("1>", ">").split(">")
    if len(tokens) < 2:
        return targets
    for raw_target in tokens[1:]:
        candidate = raw_target.strip().split()[0] if raw_target.strip().split() else ""
        candidate = candidate.strip("\"'")
        if not candidate:
            continue
        path = _safe_workspace_path(root, candidate)
        if path is not None:
            targets.append(path)
    return targets


def _surface_for_effect(kind: ToolSemanticEffectKind) -> ToolSemanticSurface:
    if kind in {ToolSemanticEffectKind.FILE_WRITE_WORKSPACE, ToolSemanticEffectKind.FILE_EDIT_WORKSPACE}:
        return ToolSemanticSurface.WORKSPACE
    if kind == ToolSemanticEffectKind.FILE_READ_CONTEXT:
        return ToolSemanticSurface.CONTEXT
    if kind == ToolSemanticEffectKind.SHELL_PERMISSION_PENDING:
        return ToolSemanticSurface.PERMISSION
    if kind == ToolSemanticEffectKind.BUDGET_EXTERNALIZED_CONTEXT:
        return ToolSemanticSurface.BUDGET
    if kind == ToolSemanticEffectKind.SESSION_TOOL_USE_EXECUTED:
        return ToolSemanticSurface.SESSION_BRIDGE
    return ToolSemanticSurface.RESULT_CONTEXT
