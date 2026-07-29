from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from zyra_core import ArtifactKind
from zyra_runtime import LocalArtifactStore
from zyra_runtime.runtime_events import RuntimeEventSpineBridge

from .contracts import (
    ApplyReceipt,
    BridgeCommand,
    BridgeError,
    DispatchReceipt,
    LoopXClaimConflictError,
    LoopXUnavailableError,
    OutboxRecord,
    OutboxState,
    SyncStatus,
    stable_digest,
)
from .outbox import LoopXOutbox
from .single_writer import LoopXSingleWriter, WriterFence
from .state_mapping import LoopXStateMapper, quota_spent_slots


LOOPX_PRIVATE_STATE_SCHEMA = "zyra.loopx-private-state/v1"
# Sync receipts use the canonical audit route while preserving the more
# specific bridge contract in the durable inline payload.
LOOPX_SYNC_EVENT_TYPE = "runtime.audit.finding"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


@dataclass(frozen=True, slots=True)
class _LoadedRuntime:
    package: ModuleType
    event_state: ModuleType
    interaction_contract: ModuleType
    module_origin: str


_RUNTIME_CACHE: dict[str, _LoadedRuntime] = {}
_RUNTIME_CACHE_LOCK = threading.RLock()


def _load_installed_runtime(module_root: Path) -> _LoadedRuntime:
    root = module_root.resolve()
    package_init = root / "loopx" / "__init__.py"
    if not package_init.is_file():
        raise LoopXUnavailableError(
            "Pinned LoopX module root is missing.",
            code="loopx_runtime_unavailable",
            retryable=True,
            details={"module_root": str(root)},
        )
    cache_key = os.path.normcase(str(root))
    with _RUNTIME_CACHE_LOCK:
        prior = _RUNTIME_CACHE.get(cache_key)
        if prior is not None:
            return prior
        alias = f"_zyra_pinned_loopx_{stable_digest(cache_key)[:16]}"
        try:
            spec = importlib.util.spec_from_file_location(
                alias,
                package_init,
                submodule_search_locations=[str(package_init.parent)],
            )
            if spec is None or spec.loader is None:
                raise ImportError("cannot build pinned LoopX import spec")
            package = importlib.util.module_from_spec(spec)
            sys.modules[alias] = package
            spec.loader.exec_module(package)
            event_state = importlib.import_module(f"{alias}.event_sourced_state")
            interaction = importlib.import_module(
                f"{alias}.control_plane.work_items.interaction_contract"
            )
        except Exception as error:
            for name in tuple(sys.modules):
                if name == alias or name.startswith(f"{alias}."):
                    sys.modules.pop(name, None)
            raise LoopXUnavailableError(
                "Pinned LoopX package could not be loaded.",
                code="loopx_runtime_unavailable",
                retryable=True,
                details={
                    "module_root": str(root),
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        origin = Path(str(event_state.__file__ or "")).resolve()
        if not origin.is_relative_to(root):
            raise LoopXUnavailableError(
                "LoopX runtime resolved outside the pinned project-local install.",
                code="loopx_runtime_origin_mismatch",
                retryable=False,
                details={"module_root": str(root), "origin": str(origin)},
            )
        loaded = _LoadedRuntime(
            package=package,
            event_state=event_state,
            interaction_contract=interaction,
            module_origin=str(origin),
        )
        _RUNTIME_CACHE[cache_key] = loaded
        return loaded


class LoopXRuntimeStateAdapter:
    """Apply mapped commands through the real pinned LoopX event store."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        install_receipt: Mapping[str, Any],
        mapper: LoopXStateMapper | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.install_receipt = dict(install_receipt)
        self.state_root = Path(str(self.install_receipt.get("state_root") or "")).resolve()
        expected_state = (self.workspace_root / ".zyra" / "loopx" / "state").resolve()
        if (
            self.install_receipt.get("ready") is not True
            or str(self.install_receipt.get("version") or "") != "0.2.4"
            or self.state_root != expected_state
        ):
            raise LoopXUnavailableError(
                "LoopX install receipt does not satisfy the pinned workspace contract.",
                code="loopx_runtime_receipt_invalid",
                retryable=False,
                details={
                    "ready": self.install_receipt.get("ready"),
                    "version": self.install_receipt.get("version"),
                    "expected_state_root": str(expected_state),
                    "state_root": str(self.state_root),
                },
            )
        install_root = Path(str(self.install_receipt.get("install_root") or "")).resolve()
        self.module_root = (
            install_root / str(self.install_receipt.get("module_root") or "")
        ).resolve()
        if not self.module_root.is_relative_to(install_root):
            raise LoopXUnavailableError(
                "LoopX module root escapes its pinned installation.",
                code="loopx_runtime_receipt_invalid",
                retryable=False,
            )
        self.runtime_root = (
            self.workspace_root / ".zyra" / "loopx" / "runtime"
        ).resolve()
        self.private_root = self.state_root / "private"
        self.registry_path = self.private_root / "registry.json"
        self.mapper = mapper or LoopXStateMapper()

    def apply(
        self,
        command: BridgeCommand,
        fence: WriterFence,
        *,
        after_event_append: Callable[[int, Mapping[str, Any]], None] | None = None,
    ) -> ApplyReceipt:
        fence.assert_owned()
        runtime = _load_installed_runtime(self.module_root)
        goal_root = self.private_root / "goals" / command.update.goal_id
        event_log = goal_root / "events.jsonl"
        state_file = goal_root / "ACTIVE_GOAL_STATE.md"
        projection_file = goal_root / "bridge-state.json"
        store = runtime.event_state.AppendOnlyStateEventStore(event_log)
        before_events = store.load()
        before_projection = runtime.event_state.build_state_projection(
            before_events,
            goal_id=command.update.goal_id,
        )
        plan = self.mapper.plan(
            command,
            current_events=before_events,
            current_projection=before_projection,
        )
        before_ids = {str(item.get("event_id") or "") for item in before_events}
        applied: list[str] = []
        duplicates: list[str] = []
        for index, event in enumerate(plan.events):
            fence.assert_owned()
            event_id = str(event["event_id"])
            stored = store.append(event)
            if event_id in before_ids:
                duplicates.append(event_id)
            else:
                applied.append(event_id)
                before_ids.add(event_id)
            if after_event_append is not None:
                after_event_append(index, stored)
        if event_log.exists():
            with event_log.open("ab") as stream:
                os.fsync(stream.fileno())
        fence.assert_owned()
        events = runtime.event_state.AppendOnlyStateEventStore(event_log).load()
        projection = runtime.event_state.build_state_projection(
            events,
            goal_id=command.update.goal_id,
        )
        markdown = runtime.event_state.render_active_state_sections(projection)
        total_spent = quota_spent_slots(events)
        limit = command.update.quota.limit_slots
        quota_exhausted = total_spent >= limit
        requested_fully_admitted = (
            plan.requested_spend_slots == 0
            or plan.spend_applied_slots == plan.requested_spend_slots
        )
        interaction_gate = (
            command.update.interaction is None or plan.interaction_accepted
        )
        continuation_allowed = (
            limit > 0
            and total_spent < limit
            and requested_fully_admitted
            and interaction_gate
        )
        interaction_payload = {
            "goal_id": command.update.goal_id,
            "state": "throttled" if quota_exhausted else "eligible",
            "should_run": continuation_allowed,
            "normal_delivery_allowed": continuation_allowed,
            "requires_user_action": False,
            "user_todo_summary": projection.get("user_todos") or {},
            "execution_obligation": {
                "kind": "bounded_delivery",
                "must_attempt_work": continuation_allowed,
                "delivery_allowed": continuation_allowed,
            },
            "heartbeat_recommendation": {},
        }
        interaction_contract = (
            runtime.interaction_contract.build_interaction_contract(
                interaction_payload
            )
        )
        last_validated = {
            **command.update.validation.to_dict(),
            "canonical_receipt_digest": command.canonical_commit.receipt_digest,
            "canonical_commit_id": command.canonical_commit.commit_id,
        }
        status = (
            SyncStatus.QUOTA_EXHAUSTED
            if quota_exhausted or plan.quota_exhausted
            else (
                SyncStatus.IDEMPOTENT_REPLAY
                if not applied and bool(duplicates)
                else SyncStatus.APPLIED
            )
        )
        private_state = {
            "schema": LOOPX_PRIVATE_STATE_SCHEMA,
            "mapping_version": command.mapping_version,
            "goal_id": command.update.goal_id,
            "objective_ref": command.update.objective_ref,
            "requirement_revision": command.update.requirement_revision,
            "canonical_commit": command.canonical_commit.to_dict(),
            "sync_status": status.value,
            "last_validated_receipt": last_validated,
            "quota": {
                "limit_slots": limit,
                "spent_slots": total_spent,
                "remaining_slots": max(0, limit - total_spent),
                "window_hours": command.update.quota.window_hours,
                "exhausted": quota_exhausted,
                "owner": "loopx_private_control",
                "zyra_execution_budget_owner": "ResourceScheduler",
            },
            "todo_projection": {
                "user_todos": projection.get("user_todos") or {},
                "agent_todos": projection.get("agent_todos") or {},
            },
            "history": projection.get("timeline") or [],
            "interaction_contract": interaction_contract,
            "continuation_allowed": continuation_allowed,
            "source_event_count": projection.get("source_event_count"),
            "source_checksum": projection.get("source_checksum"),
            "last_event_id": projection.get("last_event_id"),
            "last_append_sequence": projection.get("last_append_sequence"),
            "workspace_id": command.workspace_id,
        }
        _atomic_write(state_file, markdown)
        _atomic_json(projection_file, private_state)
        self._update_registry(
            command,
            state_file=state_file,
            event_log=event_log,
            private_state=private_state,
        )
        fence.assert_owned()
        return ApplyReceipt(
            status=status,
            goal_id=command.update.goal_id,
            idempotency_key=command.idempotency_key,
            applied_event_ids=tuple(applied),
            duplicate_event_ids=tuple(duplicates),
            source_event_count=int(projection.get("source_event_count") or 0),
            source_checksum=str(projection.get("source_checksum") or ""),
            spend_applied_slots=plan.spend_applied_slots,
            spend_rejected_slots=plan.spend_rejected_slots,
            quota_spent_slots=total_spent,
            quota_limit_slots=limit,
            continuation_allowed=continuation_allowed,
            last_validated_receipt=last_validated,
            quota_exhausted=quota_exhausted or plan.quota_exhausted,
            degraded_reason=(
                "validation_before_spend_rejected:"
                + ",".join(plan.failed_spend_gates)
                if plan.spend_rejected_slots and plan.failed_spend_gates
                else ""
            ),
            module_origin=runtime.module_origin,
        )

    def read_private_state(self, goal_id: str) -> dict[str, Any]:
        path = self.private_root / "goals" / goal_id / "bridge-state.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("LoopX private state projection is invalid")
        return value

    def load_events(self, goal_id: str) -> list[dict[str, Any]]:
        runtime = _load_installed_runtime(self.module_root)
        event_log = self.private_root / "goals" / goal_id / "events.jsonl"
        return runtime.event_state.AppendOnlyStateEventStore(event_log).load()

    def _update_registry(
        self,
        command: BridgeCommand,
        *,
        state_file: Path,
        event_log: Path,
        private_state: Mapping[str, Any],
    ) -> None:
        registry: dict[str, Any] = {}
        if self.registry_path.exists():
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise LoopXUnavailableError(
                    "LoopX private registry is corrupt.",
                    code="loopx_private_state_corrupt",
                    retryable=False,
                    details={"path": str(self.registry_path)},
                )
            registry = value
        goals = [
            dict(item)
            for item in registry.get("goals") or []
            if isinstance(item, Mapping)
            and str(item.get("id") or "") != command.update.goal_id
        ]
        quota = private_state["quota"]
        goals.append(
            {
                "id": command.update.goal_id,
                "domain": "zyra-long-horizon",
                "status": (
                    "quota_exhausted"
                    if quota.get("exhausted")
                    else "connected"
                ),
                "role": "controller",
                "repo": str(self.workspace_root),
                "state_file": str(state_file),
                "state_event_log": str(event_log),
                "adapter": {
                    "kind": "zyra_loopx_state_bridge",
                    "status": private_state["sync_status"],
                    "mapping_version": command.mapping_version,
                },
                "quota": {
                    "compute": quota["limit_slots"],
                    "window_hours": quota["window_hours"],
                    "spent_slots": quota["spent_slots"],
                },
                "objective_ref": command.update.objective_ref,
                "requirement_revision": command.update.requirement_revision,
                "interaction_contract": private_state["interaction_contract"],
                "continuation_allowed": private_state["continuation_allowed"],
                "zyra_owner_boundary": {
                    "task": "Zyra orchestration/runtime",
                    "worker_lease": "WorkerLeaseManager",
                    "permission": "ToolPermissionRuntime",
                    "execution_budget": "ResourceScheduler",
                },
            }
        )
        goals.sort(key=lambda item: str(item.get("id") or ""))
        _atomic_json(
            self.registry_path,
            {
                "schema_version": "0.1",
                "registry_role": "project-local-private",
                "common_runtime_root": str(self.runtime_root),
                "workspace_id": command.workspace_id,
                "goals": goals,
            },
        )


class BridgeObservability(Protocol):
    def publish(
        self,
        record: OutboxRecord,
        *,
        status: SyncStatus,
        apply_receipt: Mapping[str, Any] | None,
        error_code: str = "",
        degraded_reason: str = "",
    ) -> tuple[str, str]: ...


class LoopXBridgeObservability:
    """Publish sync receipts through Zyra's canonical event/artifact owners."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        event_spine: RuntimeEventSpineBridge,
    ) -> None:
        self.artifact_store = artifact_store
        self.event_spine = event_spine

    def publish(
        self,
        record: OutboxRecord,
        *,
        status: SyncStatus,
        apply_receipt: Mapping[str, Any] | None,
        error_code: str = "",
        degraded_reason: str = "",
    ) -> tuple[str, str]:
        command = record.command
        receipt = dict(apply_receipt or {})
        payload = {
            "schema": "zyra.loopx-sync-status/v1",
            "status": status.value,
            "outbox_sequence": record.sequence,
            "outbox_attempt": record.attempts,
            "idempotency_key": command.idempotency_key,
            "source_causation_id": command.causation_id,
            "mapping_version": command.mapping_version,
            "workspace_id": command.workspace_id,
            "goal_id": command.update.goal_id,
            "canonical_commit": command.canonical_commit.to_dict(),
            "last_validated_receipt": receipt.get("last_validated_receipt"),
            "claim_conflict": receipt.get("claim_conflict"),
            "quota_exhausted": bool(receipt.get("quota_exhausted")),
            "degraded_reason": degraded_reason or receipt.get("degraded_reason") or "",
            "error_code": error_code,
            "owners": {
                "event": "typescript.RuntimeEventSpine",
                "artifact": "LocalArtifactStore",
                "bridge": "ZyraLoopXBridge",
                "loopx_private_state": "LoopX",
            },
            "apply_receipt": receipt,
        }
        artifact = self.artifact_store.write_text(
            run_id=command.run_id,
            task_id=command.task_id,
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            title=f"LoopX bridge sync {record.sequence}: {status.value}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id="loopx-state-bridge",
            metadata={
                "contract": "zyra.loopx-sync-status/v1",
                "outbox_sequence": record.sequence,
                "idempotency_key": command.idempotency_key,
                "causation_id": command.causation_id,
                "correlation_id": command.correlation_id,
            },
        )
        event_id = (
            "loopx-sync-"
            + stable_digest(
                {
                    "sequence": record.sequence,
                    "status": status.value,
                    "idempotency_key": command.idempotency_key,
                }
            )[:24]
        )
        artifact_metadata = dict(artifact.metadata)
        artifact_pointer = {
            "artifactId": artifact.artifact_id,
            "digest": (
                "sha256:" + str(artifact_metadata.get("sha256") or "")
            ),
            "mediaType": str(
                artifact_metadata.get("media_type") or "application/json"
            ),
            "sizeBytes": int(artifact_metadata.get("size_bytes") or 0),
            "title": artifact.title,
            "uri": artifact.uri,
            "producerNodeId": artifact.producer_node_id,
            "metadata": {
                "role": "loopx_sync_receipt",
                "contract": "zyra.loopx-sync-status/v1",
            },
        }
        event_inline = {
            "schema": payload["schema"],
            "status": status.value,
            "outbox_sequence": record.sequence,
            "idempotency_key": command.idempotency_key,
            "mapping_version": command.mapping_version,
            "workspace_id": command.workspace_id,
            "goal_id": command.update.goal_id,
            "canonical_commit_id": command.canonical_commit.commit_id,
            "canonical_receipt_digest": (
                command.canonical_commit.receipt_digest
            ),
            "source_causation_id": command.causation_id,
            "last_validated_receipt": payload["last_validated_receipt"],
            "claim_conflict": payload["claim_conflict"],
            "quota_exhausted": payload["quota_exhausted"],
            "degraded_reason": payload["degraded_reason"],
            "error_code": error_code,
            "artifact_id": artifact.artifact_id,
        }
        self.event_spine.append_canonical(
            {
                "eventId": event_id,
                "eventType": LOOPX_SYNC_EVENT_TYPE,
                "aggregateId": (
                    f"run:{command.run_id}:task:{command.task_id}"
                ),
                "createdAt": command.created_at,
                "correlationId": command.correlation_id,
                "idempotencyKey": (
                    f"{command.idempotency_key}:sync:{status.value}"
                ),
                "durability": "durable",
                "effect": "non_effective",
                "identity": {
                    "runId": command.run_id,
                    "taskId": command.task_id,
                    "nodeId": "loopx-state-bridge",
                },
                "sender": {
                    "kind": "system",
                    "id": "loopx-state-bridge",
                    "role": "supplementary-state-bridge",
                    "capabilityRefs": [],
                },
                "intent": "audit",
                "target": {
                    "kind": "auditor",
                    "id": "runtime-auditor",
                    "requiredCapabilities": [],
                },
                "summary": (
                    f"LoopX bridge sync {record.sequence}: {status.value}"
                ),
                "stateDelta": {
                    "domain": "audit",
                    "operation": "none",
                    "path": ["audit", "loopx", command.update.goal_id],
                    "afterDigest": "sha256:" + stable_digest(payload),
                    "value": {
                        "schema": payload["schema"],
                        "status": status.value,
                        "outbox_sequence": record.sequence,
                    },
                    "effective": False,
                },
                "artifactRefs": [artifact_pointer],
                "provenance": {
                    "sourceRepository": "zyra",
                    "sourceModule": (
                        "zyra_integrations.loopx.bridge.dispatcher"
                    ),
                    "migrationRole": "zyra_owned",
                    "producerVersion": "p2-s01-02",
                    "trust": "internal",
                    "normalizedFrom": "canonical-commit-outbox",
                    "sourceEventId": command.canonical_commit.commit_id,
                    "sourceDigest": (
                        "sha256:"
                        + command.canonical_commit.receipt_digest.removeprefix(
                            "sha256:"
                        )
                    ),
                },
                "inline": event_inline,
                "sourceBytes": len(
                    json.dumps(
                        event_inline,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ),
                "metadata": {
                    "contract": "zyra.loopx-sync-status/v1",
                    "bridge_mapping_version": command.mapping_version,
                    "sync_status": status.value,
                    "outbox_sequence": record.sequence,
                    "canonical_commit_id": command.canonical_commit.commit_id,
                    "source_causation_id": command.causation_id,
                },
            }
        )
        return event_id, artifact.artifact_id


class LoopXDispatcher:
    def __init__(
        self,
        *,
        outbox: LoopXOutbox,
        single_writer: LoopXSingleWriter,
        runtime: LoopXRuntimeStateAdapter,
        observability: BridgeObservability | None = None,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.0,
    ) -> None:
        self.outbox = outbox
        self.single_writer = single_writer
        self.runtime = runtime
        self.observability = observability
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self._dispatch_guard = threading.RLock()

    def dispatch(
        self,
        *,
        limit: int = 100,
        enabled: bool = True,
        writer_timeout_seconds: float = 0.0,
        after_apply: Callable[[OutboxRecord, ApplyReceipt], None] | None = None,
    ) -> tuple[DispatchReceipt, ...]:
        results: list[DispatchReceipt] = []
        with self._dispatch_guard:
            with self.single_writer.acquire(
                timeout_seconds=writer_timeout_seconds
            ) as fence:
                self.outbox.recover_inflight(fence)
                if not enabled:
                    pending = self.outbox.list_records(
                        state=OutboxState.PENDING,
                        limit=1,
                    )
                    if pending:
                        record = pending[0]
                        event_id, artifact_id = self._publish_safely(
                            record,
                            status=SyncStatus.SYNC_DEGRADED,
                            apply_receipt=None,
                            error_code="loopx_bridge_disabled",
                            degraded_reason="bridge_disabled",
                            suppress_errors=True,
                        )
                        results.append(
                            DispatchReceipt(
                                sequence=record.sequence,
                                status=SyncStatus.SYNC_DEGRADED,
                                idempotency_key=record.command.idempotency_key,
                                attempts=record.attempts,
                                event_id=event_id,
                                artifact_id=artifact_id,
                                error_code="loopx_bridge_disabled",
                                degraded_reason="bridge_disabled",
                            )
                        )
                    return tuple(results)
                for _ in range(max(0, int(limit))):
                    record = self.outbox.claim_next(fence)
                    if record is None:
                        break
                    try:
                        applied = self.runtime.apply(record.command, fence)
                        record = self.outbox.record_apply_receipt(
                            record,
                            fence,
                            applied.to_dict(),
                        )
                        if after_apply is not None:
                            after_apply(record, applied)
                        event_id, artifact_id = self._publish_safely(
                            record,
                            status=applied.status,
                            apply_receipt=applied.to_dict(),
                        )
                        self.outbox.acknowledge(
                            record,
                            fence,
                            receipt=applied.to_dict(),
                        )
                        results.append(
                            DispatchReceipt(
                                sequence=record.sequence,
                                status=applied.status,
                                idempotency_key=record.command.idempotency_key,
                                attempts=record.attempts,
                                apply_receipt=applied.to_dict(),
                                event_id=event_id,
                                artifact_id=artifact_id,
                                degraded_reason=applied.degraded_reason,
                            )
                        )
                    except LoopXClaimConflictError as error:
                        conflict_receipt = {
                            "schema": "zyra.loopx-claim-conflict/v1",
                            "status": SyncStatus.CLAIM_CONFLICT.value,
                            "claim_conflict": error.details,
                            "quota_exhausted": False,
                            "last_validated_receipt": (
                                record.command.update.validation.to_dict()
                            ),
                            "degraded_reason": str(error),
                        }
                        event_id, artifact_id = self._publish_safely(
                            record,
                            status=SyncStatus.CLAIM_CONFLICT,
                            apply_receipt=conflict_receipt,
                            error_code=error.code,
                            degraded_reason=str(error),
                            suppress_errors=True,
                        )
                        self.outbox.dead_letter(
                            record,
                            fence,
                            error_code=error.code,
                            error_message=str(error),
                            receipt=conflict_receipt,
                        )
                        results.append(
                            DispatchReceipt(
                                sequence=record.sequence,
                                status=SyncStatus.CLAIM_CONFLICT,
                                idempotency_key=record.command.idempotency_key,
                                attempts=record.attempts,
                                apply_receipt=conflict_receipt,
                                event_id=event_id,
                                artifact_id=artifact_id,
                                error_code=error.code,
                                degraded_reason=str(error),
                            )
                        )
                    except Exception as error:
                        code = (
                            error.code
                            if isinstance(error, BridgeError)
                            else "loopx_dispatch_failed"
                        )
                        retryable = (
                            error.retryable
                            if isinstance(error, BridgeError)
                            else True
                        )
                        dead = not retryable or record.attempts >= self.max_attempts
                        status = (
                            SyncStatus.DEAD_LETTER
                            if dead
                            else SyncStatus.SYNC_DEGRADED
                        )
                        event_id, artifact_id = self._publish_safely(
                            record,
                            status=status,
                            apply_receipt=record.apply_receipt,
                            error_code=code,
                            degraded_reason=f"{type(error).__name__}: {error}",
                            suppress_errors=True,
                        )
                        if dead:
                            self.outbox.dead_letter(
                                record,
                                fence,
                                error_code=code,
                                error_message=str(error),
                                receipt=record.apply_receipt,
                            )
                        else:
                            self.outbox.retry(
                                record,
                                fence,
                                error_code=code,
                                error_message=str(error),
                                delay_seconds=(
                                    self.retry_base_seconds
                                    * (2 ** max(0, record.attempts - 1))
                                ),
                            )
                        results.append(
                            DispatchReceipt(
                                sequence=record.sequence,
                                status=status,
                                idempotency_key=record.command.idempotency_key,
                                attempts=record.attempts,
                                apply_receipt=record.apply_receipt,
                                event_id=event_id,
                                artifact_id=artifact_id,
                                error_code=code,
                                degraded_reason=f"{type(error).__name__}: {error}",
                            )
                        )
        return tuple(results)

    def replay_dead_letter(
        self,
        sequence: int,
        *,
        writer_timeout_seconds: float = 0.0,
    ) -> OutboxRecord:
        with self._dispatch_guard:
            with self.single_writer.acquire(
                timeout_seconds=writer_timeout_seconds
            ) as fence:
                return self.outbox.replay_dead_letter(sequence, fence)

    def _publish_safely(
        self,
        record: OutboxRecord,
        *,
        status: SyncStatus,
        apply_receipt: Mapping[str, Any] | None,
        error_code: str = "",
        degraded_reason: str = "",
        suppress_errors: bool = False,
    ) -> tuple[str, str]:
        if self.observability is None:
            return "", ""
        try:
            return self.observability.publish(
                record,
                status=status,
                apply_receipt=apply_receipt,
                error_code=error_code,
                degraded_reason=degraded_reason,
            )
        except Exception:
            if suppress_errors:
                return "", ""
            raise


__all__ = [
    "LOOPX_PRIVATE_STATE_SCHEMA",
    "LOOPX_SYNC_EVENT_TYPE",
    "BridgeObservability",
    "LoopXBridgeObservability",
    "LoopXDispatcher",
    "LoopXRuntimeStateAdapter",
]
