from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from zyra_core import ArtifactRef, EventRecord, now_iso, to_jsonable

from ..browser_state.contracts import BrowserContextDisclosure, BrowserLowEntropyMetrics, digest_json, state_id


class BrowserMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    META = "meta"


class BrowserMessageKind(StrEnum):
    STATE = "browser_state"
    ACTION_CALL = "browser_action_call"
    ACTION_RESULT = "browser_action_result"
    ARTIFACT_REF = "artifact_ref"
    MEMORY_CANDIDATE = "memory_candidate"
    COMPACTED_HISTORY = "compacted_history"
    DIAGNOSTIC = "diagnostic"


class BrowserMemoryCandidateKind(StrEnum):
    FACT = "fact"
    FAILURE_PATTERN = "failure_pattern"
    PROCEDURE_CANDIDATE = "procedure_candidate"


class BrowserProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class BrowserMessagePart:
    message_id: str
    role: BrowserMessageRole
    kind: BrowserMessageKind
    content: str
    source_id: str
    causation_id: str = ""
    artifact_ids: tuple[str, ...] = ()
    selector_refs: tuple[str, ...] = ()
    trust: str = "external_untrusted"
    read_once: bool = False
    priority: int = 500
    token_estimate: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def fingerprint(self) -> str:
        return digest_json({
            "role": str(self.role),
            "kind": str(self.kind),
            "content": self.content,
            "source_id": self.source_id,
            "causation_id": self.causation_id,
            "artifact_ids": self.artifact_ids,
            "selector_refs": self.selector_refs,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": str(self.role),
            "kind": str(self.kind),
            "content": self.content,
            "source_id": self.source_id,
            "causation_id": self.causation_id,
            "artifact_ids": list(self.artifact_ids),
            "selector_refs": list(self.selector_refs),
            "trust": self.trust,
            "read_once": self.read_once,
            "priority": self.priority,
            "token_estimate": self.token_estimate,
            "metadata": to_jsonable(dict(self.metadata)),
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserActionResultProjection:
    projection_id: str
    receipt_id: str
    request_id: str
    request_fingerprint: str
    browser_session_id: str
    action: str
    step_index: int
    ok: bool
    status: str
    summary: str
    error_code: str
    error_message: str
    output_preview: str
    original_output_bytes: int
    projected_output_bytes: int
    artifact_ids: tuple[str, ...]
    tool_call_message: BrowserMessagePart
    tool_result_message: BrowserMessagePart
    selector_revision_id: str = ""
    selector_refs: tuple[str, ...] = ()
    capture_id: str = ""
    budget_decision: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pair_fingerprint(self) -> str:
        return digest_json({
            "call": self.tool_call_message.fingerprint,
            "result": self.tool_result_message.fingerprint,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "browser_session_id": self.browser_session_id,
            "action": self.action,
            "step_index": self.step_index,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "output_preview": self.output_preview,
            "original_output_bytes": self.original_output_bytes,
            "projected_output_bytes": self.projected_output_bytes,
            "artifact_ids": list(self.artifact_ids),
            "tool_call_message": self.tool_call_message.to_dict(),
            "tool_result_message": self.tool_result_message.to_dict(),
            "selector_revision_id": self.selector_revision_id,
            "selector_refs": list(self.selector_refs),
            "capture_id": self.capture_id,
            "budget_decision": to_jsonable(dict(self.budget_decision)),
            "metadata": to_jsonable(dict(self.metadata)),
            "pair_fingerprint": self.pair_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BrowserArtifactExternalization:
    capture_id: str
    artifacts: tuple[ArtifactRef, ...]
    raw_bytes: int
    externalized_bytes: int
    verified_bytes: int
    artifact_ids: tuple[str, ...]
    digests: Mapping[str, str]
    complete: bool
    error: str = ""

    @property
    def offload_ratio(self) -> float:
        return self.externalized_bytes / self.raw_bytes if self.raw_bytes else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "artifacts": [to_jsonable(item) for item in self.artifacts],
            "raw_bytes": self.raw_bytes,
            "externalized_bytes": self.externalized_bytes,
            "verified_bytes": self.verified_bytes,
            "artifact_ids": list(self.artifact_ids),
            "digests": dict(self.digests),
            "complete": self.complete,
            "error": self.error,
            "offload_ratio": self.offload_ratio,
        }


@dataclass(frozen=True, slots=True)
class BrowserMemoryCandidate:
    candidate_id: str
    kind: BrowserMemoryCandidateKind
    summary: str
    source_event_id: str
    source_action_receipt_id: str
    source_dom_capture_id: str
    artifact_ids: tuple[str, ...]
    confidence: float
    retention_hint: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    candidate_only: bool = True
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory candidate confidence must be between zero and one")
        if not self.candidate_only:
            raise ValueError("04B may only emit memory candidates")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": str(self.kind),
            "summary": self.summary,
            "source_event_id": self.source_event_id,
            "source_action_receipt_id": self.source_action_receipt_id,
            "source_dom_capture_id": self.source_dom_capture_id,
            "artifact_ids": list(self.artifact_ids),
            "confidence": self.confidence,
            "retention_hint": self.retention_hint,
            "evidence": to_jsonable(dict(self.evidence)),
            "candidate_only": True,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserNextContextReceipt:
    receipt_id: str
    disclosure_id: str
    source_id: str
    accepted: bool
    duplicate: bool
    context_block: Mapping[str, Any]
    reason: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "disclosure_id": self.disclosure_id,
            "source_id": self.source_id,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "context_block": to_jsonable(dict(self.context_block)),
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserMessageTurn:
    turn_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    browser_session_id: str
    capture_id: str
    selector_revision_id: str
    disclosure: BrowserContextDisclosure
    action_results: tuple[BrowserActionResultProjection, ...]
    messages: tuple[BrowserMessagePart, ...]
    artifacts: tuple[ArtifactRef, ...]
    memory_candidates: tuple[BrowserMemoryCandidate, ...]
    next_context: BrowserNextContextReceipt
    events: tuple[EventRecord, ...]
    metrics: BrowserLowEntropyMetrics
    status: BrowserProjectionStatus = BrowserProjectionStatus.READY
    findings: tuple[str, ...] = ()
    ablation: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status != BrowserProjectionStatus.BLOCKED and self.next_context.accepted

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.artifact_id for item in self.artifacts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "disclosure": self.disclosure.to_dict(),
            "action_results": [item.to_dict() for item in self.action_results],
            "messages": [item.to_dict() for item in self.messages],
            "artifacts": [to_jsonable(item) for item in self.artifacts],
            "memory_candidates": [item.to_dict() for item in self.memory_candidates],
            "next_context": self.next_context.to_dict(),
            "events": [to_jsonable(item) for item in self.events],
            "metrics": self.metrics.to_dict(),
            "status": str(self.status),
            "ok": self.ok,
            "findings": list(self.findings),
            "ablation": to_jsonable(dict(self.ablation)),
            "created_at": self.created_at,
        }


def stable_projection_id(receipt_id: str, request_fingerprint: str) -> str:
    digest = hashlib.sha256(f"{receipt_id}:{request_fingerprint}".encode("utf-8")).hexdigest()[:24]
    return f"browser_projection_{digest}"


def message_id(kind: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{source_id}".encode("utf-8")).hexdigest()[:24]
    return f"browser_message_{digest}"
