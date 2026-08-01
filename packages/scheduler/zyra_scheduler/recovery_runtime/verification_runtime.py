from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import (
    LayeredRouteDecision,
    RecoveryAction,
    RecoveryActionReceipt,
    RecoveryAttemptStatus,
    RecoveryContext,
    RecoveryOutcome,
    RecoveryOutcomeKind,
    RecoveryPlan,
    RouteLayer,
    stable_digest,
    utc_now,
)
from .route_runtime import LayeredRouteRuntime, RouteOwnerUnavailable


class RecoveryVerificationError(RuntimeError):
    pass


class AppliedReceiptRejected(RecoveryVerificationError):
    pass


class ContinuationDispatchRejected(RecoveryVerificationError):
    pass


class VerificationKind(StrEnum):
    ROUTE_MUTATION = "route_mutation"
    TASK_MUTATION = "task_mutation"
    PERMISSION_BLOCK = "permission_block"
    MCP_CONTROL = "mcp_control"
    CONTEXT_COMPACTION = "context_compaction"
    CHECKPOINT_RESUME = "checkpoint_resume"
    TASK_ABORT = "task_abort"
    RETRY_GENERATION = "retry_generation"


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    owner: str
    operation: str
    run_id: str
    task_id: str
    revision: str
    projection: Mapping[str, Any]
    observed_at: str = field(default_factory=utc_now)

    @property
    def digest(self) -> str:
        return stable_digest({
            "owner": self.owner,
            "operation": self.operation,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "projection": dict(self.projection),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "operation": self.operation,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "projection": copy.deepcopy(dict(self.projection)),
            "observed_at": self.observed_at,
            "digest": self.digest,
        }


class ProjectionPort(Protocol):
    owner: str

    def snapshot(self, plan: RecoveryPlan, receipt: RecoveryActionReceipt) -> ProjectionSnapshot | Mapping[str, Any]: ...


class CallbackProjectionPort:
    def __init__(
        self,
        owner: str,
        callback: Callable[[RecoveryPlan, RecoveryActionReceipt], ProjectionSnapshot | Mapping[str, Any]],
    ) -> None:
        self.owner = str(owner).strip()
        if not self.owner:
            raise ValueError("projection owner is required")
        self._callback = callback

    def snapshot(self, plan: RecoveryPlan, receipt: RecoveryActionReceipt) -> ProjectionSnapshot:
        value = self._callback(plan, receipt)
        if isinstance(value, ProjectionSnapshot):
            if value.owner != self.owner:
                raise RecoveryVerificationError("projection port returned another owner")
            return value
        result = copy.deepcopy(dict(value))
        projection = copy.deepcopy(dict(result.pop("projection", result)))
        return ProjectionSnapshot(
            owner=self.owner,
            operation=receipt.action.value,
            run_id=str(result.get("run_id") or plan.signal.refs.run_id),
            task_id=str(result.get("task_id") or plan.signal.refs.task_id),
            revision=str(result.get("revision") or projection.get("revision") or ""),
            projection=projection,
            observed_at=str(result.get("observed_at") or utc_now()),
        )


@dataclass(frozen=True, slots=True)
class ContinuationRequest:
    run_id: str
    task_id: str
    plan_id: str
    signal_id: str
    action: RecoveryAction
    action_receipt_ids: tuple[str, ...]
    route_decision_id: str
    checkpoint_id: str
    expected_effects: tuple[str, ...]
    context_digest: str
    idempotency_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-continuation-request/v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "signal_id": self.signal_id,
            "action": self.action.value,
            "action_receipt_ids": list(self.action_receipt_ids),
            "route_decision_id": self.route_decision_id,
            "checkpoint_id": self.checkpoint_id,
            "expected_effects": list(self.expected_effects),
            "context_digest": self.context_digest,
            "idempotency_key": self.idempotency_key,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class ContinuationReceipt:
    owner: str
    receipt_id: str
    accepted: bool
    changed_execution: bool
    dispatch_kind: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    canonical_ref: Mapping[str, Any]
    error_code: str = ""
    message: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-continuation-receipt/v1",
            "owner": self.owner,
            "receipt_id": self.receipt_id,
            "accepted": self.accepted,
            "changed_execution": self.changed_execution,
            "dispatch_kind": self.dispatch_kind,
            "before": copy.deepcopy(dict(self.before)),
            "after": copy.deepcopy(dict(self.after)),
            "canonical_ref": copy.deepcopy(dict(self.canonical_ref)),
            "error_code": self.error_code,
            "message": self.message,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class ContinuationDispatchPort(Protocol):
    owner: str

    def dispatch(self, request: ContinuationRequest) -> ContinuationReceipt | Mapping[str, Any]: ...


class CallbackContinuationDispatch:
    def __init__(
        self,
        owner: str,
        callback: Callable[[ContinuationRequest], ContinuationReceipt | Mapping[str, Any]],
    ) -> None:
        self.owner = str(owner).strip()
        if not self.owner:
            raise ValueError("continuation dispatch owner is required")
        self._callback = callback

    def dispatch(self, request: ContinuationRequest) -> ContinuationReceipt:
        value = self._callback(request)
        if isinstance(value, ContinuationReceipt):
            if value.owner != self.owner:
                raise ContinuationDispatchRejected("continuation receipt owner mismatch")
            return value
        result = copy.deepcopy(dict(value))
        before = dict(result.get("before") or {})
        after = dict(result.get("after") or before)
        accepted = bool(result.get("accepted", not result.get("error_code")))
        changed = bool(result.get("changed_execution", result.get("changed", accepted and before != after)))
        receipt_id = str(result.get("receipt_id") or stable_digest({
            "owner": self.owner,
            "request": request.to_dict(),
            "after": after,
        }))
        return ContinuationReceipt(
            owner=self.owner,
            receipt_id=receipt_id,
            accepted=accepted,
            changed_execution=changed,
            dispatch_kind=str(result.get("dispatch_kind") or request.action.value),
            before=before,
            after=after,
            canonical_ref=dict(result.get("canonical_ref") or {}),
            error_code=str(result.get("error_code") or ""),
            message=str(result.get("message") or ""),
            metadata=dict(result.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    receipt_id: str
    action: RecoveryAction
    owner: str
    kind: VerificationKind
    verified: bool
    changed: bool
    projection_digest: str
    checks: tuple[str, ...]
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action": self.action.value,
            "owner": self.owner,
            "kind": self.kind.value,
            "verified": self.verified,
            "changed": self.changed,
            "projection_digest": self.projection_digest,
            "checks": list(self.checks),
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class AppliedOutcomeProof:
    proof_id: str
    plan_id: str
    outcome_id: str
    run_id: str
    task_id: str
    action: RecoveryAction
    verifications: tuple[ReceiptVerification, ...]
    continuation: ContinuationReceipt
    applied_route_layers: tuple[RouteLayer, ...]
    explicit_escalation: bool
    verified_at: str = field(default_factory=utc_now)

    @property
    def applied(self) -> bool:
        return (
            bool(self.verifications)
            and all(item.verified for item in self.verifications)
            and self.continuation.accepted
            and self.continuation.changed_execution
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.applied-recovery-outcome-proof/v1",
            "proof_id": self.proof_id,
            "plan_id": self.plan_id,
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "action": self.action.value,
            "verifications": [item.to_dict() for item in self.verifications],
            "continuation": self.continuation.to_dict(),
            "applied_route_layers": [item.value for item in self.applied_route_layers],
            "explicit_escalation": self.explicit_escalation,
            "applied": self.applied,
            "verified_at": self.verified_at,
        }


class AppliedOutcomeVerifier:
    ROUTE_ACTION_LAYER: Mapping[RecoveryAction, RouteLayer] = {
        RecoveryAction.REPLAN: RouteLayer.GRAPH,
        RecoveryAction.REROUTE: RouteLayer.WORKER,
        RecoveryAction.SWITCH_BACKEND: RouteLayer.BACKEND,
        RecoveryAction.SWITCH_PROVIDER: RouteLayer.PROVIDER,
        RecoveryAction.DEGRADE_MODEL: RouteLayer.MODEL,
    }
    KIND_BY_ACTION: Mapping[RecoveryAction, VerificationKind] = {
        RecoveryAction.REPLAN: VerificationKind.ROUTE_MUTATION,
        RecoveryAction.REROUTE: VerificationKind.ROUTE_MUTATION,
        RecoveryAction.SWITCH_BACKEND: VerificationKind.ROUTE_MUTATION,
        RecoveryAction.SWITCH_PROVIDER: VerificationKind.ROUTE_MUTATION,
        RecoveryAction.DEGRADE_MODEL: VerificationKind.ROUTE_MUTATION,
        RecoveryAction.RETRY: VerificationKind.RETRY_GENERATION,
        RecoveryAction.ASK_PERMISSION: VerificationKind.PERMISSION_BLOCK,
        RecoveryAction.AUTHENTICATE_MCP: VerificationKind.MCP_CONTROL,
        RecoveryAction.COMPACT: VerificationKind.CONTEXT_COMPACTION,
        RecoveryAction.RESUME_CHECKPOINT: VerificationKind.CHECKPOINT_RESUME,
        RecoveryAction.ABORT: VerificationKind.TASK_ABORT,
        RecoveryAction.NOOP: VerificationKind.TASK_MUTATION,
    }

    def __init__(
        self,
        routes: LayeredRouteRuntime,
        continuation: ContinuationDispatchPort,
        *,
        projection_ports: Mapping[str, ProjectionPort] | None = None,
        components: RecoveryComponentControl | None = None,
        proof_sink: Callable[[AppliedOutcomeProof], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.routes = routes
        self.continuation = continuation
        self.projection_ports = dict(projection_ports or {})
        self.components = components or RecoveryComponentControl()
        self.proof_sink = proof_sink

    def verify(
        self,
        plan: RecoveryPlan,
        outcome: RecoveryOutcome,
        receipts: Sequence[RecoveryActionReceipt],
        context: RecoveryContext,
    ) -> AppliedOutcomeProof:
        self.components.require(RecoveryComponent.CONTINUATION_DISPATCH, operation="verify and dispatch recovery continuation")
        if outcome.plan_id != plan.plan_id or outcome.signal_id != plan.signal.signal_id:
            raise AppliedReceiptRejected("recovery outcome identity does not match the plan")
        if outcome.kind is not RecoveryOutcomeKind.RECOVERED or not outcome.success:
            raise AppliedReceiptRejected("only a recovered applied outcome may produce routing memory")
        linked = [item for item in receipts if item.receipt_id in outcome.receipt_ids]
        if not linked:
            raise AppliedReceiptRejected("outcome does not link an action receipt")
        if any(item.plan_id != plan.plan_id for item in linked):
            raise AppliedReceiptRejected("action receipt crosses recovery plan custody")
        verifications = tuple(self._verify_receipt(plan, receipt) for receipt in linked)
        failed = [item for item in verifications if not item.verified]
        if failed:
            raise AppliedReceiptRejected("; ".join(failure for item in failed for failure in item.failures))
        layers = self._route_layers(linked)
        explicit = bool(plan.provenance.get("explicit_escalation") or plan.signal.details.get("explicit_escalation"))
        self._assert_layer_scope(plan, layers, explicit)
        request = ContinuationRequest(
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            plan_id=plan.plan_id,
            signal_id=plan.signal.signal_id,
            action=outcome.action,
            action_receipt_ids=tuple(item.receipt_id for item in linked),
            route_decision_id=outcome.route_decision_id,
            checkpoint_id=outcome.checkpoint_id or str(context.checkpoint_state.get("checkpoint_id") or ""),
            expected_effects=tuple(plan.decision.selected.expected_effects),
            context_digest=stable_digest(context.to_dict()),
            idempotency_key="continuation:" + stable_digest({
                "plan_id": plan.plan_id,
                "outcome_id": outcome.outcome_id,
                "receipts": [item.receipt_id for item in linked],
            })[:48],
            metadata={
                "policy_rule": plan.decision.selected.policy_rule,
                "route_layers": [item.value for item in layers],
                "explicit_escalation": explicit,
                "session_id": plan.signal.refs.session_id,
                "permission_tool_dispatch_allowed": outcome.action is not RecoveryAction.ASK_PERMISSION,
            },
        )
        continuation = self._dispatch(request)
        if not continuation.accepted:
            raise ContinuationDispatchRejected(continuation.error_code or continuation.message or "continuation rejected")
        if not continuation.changed_execution:
            raise ContinuationDispatchRejected("continuation owner acknowledged recovery without changing later execution")
        proof_id = "recoveryproof:" + stable_digest({
            "plan": plan.plan_id,
            "outcome": outcome.outcome_id,
            "verifications": [item.to_dict() for item in verifications],
            "continuation": continuation.to_dict(),
        })[:40]
        proof = AppliedOutcomeProof(
            proof_id=proof_id,
            plan_id=plan.plan_id,
            outcome_id=outcome.outcome_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            action=outcome.action,
            verifications=verifications,
            continuation=continuation,
            applied_route_layers=layers,
            explicit_escalation=explicit,
        )
        if not proof.applied:
            raise AppliedReceiptRejected("applied recovery proof did not close")
        if self.proof_sink is not None:
            sink_receipt = dict(self.proof_sink(proof) or {})
            if sink_receipt and not str(sink_receipt.get("event_id") or sink_receipt.get("receipt_id") or ""):
                raise AppliedReceiptRejected("proof sink returned an unidentifiable receipt")
        return proof

    def _verify_receipt(self, plan: RecoveryPlan, receipt: RecoveryActionReceipt) -> ReceiptVerification:
        checks: list[str] = []
        failures: list[str] = []
        if receipt.status not in {RecoveryAttemptStatus.APPLIED, RecoveryAttemptStatus.SUCCEEDED}:
            failures.append(f"receipt {receipt.receipt_id} is not applied")
        else:
            checks.append("applied_status")
        if not receipt.changed_execution:
            failures.append(f"receipt {receipt.receipt_id} did not change execution")
        else:
            checks.append("changed_execution")
        if not receipt.external_receipt_ref and receipt.route_decision is None and receipt.checkpoint_receipt is None:
            failures.append(f"receipt {receipt.receipt_id} lacks a canonical owner reference")
        else:
            checks.append("canonical_owner_reference")
        projection = self._projection(plan, receipt)
        if projection.run_id != plan.signal.refs.run_id or projection.task_id != plan.signal.refs.task_id:
            failures.append(f"receipt {receipt.receipt_id} projection crosses run/task custody")
        else:
            checks.append("projection_scope")
        if not self._projection_changed(receipt, projection):
            failures.append(f"receipt {receipt.receipt_id} after-state is absent from the canonical projection")
        else:
            checks.append("canonical_projection_changed")
        if receipt.route_decision is not None:
            route_failures = self._route_checks(plan, receipt.route_decision)
            failures.extend(route_failures)
            if not route_failures:
                checks.append("route_owner_current_matches_receipt")
        if receipt.checkpoint_receipt is not None:
            if not receipt.checkpoint_receipt.resume_token:
                failures.append("checkpoint resume receipt lacks a resume token")
            else:
                checks.append("checkpoint_resume_token")
            if receipt.checkpoint_receipt.metadata.get("committed_side_effect_replay", False):
                failures.append("checkpoint receipt permits committed side-effect replay")
            else:
                checks.append("no_committed_side_effect_replay")
        return ReceiptVerification(
            receipt_id=receipt.receipt_id,
            action=receipt.action,
            owner=receipt.owner,
            kind=self.KIND_BY_ACTION[receipt.action],
            verified=not failures,
            changed=receipt.changed_execution,
            projection_digest=projection.digest,
            checks=tuple(checks),
            failures=tuple(failures),
        )

    def _projection(self, plan: RecoveryPlan, receipt: RecoveryActionReceipt) -> ProjectionSnapshot:
        port = self.projection_ports.get(receipt.owner) or self.projection_ports.get(receipt.action.value)
        if port is not None:
            return port.snapshot(plan, receipt)
        if receipt.route_decision is not None:
            current: dict[str, Any] = {}
            revisions: list[str] = []
            for change in receipt.route_decision.changes:
                try:
                    value = dict(self.routes.owners.current(change.layer, plan.signal.refs.run_id, plan.signal.refs.task_id))
                except RouteOwnerUnavailable:
                    value = {}
                current[change.layer.value] = value
                revisions.append(str(value.get("revision") or value.get("lease_id") or value.get("route_id") or ""))
            return ProjectionSnapshot(
                owner=receipt.owner,
                operation=receipt.action.value,
                run_id=plan.signal.refs.run_id,
                task_id=plan.signal.refs.task_id,
                revision="|".join(revisions),
                projection=current,
            )
        return ProjectionSnapshot(
            owner=receipt.owner,
            operation=receipt.action.value,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            revision=str(receipt.after.get("revision") or receipt.external_receipt_ref),
            projection=copy.deepcopy(dict(receipt.after)),
        )

    @staticmethod
    def _projection_changed(receipt: RecoveryActionReceipt, projection: ProjectionSnapshot) -> bool:
        if not receipt.changed_execution:
            return False
        if receipt.route_decision is None:
            return bool(projection.projection) and (
                dict(receipt.before) != dict(receipt.after)
                or bool(receipt.external_receipt_ref)
            )
        for change in receipt.route_decision.changes:
            if not change.applied:
                continue
            current = projection.projection.get(change.layer.value)
            if not isinstance(current, Mapping):
                return False
            for key, value in change.after_ref.items():
                if key in current and current.get(key) != value:
                    return False
        return True

    def _route_checks(self, plan: RecoveryPlan, decision: LayeredRouteDecision) -> list[str]:
        failures: list[str] = []
        if decision.plan_id != plan.plan_id or decision.trigger_signal_id != plan.signal.signal_id:
            failures.append("route decision identity does not match the recovery plan")
            return failures
        for change in decision.changes:
            if not change.applied:
                failures.append(f"route layer {change.layer.value} was requested but not applied")
                continue
            if not change.receipt_id:
                failures.append(f"route layer {change.layer.value} lacks owner receipt")
                continue
            try:
                current = self.routes.owners.current(change.layer, plan.signal.refs.run_id, plan.signal.refs.task_id)
            except RouteOwnerUnavailable:
                failures.append(f"route owner unavailable after {change.layer.value} mutation")
                continue
            if not self._matches(change.after_ref, current):
                failures.append(f"canonical {change.layer.value} route differs from applied receipt")
        return failures

    def _dispatch(self, request: ContinuationRequest) -> ContinuationReceipt:
        result = self.continuation.dispatch(request)
        if isinstance(result, ContinuationReceipt):
            receipt = result
        else:
            receipt = CallbackContinuationDispatch(self.continuation.owner, lambda _: result).dispatch(request)
        if receipt.owner != self.continuation.owner:
            raise ContinuationDispatchRejected("continuation owner receipt mismatch")
        if not receipt.receipt_id:
            raise ContinuationDispatchRejected("continuation receipt id is required")
        if receipt.before == receipt.after and receipt.changed_execution:
            changed_ref = receipt.canonical_ref.get("dispatch_id") or receipt.canonical_ref.get("event_id")
            if not changed_ref:
                raise ContinuationDispatchRejected("continuation claims a change without state or dispatch identity")
        return receipt

    @classmethod
    def _route_layers(cls, receipts: Sequence[RecoveryActionReceipt]) -> tuple[RouteLayer, ...]:
        result: list[RouteLayer] = []
        for receipt in receipts:
            if receipt.route_decision is None:
                continue
            for change in receipt.route_decision.changes:
                if change.applied and change.layer not in result:
                    result.append(change.layer)
        return tuple(result)

    @classmethod
    def _assert_layer_scope(
        cls,
        plan: RecoveryPlan,
        layers: Sequence[RouteLayer],
        explicit_escalation: bool,
    ) -> None:
        expected = cls.ROUTE_ACTION_LAYER.get(plan.decision.selected.action)
        if expected is None:
            if layers:
                raise AppliedReceiptRejected("non-route action unexpectedly mutated a route layer")
            return
        if expected not in layers:
            raise AppliedReceiptRejected(f"selected {expected.value} route layer was not applied")
        unexpected = [item for item in layers if item is not expected]
        if unexpected and not explicit_escalation:
            raise AppliedReceiptRejected(
                "cross-layer route mutation requires explicit escalation: "
                + ", ".join(item.value for item in unexpected)
            )

    @staticmethod
    def _matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
        identity_keys = (
            "graph_id", "revision", "commit_id", "worker_id", "lease_id", "attempt_id",
            "backend_id", "backend_lease_id", "workspace_id", "provider_id", "route_id",
            "model_id", "credential_id", "transport_id", "checksum",
        )
        compared = False
        for key in identity_keys:
            if key not in expected:
                continue
            compared = True
            if key in actual and actual.get(key) != expected.get(key):
                return False
        return compared or stable_digest(dict(expected)) == stable_digest(dict(actual))


def verification_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.applied-outcome-verification-contract/v1",
        "feedback_gate": "verified applied owner receipt plus changed continuation",
        "event_only_success": False,
        "route_layer_isolation": True,
        "cross_layer_requires_explicit_escalation": True,
        "permission_ask_dispatches_tool": False,
        "canonical_projection_required": True,
    }


__all__ = [
    "AppliedOutcomeProof",
    "AppliedOutcomeVerifier",
    "AppliedReceiptRejected",
    "CallbackContinuationDispatch",
    "CallbackProjectionPort",
    "ContinuationDispatchPort",
    "ContinuationDispatchRejected",
    "ContinuationReceipt",
    "ContinuationRequest",
    "ProjectionPort",
    "ProjectionSnapshot",
    "ReceiptVerification",
    "RecoveryVerificationError",
    "VerificationKind",
    "verification_runtime_contract",
]
