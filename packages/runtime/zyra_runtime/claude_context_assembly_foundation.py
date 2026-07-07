from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso, to_jsonable

from .claude_input_processor import QueryInputRecord, QuerySourceKind, QuerySourceMetadata, input_records_to_messages


class ContextAssemblyBlockKind(StrEnum):
    SYSTEM_PROMPT = "system_prompt"
    USER_INPUT = "user_input"
    TOOL_INVENTORY = "tool_inventory"
    PERMISSION_STATE = "permission_state"
    WORKSPACE_STATE = "workspace_state"
    SESSION_STATE = "session_state"
    MEMORY_HINT = "memory_hint"
    CONTROL_STATE = "control_state"
    RUNTIME_CONTRACT = "runtime_contract"
    DIAGNOSTIC = "diagnostic"


class ContextAssemblyBlockRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    META = "meta"


class ContextAssemblyStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ContextAssemblyFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ContextAssemblyRequirement(StrEnum):
    INPUT_RECORDS = "input_records"
    SESSION_SEED = "session_seed"
    TOOL_INVENTORY = "tool_inventory"
    WORKSPACE_ROOT = "workspace_root"
    CONTRACT_METADATA = "contract_metadata"
    PERMISSION_MODE = "permission_mode"


@dataclass(frozen=True, slots=True)
class ContextAssemblyFinding:
    code: str
    severity: ContextAssemblyFindingSeverity
    requirement: ContextAssemblyRequirement
    message: str
    source_path: str
    target_path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ContextAssemblyFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "requirement": str(self.requirement),
            "message": self.message,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextAssemblySource:
    source_id: str
    source_kind: QuerySourceKind
    source_repo: str
    source_path: str
    target_path: str
    runtime_owner: str = "zyra-claude-productized"
    owner_unit: str = "M1-02B"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": str(self.source_kind),
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "runtime_owner": self.runtime_owner,
            "owner_unit": self.owner_unit,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextAssemblyBlock:
    block_id: str
    kind: ContextAssemblyBlockKind
    role: ContextAssemblyBlockRole
    text: str
    priority: int
    source: ContextAssemblySource
    created_at: str = field(default_factory=now_iso)
    token_estimate: int = 0
    cache_breaker: bool = False
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        return self.token_estimate or max(1, self.chars // 4)

    def compact_preview(self, limit: int = 240) -> str:
        normalized = " ".join(self.text.split())
        return normalized if len(normalized) <= limit else normalized[: max(0, limit - 3)] + "..."

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": str(self.kind),
            "role": str(self.role),
            "text": self.text if include_text else self.compact_preview(),
            "priority": self.priority,
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "chars": self.chars,
            "token_estimate": self.tokens,
            "cache_breaker": self.cache_breaker,
            "pinned": self.pinned,
            "metadata": to_jsonable(self.metadata),
        }

    def to_context_message(self) -> dict[str, Any]:
        role = "system" if self.role in {ContextAssemblyBlockRole.SYSTEM, ContextAssemblyBlockRole.META} else str(self.role)
        return {
            "role": role,
            "content": self.text,
            "metadata": {
                "context_block_id": self.block_id,
                "context_block_kind": str(self.kind),
                "priority": str(self.priority),
                "cache_breaker": str(self.cache_breaker).lower(),
                "source_path": self.source.source_path,
                "target_path": self.source.target_path,
                **{str(k): str(v) for k, v in self.metadata.items() if isinstance(v, (str, int, float, bool))},
            },
        }


@dataclass(frozen=True, slots=True)
class ContextAssemblyBudget:
    max_chars: int = 32000
    reserve_chars: int = 4000
    min_user_blocks: int = 1
    min_system_blocks: int = 1
    max_tool_inventory_chars: int = 6000
    max_memory_hint_chars: int = 6000
    max_diagnostic_chars: int = 4000

    @property
    def active_limit(self) -> int:
        return max(1, self.max_chars - max(0, self.reserve_chars))

    def normalize(self) -> "ContextAssemblyBudget":
        return ContextAssemblyBudget(
            max_chars=max(1000, self.max_chars),
            reserve_chars=max(0, min(self.reserve_chars, max(0, self.max_chars - 1))),
            min_user_blocks=max(0, self.min_user_blocks),
            min_system_blocks=max(0, self.min_system_blocks),
            max_tool_inventory_chars=max(1000, self.max_tool_inventory_chars),
            max_memory_hint_chars=max(1000, self.max_memory_hint_chars),
            max_diagnostic_chars=max(1000, self.max_diagnostic_chars),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_chars": self.max_chars,
            "reserve_chars": self.reserve_chars,
            "active_limit": self.active_limit,
            "min_user_blocks": self.min_user_blocks,
            "min_system_blocks": self.min_system_blocks,
            "max_tool_inventory_chars": self.max_tool_inventory_chars,
            "max_memory_hint_chars": self.max_memory_hint_chars,
            "max_diagnostic_chars": self.max_diagnostic_chars,
        }


@dataclass(frozen=True, slots=True)
class ContextAssemblySelection:
    selected_blocks: tuple[ContextAssemblyBlock, ...]
    dropped_blocks: tuple[ContextAssemblyBlock, ...]
    budget: ContextAssemblyBudget
    active_chars: int
    dropped_chars: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "selected_blocks": [block.to_dict(include_text=include_text) for block in self.selected_blocks],
            "dropped_blocks": [block.to_dict(include_text=False) for block in self.dropped_blocks],
            "budget": self.budget.to_dict(),
            "active_chars": self.active_chars,
            "dropped_chars": self.dropped_chars,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextAssemblySnapshot:
    snapshot_id: str
    session_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    status: ContextAssemblyStatus
    created_at: str
    source: QuerySourceMetadata
    budget: ContextAssemblyBudget
    blocks: tuple[ContextAssemblyBlock, ...]
    selected_blocks: tuple[ContextAssemblyBlock, ...]
    dropped_blocks: tuple[ContextAssemblyBlock, ...]
    findings: tuple[ContextAssemblyFinding, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != ContextAssemblyStatus.BLOCKED

    @property
    def active_chars(self) -> int:
        return sum(block.chars for block in self.selected_blocks)

    @property
    def active_tokens(self) -> int:
        return sum(block.tokens for block in self.selected_blocks)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ContextAssemblyFindingSeverity.WARNING)

    @property
    def fingerprint(self) -> str:
        payload = {
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "blocks": [
                {
                    "kind": str(block.kind),
                    "role": str(block.role),
                    "text_sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                    "source": block.source.source_path,
                }
                for block in self.selected_blocks
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_messages(self) -> list[dict[str, Any]]:
        return [block.to_context_message() for block in self.selected_blocks]

    def render_text(self) -> str:
        lines = []
        for block in self.selected_blocks:
            lines.append(f"## {block.kind}")
            lines.append(block.text)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def metadata_values(self) -> dict[str, str]:
        counts: dict[str, int] = {}
        for block in self.selected_blocks:
            counts[str(block.kind)] = counts.get(str(block.kind), 0) + 1
        return {
            "context_assembly_ok": str(self.ok).lower(),
            "context_assembly_status": str(self.status),
            "context_assembly_snapshot_id": self.snapshot_id,
            "context_assembly_fingerprint": self.fingerprint,
            "context_assembly_block_count": str(len(self.blocks)),
            "context_assembly_selected_block_count": str(len(self.selected_blocks)),
            "context_assembly_dropped_block_count": str(len(self.dropped_blocks)),
            "context_assembly_active_chars": str(self.active_chars),
            "context_assembly_active_tokens": str(self.active_tokens),
            "context_assembly_blocker_count": str(self.blocker_count),
            "context_assembly_warning_count": str(self.warning_count),
            "context_assembly_system_blocks": str(counts.get(str(ContextAssemblyBlockKind.SYSTEM_PROMPT), 0)),
            "context_assembly_user_blocks": str(counts.get(str(ContextAssemblyBlockKind.USER_INPUT), 0)),
            "context_assembly_tool_inventory_blocks": str(counts.get(str(ContextAssemblyBlockKind.TOOL_INVENTORY), 0)),
            **self.source.metadata("context_assembly"),
        }

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": str(self.status),
            "created_at": self.created_at,
            "source": self.source.to_dict(),
            "budget": self.budget.to_dict(),
            "blocks": [block.to_dict(include_text=include_text) for block in self.blocks],
            "selected_blocks": [block.to_dict(include_text=include_text) for block in self.selected_blocks],
            "dropped_blocks": [block.to_dict(include_text=False) for block in self.dropped_blocks],
            "findings": [finding.to_dict() for finding in self.findings],
            "fingerprint": self.fingerprint,
            "active_chars": self.active_chars,
            "active_tokens": self.active_tokens,
            "metadata": to_jsonable(self.metadata),
        }


class ContextAssemblyRuntime:
    """Builds the pre-query context snapshot used by CodeWorker sessions."""

    def __init__(
        self,
        *,
        source: QuerySourceMetadata | None = None,
        budget: ContextAssemblyBudget | None = None,
    ) -> None:
        self.source = source or default_context_assembly_source()
        self.budget = (budget or ContextAssemblyBudget()).normalize()

    def assemble(
        self,
        *,
        request: Any,
        session_id: str,
        input_records: Sequence[QueryInputRecord],
        tool_specs: Sequence[Any],
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        runtime_contracts: Any | None = None,
        integration_report: Any | None = None,
        runtime_context_report: Any | None = None,
        source_graph_audit: Any | None = None,
        permission_mode: str = "workspace",
        disabled: bool = False,
    ) -> ContextAssemblySnapshot:
        if disabled:
            return self._blocked_snapshot(
                request=request,
                session_id=session_id,
                code="context_assembly_disabled",
                requirement=ContextAssemblyRequirement.CONTRACT_METADATA,
                message="ContextAssemblyRuntime was disabled by request constraints.",
            )
        findings = list(
            self._validate_inputs(
                request=request,
                input_records=input_records,
                tool_specs=tool_specs,
                project_root=project_root,
                workspace_root=workspace_root,
                runtime_contracts=runtime_contracts,
                permission_mode=permission_mode,
            )
        )
        blocks = list(
            self._build_blocks(
                request=request,
                session_id=session_id,
                input_records=input_records,
                tool_specs=tool_specs,
                project_root=Path(project_root),
                workspace_root=Path(workspace_root),
                artifact_root=Path(artifact_root),
                runtime_contracts=runtime_contracts,
                integration_report=integration_report,
                runtime_context_report=runtime_context_report,
                source_graph_audit=source_graph_audit,
                permission_mode=permission_mode,
            )
        )
        selection = self._select_blocks(blocks)
        status = ContextAssemblyStatus.READY
        if any(finding.blocking for finding in findings):
            status = ContextAssemblyStatus.BLOCKED
        elif findings or selection.dropped_blocks:
            status = ContextAssemblyStatus.DEGRADED
        return ContextAssemblySnapshot(
            snapshot_id=new_id("ctxsnap"),
            session_id=session_id,
            worker_request_id=str(getattr(request, "request_id", "")),
            run_id=str(getattr(request, "run_id", "")),
            task_id=str(getattr(request, "task_id", "")),
            status=status,
            created_at=now_iso(),
            source=self.source,
            budget=self.budget,
            blocks=tuple(blocks),
            selected_blocks=selection.selected_blocks,
            dropped_blocks=selection.dropped_blocks,
            findings=tuple(findings),
            metadata={
                "permission_mode": permission_mode,
                "selection": selection.to_dict(include_text=False),
            },
        )

    def _blocked_snapshot(
        self,
        *,
        request: Any,
        session_id: str,
        code: str,
        requirement: ContextAssemblyRequirement,
        message: str,
    ) -> ContextAssemblySnapshot:
        finding = ContextAssemblyFinding(
            code=code,
            severity=ContextAssemblyFindingSeverity.BLOCKER,
            requirement=requirement,
            message=message,
            source_path=self.source.source_path,
            target_path=self.source.target_path,
        )
        return ContextAssemblySnapshot(
            snapshot_id=new_id("ctxsnap"),
            session_id=session_id,
            worker_request_id=str(getattr(request, "request_id", "")),
            run_id=str(getattr(request, "run_id", "")),
            task_id=str(getattr(request, "task_id", "")),
            status=ContextAssemblyStatus.BLOCKED,
            created_at=now_iso(),
            source=self.source,
            budget=self.budget,
            blocks=(),
            selected_blocks=(),
            dropped_blocks=(),
            findings=(finding,),
        )

    def _validate_inputs(
        self,
        *,
        request: Any,
        input_records: Sequence[QueryInputRecord],
        tool_specs: Sequence[Any],
        project_root: str | Path,
        workspace_root: str | Path,
        runtime_contracts: Any | None,
        permission_mode: str,
    ) -> Iterable[ContextAssemblyFinding]:
        if not input_records:
            yield self._finding(
                "missing_input_records",
                ContextAssemblyFindingSeverity.BLOCKER,
                ContextAssemblyRequirement.INPUT_RECORDS,
                "No accepted input records were available for context assembly.",
            )
        elif not any(record.accepted for record in input_records):
            yield self._finding(
                "no_accepted_input_records",
                ContextAssemblyFindingSeverity.BLOCKER,
                ContextAssemblyRequirement.INPUT_RECORDS,
                "All input records were rejected before QueryEngine dispatch.",
            )
        if not str(getattr(request, "request_id", "")):
            yield self._finding(
                "missing_worker_request_id",
                ContextAssemblyFindingSeverity.BLOCKER,
                ContextAssemblyRequirement.SESSION_SEED,
                "WorkerRequest.request_id is required for the session seed.",
            )
        if not Path(project_root).exists():
            yield self._finding(
                "project_root_missing",
                ContextAssemblyFindingSeverity.WARNING,
                ContextAssemblyRequirement.WORKSPACE_ROOT,
                "Project root is not present; context will omit repository metadata.",
                metadata={"project_root": str(project_root)},
            )
        if not Path(workspace_root).exists():
            yield self._finding(
                "workspace_root_missing",
                ContextAssemblyFindingSeverity.WARNING,
                ContextAssemblyRequirement.WORKSPACE_ROOT,
                "Workspace root does not exist yet; tool runtime may create it later.",
                metadata={"workspace_root": str(workspace_root)},
            )
        if not tool_specs:
            yield self._finding(
                "tool_inventory_empty",
                ContextAssemblyFindingSeverity.BLOCKER,
                ContextAssemblyRequirement.TOOL_INVENTORY,
                "Tool inventory must be assembled before QueryEngine dispatch.",
            )
        if runtime_contracts is None:
            yield self._finding(
                "runtime_contract_missing",
                ContextAssemblyFindingSeverity.WARNING,
                ContextAssemblyRequirement.CONTRACT_METADATA,
                "Runtime contracts are missing; source-to-target metadata will be incomplete.",
            )
        if not permission_mode:
            yield self._finding(
                "permission_mode_missing",
                ContextAssemblyFindingSeverity.WARNING,
                ContextAssemblyRequirement.PERMISSION_MODE,
                "Permission mode was not supplied; default workspace policy is assumed.",
            )

    def _build_blocks(
        self,
        *,
        request: Any,
        session_id: str,
        input_records: Sequence[QueryInputRecord],
        tool_specs: Sequence[Any],
        project_root: Path,
        workspace_root: Path,
        artifact_root: Path,
        runtime_contracts: Any | None,
        integration_report: Any | None,
        runtime_context_report: Any | None,
        source_graph_audit: Any | None,
        permission_mode: str,
    ) -> Iterable[ContextAssemblyBlock]:
        yield self._system_block(request=request, session_id=session_id, runtime_contracts=runtime_contracts)
        yield self._workspace_block(
            request=request,
            session_id=session_id,
            project_root=project_root,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
        )
        yield self._session_state_block(request=request, session_id=session_id, input_records=input_records)
        for block in self._input_blocks(request=request, session_id=session_id, input_records=input_records):
            yield block
        yield self._tool_inventory_block(request=request, session_id=session_id, tool_specs=tool_specs)
        yield self._permission_state_block(request=request, session_id=session_id, permission_mode=permission_mode)
        contract_block = self._runtime_contract_block(
            request=request,
            session_id=session_id,
            runtime_contracts=runtime_contracts,
            integration_report=integration_report,
            runtime_context_report=runtime_context_report,
            source_graph_audit=source_graph_audit,
        )
        if contract_block is not None:
            yield contract_block
        memory_block = self._memory_hint_block(request=request, session_id=session_id)
        if memory_block is not None:
            yield memory_block
        diagnostic_block = self._diagnostic_block(request=request, session_id=session_id)
        if diagnostic_block is not None:
            yield diagnostic_block

    def _system_block(self, *, request: Any, session_id: str, runtime_contracts: Any | None) -> ContextAssemblyBlock:
        source = self._source(
            "system_prompt",
            source_kind=QuerySourceKind.CLAUDE_QUERY_CONTEXT,
            source_path="src/utils/queryContext.ts",
        )
        runtime_id = str(getattr(runtime_contracts, "runtime_id", "zyra-claude-code-productized-runtime"))
        text = "\n".join(
            [
                "You are running inside Zyra CodeWorkerRuntime.",
                "Use Zyra-owned session, permission, context, artifact and tool-result state.",
                "Do not depend on root-level source repositories or sidecar runtimes for the default path.",
                f"runtime_id: {runtime_id}",
                f"session_id: {session_id}",
                f"worker_request_id: {getattr(request, 'request_id', '')}",
            ]
        )
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.SYSTEM_PROMPT,
            role=ContextAssemblyBlockRole.SYSTEM,
            text=text,
            priority=1000,
            source=source,
            cache_breaker=True,
            pinned=True,
            metadata={"contract_runtime_id": runtime_id},
        )

    def _workspace_block(
        self,
        *,
        request: Any,
        session_id: str,
        project_root: Path,
        workspace_root: Path,
        artifact_root: Path,
    ) -> ContextAssemblyBlock:
        source = self._source(
            "workspace_state",
            source_kind=QuerySourceKind.ZYRA_WORKER_REQUEST,
            source_path="packages/workers/zyra_workers/code_worker_runtime.py",
        )
        summary = {
            "project_root": str(project_root),
            "workspace_root": str(workspace_root),
            "artifact_root": str(artifact_root),
            "project_exists": project_root.exists(),
            "workspace_exists": workspace_root.exists(),
            "artifact_exists": artifact_root.exists(),
            "cwd": os.getcwd(),
            "run_id": str(getattr(request, "run_id", "")),
            "task_id": str(getattr(request, "task_id", "")),
            "node_id": str(getattr(request, "node_id", "")),
        }
        text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.WORKSPACE_STATE,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=760,
            source=source,
            metadata={"session_id": session_id},
        )

    def _session_state_block(
        self,
        *,
        request: Any,
        session_id: str,
        input_records: Sequence[QueryInputRecord],
    ) -> ContextAssemblyBlock:
        source = self._source(
            "session_state",
            source_kind=QuerySourceKind.CLAUDE_SESSION_STORAGE,
            source_path="src/utils/sessionStorage.ts",
            target_path="packages/runtime/zyra_runtime/claude_session_store.py",
        )
        accepted = [record.input_id for record in input_records if record.accepted]
        text = json.dumps(
            {
                "session_id": session_id,
                "worker_request_id": str(getattr(request, "request_id", "")),
                "accepted_input_ids": accepted,
                "input_count": len(input_records),
                "created_at": now_iso(),
                "transcript_policy": "append_only_jsonl",
                "parent_uuid_policy": "query_session_leaf_chain",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.SESSION_STATE,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=860,
            source=source,
            cache_breaker=True,
            pinned=True,
        )

    def _input_blocks(
        self,
        *,
        request: Any,
        session_id: str,
        input_records: Sequence[QueryInputRecord],
    ) -> Iterable[ContextAssemblyBlock]:
        for record in input_records:
            source = self._source(
                f"user_input:{record.sequence}",
                source_kind=QuerySourceKind.CLAUDE_PROCESS_USER_INPUT,
                source_path=record.source.source_path,
                target_path=record.source.target_path,
            )
            metadata = {
                "input_id": record.input_id,
                "sequence": record.sequence,
                "kind": str(record.kind),
                "disposition": str(record.disposition),
                "accepted": record.accepted,
                "risk": str(record.risk),
                "session_id": session_id,
                "worker_request_id": str(getattr(request, "request_id", "")),
            }
            yield ContextAssemblyBlock(
                block_id=new_id("ctxblock"),
                kind=ContextAssemblyBlockKind.USER_INPUT,
                role=ContextAssemblyBlockRole.USER,
                text=record.normalized_text,
                priority=920 if record.accepted else 360,
                source=source,
                cache_breaker=True,
                pinned=record.accepted,
                metadata=metadata,
            )

    def _tool_inventory_block(self, *, request: Any, session_id: str, tool_specs: Sequence[Any]) -> ContextAssemblyBlock:
        source = self._source(
            "tool_inventory",
            source_kind=QuerySourceKind.CLAUDE_QUERY_CONTEXT,
            source_path="src/tools.ts",
            target_path="packages/runtime/zyra_runtime/tools.py",
        )
        tools = []
        for spec in tool_specs:
            data = to_jsonable(spec)
            if isinstance(data, Mapping):
                tools.append(data)
            else:
                tools.append(
                    {
                        "name": str(getattr(spec, "name", "")),
                        "metadata": to_jsonable(getattr(spec, "metadata", {})),
                    }
                )
        text = json.dumps(
            {
                "tool_count": len(tools),
                "tools": tools,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if len(text) > self.budget.max_tool_inventory_chars:
            text = text[: self.budget.max_tool_inventory_chars] + "\n... truncated tool inventory ..."
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.TOOL_INVENTORY,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=780,
            source=source,
            metadata={"session_id": session_id, "worker_request_id": str(getattr(request, "request_id", ""))},
        )

    def _permission_state_block(self, *, request: Any, session_id: str, permission_mode: str) -> ContextAssemblyBlock:
        source = self._source(
            "permission_state",
            source_kind=QuerySourceKind.CLAUDE_QUERY_ENGINE,
            source_path="src/cli/src/utils/permissions/*",
            target_path="packages/runtime/zyra_runtime/permissions.py",
        )
        text = json.dumps(
            {
                "permission_mode": permission_mode or "workspace",
                "policy_owner": "ToolPermissionPolicy",
                "request_id": str(getattr(request, "request_id", "")),
                "session_id": session_id,
                "decisions_are_runtime_blocking": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.PERMISSION_STATE,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=740,
            source=source,
            metadata={"session_id": session_id},
        )

    def _runtime_contract_block(
        self,
        *,
        request: Any,
        session_id: str,
        runtime_contracts: Any | None,
        integration_report: Any | None,
        runtime_context_report: Any | None,
        source_graph_audit: Any | None,
    ) -> ContextAssemblyBlock | None:
        if runtime_contracts is None and integration_report is None and runtime_context_report is None and source_graph_audit is None:
            return None
        source = self._source(
            "runtime_contract",
            source_kind=QuerySourceKind.CLAUDE_QUERY_ENGINE,
            source_path="src/QueryEngine.ts",
            target_path="packages/runtime/zyra_runtime/claude_runtime_contracts.py",
        )
        payload = {
            "runtime_contracts": _safe_report(runtime_contracts),
            "integration_report": _safe_report(integration_report),
            "runtime_context_report": _safe_report(runtime_context_report),
            "source_graph_audit": _safe_report(source_graph_audit),
            "session_id": session_id,
            "worker_request_id": str(getattr(request, "request_id", "")),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.RUNTIME_CONTRACT,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=680,
            source=source,
            metadata={"session_id": session_id},
        )

    def _memory_hint_block(self, *, request: Any, session_id: str) -> ContextAssemblyBlock | None:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        memory = constraints.get("memory_hints") or constraints.get("context_hints")
        if not memory:
            return None
        source = self._source(
            "memory_hint",
            source_kind=QuerySourceKind.ZYRA_WORKER_REQUEST,
            source_path="packages/memory/zyra_memory",
            target_path="packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
        )
        text = json.dumps(to_jsonable(memory), ensure_ascii=False, indent=2, sort_keys=True)
        if len(text) > self.budget.max_memory_hint_chars:
            text = text[: self.budget.max_memory_hint_chars] + "\n... truncated memory hints ..."
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.MEMORY_HINT,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=640,
            source=source,
            metadata={"session_id": session_id},
        )

    def _diagnostic_block(self, *, request: Any, session_id: str) -> ContextAssemblyBlock | None:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        diagnostics = constraints.get("diagnostics") or constraints.get("debug_context")
        if not diagnostics:
            return None
        source = self._source(
            "diagnostic",
            source_kind=QuerySourceKind.ZYRA_WORKER_REQUEST,
            source_path="packages/workers/zyra_workers/code_worker_runtime.py",
            target_path="packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
        )
        text = json.dumps(to_jsonable(diagnostics), ensure_ascii=False, indent=2, sort_keys=True)
        if len(text) > self.budget.max_diagnostic_chars:
            text = text[: self.budget.max_diagnostic_chars] + "\n... truncated diagnostics ..."
        return ContextAssemblyBlock(
            block_id=new_id("ctxblock"),
            kind=ContextAssemblyBlockKind.DIAGNOSTIC,
            role=ContextAssemblyBlockRole.META,
            text=text,
            priority=260,
            source=source,
            metadata={"session_id": session_id},
        )

    def _select_blocks(self, blocks: Sequence[ContextAssemblyBlock]) -> ContextAssemblySelection:
        budget = self.budget.normalize()
        selected: list[ContextAssemblyBlock] = []
        dropped: list[ContextAssemblyBlock] = []
        running_chars = 0
        for block in sorted(blocks, key=lambda item: (-item.pinned, -item.priority, item.created_at)):
            if block.pinned or running_chars + block.chars <= budget.active_limit:
                selected.append(block)
                running_chars += block.chars
            else:
                dropped.append(block)
        selected.sort(key=lambda item: (item.created_at, item.priority))
        return ContextAssemblySelection(
            selected_blocks=tuple(selected),
            dropped_blocks=tuple(dropped),
            budget=budget,
            active_chars=sum(block.chars for block in selected),
            dropped_chars=sum(block.chars for block in dropped),
        )

    def _source(
        self,
        source_id: str,
        *,
        source_kind: QuerySourceKind,
        source_path: str,
        target_path: str | None = None,
    ) -> ContextAssemblySource:
        return ContextAssemblySource(
            source_id=source_id,
            source_kind=source_kind,
            source_repo="claude-code-best" if source_path.startswith("src/") else "zyra",
            source_path=source_path,
            target_path=target_path or self.source.target_path,
            runtime_owner=self.source.runtime_owner,
            owner_unit=self.source.owner_unit,
        )

    def _finding(
        self,
        code: str,
        severity: ContextAssemblyFindingSeverity,
        requirement: ContextAssemblyRequirement,
        message: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextAssemblyFinding:
        return ContextAssemblyFinding(
            code=code,
            severity=severity,
            requirement=requirement,
            message=message,
            source_path=self.source.source_path,
            target_path=self.source.target_path,
            metadata=dict(metadata or {}),
        )


def default_context_assembly_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/utils/queryContext.ts",
        target_path="packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
        source_kind=QuerySourceKind.CLAUDE_QUERY_CONTEXT,
        upstream_signals=(
            "fetchSystemPromptParts",
            "ToolUseContext",
            "context cache breaker",
            "processUserInputContext",
            "system/user context prefix",
        ),
        notes=(
            "Context snapshot is built before model/tool-loop dispatch.",
            "Snapshot blocks are fed into ClaudeContextWindowManager and the session store.",
        ),
    )


def context_snapshot_to_messages(snapshot: ContextAssemblySnapshot, input_records: Sequence[QueryInputRecord] = ()) -> list[dict[str, Any]]:
    messages = snapshot.to_messages()
    existing_input_ids = {
        str(message.get("metadata", {}).get("input_id"))
        for message in messages
        if isinstance(message.get("metadata"), Mapping) and message.get("metadata", {}).get("input_id")
    }
    for message in input_records_to_messages(input_records):
        input_id = str(message.get("metadata", {}).get("input_id", ""))
        if input_id and input_id not in existing_input_ids:
            messages.append(message)
    return messages


def context_snapshot_metadata(snapshot: ContextAssemblySnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {
            "context_assembly_ok": "false",
            "context_assembly_status": "",
            "context_assembly_snapshot_id": "",
            "context_assembly_fingerprint": "",
        }
    return snapshot.metadata_values()


def render_context_snapshot_markdown(snapshot: ContextAssemblySnapshot) -> str:
    lines = [
        "# Context Assembly Snapshot",
        "",
        f"- ok: `{str(snapshot.ok).lower()}`",
        f"- status: `{snapshot.status}`",
        f"- session_id: `{snapshot.session_id}`",
        f"- worker_request_id: `{snapshot.worker_request_id}`",
        f"- snapshot_id: `{snapshot.snapshot_id}`",
        f"- fingerprint: `{snapshot.fingerprint}`",
        f"- active_chars: `{snapshot.active_chars}`",
        f"- active_tokens: `{snapshot.active_tokens}`",
        f"- selected_blocks: `{len(snapshot.selected_blocks)}`",
        f"- dropped_blocks: `{len(snapshot.dropped_blocks)}`",
        "",
        "## Findings",
        "",
    ]
    if snapshot.findings:
        for finding in snapshot.findings:
            lines.append(f"- `{finding.severity}` `{finding.code}` {finding.message}")
    else:
        lines.append("- none")
    lines.extend(["", "## Blocks", ""])
    for block in snapshot.selected_blocks:
        lines.extend(
            [
                f"- `{block.kind}` `{block.role}` chars=`{block.chars}` priority=`{block.priority}`",
                f"  - source: `{block.source.source_path}`",
                f"  - target: `{block.source.target_path}`",
                f"  - preview: {block.compact_preview(180)}",
            ]
        )
    return "\n".join(lines) + "\n"


def _safe_report(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except TypeError:
            return value.to_dict
    return to_jsonable(value)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
