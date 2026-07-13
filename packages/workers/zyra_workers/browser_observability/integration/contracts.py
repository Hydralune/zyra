from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactRef, EventRecord

from ..models import (
    ArtifactLineage,
    ObservationScope,
    RecoveryInput,
    TraceSpan,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)


class EvidenceSource(StrEnum):
    BROWSER_SESSION = "browser_session"
    BROWSER_CDP = "browser_cdp"
    BROWSER_ACTION = "browser_action"
    BROWSER_DOWNLOAD = "browser_download"
    BROWSER_STORAGE = "browser_storage"
    BROWSER_SCREENSHOT = "browser_screenshot"
    BROWSER_SECURITY = "browser_security"
    PROVIDER_STREAM = "provider_stream"
    MCP = "mcp"
    SUBAGENT = "subagent"
    BACKGROUND_TASK = "background_task"
    HASHLINE = "hashline"
    WORKTREE = "worktree"


class EvidenceTerminalState(StrEnum):
    NONE = "none"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    EXHAUSTED = "exhausted"
    CONFLICT = "conflict"


class CommitPhase(StrEnum):
    PREPARED = "prepared"
    HISTORY_COMMITTED = "history_committed"
    ARTIFACTS_COMMITTED = "artifacts_committed"
    EVENTS_PENDING = "events_pending"
    EVENTS_COMMITTED = "events_committed"
    CHECKPOINT_COMMITTED = "checkpoint_committed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceEnvelope:
    scope: ObservationScope
    source: EvidenceSource
    event_type: str
    payload: Mapping[str, Any]
    evidence_id: str = field(
        default_factory=lambda: new_observation_id("browser-runtime-evidence")
    )
    created_at: str = field(default_factory=utc_now)
    observation_commit_id: str = ""
    source_event_id: str = ""
    causation_event_id: str = ""
    correlation_event_ids: tuple[str, ...] = ()
    action_id: str = ""
    tool_call_id: str = ""
    action_receipt_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    provider_request_id: str = ""
    mcp_server_id: str = ""
    mcp_connection_id: str = ""
    mcp_request_id: str = ""
    subagent_task_id: str = ""
    parent_tool_call_id: str = ""
    background_job_id: str = ""
    worktree_id: str = ""
    branch: str = ""
    base_sha: str = ""
    hashline_receipt_id: str = ""
    partial_sequence: int = 0
    terminal_state: EvidenceTerminalState = EvidenceTerminalState.NONE
    outcome_unknown: bool = False
    retryable: bool = False
    artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("runtime evidence event_type is required")
        if self.partial_sequence < 0:
            raise ValueError("runtime evidence sequence must be non-negative")
        if self.source == EvidenceSource.PROVIDER_STREAM and not self.provider_request_id:
            raise ValueError("provider evidence requires provider_request_id")
        if self.source == EvidenceSource.MCP and not (
            self.mcp_connection_id or self.mcp_request_id
        ):
            raise ValueError("MCP evidence requires a connection or request identity")
        if self.source == EvidenceSource.SUBAGENT and not self.subagent_task_id:
            raise ValueError("subagent evidence requires a stable task identity")
        if self.source == EvidenceSource.BACKGROUND_TASK and not self.background_job_id:
            raise ValueError("background evidence requires a stable job identity")
        if self.source == EvidenceSource.HASHLINE and not self.hashline_receipt_id:
            raise ValueError("Hashline evidence requires a receipt identity")
        if self.source == EvidenceSource.WORKTREE and not self.worktree_id:
            raise ValueError("worktree evidence requires a worktree identity")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(
            self,
            "correlation_event_ids",
            tuple(str(item) for item in self.correlation_event_ids if str(item)),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            tuple(str(item) for item in self.artifact_ids if str(item)),
        )

    @property
    def terminal(self) -> bool:
        return self.terminal_state != EvidenceTerminalState.NONE

    @property
    def correlation_key(self) -> str:
        identities = {
            "provider": self.provider_request_id,
            "mcp_connection": self.mcp_connection_id,
            "mcp_request": self.mcp_request_id,
            "subagent": self.subagent_task_id,
            "background": self.background_job_id,
            "worktree": self.worktree_id,
            "hashline": self.hashline_receipt_id,
            "action": self.action_id or self.tool_call_id,
        }
        selected = {key: value for key, value in identities.items() if value}
        return digest_value(
            {
                "scope_key": self.scope.key,
                "source": str(self.source),
                "identities": selected,
            }
        )[7:39]

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.runtime-evidence.v1",
            "evidence_id": self.evidence_id,
            "scope": self.scope.to_dict(),
            "source": str(self.source),
            "event_type": self.event_type,
            "created_at": self.created_at,
            "observation_commit_id": self.observation_commit_id,
            "source_event_id": self.source_event_id,
            "causation_event_id": self.causation_event_id,
            "correlation_event_ids": list(self.correlation_event_ids),
            "action_id": self.action_id,
            "tool_call_id": self.tool_call_id,
            "action_receipt_id": self.action_receipt_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "provider_request_id": self.provider_request_id,
            "mcp_server_id": self.mcp_server_id,
            "mcp_connection_id": self.mcp_connection_id,
            "mcp_request_id": self.mcp_request_id,
            "subagent_task_id": self.subagent_task_id,
            "parent_tool_call_id": self.parent_tool_call_id,
            "background_job_id": self.background_job_id,
            "worktree_id": self.worktree_id,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "hashline_receipt_id": self.hashline_receipt_id,
            "partial_sequence": self.partial_sequence,
            "terminal_state": str(self.terminal_state),
            "outcome_unknown": self.outcome_unknown,
            "retryable": self.retryable,
            "artifact_ids": list(self.artifact_ids),
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
            "correlation_key": self.correlation_key,
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    @classmethod
    def from_mapping(
        cls,
        scope: ObservationScope,
        value: Mapping[str, Any],
    ) -> "RuntimeEvidenceEnvelope":
        source = EvidenceSource(
            str(value.get("source") or value.get("source_runtime") or "")
        )
        terminal_raw = str(value.get("terminal_state") or "none")
        return cls(
            scope=scope,
            source=source,
            event_type=str(
                value.get("event_type")
                or value.get("source_event_type")
                or value.get("type")
                or ""
            ),
            payload=_mapping(value.get("payload")),
            evidence_id=str(
                value.get("evidence_id")
                or new_observation_id("browser-runtime-evidence")
            ),
            created_at=str(value.get("created_at") or utc_now()),
            observation_commit_id=str(value.get("observation_commit_id") or ""),
            source_event_id=str(value.get("source_event_id") or value.get("event_id") or ""),
            causation_event_id=str(value.get("causation_event_id") or value.get("causation_id") or ""),
            correlation_event_ids=_strings(value.get("correlation_event_ids")),
            action_id=str(value.get("action_id") or ""),
            tool_call_id=str(value.get("tool_call_id") or ""),
            action_receipt_id=str(value.get("action_receipt_id") or value.get("receipt_id") or ""),
            trace_id=str(value.get("trace_id") or ""),
            span_id=str(value.get("span_id") or ""),
            parent_span_id=str(value.get("parent_span_id") or ""),
            provider_request_id=str(value.get("provider_request_id") or value.get("message_id") or ""),
            mcp_server_id=str(value.get("mcp_server_id") or value.get("server_id") or ""),
            mcp_connection_id=str(value.get("mcp_connection_id") or value.get("connection_id") or ""),
            mcp_request_id=str(value.get("mcp_request_id") or value.get("request_id") or ""),
            subagent_task_id=str(value.get("subagent_task_id") or value.get("task_id") or ""),
            parent_tool_call_id=str(value.get("parent_tool_call_id") or value.get("parentToolCallId") or ""),
            background_job_id=str(value.get("background_job_id") or value.get("job_id") or ""),
            worktree_id=str(value.get("worktree_id") or ""),
            branch=str(value.get("branch") or ""),
            base_sha=str(value.get("base_sha") or value.get("base") or ""),
            hashline_receipt_id=str(value.get("hashline_receipt_id") or ""),
            partial_sequence=int(value.get("partial_sequence") or value.get("sequence") or 0),
            terminal_state=EvidenceTerminalState(terminal_raw),
            outcome_unknown=bool(value.get("outcome_unknown")),
            retryable=bool(value.get("retryable")),
            artifact_ids=_strings(value.get("artifact_ids")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class IntegrationCommitReceipt:
    scope: ObservationScope
    commit_id: str
    input_digest: str
    phase: CommitPhase
    history_head_digest: str = ""
    event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    recovery_input_ids: tuple[str, ...] = ()
    error: str = ""
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.commit_id or not self.input_digest:
            raise ValueError("integration commit requires identity and input digest")
        if self.revision < 0:
            raise ValueError("integration commit revision must be non-negative")

    @property
    def terminal(self) -> bool:
        return self.phase in {
            CommitPhase.CHECKPOINT_COMMITTED,
            CommitPhase.FAILED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.integration-commit.v1",
            "scope": self.scope.to_dict(),
            "commit_id": self.commit_id,
            "input_digest": self.input_digest,
            "phase": str(self.phase),
            "history_head_digest": self.history_head_digest,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "recovery_input_ids": list(self.recovery_input_ids),
            "error": self.error,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "terminal": self.terminal,
            "recovery_planner_owner": "M1-07C",
            "is_recovery_plan": False,
        }


@dataclass(frozen=True, slots=True)
class BrowserIntegrationOutput:
    evidence: tuple[RuntimeEvidenceEnvelope, ...] = ()
    signals: tuple[WatchdogSignal, ...] = ()
    recovery_inputs: tuple[RecoveryInput, ...] = ()
    spans: tuple[TraceSpan, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    lineage: tuple[ArtifactLineage, ...] = ()
    events: tuple[EventRecord, ...] = ()
    projection: Mapping[str, Any] = field(default_factory=dict)

    def merge(self, *values: "BrowserIntegrationOutput") -> "BrowserIntegrationOutput":
        all_values = (self, *values)
        return BrowserIntegrationOutput(
            evidence=_dedupe(
                (item for value in all_values for item in value.evidence),
                lambda item: item.evidence_id,
            ),
            signals=_dedupe(
                (item for value in all_values for item in value.signals),
                lambda item: item.signal_id,
            ),
            recovery_inputs=_dedupe(
                (item for value in all_values for item in value.recovery_inputs),
                lambda item: item.input_id,
            ),
            spans=_dedupe(
                (item for value in all_values for item in value.spans),
                lambda item: item.span_id,
            ),
            artifacts=_dedupe(
                (item for value in all_values for item in value.artifacts),
                lambda item: item.artifact_id,
            ),
            lineage=_dedupe(
                (item for value in all_values for item in value.lineage),
                lambda item: item.receipt_id,
            ),
            events=_dedupe(
                (item for value in all_values for item in value.events),
                lambda item: item.event_id,
            ),
            projection={
                key: item
                for value in all_values
                for key, item in value.projection.items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.integration-output.v1",
            "evidence": [item.to_dict() for item in self.evidence],
            "signals": [item.to_dict() for item in self.signals],
            "recovery_inputs": [item.to_dict() for item in self.recovery_inputs],
            "spans": [item.to_dict() for item in self.spans],
            "artifact_ids": [item.artifact_id for item in self.artifacts],
            "lineage": [item.to_dict() for item in self.lineage],
            "event_ids": [item.event_id for item in self.events],
            "projection": dict(self.projection),
            "recovery_planned_emitted": False,
        }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(str(item) for item in value if str(item))
    return ()


def _dedupe(values: Any, key: Any) -> tuple[Any, ...]:
    output: list[Any] = []
    seen: set[str] = set()
    for item in values:
        identity = str(key(item))
        if identity in seen:
            continue
        seen.add(identity)
        output.append(item)
    return tuple(output)
