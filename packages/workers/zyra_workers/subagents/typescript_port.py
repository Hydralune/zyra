from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from zyra_core import EventRecord, EventType


_PORT_LOCKS_GUARD = RLock()
_PORT_LOCKS: dict[Path, RLock] = {}
_TERMINAL = frozenset({"completed", "failed", "cancelled", "killed"})


def _shared_port_lock(state_root: Path) -> RLock:
    with _PORT_LOCKS_GUARD:
        return _PORT_LOCKS.setdefault(state_root, RLock())


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _without_checksum(value: Mapping[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(dict(value))
    output.pop("checksum", None)
    return output


@dataclass(frozen=True, slots=True)
class TypeScriptTaskStatusProjection:
    value: str

    @property
    def terminal(self) -> bool:
        return self.value in _TERMINAL


@dataclass(frozen=True, slots=True)
class TypeScriptTaskProjection:
    task_id: str
    parent_task_id: str
    parent_session_id: str
    run_id: str
    status: TypeScriptTaskStatusProjection
    revision: int
    payload: dict[str, Any]

    def safe_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


class TypeScriptAgentDurablePort:
    """Narrow E03 CAS/physical-effect port.

    TypeScript supplies the complete canonical registry snapshot and every next
    task transition. Python validates custody, executes explicitly requested
    physical effects, deduplicates receipts, and atomically persists the exact
    snapshot. It never selects an agent, advances a phase, fans out, resumes,
    cancels, kills, or rewrites a logical result.
    """

    def __init__(
        self,
        state_root: str | Path,
        *,
        workspace_root: str | Path,
        event_sink: Callable[[EventRecord], None] | None = None,
        **_legacy_ports: object,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.path = self.state_root / "e03-registry.json"
        self.event_sink = event_sink
        self._lock = _shared_port_lock(self.state_root)

    def handle(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        task_id = str(payload.get("task_id") or parent_task_id)
        with self._lock:
            try:
                if action == "e03.restore":
                    return self._restore(run_id, parent_task_id, parent_session_id)
                if action == "e03.effect":
                    return self._effect(payload, run_id, parent_task_id, parent_session_id)
                if action == "e03.cas":
                    return self._compare_and_swap(payload, run_id, parent_task_id, parent_session_id)
                raise ValueError(f"unsupported E03 physical-port action: {action}")
            except Exception as error:  # noqa: BLE001 - typed protocol boundary.
                return {
                    "accepted": False,
                    "task_id": task_id,
                    "status": "",
                    "revision": self._document().get("revision", 0),
                    "error": f"{type(error).__name__}: {error}",
                    "error_code": "e03_snapshot_not_found" if isinstance(error, FileNotFoundError) else "e03_physical_port_rejected",
                    "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
                    "durable_owner": "python.atomic-cas-port",
                    "python_logical_fallback": False,
                }

    def records(self, *, parent_task_id: str | None = None) -> tuple[TypeScriptTaskProjection, ...]:
        with self._lock:
            snapshot = dict(self._document().get("snapshot") or {})
            tasks = dict(snapshot.get("tasks") or {})
            values = [self._projection(dict(value)) for value in tasks.values() if isinstance(value, Mapping)]
        selected = [item for item in values if parent_task_id is None or item.parent_task_id == parent_task_id]
        return tuple(sorted(selected, key=lambda item: (str(item.payload.get("createdAt") or ""), item.task_id)))

    def get_task(self, task_id: str) -> TypeScriptTaskProjection:
        with self._lock:
            snapshot = dict(self._document().get("snapshot") or {})
            raw = dict(snapshot.get("tasks") or {}).get(task_id)
            if not isinstance(raw, Mapping):
                raise KeyError(task_id)
            return self._projection(dict(raw))

    def snapshot(self, *, parent_task_id: str | None = None) -> dict[str, Any]:
        tasks = self.records(parent_task_id=parent_task_id)
        document = self._document()
        return {
            "schema": "zyra.e03-python-physical-port/v1",
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
            "durable_owner": "python.atomic-cas-port",
            "physical_owner": "Zyra workspace/process/artifact ports",
            "python_logical_fallback": False,
            "revision": int(document.get("revision") or 0),
            "tasks": [item.safe_dict() for item in tasks],
            "active_task_ids": [item.task_id for item in tasks if not item.status.terminal],
            "terminal_task_ids": [item.task_id for item in tasks if item.status.terminal],
        }

    def _restore(self, run_id: str, parent_task_id: str, parent_session_id: str) -> dict[str, Any]:
        document = self._document(required=True)
        self._assert_authority(document, run_id, parent_task_id, parent_session_id)
        snapshot = document.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise FileNotFoundError("E03 snapshot is not initialized")
        return {
            "accepted": True,
            "task_id": parent_task_id,
            "status": "restored",
            "revision": int(document.get("revision") or 0),
            "error": "",
            "snapshot": copy.deepcopy(dict(snapshot)),
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
            "durable_owner": "python.atomic-cas-port",
            "python_logical_fallback": False,
        }

    def _effect(
        self,
        payload: Mapping[str, Any],
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> dict[str, Any]:
        raw = payload.get("effect_request")
        if not isinstance(raw, Mapping):
            raise ValueError("e03.effect requires effect_request")
        request = copy.deepcopy(dict(raw))
        self._assert_effect_request(request)
        document = self._document()
        if document.get("authorities"):
            self._assert_authority(document, run_id, parent_task_id, parent_session_id)
        effects = dict(document.get("effects") or {})
        effect_id = str(request["effectId"])
        prior = effects.get(effect_id)
        if isinstance(prior, Mapping):
            self._assert_receipt_request(dict(prior), request)
            replay = copy.deepcopy(dict(prior))
            replay["replayed"] = True
            replay = self._seal(replay, "digest")
            return self._effect_response(replay, int(document.get("revision") or 0))
        result = self._execute_physical_effect(request)
        unsigned = {
            "receiptId": f"receipt-{_digest(effect_id)[:32]}",
            "effectId": effect_id,
            "requestId": str(request["requestId"]),
            "taskId": str(request["taskId"]),
            "leaseId": str(request["leaseId"]),
            "expectedRevision": int(request["expectedRevision"]),
            "accepted": True,
            "replayed": False,
            "result": result,
            "artifacts": [],
            "error": "",
            "completedAt": self._now(),
        }
        receipt = self._seal(unsigned, "digest")
        effects[effect_id] = receipt
        next_document = {
            **document,
            "version": "zyra.e03-python-port/v1",
            "authorities": document.get("authorities") or {"run_id": run_id, "parent_task_id": parent_task_id, "parent_session_id": parent_session_id},
            "revision": int(document.get("revision") or 0),
            "effects": effects,
            "updated_at": self._now(),
        }
        self._write_document(next_document)
        self._emit("effect", request, run_id, parent_task_id)
        return self._effect_response(receipt, int(next_document["revision"]))

    def _compare_and_swap(
        self,
        payload: Mapping[str, Any],
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
    ) -> dict[str, Any]:
        expected = payload.get("expected_registry_revision")
        snapshot = payload.get("registry_snapshot")
        if not isinstance(expected, int) or expected < 0:
            raise ValueError("e03.cas requires a non-negative expected_registry_revision")
        if not isinstance(snapshot, Mapping):
            raise ValueError("e03.cas requires registry_snapshot")
        candidate = copy.deepcopy(dict(snapshot))
        self._assert_snapshot(candidate, run_id, parent_task_id, parent_session_id)
        document = self._document()
        if document.get("authorities"):
            self._assert_authority(document, run_id, parent_task_id, parent_session_id)
        actual = int(document.get("revision") or 0)
        if actual != expected:
            current = document.get("snapshot")
            replayed = isinstance(current, Mapping) and str(current.get("checksum") or "") == str(candidate.get("checksum") or "")
            return {
                "accepted": replayed,
                "task_id": parent_task_id,
                "status": "replayed" if replayed else "conflict",
                "revision": actual,
                "replayed": replayed,
                "error": "" if replayed else "stale_revision",
                "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
                "durable_owner": "python.atomic-cas-port",
                "python_logical_fallback": False,
            }
        next_revision = int(candidate.get("revision") or 0)
        if next_revision != expected + 1:
            raise ValueError(f"registry revision must advance exactly once: {expected} -> {next_revision}")
        next_document = {
            "version": "zyra.e03-python-port/v1",
            "authorities": {"run_id": run_id, "parent_task_id": parent_task_id, "parent_session_id": parent_session_id},
            "revision": next_revision,
            "snapshot": candidate,
            "effects": dict(document.get("effects") or {}),
            "updated_at": self._now(),
        }
        self._write_document(next_document)
        self._emit("cas", {"revision": next_revision}, run_id, parent_task_id)
        return {
            "accepted": True,
            "task_id": parent_task_id,
            "status": "committed",
            "revision": next_revision,
            "replayed": False,
            "error": "",
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
            "durable_owner": "python.atomic-cas-port",
            "python_logical_fallback": False,
        }

    def _execute_physical_effect(self, request: Mapping[str, Any]) -> dict[str, Any]:
        effect_kind = str(request.get("effectKind") or "")
        operation = str(request.get("operation") or "")
        body = dict(request.get("payload") or {})
        if effect_kind == "workspace" and operation == "prepare_workspace_isolation":
            return self._prepare_workspace(body)
        if effect_kind == "process" and operation in {"cancel_child_process", "kill_child_process", "abort_child_process"}:
            return {"operation": operation, "signal_recorded": True, "physical_process_found": False}
        if effect_kind in {"persist", "artifact", "event"}:
            return {"operation": operation, "persisted": effect_kind == "persist", "physical_port": "python.e03-effect-port"}
        raise ValueError(f"unsupported E03 physical effect: {effect_kind}/{operation}")

    def _prepare_workspace(self, request: Mapping[str, Any]) -> dict[str, Any]:
        mode = str(request.get("mode") or "workspace")
        root = Path(str(request.get("workspaceRoot") or self.workspace_root)).resolve()
        self._require_contained(self.workspace_root, root)
        if not root.is_dir():
            raise ValueError(f"workspace root does not exist: {root}")
        base_revision = str(request.get("baseRevision") or "HEAD")
        dirty = bool(self._git(root, "status", "--porcelain"))
        if dirty and not bool(request.get("allowDirtyBaseline")):
            raise ValueError("workspace baseline is dirty")
        observed = self._git(root, "rev-parse", base_revision) if (root / ".git").exists() else base_revision
        if mode != "worktree":
            return {
                "workspace_path": str(root),
                "observed_base_revision": observed,
                "resulting_revision": observed,
                "dirty_baseline": dirty,
                "nested_repository": False,
                "physical_isolation": mode == "workspace",
            }
        worktree_root = (self.state_root / "worktrees").resolve()
        worktree_root.mkdir(parents=True, exist_ok=True)
        task_fragment = _digest(str(request.get("taskId") or request.get("requestId") or "task"))[:24]
        target = (worktree_root / task_fragment).resolve()
        self._require_contained(worktree_root, target)
        if target.exists():
            current = self._git(target, "rev-parse", "HEAD")
            if current != observed:
                raise ValueError("existing worktree revision differs from requested base")
        else:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(target), observed],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        return {
            "workspace_path": str(target),
            "observed_base_revision": observed,
            "resulting_revision": self._git(target, "rev-parse", "HEAD"),
            "dirty_baseline": dirty,
            "nested_repository": False,
            "physical_isolation": True,
            "backend": "git-worktree",
        }

    def _assert_snapshot(self, snapshot: Mapping[str, Any], run_id: str, parent_task_id: str, parent_session_id: str) -> None:
        if snapshot.get("schemaVersion") != "3.0":
            raise ValueError("E03 snapshot schemaVersion must be 3.0")
        checksum = str(snapshot.get("checksum") or "")
        if checksum != _digest(_without_checksum(snapshot)):
            raise ValueError("E03 registry checksum mismatch")
        tasks = snapshot.get("tasks")
        if not isinstance(tasks, Mapping):
            raise ValueError("E03 registry tasks must be an object")
        for task_id, raw in tasks.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"E03 task {task_id} is not an object")
            task = dict(raw)
            if str(task.get("checksum") or "") != _digest(_without_checksum(task)):
                raise ValueError(f"E03 task {task_id} checksum mismatch")
            identity = dict(task.get("identity") or {})
            if str(identity.get("taskId") or "") != str(task_id):
                raise ValueError(f"E03 task map key differs from identity: {task_id}")
            if str(identity.get("runId") or "") != run_id:
                raise ValueError("E03 task run authority mismatch")
            session_matches = str(identity.get("sessionId") or "") == parent_session_id or str(identity.get("parentSessionId") or "") == parent_session_id
            if not session_matches:
                raise ValueError("E03 task session authority mismatch")
            parent_matches = str(identity.get("parentTaskId") or "") == parent_task_id or parent_task_id in tuple(identity.get("lineage") or ())
            if not parent_matches:
                raise ValueError("E03 task parent authority mismatch")

    @staticmethod
    def _assert_effect_request(request: Mapping[str, Any]) -> None:
        required = ("effectId", "requestId", "taskId", "leaseId", "expectedRevision", "effectKind", "operation", "idempotencyKey", "preparedAt", "digest")
        if any(key not in request for key in required):
            raise ValueError("E03 effect request is incomplete")
        if str(request.get("digest") or "") != _digest(_without_checksum({**request, "checksum": request.get("digest")})):
            unsigned = dict(request)
            unsigned.pop("digest", None)
            if str(request.get("digest") or "") != _digest(unsigned):
                raise ValueError("E03 effect request checksum mismatch")

    @staticmethod
    def _assert_receipt_request(receipt: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        for receipt_key, request_key in (("effectId", "effectId"), ("requestId", "requestId"), ("taskId", "taskId"), ("leaseId", "leaseId"), ("expectedRevision", "expectedRevision")):
            if receipt.get(receipt_key) != request.get(request_key):
                raise ValueError("E03 effect receipt idempotency conflict")

    @staticmethod
    def _assert_authority(document: Mapping[str, Any], run_id: str, parent_task_id: str, parent_session_id: str) -> None:
        authority = dict(document.get("authorities") or {})
        expected = {"run_id": run_id, "parent_task_id": parent_task_id, "parent_session_id": parent_session_id}
        if authority and authority != expected:
            raise ValueError("E03 port authority mismatch")

    def _document(self, *, required: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise
            return {"version": "zyra.e03-python-port/v1", "authorities": {}, "revision": 0, "snapshot": None, "effects": {}, "updated_at": ""}
        if not isinstance(raw, dict):
            raise ValueError("E03 port document must be an object")
        checksum = str(raw.pop("checksum", ""))
        if checksum != _digest(raw):
            raise ValueError("E03 port document checksum mismatch")
        return raw

    def _write_document(self, document: Mapping[str, Any]) -> None:
        payload = copy.deepcopy(dict(document))
        payload["checksum"] = _digest(payload)
        temporary = self.path.with_suffix(f".json.{os.getpid()}.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
        output = copy.deepcopy(dict(value))
        output.pop(field, None)
        output[field] = _digest(output)
        return output

    @staticmethod
    def _projection(task: dict[str, Any]) -> TypeScriptTaskProjection:
        identity = dict(task.get("identity") or {})
        payload = {
            "task_id": str(identity.get("taskId") or ""),
            "parent_task_id": str(identity.get("parentTaskId") or ""),
            "parent_session_id": str(identity.get("parentSessionId") or ""),
            "session_id": str(identity.get("sessionId") or ""),
            "run_id": str(identity.get("runId") or ""),
            "lease_id": str(identity.get("leaseId") or ""),
            "attempt": int(identity.get("attempt") or 0),
            "lineage": list(identity.get("lineage") or ()),
            "status": str(task.get("status") or ""),
            "revision": int(task.get("revision") or 0),
            "sequence": int(task.get("sequence") or 0),
            "messages": copy.deepcopy(list(task.get("messages") or ())),
            "deliveries": copy.deepcopy(list(task.get("deliveries") or ())),
            "result": copy.deepcopy(task.get("result")),
            "error": str(task.get("error") or ""),
            "checksum": str(task.get("checksum") or ""),
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
        }
        return TypeScriptTaskProjection(
            task_id=payload["task_id"],
            parent_task_id=payload["parent_task_id"],
            parent_session_id=payload["parent_session_id"],
            run_id=payload["run_id"],
            status=TypeScriptTaskStatusProjection(payload["status"]),
            revision=payload["revision"],
            payload=payload,
        )

    @staticmethod
    def _require_contained(root: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError(f"physical workspace escapes bound root: {candidate}") from error

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True, timeout=30)
        return result.stdout.strip()

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _effect_response(receipt: Mapping[str, Any], revision: int) -> dict[str, Any]:
        return {
            "accepted": bool(receipt.get("accepted")),
            "task_id": str(receipt.get("taskId") or ""),
            "status": "effected" if receipt.get("accepted") else "rejected",
            "revision": revision,
            "error": str(receipt.get("error") or ""),
            "effect_receipt": copy.deepcopy(dict(receipt)),
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
            "durable_owner": "python.atomic-cas-port",
            "python_logical_fallback": False,
        }

    def _emit(self, action: str, payload: Mapping[str, Any], run_id: str, parent_task_id: str) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=parent_task_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "e03_physical_port": {
                        "action": action,
                        "task_id": str(payload.get("taskId") or parent_task_id),
                        "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
                        "python_logical_fallback": False,
                    }
                },
            )
        )
