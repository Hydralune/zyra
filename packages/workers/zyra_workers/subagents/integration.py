from __future__ import annotations

"""Composition layer for the M1-S03D-02 logical subagent integration."""

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from .delivery import DurableDeliveryStore, LogicalTaskEdge
from .execution_receipts import (
    ExecutionClaim,
    ExecutionIdentity,
    ExecutionPhase,
    ExecutionReceiptStore,
    request_digest,
)
from .models import PermissionMode, SubagentSpawnRequest, SubagentTaskRecord
from .parent_scope import (
    ChildExecutionScope,
    ChildScopeDeriver,
    ChildScopeRequest,
    ParentExecutionScopeSnapshot,
    ParentScopeStore,
)
from .resume_capsule import ResumeCapsuleRuntime, ResumeCapsuleStore
from .task_store import SubagentTaskStore
from .transcript import SubagentTranscriptStore
from .typed_yield import TypedYieldStore


class SubagentIntegrationError(RuntimeError):
    pass


class SubagentIntegrationDisabled(SubagentIntegrationError):
    pass


class ParentScopeRequired(SubagentIntegrationError, PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class SpawnPreflight:
    request: SubagentSpawnRequest
    parent_scope: ParentExecutionScopeSnapshot
    child_scope: ChildExecutionScope
    execution_claim: ExecutionClaim
    replay_record: SubagentTaskRecord | None = None

    @property
    def replay(self) -> bool:
        return self.replay_record is not None or self.execution_claim.replay


@dataclass(frozen=True, slots=True)
class RestartReconciliation:
    parked_receipts: tuple[Mapping[str, Any], ...]
    recovered_deliveries: tuple[Mapping[str, Any], ...]
    parked_tasks: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parked_receipts": [dict(item) for item in self.parked_receipts],
            "recovered_deliveries": [dict(item) for item in self.recovered_deliveries],
            "parked_tasks": list(self.parked_tasks),
            "created_at": self.created_at,
        }


class SubagentIntegrationRuntime:
    """Own integration receipts while delegating core task state to 03D stores."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        task_store: SubagentTaskStore,
        transcript_store: SubagentTranscriptStore,
        disabled: bool = False,
        require_signed_parent_scope: bool = True,
    ) -> None:
        root = Path(state_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self.require_signed_parent_scope = bool(require_signed_parent_scope)
        self.task_store = task_store
        self.transcript_store = transcript_store
        self.parent_scopes = ParentScopeStore(root / "parent-scopes.json")
        self.scope_deriver = ChildScopeDeriver()
        self.execution_receipts = ExecutionReceiptStore(root / "execution-receipts.json")
        self.typed_yields = TypedYieldStore(root / "typed-yields.json")
        self.delivery_store = DurableDeliveryStore(root / "deliveries.json")
        self.resume_capsules = ResumeCapsuleStore(root / "resume-capsules.json")
        self.resume_runtime = ResumeCapsuleRuntime(
            self.resume_capsules,
            transcript_store,
        )
        self._lock = RLock()
        self._attempt_tokens: dict[str, str] = {}

    def preflight(self, request: SubagentSpawnRequest) -> SpawnPreflight:
        self._require_enabled()
        existing = self.task_store.list()
        existing_by_key = {
            str(item.metadata.get("idempotency_key") or ""): item
            for item in existing
            if item.metadata.get("idempotency_key")
        }
        replay_record = existing_by_key.get(request.idempotency_key)
        scope_id = str(request.parent_scope_snapshot_id or request.metadata.get("parent_scope_snapshot_id") or "")
        if not scope_id:
            if self.require_signed_parent_scope:
                raise ParentScopeRequired("server-issued parent_scope_snapshot_id is required")
            raise ParentScopeRequired("unsigned parent scope is not supported")
        current_revision = request.metadata.get("current_parent_session_revision")
        parent_scope = self.parent_scopes.get(
            scope_id,
            current_session_revision=(int(current_revision) if current_revision is not None else None),
        )
        self._assert_parent_identity(request, parent_scope)
        child_scope = self.scope_deriver.derive(parent_scope, ChildScopeRequest(
            requested_tools=request.requested_tools,
            requested_mcp_servers=request.requested_mcp_servers,
            requested_permission_mode=request.requested_permission_mode,
            requested_model=str(request.constraints.get("model_name") or request.constraints.get("model") or ""),
            requested_writable_paths=tuple(request.constraints.get("writable_paths") or ()),
            requested_readable_paths=tuple(request.constraints.get("read_only_paths") or ()),
            requested_network=bool(request.constraints.get("network_allowed", False)),
            requested_skill_refs=tuple(request.constraints.get("agent_skills") or ()),
            requested_hook_refs=tuple(request.constraints.get("agent_hooks") or ()),
            expected_session_revision=(
                int(request.metadata["expected_parent_session_revision"])
                if request.metadata.get("expected_parent_session_revision") is not None else None
            ),
            expected_permission_revision=(
                int(request.metadata["expected_parent_permission_revision"])
                if request.metadata.get("expected_parent_permission_revision") is not None else None
            ),
            expected_tool_generation=(
                int(request.metadata["expected_parent_tool_generation"])
                if request.metadata.get("expected_parent_tool_generation") is not None else None
            ),
            expected_mcp_generations={
                str(key): int(value)
                for key, value in (request.metadata.get("expected_parent_mcp_generations") or {}).items()
            },
        ))
        canonical_request = self._canonical_request(request, parent_scope, child_scope)
        identity = ExecutionIdentity(
            run_id=request.run_id,
            parent_task_id=request.parent_task_id,
            task_id=(replay_record.task_id if replay_record else request.task_id),
            parent_session_id=request.parent_session_id,
            request_digest=request_digest({
                "agent_type": request.agent_type,
                "prompt": request.prompt,
                "parent_scope_digest": parent_scope.unsigned_digest,
                "child_scope_digest": child_scope.digest,
                "context_mode": request.context_mode.value,
                "execution_mode": request.execution_mode.value,
            }),
            idempotency_key=request.idempotency_key,
        )
        claim = self.execution_receipts.claim(identity)
        if claim.created:
            with self._lock:
                self._attempt_tokens[claim.receipt.receipt_id] = claim.attempt_token
        return SpawnPreflight(
            request=canonical_request,
            parent_scope=parent_scope,
            child_scope=child_scope,
            execution_claim=claim,
            replay_record=copy.deepcopy(replay_record),
        )

    def bind_created_task(self, preflight: SpawnPreflight, record: SubagentTaskRecord) -> None:
        self._require_enabled()
        edge = LogicalTaskEdge(
            parent_task_id=record.parent_task_id,
            child_task_id=record.task_id,
            run_id=record.run_id,
            parent_session_id=record.parent_session_id,
            child_session_id=str(record.metadata.get("child_session_id") or ""),
        )
        self.delivery_store.bind_edge(edge)

    def transition_receipt(
        self,
        receipt_id: str,
        phase: ExecutionPhase,
        *,
        digest_value: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> Mapping[str, Any]:
        token = self.attempt_token(receipt_id)
        receipt = self.execution_receipts.transition(
            receipt_id,
            phase,
            attempt_token=token,
            effect_digest=request_digest(digest_value or {}) if digest_value is not None else "",
            reason=reason,
        )
        if receipt.terminal:
            with self._lock:
                self._attempt_tokens.pop(receipt_id, None)
        return receipt.to_dict()

    def create_resume_capsule(
        self,
        record: SubagentTaskRecord,
        *,
        yield_assembly: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        receipt = self.execution_receipts.for_task(record.task_id)
        if receipt is None:
            raise SubagentIntegrationError("execution receipt is missing for resume capsule")
        capsule = self.resume_runtime.create(
            task_record=record,
            execution_receipt=receipt.to_dict(),
            yield_assembly=yield_assembly,
            pending_messages=tuple(item.to_dict() for item in record.pending_messages),
        )
        return capsule.to_dict()

    def materialize_resume(self, task_id: str, *, idempotency_key: str) -> Mapping[str, Any]:
        capsule = self.resume_capsules.latest(task_id)
        material = self.resume_runtime.materialize(capsule.capsule_id, idempotency_key=idempotency_key)
        return material.to_dict()

    def attempt_token(self, receipt_id: str) -> str:
        with self._lock:
            token = self._attempt_tokens.get(receipt_id)
        if not token:
            raise SubagentIntegrationError(
                "plaintext attempt token is unavailable; restart reconciliation is required and replay is forbidden"
            )
        return token

    def reconcile_after_restart(self) -> RestartReconciliation:
        from zyra_core import now_iso

        parked = self.execution_receipts.reconcile_after_restart()
        recovered = self.delivery_store.recover_claims()
        parked_tasks = []
        for receipt in parked:
            task_id = receipt.identity.task_id
            parked_tasks.append(task_id)
            try:
                record = self.task_store.get(task_id)
            except Exception:
                continue
            if record.status.terminal:
                continue

            def update(item: SubagentTaskRecord) -> None:
                item.metadata["restart_parked"] = True
                item.metadata["restart_receipt_id"] = receipt.receipt_id
                item.metadata["restart_reason"] = receipt.terminal_reason

            try:
                self.task_store.mutate(task_id, "restart_parked_no_replay", update)
            except Exception:
                pass
        return RestartReconciliation(
            parked_receipts=tuple(item.to_dict() for item in parked),
            recovered_deliveries=tuple(item.to_dict() for item in recovered),
            parked_tasks=tuple(parked_tasks),
            created_at=now_iso(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "zyra.subagent-integration/v1",
            "owner": "M1-S03D-02 SubagentIntegrationRuntime",
            "parent_scopes": self.parent_scopes.snapshot(),
            "execution_receipts": self.execution_receipts.snapshot(),
            "typed_yields": self.typed_yields.snapshot(),
            "deliveries": self.delivery_store.snapshot(),
            "resume_capsules": self.resume_capsules.snapshot(),
            "physical_worker_state_owned": False,
            "workspace_lifecycle_owned": False,
        }

    def _canonical_request(
        self,
        request: SubagentSpawnRequest,
        parent: ParentExecutionScopeSnapshot,
        child: ChildExecutionScope,
    ) -> SubagentSpawnRequest:
        context_payload = copy.deepcopy(request.context_payload)
        context_payload["parent_permission_rule_ids"] = list(child.permission_rule_ids)
        context_payload["parent_permission_denials"] = list(child.deny_rule_ids)
        context_payload["context_epoch"] = parent.context_epoch
        context_payload["compact_boundary_id"] = parent.compact_boundary_id
        context_payload["parent_scope_snapshot_id"] = parent.snapshot_id
        context_payload["parent_scope_digest"] = parent.unsigned_digest
        ancestry, cycle_keys = self._canonical_ancestry(request)
        context_payload["ancestry"] = list(ancestry)
        context_payload["cycle_keys"] = list(cycle_keys)
        constraints = {
            **copy.deepcopy(request.constraints),
            "model_name": child.model,
            "model": child.model,
            "writable_paths": list(child.writable_paths),
            "read_only_paths": list(child.readable_paths),
            "network_allowed": child.network_allowed,
            "agent_skills": list(child.skill_refs),
            "agent_hooks": list(child.hook_refs),
            "signed_child_scope": child.to_dict(),
            "expected_child_tool_names": list(child.tool_names),
        }
        metadata = {
            **copy.deepcopy(request.metadata),
            "parent_scope_snapshot_id": parent.snapshot_id,
            "parent_scope_digest": parent.unsigned_digest,
            "child_scope_digest": child.digest,
            "client_declared_parent_ceiling_used": False,
        }
        return replace(
            request,
            parent_scope_snapshot_id=parent.snapshot_id,
            parent_tools=child.tool_names,
            parent_permission_mode=parent.permission_mode,
            requested_tools=child.tool_names,
            requested_permission_mode=child.permission_mode,
            available_mcp_servers=parent.mcp_servers,
            requested_mcp_servers=child.mcp_servers,
            workspace_root=child.workspace_root,
            context_payload=context_payload,
            constraints=constraints,
            metadata=metadata,
        )

    def _canonical_ancestry(self, request: SubagentSpawnRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            parent = self.task_store.get(request.parent_task_id)
        except Exception:
            # Root task ancestry is supplied by the signed parent scope, not by
            # arbitrary client payload.
            return ((request.parent_task_id,), (f"root:{request.parent_task_id}",))
        ancestry = tuple(parent.context_snapshot.ancestry) + (parent.task_id,)
        cycle_keys = tuple(parent.context_snapshot.metadata.get("cycle_keys") or ())
        key = f"agent:{parent.agent_type}:{parent.prompt_digest}"
        return ancestry, tuple(dict.fromkeys((*cycle_keys, key)))

    @staticmethod
    def _assert_parent_identity(request: SubagentSpawnRequest, parent: ParentExecutionScopeSnapshot) -> None:
        values = {
            "run_id": (request.run_id, parent.run_id),
            "parent_task_id": (request.parent_task_id, parent.parent_task_id),
            "parent_session_id": (request.parent_session_id, parent.parent_session_id),
        }
        mismatches = [name for name, (left, right) in values.items() if left != right]
        if mismatches:
            raise ParentScopeRequired("parent scope identity mismatch: " + ", ".join(mismatches))

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SubagentIntegrationDisabled("SubagentIntegrationRuntime is disabled")


__all__ = [
    "ParentScopeRequired",
    "RestartReconciliation",
    "SpawnPreflight",
    "SubagentIntegrationDisabled",
    "SubagentIntegrationError",
    "SubagentIntegrationRuntime",
]
