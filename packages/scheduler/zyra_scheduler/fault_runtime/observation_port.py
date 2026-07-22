from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from zyra_core import TaskState

from .contracts import runtime_id, utc_now
from .source_session import ProcessHandle

if TYPE_CHECKING:
    from .integration import IntegrationResponse, WatchdogFaultIntegrationRuntime
    from .state_store import FaultStateStore


class RuntimeObservationStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    CONTROL_REQUIRED = "control_required"


@dataclass(frozen=True, slots=True)
class RuntimeObservationEnvelope:
    event_id: str
    event_type: str
    producer: str
    run_id: str
    task_id: str
    sequence: int
    payload: Mapping[str, Any]
    occurred_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.runtime-fault-observation-envelope/v1",
            "event_id": self.event_id,
            "event_type": self.event_type,
            "producer": self.producer,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True, slots=True)
class RuntimeObservationReceipt:
    event_id: str
    event_type: str
    run_id: str
    task_id: str
    sequence: int
    status: RuntimeObservationStatus
    operation: str
    result: Mapping[str, Any]
    payload_digest: str
    changed: bool
    receipt_id: str = field(default_factory=lambda: runtime_id("runtime-observation-receipt"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.runtime-fault-observation-receipt/v1",
            "receipt_id": self.receipt_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "status": self.status.value,
            "operation": self.operation,
            "result": dict(self.result),
            "payload_digest": self.payload_digest,
            "changed": self.changed,
            "created_at": self.created_at,
        }


class RuntimeObservationRejected(ValueError):
    pass


class RuntimeObservationControlRequired(RuntimeObservationRejected):
    pass


ObservationHandler = Callable[
    [TaskState, RuntimeObservationEnvelope, ProcessHandle | None],
    Mapping[str, Any],
]


class RuntimeFaultObservationPort:
    """Typed native-runtime ingress for the default watchdog path.

    Runtime producers submit lifecycle facts rather than fault labels. Each
    handler invokes ``WatchdogFaultIntegrationRuntime`` so the existing active
    source, classifier, projection and handoff owners remain authoritative.
    The port owns only event-id idempotency and producer sequence cursors.
    """

    _METADATA_KEY = "M1-S07B-02.runtime-observation-port"

    def __init__(
        self,
        integration: WatchdogFaultIntegrationRuntime,
        store: FaultStateStore,
        *,
        retained_receipts: int = 2_000,
    ) -> None:
        if retained_receipts < 100:
            raise ValueError("runtime observation port must retain at least 100 receipts")
        self.integration = integration
        self.store = store
        self.retained_receipts = retained_receipts
        self._guard = threading.RLock()
        self._receipts: dict[str, RuntimeObservationReceipt] = {}
        self._receipt_order: list[str] = []
        self._producer_sequences: dict[str, int] = {}
        self._metadata_revision = 0
        self._accepted_count = 0
        self._duplicate_count = 0
        self._rejected_count = 0
        self._handlers: dict[str, ObservationHandler] = {}
        self._install_handlers()
        self._restore()

    def ingest(
        self,
        state: TaskState,
        value: RuntimeObservationEnvelope | Mapping[str, Any],
        *,
        process_handle: ProcessHandle | None = None,
    ) -> RuntimeObservationReceipt:
        envelope = value if isinstance(value, RuntimeObservationEnvelope) else self.envelope(value)
        self._validate_scope(state, envelope)
        digest = self._digest(envelope)
        with self._guard:
            prior = self._receipts.get(envelope.event_id)
            if prior is not None:
                if prior.payload_digest != digest:
                    self._rejected_count += 1
                    raise RuntimeObservationRejected(
                        "runtime observation event_id replay carries a different payload"
                    )
                self._duplicate_count += 1
                return RuntimeObservationReceipt(
                    event_id=prior.event_id,
                    event_type=prior.event_type,
                    run_id=prior.run_id,
                    task_id=prior.task_id,
                    sequence=prior.sequence,
                    status=RuntimeObservationStatus.DUPLICATE,
                    operation=prior.operation,
                    result=prior.result,
                    payload_digest=prior.payload_digest,
                    changed=False,
                )
            self._validate_sequence_locked(envelope)
            handler = self._handlers.get(envelope.event_type)
            if handler is None:
                self._rejected_count += 1
                raise RuntimeObservationRejected(
                    f"unsupported structured runtime observation: {envelope.event_type}"
                )
        try:
            result = handler(state, envelope, process_handle)
        except RuntimeObservationControlRequired:
            with self._guard:
                self._rejected_count += 1
            raise
        except (KeyError, TypeError, ValueError, RuntimeError):
            with self._guard:
                self._rejected_count += 1
            raise
        receipt = RuntimeObservationReceipt(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            sequence=envelope.sequence,
            status=RuntimeObservationStatus.ACCEPTED,
            operation=str(result.get("operation") or envelope.event_type),
            result=dict(result),
            payload_digest=digest,
            changed=True,
        )
        with self._guard:
            self._receipts[envelope.event_id] = receipt
            self._receipt_order.append(envelope.event_id)
            self._producer_sequences[self._producer_key(envelope)] = envelope.sequence
            self._accepted_count += 1
            self._trim_locked()
            self._persist_locked()
        return receipt

    def ingest_batch(
        self,
        state: TaskState,
        values: Sequence[RuntimeObservationEnvelope | Mapping[str, Any]],
        *,
        stop_on_error: bool = True,
    ) -> tuple[RuntimeObservationReceipt, ...]:
        if not values:
            return ()
        envelopes = [value if isinstance(value, RuntimeObservationEnvelope) else self.envelope(value) for value in values]
        keys: set[str] = set()
        sequences: dict[str, int] = {}
        for envelope in envelopes:
            self._validate_scope(state, envelope)
            if envelope.event_id in keys:
                raise RuntimeObservationRejected("runtime observation batch repeats event_id")
            keys.add(envelope.event_id)
            producer_key = self._producer_key(envelope)
            prior = sequences.get(producer_key)
            if prior is not None and envelope.sequence <= prior:
                raise RuntimeObservationRejected("runtime observation batch sequence is not increasing")
            sequences[producer_key] = envelope.sequence
        receipts: list[RuntimeObservationReceipt] = []
        for envelope in envelopes:
            try:
                receipts.append(self.ingest(state, envelope))
            except (RuntimeObservationRejected, KeyError, TypeError, ValueError, RuntimeError):
                if stop_on_error:
                    raise
        return tuple(receipts)

    def receipt(self, event_id: str) -> RuntimeObservationReceipt | None:
        with self._guard:
            return self._receipts.get(event_id)

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            receipts = [
                self._receipts[event_id].to_dict()
                for event_id in self._receipt_order[-200:]
                if event_id in self._receipts
                and (not task_id or self._receipts[event_id].task_id == task_id)
            ]
            sequences = dict(sorted(self._producer_sequences.items()))
            counts = {
                "accepted": self._accepted_count,
                "duplicate": self._duplicate_count,
                "rejected": self._rejected_count,
            }
        return {
            "schema": "zyra.runtime-fault-observation-port/v1",
            "task_id": task_id,
            "registered_event_types": sorted(self._handlers),
            "receipts": receipts,
            "producer_sequences": sequences,
            "counts": counts,
            "metadata_revision": self._metadata_revision,
            "fault_truth_source": "active structured runtime facts",
            "canonical_fault_owner": "FaultStateStore",
            "canonical_classifier": "WatchdogSignalClassifier",
            "requirement_change_route": "ConstraintKeeper/TopologyRouter",
            "unknown_event_fallback": False,
        }

    @staticmethod
    def envelope(value: Mapping[str, Any]) -> RuntimeObservationEnvelope:
        payload_value = value.get("payload")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {
            key: item
            for key, item in value.items()
            if key not in {
                "schema",
                "event_id",
                "event_type",
                "producer",
                "run_id",
                "task_id",
                "sequence",
                "occurred_at",
            }
        }
        event_id = str(value.get("event_id") or "").strip()
        event_type = str(value.get("event_type") or "").strip().lower()
        producer = str(value.get("producer") or "").strip()
        run_id = str(value.get("run_id") or payload.get("run_id") or "").strip()
        task_id = str(value.get("task_id") or payload.get("task_id") or "").strip()
        if not event_id or not event_type or not producer or not run_id or not task_id:
            raise RuntimeObservationRejected(
                "runtime observation requires event_id, event_type, producer, run_id and task_id"
            )
        sequence = int(value.get("sequence") or 0)
        if sequence < 1:
            raise RuntimeObservationRejected("runtime observation sequence must be positive")
        return RuntimeObservationEnvelope(
            event_id=event_id,
            event_type=event_type,
            producer=producer,
            run_id=run_id,
            task_id=task_id,
            sequence=sequence,
            payload=payload,
            occurred_at=str(value.get("occurred_at") or utc_now()),
        )

    def _install_handlers(self) -> None:
        handlers: dict[str, ObservationHandler] = {
            "tool.started": self._tool_started,
            "tool.succeeded": self._tool_succeeded,
            "tool.failed": self._tool_failed,
            "tool.cancelled": self._tool_cancelled,
            "worker.attached": self._worker_attached,
            "worker.heartbeat": self._worker_heartbeat,
            "worker.stopped": self._worker_stopped,
            "browser.attached": self._browser_attached,
            "browser.heartbeat": self._browser_heartbeat,
            "browser.cdp_connected": self._browser_connected,
            "browser.cdp_disconnected": self._browser_disconnected,
            "browser.stopped": self._browser_stopped,
            "provider.attached": self._provider_attached,
            "provider.attempt_started": self._provider_attempt_started,
            "provider.succeeded": self._provider_succeeded,
            "provider.failed": self._provider_failed,
            "mcp.attached": self._mcp_attached,
            "mcp.disconnected": self._mcp_disconnected,
            "mcp.reconnect_failed": self._mcp_reconnect_failed,
            "mcp.reconnect_succeeded": self._mcp_reconnect_succeeded,
            "mcp.stopped": self._mcp_stopped,
            "permission.settled": self._permission_settled,
            "schema.validated": self._schema_validated,
            "workspace.attested": self._workspace_attested,
            "subagent.settled": self._subagent_settled,
            "typescript.watchdog": self._typescript_watchdog,
            "watchdog.tick": self._watchdog_tick,
            "requirement.changed": self._requirement_changed,
        }
        self._handlers.update(handlers)

    def _tool_started(
        self,
        state: TaskState,
        envelope: RuntimeObservationEnvelope,
        _process: ProcessHandle | None,
    ) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="tool")
        payload["deadline_ms"] = self._positive(payload, "deadline_ms")
        return self.integration.bind_source(state, payload).to_dict()

    def _tool_succeeded(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self._settle_tool(state, envelope, "succeeded")

    def _tool_failed(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self._settle_tool(state, envelope, "failed")

    def _tool_cancelled(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self._settle_tool(state, envelope, "stop")

    def _settle_tool(self, state: TaskState, envelope: RuntimeObservationEnvelope, action: str) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="tool", action=action)
        self._required(payload, "lease_token", "result_id", "tool_call_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _worker_attached(self, state: TaskState, envelope: RuntimeObservationEnvelope, process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="worker")
        payload["interval_ms"] = self._positive(payload, "interval_ms", default=1_000)
        payload["grace_intervals"] = self._positive(payload, "grace_intervals", default=3)
        return self.integration.bind_source(state, payload, process_handle=process).to_dict()

    def _worker_heartbeat(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="worker", action="heartbeat")
        payload["sequence"] = self._positive(payload, "sequence")
        return self.integration.observe_source(state, payload).to_dict()

    def _worker_stopped(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="worker", action="stop")
        payload["reason"] = str(payload.get("reason") or "worker runtime stopped")
        return self.integration.observe_source(state, payload).to_dict()

    def _browser_attached(self, state: TaskState, envelope: RuntimeObservationEnvelope, process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="browser")
        return self.integration.bind_source(state, payload, process_handle=process).to_dict()

    def _browser_heartbeat(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self.integration.observe_source(
            state,
            self._payload(envelope, source_kind="browser", action="heartbeat"),
        ).to_dict()

    def _browser_connected(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self.integration.observe_source(
            state,
            self._payload(envelope, source_kind="browser", action="connected"),
        ).to_dict()

    def _browser_disconnected(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="browser", action="disconnected")
        self._required(payload, "reason_code", "browser_session_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _browser_stopped(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="browser", action="stop")
        payload["reason"] = str(payload.get("reason") or "browser runtime stopped")
        return self.integration.observe_source(state, payload).to_dict()

    def _provider_attached(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self.integration.bind_source(
            state,
            self._payload(envelope, source_kind="provider"),
        ).to_dict()

    def _provider_attempt_started(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="provider", action="begin")
        self._required(payload, "attempt_id", "provider_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _provider_succeeded(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="provider", action="succeeded")
        self._required(payload, "attempt_id", "provider_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _provider_failed(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="provider", action="failed")
        self._required(payload, "attempt_id", "provider_id", "error_code")
        return self.integration.observe_source(state, payload).to_dict()

    def _mcp_attached(self, state: TaskState, envelope: RuntimeObservationEnvelope, process: ProcessHandle | None) -> Mapping[str, Any]:
        return self.integration.bind_source(
            state,
            self._payload(envelope, source_kind="mcp"),
            process_handle=process,
        ).to_dict()

    def _mcp_disconnected(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="mcp", action="disconnected")
        self._required(payload, "reason_code", "mcp_server_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _mcp_reconnect_failed(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="mcp", action="reconnect_failed")
        self._required(payload, "error_code", "mcp_server_id")
        return self.integration.observe_source(state, payload).to_dict()

    def _mcp_reconnect_succeeded(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        return self.integration.observe_source(
            state,
            self._payload(envelope, source_kind="mcp", action="reconnect_succeeded"),
        ).to_dict()

    def _mcp_stopped(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="mcp", action="stop")
        payload["reason"] = str(payload.get("reason") or "MCP runtime stopped")
        return self.integration.observe_source(state, payload).to_dict()

    def _permission_settled(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope)
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            raise RuntimeObservationRejected("permission observation requires a structured receipt")
        return self.integration.observe_permission(state, payload).to_dict()

    def _schema_validated(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope)
        self._required(payload, "schema_id")
        return self.integration.observe_schema(state, payload).to_dict()

    def _workspace_attested(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope, source_kind="workspace")
        self._required(payload, "workspace_id", "relative_path", "expected_digest")
        return self.integration.observe_workspace(state, payload).to_dict()

    def _subagent_settled(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope)
        if not isinstance(payload.get("receipt"), Mapping):
            raise RuntimeObservationRejected("subagent observation requires a structured receipt")
        return self.integration.observe_subagent(state, payload).to_dict()

    def _typescript_watchdog(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope)
        payload.setdefault("run_id", envelope.run_id)
        payload.setdefault("task_id", envelope.task_id)
        return self.integration.ingest_typescript_event(state, payload).to_dict()

    def _watchdog_tick(self, state: TaskState, envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        payload = self._payload(envelope)
        return self.integration.tick(
            task_id=state.task_id,
            at_ms=(int(payload["at_ms"]) if payload.get("at_ms") is not None else None),
        ).to_dict()

    def _requirement_changed(self, _state: TaskState, _envelope: RuntimeObservationEnvelope, _process: ProcessHandle | None) -> Mapping[str, Any]:
        raise RuntimeObservationControlRequired(
            "RequirementChanged is a control event; route it through ConstraintKeeper/TopologyRouter replan"
        )

    def _validate_scope(self, state: TaskState, envelope: RuntimeObservationEnvelope) -> None:
        if state.run_id != envelope.run_id or state.task_id != envelope.task_id:
            raise RuntimeObservationRejected("runtime observation is outside task run scope")

    def _validate_sequence_locked(self, envelope: RuntimeObservationEnvelope) -> None:
        key = self._producer_key(envelope)
        prior = self._producer_sequences.get(key, 0)
        if envelope.sequence <= prior:
            raise RuntimeObservationRejected(
                f"runtime observation sequence is stale for producer {envelope.producer}"
            )

    @staticmethod
    def _producer_key(envelope: RuntimeObservationEnvelope) -> str:
        return f"{envelope.run_id}:{envelope.task_id}:{envelope.producer}"

    @staticmethod
    def _payload(
        envelope: RuntimeObservationEnvelope,
        *,
        source_kind: str = "",
        action: str = "",
    ) -> dict[str, Any]:
        payload = dict(envelope.payload)
        payload.setdefault("run_id", envelope.run_id)
        payload.setdefault("task_id", envelope.task_id)
        payload.setdefault("observation_id", envelope.event_id)
        payload.setdefault("source_state_revision", envelope.sequence)
        if source_kind:
            payload["source_kind"] = source_kind
        if action:
            payload["action"] = action
        metadata = payload.get("metadata")
        normalized = dict(metadata) if isinstance(metadata, Mapping) else {}
        normalized.update(
            {
                "runtime_event_id": envelope.event_id,
                "runtime_event_type": envelope.event_type,
                "runtime_event_producer": envelope.producer,
                "runtime_event_sequence": envelope.sequence,
                "runtime_event_occurred_at": envelope.occurred_at,
            }
        )
        payload["metadata"] = normalized
        return payload

    @staticmethod
    def _required(payload: Mapping[str, Any], *names: str) -> None:
        missing = [name for name in names if not str(payload.get(name) or "").strip()]
        if missing:
            raise RuntimeObservationRejected(
                "runtime observation lacks structured fields: " + ", ".join(missing)
            )

    @staticmethod
    def _positive(payload: Mapping[str, Any], name: str, *, default: int | None = None) -> int:
        raw = payload.get(name, default)
        if raw is None:
            raise RuntimeObservationRejected(f"runtime observation requires {name}")
        selected = int(raw)
        if selected < 1:
            raise RuntimeObservationRejected(f"runtime observation {name} must be positive")
        return selected

    @staticmethod
    def _digest(envelope: RuntimeObservationEnvelope) -> str:
        stable = envelope.to_dict()
        stable.pop("occurred_at", None)
        value = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _trim_locked(self) -> None:
        overflow = len(self._receipt_order) - self.retained_receipts
        if overflow <= 0:
            return
        removed = self._receipt_order[:overflow]
        del self._receipt_order[:overflow]
        for event_id in removed:
            self._receipts.pop(event_id, None)

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.runtime-fault-observation-port/v1",
            "receipts": {
                event_id: self._receipts[event_id].to_dict()
                for event_id in self._receipt_order
                if event_id in self._receipts
            },
            "receipt_order": list(self._receipt_order),
            "producer_sequences": dict(sorted(self._producer_sequences.items())),
            "counts": {
                "accepted": self._accepted_count,
                "duplicate": self._duplicate_count,
                "rejected": self._rejected_count,
            },
            "updated_at": utc_now(),
            "owns_fault_truth": False,
            "owns_recovery_plan": False,
        }
        self._metadata_revision = self.store.put_metadata(
            self._METADATA_KEY,
            payload,
            expected_revision=self._metadata_revision,
        )

    def _restore(self) -> None:
        saved = self.store.metadata(self._METADATA_KEY)
        if saved is None:
            return
        payload, revision = saved
        receipts: dict[str, RuntimeObservationReceipt] = {}
        for event_id, raw in dict(payload.get("receipts") or {}).items():
            value = dict(raw or {})
            try:
                receipts[str(event_id)] = RuntimeObservationReceipt(
                    event_id=str(value.get("event_id") or event_id),
                    event_type=str(value.get("event_type") or ""),
                    run_id=str(value.get("run_id") or ""),
                    task_id=str(value.get("task_id") or ""),
                    sequence=int(value.get("sequence") or 0),
                    status=RuntimeObservationStatus(str(value.get("status") or "accepted")),
                    operation=str(value.get("operation") or ""),
                    result=dict(value.get("result") or {}),
                    payload_digest=str(value.get("payload_digest") or ""),
                    changed=bool(value.get("changed")),
                    receipt_id=str(value.get("receipt_id") or runtime_id("restored-runtime-observation")),
                    created_at=str(value.get("created_at") or utc_now()),
                )
            except (TypeError, ValueError):
                continue
        order = [str(item) for item in payload.get("receipt_order") or () if str(item) in receipts]
        for event_id in receipts:
            if event_id not in order:
                order.append(event_id)
        counts = dict(payload.get("counts") or {})
        self._receipts = receipts
        self._receipt_order = order[-self.retained_receipts :]
        self._producer_sequences = {
            str(key): int(value)
            for key, value in dict(payload.get("producer_sequences") or {}).items()
        }
        self._accepted_count = int(counts.get("accepted") or len(receipts))
        self._duplicate_count = int(counts.get("duplicate") or 0)
        self._rejected_count = int(counts.get("rejected") or 0)
        self._metadata_revision = revision


def runtime_observation_port_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.runtime-fault-observation-port-contract/v1",
        "owner": "RuntimeFaultObservationPort",
        "inputs_are_structured_runtime_facts": True,
        "event_id_idempotency": True,
        "producer_sequence_fence": True,
        "run_task_scope_fence": True,
        "unknown_event_fallback": False,
        "requirement_changed_is_fault": False,
        "canonical_fault_owner": "FaultStateStore",
        "canonical_classifier": "WatchdogSignalClassifier",
        "recovery_plan_owner": "M1-S07C",
    }


__all__ = [
    "RuntimeFaultObservationPort",
    "RuntimeObservationControlRequired",
    "RuntimeObservationEnvelope",
    "RuntimeObservationReceipt",
    "RuntimeObservationRejected",
    "RuntimeObservationStatus",
    "runtime_observation_port_contract",
]
