from __future__ import annotations

"""Task-checkpoint integration for one-shot browser context delivery.

The browser state pipeline owns neither the task checkpoint nor the model
context.  This module is therefore deliberately a transactional projection:
``TaskState.metadata`` persists the delivery queue, while
``ClaudeContextWindowManager`` remains the only component that selects and
budgets provider messages.  A disclosure is consumed only after a real
CodeWorker invocation returns from its query runtime; creating the browser
turn merely enqueues it.
"""

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType, now_iso, to_jsonable
from .window_contract import (
    ClaudeContextBudget,
    ClaudeContextWindowManager,
    context_window_from_payload,
)

from ..browser_state.contracts import digest_json, state_id
from ..browser_state.errors import BrowserNextContextUnavailable
from .models import BrowserMemoryCandidate, BrowserMessageTurn


BROWSER_CONTEXT_CHECKPOINT_SCHEMA = "zyra.browser-context-task-checkpoint.v1"
BROWSER_CONTEXT_DELIVERY_SCHEMA = "zyra.browser-context-delivery.v1"
BROWSER_MEMORY_CANDIDATE_SCHEMA = "zyra.browser-memory-candidate.v1"


class BrowserContextCheckpointError(RuntimeError):
    """Base error for corrupt, foreign, or conflicting context projections."""

    code = "browser_context_checkpoint_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class BrowserContextCheckpointCorrupt(BrowserContextCheckpointError):
    code = "browser_context_checkpoint_corrupt"


class BrowserContextScopeMismatch(BrowserContextCheckpointError):
    code = "browser_context_checkpoint_scope_mismatch"


class BrowserContextDeliveryConflict(BrowserContextCheckpointError):
    code = "browser_context_delivery_conflict"


class BrowserContextDeliveryState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    RELEASED = "released"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class BrowserContextScope:
    run_id: str
    task_id: str
    session_id: str

    def __post_init__(self) -> None:
        missing = [name for name, value in (
            ("run_id", self.run_id),
            ("task_id", self.task_id),
            ("session_id", self.session_id),
        ) if not str(value).strip()]
        if missing:
            raise BrowserContextCheckpointCorrupt(
                "browser context scope is incomplete",
                details={"missing": missing},
            )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BrowserContextScope":
        return cls(
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            session_id=str(value.get("session_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class BrowserContextQueueItem:
    source_id: str
    disclosure_id: str
    disclosure_fingerprint: str
    context_block: Mapping[str, Any]
    capture_id: str
    selector_revision_id: str
    producer_worker_request_id: str
    artifact_ids: tuple[str, ...]
    action_receipt_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    state: BrowserContextDeliveryState = BrowserContextDeliveryState.PENDING
    claimed_by_worker_request_id: str = ""
    claim_id: str = ""
    claim_attempt: int = 0
    consumed_by_worker_request_id: str = ""
    consumed_event_ids: tuple[str, ...] = ()
    release_reason: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.source_id.startswith("browser-disclosure:"):
            raise BrowserContextCheckpointCorrupt(
                "browser disclosure source id is invalid",
                details={"source_id": self.source_id},
            )
        if not self.disclosure_id or not self.disclosure_fingerprint:
            raise BrowserContextCheckpointCorrupt("browser context queue identity is incomplete")
        block = dict(self.context_block)
        metadata = block.get("metadata") if isinstance(block.get("metadata"), Mapping) else {}
        if str(metadata.get("browser_disclosure_id") or "") != self.disclosure_id:
            raise BrowserContextCheckpointCorrupt(
                "context block disclosure identity does not match queue item",
                details={"disclosure_id": self.disclosure_id},
            )
        if self.state == BrowserContextDeliveryState.CLAIMED and not (
            self.claim_id and self.claimed_by_worker_request_id
        ):
            raise BrowserContextCheckpointCorrupt("claimed browser disclosure has no claim identity")
        if self.state == BrowserContextDeliveryState.CONSUMED and not self.consumed_by_worker_request_id:
            raise BrowserContextCheckpointCorrupt("consumed browser disclosure has no consumer identity")
        object.__setattr__(self, "context_block", block)

    @property
    def read_once(self) -> bool:
        metadata = self.context_block.get("metadata")
        return bool(metadata.get("read_once")) if isinstance(metadata, Mapping) else True

    @property
    def pending(self) -> bool:
        return self.state in {BrowserContextDeliveryState.PENDING, BrowserContextDeliveryState.RELEASED}

    @property
    def active_claim(self) -> bool:
        return self.state in {
            BrowserContextDeliveryState.CLAIMED,
            BrowserContextDeliveryState.INDETERMINATE,
        }

    @property
    def payload_digest(self) -> str:
        return digest_json({
            "source_id": self.source_id,
            "disclosure_fingerprint": self.disclosure_fingerprint,
            "context_block": self.context_block,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "artifact_ids": self.artifact_ids,
            "action_receipt_ids": self.action_receipt_ids,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "disclosure_id": self.disclosure_id,
            "disclosure_fingerprint": self.disclosure_fingerprint,
            "context_block": to_jsonable(dict(self.context_block)),
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "producer_worker_request_id": self.producer_worker_request_id,
            "artifact_ids": list(self.artifact_ids),
            "action_receipt_ids": list(self.action_receipt_ids),
            "event_ids": list(self.event_ids),
            "state": str(self.state),
            "claimed_by_worker_request_id": self.claimed_by_worker_request_id,
            "claim_id": self.claim_id,
            "claim_attempt": self.claim_attempt,
            "consumed_by_worker_request_id": self.consumed_by_worker_request_id,
            "consumed_event_ids": list(self.consumed_event_ids),
            "release_reason": self.release_reason,
            "read_once": self.read_once,
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BrowserContextQueueItem":
        try:
            state = BrowserContextDeliveryState(str(value.get("state") or "pending"))
        except ValueError as error:
            raise BrowserContextCheckpointCorrupt(
                "browser disclosure has an unknown delivery state",
                details={"state": value.get("state")},
            ) from error
        item = cls(
            source_id=str(value.get("source_id") or ""),
            disclosure_id=str(value.get("disclosure_id") or ""),
            disclosure_fingerprint=str(value.get("disclosure_fingerprint") or ""),
            context_block=_mapping(value.get("context_block")),
            capture_id=str(value.get("capture_id") or ""),
            selector_revision_id=str(value.get("selector_revision_id") or ""),
            producer_worker_request_id=str(value.get("producer_worker_request_id") or ""),
            artifact_ids=_strings(value.get("artifact_ids")),
            action_receipt_ids=_strings(value.get("action_receipt_ids")),
            event_ids=_strings(value.get("event_ids")),
            state=state,
            claimed_by_worker_request_id=str(value.get("claimed_by_worker_request_id") or ""),
            claim_id=str(value.get("claim_id") or ""),
            claim_attempt=_integer(value.get("claim_attempt")),
            consumed_by_worker_request_id=str(value.get("consumed_by_worker_request_id") or ""),
            consumed_event_ids=_strings(value.get("consumed_event_ids")),
            release_reason=str(value.get("release_reason") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )
        expected = str(value.get("payload_digest") or "")
        if expected and expected != item.payload_digest:
            raise BrowserContextCheckpointCorrupt(
                "browser disclosure queue digest mismatch",
                details={"source_id": item.source_id, "expected": expected, "actual": item.payload_digest},
            )
        return item


@dataclass(frozen=True, slots=True)
class BrowserContextDeliveryBatch:
    batch_id: str
    claim_id: str
    scope: BrowserContextScope
    consumer_worker_request_id: str
    source_ids: tuple[str, ...]
    disclosure_ids: tuple[str, ...]
    messages: tuple[Mapping[str, Any], ...]
    checkpoint_revision: int
    created_at: str = field(default_factory=now_iso)

    @property
    def empty(self) -> bool:
        return not self.source_ids

    @property
    def fingerprint(self) -> str:
        return digest_json({
            "claim_id": self.claim_id,
            "scope": self.scope.to_dict(),
            "consumer_worker_request_id": self.consumer_worker_request_id,
            "source_ids": self.source_ids,
            "messages": self.messages,
            "checkpoint_revision": self.checkpoint_revision,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BROWSER_CONTEXT_DELIVERY_SCHEMA,
            "batch_id": self.batch_id,
            "claim_id": self.claim_id,
            "scope": self.scope.to_dict(),
            "consumer_worker_request_id": self.consumer_worker_request_id,
            "source_ids": list(self.source_ids),
            "disclosure_ids": list(self.disclosure_ids),
            "messages": [to_jsonable(dict(item)) for item in self.messages],
            "checkpoint_revision": self.checkpoint_revision,
            "empty": self.empty,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserContextProviderSelectionReceipt:
    batch_id: str
    claim_id: str
    consumer_worker_request_id: str
    expected_source_ids: tuple[str, ...]
    selected_source_ids: tuple[str, ...]
    provider_request_ids: tuple[str, ...]
    provider_turn_ids: tuple[str, ...]
    provider_message_count: int
    matching_message_count: int
    event_ids: tuple[str, ...]
    findings: tuple[str, ...] = ()

    @property
    def selected_exactly_once(self) -> bool:
        return (
            set(self.expected_source_ids) == set(self.selected_source_ids)
            and self.matching_message_count == len(self.expected_source_ids)
        )

    @property
    def valid(self) -> bool:
        return self.selected_exactly_once and not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-context-provider-selection.v1",
            "batch_id": self.batch_id,
            "claim_id": self.claim_id,
            "consumer_worker_request_id": self.consumer_worker_request_id,
            "expected_source_ids": list(self.expected_source_ids),
            "selected_source_ids": list(self.selected_source_ids),
            "provider_request_ids": list(self.provider_request_ids),
            "provider_turn_ids": list(self.provider_turn_ids),
            "provider_message_count": self.provider_message_count,
            "matching_message_count": self.matching_message_count,
            "selected_exactly_once": self.selected_exactly_once,
            "valid": self.valid,
            "event_ids": list(self.event_ids),
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class BrowserContextTaskCheckpoint:
    scope: BrowserContextScope
    revision: int
    context_window_state: Mapping[str, Any]
    queue: tuple[BrowserContextQueueItem, ...]
    history_messages: tuple[Mapping[str, Any], ...] = ()
    memory_candidates: tuple[Mapping[str, Any], ...] = ()
    last_turn_id: str = ""
    last_capture_id: str = ""
    last_selector_revision_id: str = ""
    last_worker_request_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise BrowserContextCheckpointCorrupt("browser context checkpoint revision cannot be negative")
        source_ids = [item.source_id for item in self.queue]
        if len(source_ids) != len(set(source_ids)):
            raise BrowserContextCheckpointCorrupt("browser context checkpoint contains duplicate source ids")
        for item in self.queue:
            if item.disclosure_id not in str(item.context_block):
                raise BrowserContextCheckpointCorrupt(
                    "browser context block no longer carries its disclosure identity",
                    details={"source_id": item.source_id},
                )

    @property
    def pending_count(self) -> int:
        return sum(item.pending for item in self.queue)

    @property
    def claimed_count(self) -> int:
        return sum(item.active_claim for item in self.queue)

    @property
    def consumed_count(self) -> int:
        return sum(item.state == BrowserContextDeliveryState.CONSUMED for item in self.queue)

    @property
    def digest(self) -> str:
        return digest_json(self._payload(include_digest=False))

    def _payload(self, *, include_digest: bool) -> dict[str, Any]:
        payload = {
            "schema": BROWSER_CONTEXT_CHECKPOINT_SCHEMA,
            "scope": self.scope.to_dict(),
            "scope_digest": self.scope.digest,
            "revision": self.revision,
            "context_window_state": to_jsonable(dict(self.context_window_state)),
            "queue": [item.to_dict() for item in self.queue],
            "history_messages": [to_jsonable(dict(item)) for item in self.history_messages],
            "memory_candidates": [to_jsonable(dict(item)) for item in self.memory_candidates],
            "last_turn_id": self.last_turn_id,
            "last_capture_id": self.last_capture_id,
            "last_selector_revision_id": self.last_selector_revision_id,
            "last_worker_request_id": self.last_worker_request_id,
            "pending_count": self.pending_count,
            "claimed_count": self.claimed_count,
            "consumed_count": self.consumed_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_digest:
            payload["checkpoint_digest"] = self.digest
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload(include_digest=True)

    @classmethod
    def empty(cls, scope: BrowserContextScope, *, max_chars: int = 32000) -> "BrowserContextTaskCheckpoint":
        manager = ClaudeContextWindowManager(
            budget=ClaudeContextBudget(
                max_chars=max(4096, int(max_chars)),
                reserve_chars=min(1024, max(256, int(max_chars) // 8)),
                min_recent_blocks=1,
            ),
            runtime_source="zyra-browser-external-context",
            runtime_id="M1-S04B-02",
            session_id=scope.session_id,
            request_id="",
        )
        return cls(scope=scope, revision=0, context_window_state=manager.snapshot(), queue=())

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_scope: BrowserContextScope | None = None,
    ) -> "BrowserContextTaskCheckpoint":
        if str(value.get("schema") or "") != BROWSER_CONTEXT_CHECKPOINT_SCHEMA:
            raise BrowserContextCheckpointCorrupt(
                "browser context checkpoint schema is missing or unsupported",
                details={"schema": value.get("schema")},
            )
        scope = BrowserContextScope.from_mapping(_mapping(value.get("scope")))
        if str(value.get("scope_digest") or "") not in {"", scope.digest}:
            raise BrowserContextCheckpointCorrupt("browser context checkpoint scope digest mismatch")
        if expected_scope is not None and scope != expected_scope:
            raise BrowserContextScopeMismatch(
                "browser context checkpoint belongs to another run, task, or session",
                details={"expected": expected_scope.to_dict(), "actual": scope.to_dict()},
            )
        checkpoint = cls(
            scope=scope,
            revision=_integer(value.get("revision")),
            context_window_state=_mapping(value.get("context_window_state")),
            queue=tuple(
                BrowserContextQueueItem.from_mapping(item)
                for item in _sequence(value.get("queue"))
                if isinstance(item, Mapping)
            ),
            history_messages=tuple(
                dict(item) for item in _sequence(value.get("history_messages")) if isinstance(item, Mapping)
            ),
            memory_candidates=tuple(
                dict(item) for item in _sequence(value.get("memory_candidates")) if isinstance(item, Mapping)
            ),
            last_turn_id=str(value.get("last_turn_id") or ""),
            last_capture_id=str(value.get("last_capture_id") or ""),
            last_selector_revision_id=str(value.get("last_selector_revision_id") or ""),
            last_worker_request_id=str(value.get("last_worker_request_id") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )
        expected_digest = str(value.get("checkpoint_digest") or "")
        if expected_digest and expected_digest != checkpoint.digest:
            raise BrowserContextCheckpointCorrupt(
                "browser context checkpoint digest mismatch",
                details={"expected": expected_digest, "actual": checkpoint.digest},
            )
        return checkpoint


@dataclass(frozen=True, slots=True)
class BrowserContextTurnSession:
    checkpoint: BrowserContextTaskCheckpoint
    context_window: ClaudeContextWindowManager
    pending_provider_messages: tuple[Mapping[str, Any], ...]
    restored_source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_revision": self.checkpoint.revision,
            "pending_provider_messages": [to_jsonable(dict(item)) for item in self.pending_provider_messages],
            "restored_source_ids": list(self.restored_source_ids),
            "context_window": self.context_window.snapshot(include_text=False),
        }


@dataclass(frozen=True, slots=True)
class BrowserMemoryCandidateReceipt:
    candidate_id: str
    event_id: str
    run_id: str
    task_id: str
    accepted_for_review: bool
    committed: bool
    source_dom_capture_id: str
    source_action_receipt_id: str
    artifact_ids: tuple[str, ...]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BROWSER_MEMORY_CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "accepted_for_review": self.accepted_for_review,
            "committed": self.committed,
            "source_dom_capture_id": self.source_dom_capture_id,
            "source_action_receipt_id": self.source_action_receipt_id,
            "artifact_ids": list(self.artifact_ids),
            "reason": self.reason,
        }


class BrowserMemoryCandidateConsumerPort:
    """Validate persisted candidate events without committing MemoryFabric state."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._accepted = 0
        self._rejected = 0

    def consume_event(
        self,
        event: EventRecord | Mapping[str, Any],
        *,
        expected_run_id: str,
        expected_task_id: str,
        known_artifact_ids: Iterable[str] = (),
        known_capture_ids: Iterable[str] = (),
        known_action_receipt_ids: Iterable[str] = (),
    ) -> BrowserMemoryCandidateReceipt:
        if self.disabled:
            raise BrowserNextContextUnavailable("browser memory candidate consumer is disabled")
        raw_value = to_jsonable(event)
        raw = dict(raw_value) if isinstance(raw_value, Mapping) else {}
        payload = _mapping(raw.get("payload"))
        candidate = _mapping(payload.get("browser_memory_candidate"))
        event_id = str(raw.get("event_id") or "")
        run_id = str(raw.get("run_id") or "")
        task_id = str(raw.get("task_id") or "")
        failures: list[str] = []
        if run_id != expected_run_id or task_id != expected_task_id:
            failures.append("scope_mismatch")
        if not event_id:
            failures.append("event_id_missing")
        if not candidate:
            failures.append("candidate_payload_missing")
        if not bool(candidate.get("candidate_only", False)):
            failures.append("candidate_only_false")
        if bool(payload.get("committed", False)):
            failures.append("unexpected_memory_commit")
        capture_id = str(candidate.get("source_dom_capture_id") or "")
        action_id = str(candidate.get("source_action_receipt_id") or "")
        artifacts = _strings(candidate.get("artifact_ids"))
        known_artifacts = set(known_artifact_ids)
        known_captures = set(known_capture_ids)
        known_actions = set(known_action_receipt_ids)
        if known_captures and capture_id not in known_captures:
            failures.append("capture_cause_missing")
        if action_id and known_actions and action_id not in known_actions:
            failures.append("action_cause_missing")
        if known_artifacts and any(item not in known_artifacts for item in artifacts):
            failures.append("artifact_cause_missing")
        source_event_id = str(candidate.get("source_event_id") or "")
        cause_event_id = str(payload.get("cause_event_id") or "")
        if not source_event_id and not cause_event_id:
            failures.append("event_cause_missing")
        accepted = not failures
        if accepted:
            self._accepted += 1
        else:
            self._rejected += 1
        receipt = BrowserMemoryCandidateReceipt(
            candidate_id=str(candidate.get("candidate_id") or ""),
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            accepted_for_review=accepted,
            committed=False,
            source_dom_capture_id=capture_id,
            source_action_receipt_id=action_id,
            artifact_ids=artifacts,
            reason=",".join(failures),
        )
        if not accepted:
            raise BrowserContextCheckpointCorrupt(
                "browser memory candidate failed consumer validation",
                details=receipt.to_dict(),
            )
        return receipt

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserMemoryCandidateConsumerPort",
            "owner_unit": "M1-S04B-02",
            "canonical_memory_owner": "MemoryFabric/M1-06B-M1-06C",
            "accepted": self._accepted,
            "rejected": self._rejected,
            "direct_memory_writes": 0,
        }


class BrowserContextTaskIntegrationRuntime:
    """Checkpoint, claim, delivery, and reconciliation for browser disclosures."""

    def __init__(self, *, disabled: bool = False, queue_limit: int = 128) -> None:
        self.disabled = disabled
        self.queue_limit = max(8, int(queue_limit))
        self._lock = threading.RLock()
        self._restores = 0
        self._enqueues = 0
        self._duplicates = 0
        self._claims = 0
        self._consumes = 0
        self._releases = 0
        self._scope_rejections = 0

    def scope(self, *, run_id: str, task_id: str, session_id: str = "") -> BrowserContextScope:
        return BrowserContextScope(
            run_id=str(run_id),
            task_id=str(task_id),
            session_id=str(session_id or f"task:{task_id}"),
        )

    def checkpoint_from_metadata(
        self,
        metadata: Mapping[str, Any],
        *,
        scope: BrowserContextScope,
        max_chars: int = 32000,
    ) -> BrowserContextTaskCheckpoint:
        raw = metadata.get("browser_context_runtime_state")
        if not isinstance(raw, Mapping) or not raw:
            return BrowserContextTaskCheckpoint.empty(scope, max_chars=max_chars)
        try:
            checkpoint = BrowserContextTaskCheckpoint.from_mapping(raw, expected_scope=scope)
        except BrowserContextScopeMismatch:
            self._scope_rejections += 1
            raise
        self._restores += 1
        return checkpoint

    def begin_browser_turn(
        self,
        checkpoint: BrowserContextTaskCheckpoint,
        *,
        worker_request_id: str,
        max_chars: int = 32000,
    ) -> BrowserContextTurnSession:
        if self.disabled:
            raise BrowserNextContextUnavailable("browser context task integration is disabled")
        window_payload = dict(checkpoint.context_window_state)
        blocks = []
        pending_ids = {item.disclosure_id for item in checkpoint.queue if item.pending or item.active_claim}
        for block in _sequence(window_payload.get("blocks")):
            if not isinstance(block, Mapping):
                continue
            metadata = _mapping(block.get("metadata"))
            disclosure_id = str(metadata.get("browser_disclosure_id") or "")
            if disclosure_id and disclosure_id not in pending_ids:
                continue
            blocks.append(dict(block))
        window_payload["blocks"] = blocks
        window_payload["session_id"] = checkpoint.scope.session_id
        window_payload["worker_request_id"] = worker_request_id
        if not window_payload.get("budget"):
            window_payload["budget"] = ClaudeContextBudget(
                max_chars=max(4096, max_chars), reserve_chars=1024, min_recent_blocks=1
            ).to_dict()
        manager = context_window_from_payload(window_payload)
        selected = tuple(
            message for message in manager.model_messages(max_chars=max_chars)
            if isinstance(message, Mapping)
            and str(_mapping(message.get("metadata")).get("browser_disclosure_id") or "") in pending_ids
        )
        restored_ids = tuple(
            str(_mapping(message.get("metadata")).get("browser_disclosure_id") or "")
            for message in selected
        )
        return BrowserContextTurnSession(
            checkpoint=checkpoint,
            context_window=manager,
            pending_provider_messages=selected,
            restored_source_ids=tuple(f"browser-disclosure:{item}" for item in restored_ids if item),
        )

    def commit_browser_turn(
        self,
        session: BrowserContextTurnSession,
        turn: BrowserMessageTurn,
    ) -> BrowserContextTaskCheckpoint:
        if self.disabled:
            raise BrowserNextContextUnavailable("browser context task integration is disabled")
        checkpoint = session.checkpoint
        if turn.run_id != checkpoint.scope.run_id or turn.task_id != checkpoint.scope.task_id:
            self._scope_rejections += 1
            raise BrowserContextScopeMismatch(
                "browser turn belongs to another checkpoint scope",
                details={
                    "checkpoint": checkpoint.scope.to_dict(),
                    "turn": {"run_id": turn.run_id, "task_id": turn.task_id},
                },
            )
        source_id = f"browser-disclosure:{turn.disclosure.disclosure_id}"
        context_block = dict(turn.next_context.context_block)
        item = BrowserContextQueueItem(
            source_id=source_id,
            disclosure_id=turn.disclosure.disclosure_id,
            disclosure_fingerprint=digest_json(turn.disclosure.to_dict()),
            context_block=context_block,
            capture_id=turn.capture_id,
            selector_revision_id=turn.selector_revision_id,
            producer_worker_request_id=turn.worker_request_id,
            artifact_ids=turn.artifact_ids,
            action_receipt_ids=tuple(action.receipt_id for action in turn.action_results),
            event_ids=tuple(event.event_id for event in turn.events),
        )
        with self._lock:
            queue = list(checkpoint.queue)
            existing_index = next((index for index, value in enumerate(queue) if value.source_id == source_id), None)
            if existing_index is not None:
                existing = queue[existing_index]
                if existing.payload_digest != item.payload_digest:
                    raise BrowserContextDeliveryConflict(
                        "browser disclosure source id was replayed with different content",
                        details={"source_id": source_id},
                    )
                self._duplicates += 1
            else:
                queue.append(item)
                self._enqueues += 1
            queue = self._bounded_queue(queue)
            histories = self._merge_history(checkpoint.history_messages, turn)
            candidates = self._merge_candidates(checkpoint.memory_candidates, turn.memory_candidates)
            return BrowserContextTaskCheckpoint(
                scope=checkpoint.scope,
                revision=checkpoint.revision + 1,
                context_window_state=session.context_window.snapshot(),
                queue=tuple(queue),
                history_messages=histories,
                memory_candidates=candidates,
                last_turn_id=turn.turn_id,
                last_capture_id=turn.capture_id,
                last_selector_revision_id=turn.selector_revision_id,
                last_worker_request_id=turn.worker_request_id,
                created_at=checkpoint.created_at,
                updated_at=now_iso(),
            )

    def persist_checkpoint(
        self,
        metadata: MutableMapping[str, Any],
        checkpoint: BrowserContextTaskCheckpoint,
    ) -> None:
        metadata["browser_context_runtime_state"] = checkpoint.to_dict()
        metadata["browser_context_projection"] = self.public_projection(checkpoint)
        metadata["query_session_id"] = checkpoint.scope.session_id

    def prepare_delivery(
        self,
        metadata: MutableMapping[str, Any],
        *,
        scope: BrowserContextScope,
        consumer_worker_request_id: str,
        max_items: int = 1,
        max_chars: int = 24000,
    ) -> BrowserContextDeliveryBatch:
        if self.disabled:
            raise BrowserNextContextUnavailable("browser context delivery is disabled")
        with self._lock:
            checkpoint = self.checkpoint_from_metadata(metadata, scope=scope, max_chars=max_chars)
            claim_id = state_id("brctxclaim")
            selected: list[BrowserContextQueueItem] = []
            messages: list[Mapping[str, Any]] = []
            remaining = max(0, int(max_chars))
            queue: list[BrowserContextQueueItem] = []
            for item in checkpoint.queue:
                eligible = item.pending or (
                    item.active_claim and item.claimed_by_worker_request_id == consumer_worker_request_id
                )
                if eligible and len(selected) < max(0, int(max_items)) and remaining > 0:
                    message = self._delivery_message(item, remaining=remaining)
                    content = str(message.get("content") or "")
                    if content:
                        effective_claim_id = item.claim_id or claim_id
                        claimed = replace(
                            item,
                            state=BrowserContextDeliveryState.CLAIMED,
                            claimed_by_worker_request_id=consumer_worker_request_id,
                            claim_id=effective_claim_id,
                            claim_attempt=item.claim_attempt + (0 if item.claim_id else 1),
                            release_reason="",
                            updated_at=now_iso(),
                        )
                        selected.append(claimed)
                        messages.append(message)
                        remaining -= len(content)
                        queue.append(claimed)
                        continue
                queue.append(item)
            actual_claim = selected[0].claim_id if selected else claim_id
            updated = replace(
                checkpoint,
                revision=checkpoint.revision + (1 if selected else 0),
                queue=tuple(queue),
                updated_at=now_iso(),
            )
            self.persist_checkpoint(metadata, updated)
            if selected:
                self._claims += len(selected)
            return BrowserContextDeliveryBatch(
                batch_id=state_id("brctxbatch"),
                claim_id=actual_claim,
                scope=scope,
                consumer_worker_request_id=consumer_worker_request_id,
                source_ids=tuple(item.source_id for item in selected),
                disclosure_ids=tuple(item.disclosure_id for item in selected),
                messages=tuple(messages),
                checkpoint_revision=updated.revision,
            )

    def commit_delivery(
        self,
        metadata: MutableMapping[str, Any],
        *,
        batch: BrowserContextDeliveryBatch,
        worker_event_ids: Sequence[str],
    ) -> BrowserContextTaskCheckpoint:
        with self._lock:
            checkpoint = self.checkpoint_from_metadata(metadata, scope=batch.scope)
            queue: list[BrowserContextQueueItem] = []
            matched: set[str] = set()
            for item in checkpoint.queue:
                if item.source_id not in batch.source_ids:
                    queue.append(item)
                    continue
                if item.claimed_by_worker_request_id != batch.consumer_worker_request_id:
                    raise BrowserContextDeliveryConflict(
                        "browser disclosure claim is owned by another worker request",
                        details={"source_id": item.source_id},
                    )
                if item.claim_id != batch.claim_id:
                    raise BrowserContextDeliveryConflict(
                        "browser disclosure claim id changed before consumption",
                        details={"source_id": item.source_id},
                    )
                queue.append(replace(
                    item,
                    state=BrowserContextDeliveryState.CONSUMED,
                    consumed_by_worker_request_id=batch.consumer_worker_request_id,
                    consumed_event_ids=tuple(dict.fromkeys(str(value) for value in worker_event_ids if value)),
                    updated_at=now_iso(),
                ))
                matched.add(item.source_id)
            if matched != set(batch.source_ids):
                raise BrowserContextDeliveryConflict(
                    "browser disclosure claim disappeared before consumption",
                    details={"expected": list(batch.source_ids), "matched": sorted(matched)},
                )
            updated = replace(
                checkpoint,
                revision=checkpoint.revision + (1 if matched else 0),
                queue=tuple(queue),
                updated_at=now_iso(),
            )
            self.persist_checkpoint(metadata, updated)
            self._consumes += len(matched)
            return updated

    def verify_provider_selection(
        self,
        batch: BrowserContextDeliveryBatch,
        events: Sequence[EventRecord | Mapping[str, Any]],
    ) -> BrowserContextProviderSelectionReceipt:
        """Prove 02D placed every claimed disclosure in a provider envelope."""

        if batch.empty:
            return BrowserContextProviderSelectionReceipt(
                batch_id=batch.batch_id,
                claim_id=batch.claim_id,
                consumer_worker_request_id=batch.consumer_worker_request_id,
                expected_source_ids=(),
                selected_source_ids=(),
                provider_request_ids=(),
                provider_turn_ids=(),
                provider_message_count=0,
                matching_message_count=0,
                event_ids=(),
            )
        expected = set(batch.source_ids)
        selected: list[str] = []
        provider_request_ids: list[str] = []
        provider_turn_ids: list[str] = []
        event_ids: list[str] = []
        provider_message_count = 0
        findings: list[str] = []
        for event in events:
            raw_value = to_jsonable(event)
            raw = dict(raw_value) if isinstance(raw_value, Mapping) else {}
            payload = _mapping(raw.get("payload"))
            session_payload = _mapping(payload.get("query_session"))
            if str(session_payload.get("phase") or "") != "model_stream_report":
                continue
            report = _mapping(session_payload.get("model_stream"))
            envelope = _mapping(report.get("envelope"))
            if str(envelope.get("worker_request_id") or "") != batch.consumer_worker_request_id:
                continue
            provider_request_ids.append(str(envelope.get("request_id") or ""))
            provider_turn_ids.append(str(envelope.get("turn_id") or ""))
            event_ids.append(str(raw.get("event_id") or ""))
            messages = _sequence(envelope.get("messages"))
            provider_message_count += len(messages)
            for message in messages:
                if not isinstance(message, Mapping):
                    continue
                metadata = _mapping(message.get("metadata"))
                source_id = str(metadata.get("browser_context_source_id") or "")
                disclosure_id = str(metadata.get("browser_disclosure_id") or "")
                if not source_id and disclosure_id:
                    source_id = f"browser-disclosure:{disclosure_id}"
                if source_id not in expected:
                    continue
                content = str(message.get("content") or "")
                if not content:
                    findings.append(f"selected_context_empty:{source_id}")
                    continue
                selected.append(source_id)
        counts = {source_id: selected.count(source_id) for source_id in expected}
        missing = sorted(source_id for source_id, count in counts.items() if count == 0)
        duplicated = sorted(source_id for source_id, count in counts.items() if count > 1)
        if missing:
            findings.append("provider_context_missing:" + ",".join(missing))
        if duplicated:
            findings.append("provider_context_repeated:" + ",".join(duplicated))
        if not provider_request_ids:
            findings.append("provider_envelope_event_missing")
        receipt = BrowserContextProviderSelectionReceipt(
            batch_id=batch.batch_id,
            claim_id=batch.claim_id,
            consumer_worker_request_id=batch.consumer_worker_request_id,
            expected_source_ids=batch.source_ids,
            selected_source_ids=tuple(dict.fromkeys(selected)),
            provider_request_ids=tuple(dict.fromkeys(item for item in provider_request_ids if item)),
            provider_turn_ids=tuple(dict.fromkeys(item for item in provider_turn_ids if item)),
            provider_message_count=provider_message_count,
            matching_message_count=len(selected),
            event_ids=tuple(dict.fromkeys(item for item in event_ids if item)),
            findings=tuple(findings),
        )
        return receipt

    def release_delivery(
        self,
        metadata: MutableMapping[str, Any],
        *,
        batch: BrowserContextDeliveryBatch,
        reason: str,
        indeterminate: bool = False,
    ) -> BrowserContextTaskCheckpoint:
        with self._lock:
            checkpoint = self.checkpoint_from_metadata(metadata, scope=batch.scope)
            queue: list[BrowserContextQueueItem] = []
            changed = 0
            for item in checkpoint.queue:
                if item.source_id in batch.source_ids and item.claim_id == batch.claim_id:
                    queue.append(replace(
                        item,
                        state=(
                            BrowserContextDeliveryState.INDETERMINATE
                            if indeterminate
                            else BrowserContextDeliveryState.RELEASED
                        ),
                        release_reason=str(reason)[:1000],
                        claimed_by_worker_request_id=(item.claimed_by_worker_request_id if indeterminate else ""),
                        claim_id=(item.claim_id if indeterminate else ""),
                        updated_at=now_iso(),
                    ))
                    changed += 1
                else:
                    queue.append(item)
            updated = replace(
                checkpoint,
                revision=checkpoint.revision + (1 if changed else 0),
                queue=tuple(queue),
                updated_at=now_iso(),
            )
            self.persist_checkpoint(metadata, updated)
            self._releases += changed
            return updated

    def event_for_enqueue(
        self,
        checkpoint: BrowserContextTaskCheckpoint,
        turn: BrowserMessageTurn,
        *,
        node_id: str,
    ) -> EventRecord:
        item = next(value for value in checkpoint.queue if value.disclosure_id == turn.disclosure.disclosure_id)
        return EventRecord(
            run_id=checkpoint.scope.run_id,
            task_id=checkpoint.scope.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_external_context": {
                    "schema": BROWSER_CONTEXT_DELIVERY_SCHEMA,
                    "operation": "pending",
                    "source_id": item.source_id,
                    "disclosure_id": item.disclosure_id,
                    "capture_id": item.capture_id,
                    "selector_revision_id": item.selector_revision_id,
                    "artifact_ids": list(item.artifact_ids),
                    "checkpoint_revision": checkpoint.revision,
                    "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
                    "state_owner": "TaskState.metadata/SQLiteStore",
                    "read_once_consumed": False,
                },
                "cause_event_ids": list(item.event_ids),
            },
        )

    def event_for_delivery(
        self,
        batch: BrowserContextDeliveryBatch,
        *,
        node_id: str,
        operation: str,
        worker_event_ids: Sequence[str] = (),
        reason: str = "",
    ) -> EventRecord:
        return EventRecord(
            run_id=batch.scope.run_id,
            task_id=batch.scope.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_external_context": {
                    "schema": BROWSER_CONTEXT_DELIVERY_SCHEMA,
                    "operation": operation,
                    "batch_id": batch.batch_id,
                    "claim_id": batch.claim_id,
                    "source_ids": list(batch.source_ids),
                    "disclosure_ids": list(batch.disclosure_ids),
                    "consumer_worker_request_id": batch.consumer_worker_request_id,
                    "provider_message_count": len(batch.messages),
                    "worker_event_ids": list(worker_event_ids),
                    "reason": reason,
                    "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
                },
                "cause_event_ids": list(worker_event_ids),
            },
        )

    def public_projection(self, checkpoint: BrowserContextTaskCheckpoint) -> dict[str, Any]:
        return {
            "schema": BROWSER_CONTEXT_CHECKPOINT_SCHEMA,
            "scope": checkpoint.scope.to_dict(),
            "revision": checkpoint.revision,
            "pending_count": checkpoint.pending_count,
            "claimed_count": checkpoint.claimed_count,
            "consumed_count": checkpoint.consumed_count,
            "pending": [
                {
                    "source_id": item.source_id,
                    "disclosure_id": item.disclosure_id,
                    "capture_id": item.capture_id,
                    "selector_revision_id": item.selector_revision_id,
                    "artifact_ids": list(item.artifact_ids),
                    "state": str(item.state),
                    "producer_worker_request_id": item.producer_worker_request_id,
                }
                for item in checkpoint.queue if item.pending or item.active_claim
            ],
            "recent_consumed": [
                {
                    "source_id": item.source_id,
                    "consumer_worker_request_id": item.consumed_by_worker_request_id,
                    "event_ids": list(item.consumed_event_ids),
                }
                for item in checkpoint.queue
                if item.state == BrowserContextDeliveryState.CONSUMED
            ][-16:],
            "history_message_count": len(checkpoint.history_messages),
            "memory_candidate_count": len(checkpoint.memory_candidates),
            "last_turn_id": checkpoint.last_turn_id,
            "last_capture_id": checkpoint.last_capture_id,
            "last_selector_revision_id": checkpoint.last_selector_revision_id,
            "checkpoint_digest": checkpoint.digest,
            "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
            "canonical_history_owner": "event log + M1-02D context",
            "canonical_memory_owner": "MemoryFabric/M1-06B-M1-06C",
        }

    def _delivery_message(self, item: BrowserContextQueueItem, *, remaining: int) -> Mapping[str, Any]:
        text = str(item.context_block.get("text") or "")
        if not text:
            raise BrowserContextCheckpointCorrupt(
                "pending browser disclosure has no context text",
                details={"source_id": item.source_id},
            )
        if len(text) > remaining:
            return {}
        metadata = _mapping(item.context_block.get("metadata"))
        source = _mapping(item.context_block.get("source"))
        delivery_metadata = {
            **dict(metadata),
            "browser_disclosure_id": item.disclosure_id,
            "browser_context_source_id": item.source_id,
            "browser_capture_id": item.capture_id,
            "browser_selector_revision_id": item.selector_revision_id,
            "browser_action_receipt_ids": list(item.action_receipt_ids),
            "browser_artifact_ids": list(item.artifact_ids),
            "source_provenance": source.get("source_kind") or "browser_context_disclosure",
            "trust_level": "external_untrusted",
            "secret_redaction_state": "clean",
            "external": True,
            "read_once": True,
            "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
        }
        return {
            # Seed as a system custody block so 02D will not compact away a
            # read-once disclosure before its first provider request.  The
            # explicit external/untrusted metadata still forces the rendered
            # provider role back to ``user``; browser data never gains system
            # authority.
            "role": "system",
            "content": text,
            # 02D's request-message boundary retains non-role/content fields as
            # context-block metadata.  Keep a nested copy for ordinary API
            # consumers and duplicate the typed provenance at the top level so
            # it survives provider selection, security labeling and compaction.
            "metadata": dict(delivery_metadata),
            **delivery_metadata,
        }

    def _merge_history(
        self,
        previous: Sequence[Mapping[str, Any]],
        turn: BrowserMessageTurn,
    ) -> tuple[Mapping[str, Any], ...]:
        values: list[Mapping[str, Any]] = [dict(item) for item in previous]
        seen = {str(item.get("message_id") or "") for item in values}
        for message in turn.messages:
            if message.message_id in seen:
                continue
            values.append(message.to_dict())
            seen.add(message.message_id)
        return tuple(values[-256:])

    def _merge_candidates(
        self,
        previous: Sequence[Mapping[str, Any]],
        candidates: Sequence[BrowserMemoryCandidate],
    ) -> tuple[Mapping[str, Any], ...]:
        values: list[Mapping[str, Any]] = [dict(item) for item in previous]
        seen = {str(item.get("candidate_id") or "") for item in values}
        for candidate in candidates:
            if candidate.candidate_id in seen:
                continue
            values.append(candidate.to_dict())
            seen.add(candidate.candidate_id)
        return tuple(values[-512:])

    def _bounded_queue(self, queue: Sequence[BrowserContextQueueItem]) -> list[BrowserContextQueueItem]:
        if len(queue) <= self.queue_limit:
            return list(queue)
        active = [item for item in queue if item.state != BrowserContextDeliveryState.CONSUMED]
        consumed = [item for item in queue if item.state == BrowserContextDeliveryState.CONSUMED]
        keep_consumed = max(0, self.queue_limit - len(active))
        return [*consumed[-keep_consumed:], *active] if keep_consumed else active[-self.queue_limit:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserContextTaskIntegrationRuntime",
            "owner_unit": "M1-S04B-02",
            "checkpoint_owner": "TaskState.metadata/SQLiteStore",
            "canonical_context_owner": "ClaudeContextWindowManager/M1-02D",
            "disabled": self.disabled,
            "queue_limit": self.queue_limit,
            "restores": self._restores,
            "enqueues": self._enqueues,
            "duplicates": self._duplicates,
            "claims": self._claims,
            "consumes": self._consumes,
            "releases": self._releases,
            "scope_rejections": self._scope_rejections,
        }


def browser_context_checkpoint_digest(value: Mapping[str, Any]) -> str:
    """Stable public helper used by API contract tests and reconciliation."""

    payload = dict(value)
    payload.pop("checkpoint_digest", None)
    return digest_json(payload)


def browser_context_message_fingerprint(messages: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or ""),
            "metadata": to_jsonable(_mapping(item.get("metadata"))),
        }
        for item in messages
    ]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in _sequence(value) if str(item)))


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
