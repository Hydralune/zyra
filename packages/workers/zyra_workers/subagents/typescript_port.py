from __future__ import annotations

import copy
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from zyra_core import EventRecord, EventType

from .errors import SubagentTaskNotFound
from .isolation import LogicalWorkspaceIsolationPort
from .models import (
    StructuredSubagentMessage,
    SubagentIsolationManifest,
    SubagentIsolationRequest,
    SubagentTaskRecord,
    SubagentTaskStatus,
    UsageLedger,
)
from .task_store import SubagentTaskStore


_PORT_LOCKS_GUARD = RLock()
_PORT_LOCKS: dict[Path, RLock] = {}


def _shared_port_lock(state_root: Path) -> RLock:
    with _PORT_LOCKS_GUARD:
        return _PORT_LOCKS.setdefault(state_root, RLock())


class TypeScriptAgentDurablePort:
    """Durable/workspace port for the TypeScript logical AgentTool owner."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        workspace_root: str | Path,
        event_sink: Callable[[EventRecord], None] | None = None,
        task_store: SubagentTaskStore | None = None,
        isolation_port: LogicalWorkspaceIsolationPort | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root).resolve()
        self.task_store = task_store or SubagentTaskStore(self.state_root / "tasks.json")
        self.isolation_port = isolation_port or LogicalWorkspaceIsolationPort()
        self.event_sink = event_sink
        self._manifests: dict[str, SubagentIsolationManifest] = {}
        self._lock = _shared_port_lock(self.state_root)

    def handle(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            return self._handle(
                payload,
                run_id=run_id,
                parent_task_id=parent_task_id,
                parent_session_id=parent_session_id,
            )

    def _handle(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        task_id = str(payload.get("task_id") or "")
        try:
            self.task_store.refresh()
            authorized: SubagentTaskRecord | None = None
            if action not in {"create", "list"}:
                authorized = self.task_store.get(task_id)
                self._assert_authority(
                    authorized,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    parent_session_id=parent_session_id,
                )
            if action == "create":
                record = self._create(
                    payload,
                    run_id=run_id,
                    parent_task_id=parent_task_id,
                    parent_session_id=parent_session_id,
                )
            elif action in {"load", "status"}:
                assert authorized is not None
                record = authorized
            elif action == "list":
                tasks = [
                    item
                    for item in self.task_store.list(parent_task_id=parent_task_id)
                    if item.run_id == run_id and item.parent_session_id == parent_session_id
                ]
                return {
                    "accepted": True,
                    "task_id": task_id,
                    "status": "listed",
                    "revision": 0,
                    "error": "",
                    "tasks": [item.safe_dict() for item in tasks],
                }
            elif action == "dispatch":
                record = self._dispatch(payload)
            elif action == "running":
                record = self._transition(payload, SubagentTaskStatus.RUNNING)
            elif action == "waiting":
                record = self._transition(payload, SubagentTaskStatus.WAITING)
            elif action == "resume":
                record = self._transition(payload, SubagentTaskStatus.RESUMING)
            elif action == "complete":
                record = self._terminal(payload, SubagentTaskStatus.COMPLETED)
            elif action == "fail":
                record = self._terminal(payload, SubagentTaskStatus.FAILED)
            elif action == "cancel":
                record = self._cancel(payload)
            elif action == "progress":
                record = self._progress(payload)
            elif action == "message":
                record = self._message(payload)
            else:
                raise ValueError(f"unsupported TypeScript agent mutation: {action}")
            self._emit(action, record)
            manifest = self._manifests.get(record.task_id)
            return {
                "accepted": True,
                "task_id": record.task_id,
                "status": record.status.value,
                "revision": record.revision,
                "error": "",
                "record": record.safe_dict(),
                "physical_capability": manifest.safe_dict() if manifest is not None else {},
                "canonical_logical_owner": "typescript",
                "durable_owner": "SubagentTaskStore",
                "python_logical_fallback": False,
            }
        except Exception as error:  # noqa: BLE001
            return {
                "accepted": False,
                "task_id": task_id,
                "status": "",
                "revision": -1,
                "error": f"{type(error).__name__}: {error}",
                "error_code": (
                    "agent_task_not_found"
                    if isinstance(error, (KeyError, SubagentTaskNotFound))
                    else "agent_durable_mutation_rejected"
                ),
                "canonical_logical_owner": "typescript",
                "durable_owner": "SubagentTaskStore",
                "python_logical_fallback": False,
            }

    def snapshot(self, *, parent_task_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self.task_store.refresh()
            tasks = self.task_store.list(parent_task_id=parent_task_id)
        return {
            "schema": "zyra.typescript-agent-durable-port/v1",
            "canonical_logical_owner": "typescript",
            "durable_owner": "SubagentTaskStore",
            "physical_owner": "Zyra workspace/scheduler ports",
            "python_logical_fallback": False,
            "tasks": [item.safe_dict() for item in tasks],
            "active_task_ids": [item.task_id for item in tasks if not item.status.terminal],
            "terminal_task_ids": [item.task_id for item in tasks if item.status.terminal],
        }

    def cancel_for_parent(
        self,
        parent_task_id: str,
        *,
        reason: str = "parent_cancelled",
    ) -> tuple[SubagentTaskRecord, ...]:
        cancelled: list[SubagentTaskRecord] = []
        with self._lock:
            self.task_store.refresh()
            for item in self.task_store.list(parent_task_id=parent_task_id, include_terminal=False):
                response = self.handle(
                    {
                        "action": "cancel",
                        "task_id": item.task_id,
                        "expected_revision": item.revision,
                        "reason": reason,
                    },
                    run_id=item.run_id,
                    parent_task_id=item.parent_task_id,
                    parent_session_id=item.parent_session_id,
                )
                if response["accepted"]:
                    cancelled.append(self.task_store.get(item.task_id))
        return tuple(cancelled)

    def records(self, *, parent_task_id: str | None = None) -> tuple[SubagentTaskRecord, ...]:
        with self._lock:
            self.task_store.refresh()
            return self.task_store.list(parent_task_id=parent_task_id)

    def get_task(self, task_id: str) -> SubagentTaskRecord:
        with self._lock:
            self.task_store.refresh()
            return self.task_store.get(task_id)

    def _create(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> SubagentTaskRecord:
        raw = payload.get("record")
        if not isinstance(raw, Mapping):
            raise ValueError("agent create requires record")
        record = SubagentTaskRecord.from_dict(raw)
        if record.run_id != run_id or record.parent_task_id != parent_task_id:
            raise ValueError("agent run/parent identity mismatch")
        if record.parent_session_id != parent_session_id:
            raise ValueError("agent parent session identity mismatch")
        if record.status is not SubagentTaskStatus.CREATED or record.revision != 0:
            raise ValueError("agent create must start at created revision zero")
        if record.metadata.get("canonical_logical_owner") != "typescript":
            raise ValueError("agent create did not declare the TypeScript logical owner")
        raw_isolation = record.isolation_request.to_dict()
        raw_isolation["workspace_root"] = str(
            Path(record.isolation_request.workspace_root or self.workspace_root).resolve()
        )
        record.isolation_request = SubagentIsolationRequest.from_dict(raw_isolation)
        requested_root = Path(record.isolation_request.workspace_root).resolve()
        try:
            requested_root.relative_to(self.workspace_root)
        except ValueError as error:
            raise ValueError("agent workspace escapes the bound workspace root") from error
        manifest = self.isolation_port.prepare(record.isolation_request)
        record.metadata = {
            **record.metadata,
            "isolation_manifest": manifest.safe_dict(),
            "physical_worker_state_owned": False,
            "worker_lease_state_owned": False,
            "workspace_lifecycle_owner": manifest.state_owner,
        }
        created = self.task_store.create(
            record,
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
        if created.status is not SubagentTaskStatus.CREATED:
            return created
        self._manifests[record.task_id] = manifest
        validating, _ = self.task_store.transition(
            record.task_id,
            SubagentTaskStatus.VALIDATING,
            expected_revision=created.revision,
            mutation="typescript_agent_validated",
        )
        ready, _ = self.task_store.transition(
            record.task_id,
            SubagentTaskStatus.READY,
            expected_revision=validating.revision,
            mutation="typescript_agent_ready",
        )
        self.task_store.add_child(parent_task_id, record.task_id)
        return ready

    def _dispatch(self, payload: Mapping[str, Any]) -> SubagentTaskRecord:
        record, _ = self.task_store.attach_dispatch(
            str(payload.get("task_id") or ""),
            dispatch_request=dict(payload.get("dispatch_request") or {}),
            execution_ref=str(payload.get("execution_ref") or ""),
            expected_revision=self._expected(payload),
        )
        return record

    def _transition(
        self,
        payload: Mapping[str, Any],
        status: SubagentTaskStatus,
    ) -> SubagentTaskRecord:
        record, _ = self.task_store.transition(
            str(payload.get("task_id") or ""),
            status,
            expected_revision=self._expected(payload),
            mutation=f"typescript_agent_{status.value}",
        )
        return record

    def _terminal(
        self,
        payload: Mapping[str, Any],
        status: SubagentTaskStatus,
    ) -> SubagentTaskRecord:
        result = dict(payload.get("result") or {})

        def update(record: SubagentTaskRecord) -> None:
            record.usage = UsageLedger.from_dict(result.get("usage"))
            record.metadata["typescript_result"] = copy.deepcopy(result)
            record.metadata["result_digest"] = str(payload.get("result_digest") or "")
            record.metadata["result_commit_owner"] = "typescript-agent-runtime"
            record.metadata["result_committed"] = True

        record, _ = self.task_store.transition(
            str(payload.get("task_id") or ""),
            status,
            expected_revision=self._expected(payload),
            mutation=f"typescript_agent_{status.value}_commit",
            update=update,
        )
        self._cleanup(record.task_id)
        return record

    def _cancel(self, payload: Mapping[str, Any]) -> SubagentTaskRecord:
        task_id = str(payload.get("task_id") or "")
        current = self.task_store.get(task_id)
        expected = self._expected(payload)
        if current.revision != expected:
            raise ValueError("agent cancel revision conflict")
        if current.status.terminal:
            return current

        def update(record: SubagentTaskRecord) -> None:
            record.metadata["cancel_reason"] = str(payload.get("reason") or "cancelled")
            record.metadata["late_result_fenced"] = True

        record, _ = self.task_store.transition(
            task_id,
            SubagentTaskStatus.CANCELLED,
            expected_revision=expected,
            mutation="typescript_agent_cancel",
            update=update,
        )
        for descendant in self.task_store.descendants(task_id):
            if descendant.status.terminal:
                continue
            self.task_store.transition(
                descendant.task_id,
                SubagentTaskStatus.CANCELLED,
                expected_revision=descendant.revision,
                mutation="typescript_parent_cancel_propagated",
                update=lambda child: child.metadata.update({"late_result_fenced": True}),
            )
            self._cleanup(descendant.task_id)
        self._cleanup(task_id)
        return record

    def _progress(self, payload: Mapping[str, Any]) -> SubagentTaskRecord:
        sequence = int(payload.get("sequence") or 0)

        def update(record: SubagentTaskRecord) -> None:
            previous = int(record.metadata.get("typescript_progress_sequence") or 0)
            if sequence <= previous:
                raise ValueError("duplicate or late agent progress sequence")
            record.metadata["typescript_progress_sequence"] = sequence
            record.metadata["typescript_progress"] = copy.deepcopy(dict(payload.get("progress") or {}))

        record, _ = self.task_store.mutate(
            str(payload.get("task_id") or ""),
            "typescript_agent_progress",
            update,
            expected_revision=self._expected(payload),
        )
        return record

    def _message(self, payload: Mapping[str, Any]) -> SubagentTaskRecord:
        raw = payload.get("message")
        if not isinstance(raw, Mapping):
            raise ValueError("agent message requires a structured message")
        message = StructuredSubagentMessage(
            sender_task_id=str(raw.get("sender_task_id") or ""),
            target_task_id=str(raw.get("target_task_id") or ""),
            intent=str(raw.get("intent") or "message"),
            summary=str(raw.get("summary") or ""),
            state_delta=dict(raw.get("state_delta") or {}),
            evidence_refs=tuple(raw.get("evidence_refs") or ()),
            artifact_refs=tuple(raw.get("artifact_refs") or ()),
            message_id=str(raw.get("message_id") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )
        current = self.task_store.get(str(payload.get("task_id") or ""))
        if current.revision != self._expected(payload):
            raise ValueError("agent message revision conflict")
        return self.task_store.append_message(current.task_id, message)

    @staticmethod
    def _expected(payload: Mapping[str, Any]) -> int:
        value = payload.get("expected_revision")
        if not isinstance(value, int) or value < 0:
            raise ValueError("agent mutation requires expected_revision")
        return value

    @staticmethod
    def _assert_authority(
        record: SubagentTaskRecord,
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> None:
        if record.run_id != run_id:
            raise ValueError("agent run authority mismatch")
        if record.parent_task_id != parent_task_id:
            raise ValueError("agent parent task authority mismatch")
        if record.parent_session_id != parent_session_id:
            raise ValueError("agent parent session authority mismatch")

    def _cleanup(self, task_id: str) -> None:
        manifest = self._manifests.pop(task_id, None)
        if manifest is not None:
            self.isolation_port.cleanup(manifest)

    def _emit(self, action: str, record: SubagentTaskRecord) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            EventRecord(
                run_id=record.run_id,
                task_id=record.parent_task_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "agent_lifecycle": {
                        "action": action,
                        "agent_task_id": record.task_id,
                        "parent_task_id": record.parent_task_id,
                        "status": record.status.value,
                        "revision": record.revision,
                        "canonical_logical_owner": "typescript",
                        "durable_owner": "SubagentTaskStore",
                    }
                },
            )
        )
