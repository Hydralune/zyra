from __future__ import annotations

"""Runtime audit for the complete 04A→04B→02D integration boundary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from zyra_core import EventRecord, EventType, to_jsonable

from ..browser_state.contracts import digest_json, state_id
from .models import BrowserMessageTurn
from .task_integration import BrowserContextDeliveryState, BrowserContextTaskCheckpoint


class BrowserIntegrationFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrowserStateCustodyEntry:
    domain: str
    owner: str
    projection_writer: str
    canonical: bool
    persisted_in: str
    restore_path: str
    mutation_event: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "owner": self.owner,
            "projection_writer": self.projection_writer,
            "canonical": self.canonical,
            "persisted_in": self.persisted_in,
            "restore_path": self.restore_path,
            "mutation_event": self.mutation_event,
        }


@dataclass(frozen=True, slots=True)
class BrowserSourceRoleEntry:
    source: str
    role: str
    capability: str
    target: str
    production_migration: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "role": self.role,
            "capability": self.capability,
            "target": self.target,
            "production_migration": self.production_migration,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class BrowserIntegrationFinding:
    code: str
    severity: BrowserIntegrationFindingSeverity
    message: str
    blocks_run: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "blocks_run": self.blocks_run,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BrowserMessageIntegrationAudit:
    audit_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    turn_id: str
    checkpoint_revision: int
    checkpoint_digest: str
    source_roles: tuple[BrowserSourceRoleEntry, ...]
    state_custody: tuple[BrowserStateCustodyEntry, ...]
    findings: tuple[BrowserIntegrationFinding, ...]
    event_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    default_path: str

    @property
    def blocking_findings(self) -> tuple[BrowserIntegrationFinding, ...]:
        return tuple(item for item in self.findings if item.blocks_run)

    @property
    def valid(self) -> bool:
        return not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-message-integration-audit.v1",
            "audit_id": self.audit_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "turn_id": self.turn_id,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint_digest": self.checkpoint_digest,
            "source_roles": [item.to_dict() for item in self.source_roles],
            "state_custody": [item.to_dict() for item in self.state_custody],
            "findings": [item.to_dict() for item in self.findings],
            "blocking_findings": len(self.blocking_findings),
            "valid": self.valid,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "default_path": self.default_path,
        }


class BrowserMessageIntegrationAuditRuntime:
    """Reject pseudo-integration at the production worker boundary."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._audits = 0
        self._failures = 0

    def audit(
        self,
        turn: BrowserMessageTurn,
        checkpoint: BrowserContextTaskCheckpoint,
        *,
        events: Sequence[EventRecord],
    ) -> BrowserMessageIntegrationAudit:
        if self.disabled:
            raise RuntimeError("browser message integration audit is disabled")
        findings: list[BrowserIntegrationFinding] = []
        if checkpoint.scope.run_id != turn.run_id or checkpoint.scope.task_id != turn.task_id:
            findings.append(BrowserIntegrationFinding(
                "checkpoint_scope_mismatch",
                BrowserIntegrationFindingSeverity.ERROR,
                "browser checkpoint and turn do not share run/task custody",
                True,
            ))
        matching = [item for item in checkpoint.queue if item.disclosure_id == turn.disclosure.disclosure_id]
        if len(matching) != 1:
            findings.append(BrowserIntegrationFinding(
                "disclosure_not_enqueued_exactly_once",
                BrowserIntegrationFindingSeverity.ERROR,
                "browser turn disclosure must appear exactly once in the checkpoint queue",
                True,
                {"matches": len(matching), "disclosure_id": turn.disclosure.disclosure_id},
            ))
        elif matching[0].state != BrowserContextDeliveryState.PENDING:
            findings.append(BrowserIntegrationFinding(
                "new_disclosure_not_pending",
                BrowserIntegrationFindingSeverity.ERROR,
                "new browser disclosure cannot be consumed during capture",
                True,
                {"state": str(matching[0].state)},
            ))
        if not turn.next_context.accepted:
            findings.append(BrowserIntegrationFinding(
                "context_window_rejected_disclosure",
                BrowserIntegrationFindingSeverity.ERROR,
                "02D context window did not accept the browser disclosure block",
                True,
            ))
        context_blocks = checkpoint.context_window_state.get("blocks")
        context_blocks = context_blocks if isinstance(context_blocks, Sequence) else ()
        matching_blocks = [
            item for item in context_blocks
            if isinstance(item, Mapping)
            and str(_mapping(item.get("metadata")).get("browser_disclosure_id") or "")
            == turn.disclosure.disclosure_id
        ]
        if len(matching_blocks) != 1:
            findings.append(BrowserIntegrationFinding(
                "context_block_not_checkpointed_exactly_once",
                BrowserIntegrationFindingSeverity.ERROR,
                "accepted browser disclosure must be restorable from one 02D context block",
                True,
                {"matches": len(matching_blocks)},
            ))
        event_ids = {event.event_id for event in events}
        missing_turn_events = [event.event_id for event in turn.events if event.event_id not in event_ids]
        if missing_turn_events:
            findings.append(BrowserIntegrationFinding(
                "turn_events_not_reachable_from_worker",
                BrowserIntegrationFindingSeverity.ERROR,
                "browser message events were dropped before the worker result boundary",
                True,
                {"event_ids": missing_turn_events},
            ))
        event_artifact_ids: set[str] = set()
        for event in events:
            artifact = _mapping(event.payload.get("browser_artifact"))
            if artifact.get("artifact_id"):
                event_artifact_ids.add(str(artifact["artifact_id"]))
        missing_artifact_events = set(turn.artifact_ids) - event_artifact_ids
        if missing_artifact_events:
            findings.append(BrowserIntegrationFinding(
                "artifact_event_projection_missing",
                BrowserIntegrationFindingSeverity.ERROR,
                "every browser turn artifact requires an ARTIFACT_WRITTEN event",
                True,
                {"artifact_ids": sorted(missing_artifact_events)},
            ))
        if not any("browser_memory_candidate" in event.payload for event in turn.events) and turn.memory_candidates:
            findings.append(BrowserIntegrationFinding(
                "memory_candidate_event_missing",
                BrowserIntegrationFindingSeverity.ERROR,
                "browser memory candidates are not reachable through the event log",
                True,
            ))
        if any(not candidate.candidate_only for candidate in turn.memory_candidates):
            findings.append(BrowserIntegrationFinding(
                "browser_memory_committed_early",
                BrowserIntegrationFindingSeverity.ERROR,
                "04B may only emit memory candidates",
                True,
            ))
        external_paths = self._external_path_findings(turn)
        findings.extend(external_paths)
        audit = BrowserMessageIntegrationAudit(
            audit_id=state_id("brintegrationaudit"),
            run_id=turn.run_id,
            task_id=turn.task_id,
            worker_request_id=turn.worker_request_id,
            turn_id=turn.turn_id,
            checkpoint_revision=checkpoint.revision,
            checkpoint_digest=checkpoint.digest,
            source_roles=self.source_roles(),
            state_custody=self.state_custody(),
            findings=tuple(findings),
            event_ids=tuple(event.event_id for event in events),
            artifact_ids=turn.artifact_ids,
            default_path="BrowserWorkerRuntime→BrowserMessageStateApplication→BrowserMessageManagerRuntime→TaskState→CodeWorker/02D",
        )
        self._audits += 1
        if not audit.valid:
            self._failures += 1
        return audit

    def event_for_audit(self, audit: BrowserMessageIntegrationAudit, *, node_id: str) -> EventRecord:
        return EventRecord(
            run_id=audit.run_id,
            task_id=audit.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_message_integration_audit": audit.to_dict(),
                "cause_event_ids": list(audit.event_ids),
            },
        )

    def require_valid(self, audit: BrowserMessageIntegrationAudit) -> None:
        if audit.valid:
            return
        raise RuntimeError(
            "browser message integration audit failed: "
            + "; ".join(item.code for item in audit.blocking_findings)
        )

    def source_roles(self) -> tuple[BrowserSourceRoleEntry, ...]:
        return (
            BrowserSourceRoleEntry(
                "browser-use",
                "primary",
                "MessageManager/context/DOM/OOPIF",
                "packages/workers/zyra_workers/browser_context + browser_state",
                True,
                "Primary browser state and message control flow is decomposed into Zyra owners.",
            ),
            BrowserSourceRoleEntry(
                "claude-code-best/M1-02D",
                "supplementary",
                "context budget/provider envelope/checkpoint",
                "ClaudeContextWindowManager + task integration bridge",
                True,
                "The existing 02D owner is reused; no second global compact owner is created.",
            ),
            BrowserSourceRoleEntry(
                "oh-my-pi",
                "supplementary",
                "malformed/partial result coercion and atomic tool pairs",
                "action_envelope.py + history.py",
                True,
                "Only the missing result-boundary semantics are migrated.",
            ),
            BrowserSourceRoleEntry(
                "Agent Framework/opencode/Hermes",
                "conformance_only",
                "history/checkpoint behavior comparison",
                "tests and review evidence",
                False,
                "They do not receive a second canonical state owner.",
            ),
            BrowserSourceRoleEntry(
                "Snapcompact",
                "experimental",
                "bitmap-frame ablation",
                "ablation.py",
                True,
                "Default-off and non-authoritative; deterministic disclosure remains default.",
            ),
        )

    def state_custody(self) -> tuple[BrowserStateCustodyEntry, ...]:
        return (
            BrowserStateCustodyEntry("browser_session_target_cdp", "BrowserRuntime/M1-04A", "BrowserFrameCaptureRuntime", True, "04A state root", "BrowserSessionResumeRuntime", "browser_session_lifecycle"),
            BrowserStateCustodyEntry("selector_revision", "BrowserSelectorMapStore/M1-04B", "BrowserLiveSelectorProbeRuntime", True, "dom-state selector index", "selector store reload", "browser_dom_state"),
            BrowserStateCustodyEntry("task_checkpoint", "SQLiteStore/TaskState", "BrowserContextTaskIntegrationRuntime", True, "TaskState.metadata", "load_task", "browser_external_context"),
            BrowserStateCustodyEntry("model_context", "ClaudeContextWindowManager/M1-02D", "BrowserNextContextPort", True, "02D context snapshot", "context_window_from_payload", "model_stream_report"),
            BrowserStateCustodyEntry("history", "event log + M1-02D context", "BrowserHistoryNormalizer", True, "event log/context blocks", "event fold/02D restore", "browser_context_disclosure"),
            BrowserStateCustodyEntry("artifact", "LocalArtifactStore", "BrowserTurnCausalRuntime", True, "artifact root", "ArtifactRef resolver", "artifact_written"),
            BrowserStateCustodyEntry("memory_candidate", "MemoryFabric/M1-06B-M1-06C", "BrowserMemoryCandidateConsumerPort", False, "browser candidate event", "candidate event fold", "browser_memory_candidate"),
        )

    def _external_path_findings(self, turn: BrowserMessageTurn) -> tuple[BrowserIntegrationFinding, ...]:
        findings: list[BrowserIntegrationFinding] = []
        parent_prefix = ".." + "/"
        forbidden = tuple(
            parent_prefix + repository
            for repository in ("browser-use", "claude-code-best", "oh-my-pi")
        ) + ("vendor-runtimes", "source-pool")
        for artifact in turn.artifacts:
            normalized = str(PurePath(artifact.uri)).replace("\\", "/").lower()
            if any(marker in normalized for marker in forbidden):
                findings.append(BrowserIntegrationFinding(
                    "external_source_runtime_dependency",
                    BrowserIntegrationFindingSeverity.ERROR,
                    "browser turn artifact points at a source repository or vendor pool",
                    True,
                    {"artifact_id": artifact.artifact_id, "uri": artifact.uri},
                ))
        return tuple(findings)

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserMessageIntegrationAuditRuntime",
            "owner_unit": "M1-S04B-02",
            "disabled": self.disabled,
            "audits": self._audits,
            "failures": self._failures,
            "source_roles": [item.to_dict() for item in self.source_roles()],
            "state_custody": [item.to_dict() for item in self.state_custody()],
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
