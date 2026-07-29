from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from zyra_core import now_iso

from ..graph_custody import (
    BranchGraphDelta,
    GraphCommitResult,
    GraphCommitStatus,
    GraphConflictStrategy,
    GraphMutationRejected,
    GraphStateCustody,
    GraphStoreConflict,
)
from .contracts import (
    ConstraintResult,
    ContractHeader,
    FrozenDict,
    PolicyDecisionDisposition,
    PolicyDecisionReceipt,
    PolicyInputSnapshot,
    TelemetryObservation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from .delta_builder import PolicyDeltaBuilder, _PROJECTOR_AUTHORIZATION


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PolicyProjectionResult:
    receipt: PolicyDecisionReceipt
    delta: BranchGraphDelta | None
    commit: GraphCommitResult | None


class TopologyConstraintProjector:
    """Fail-closed symbolic gate in front of the canonical graph custody owner."""

    def __init__(
        self,
        custody: GraphStateCustody,
        *,
        delta_builder: PolicyDeltaBuilder | None = None,
        enabled: bool = True,
        test_mode: bool = False,
    ) -> None:
        if not enabled and not test_mode:
            raise ValueError("projector disable switch is test-only")
        self.custody = custody
        self.delta_builder = delta_builder or PolicyDeltaBuilder()
        self.enabled = enabled
        self.test_mode = test_mode

    def execute(
        self,
        policy_input: PolicyInputSnapshot,
        proposal: TopologyProposalArtifact,
        *,
        decision_id: str,
        evaluated_at: str | None = None,
        strategy: GraphConflictStrategy = GraphConflictStrategy.SERIALIZE,
    ) -> PolicyProjectionResult:
        evaluated_at = evaluated_at or now_iso()
        if not self.enabled:
            disabled = ConstraintResult(
                constraint_id="policy_projector",
                passed=False,
                reason_code="policy_projector_disabled",
                message="policy projector is disabled; canonical mutation is fail-closed",
            )
            return PolicyProjectionResult(
                receipt=self._receipt(
                    policy_input,
                    proposal,
                    decision_id,
                    evaluated_at,
                    PolicyDecisionDisposition.REJECT,
                    (disabled,),
                    fallback_reason="policy_projector_disabled",
                ),
                delta=None,
                commit=None,
            )
        current = self.custody.current(policy_input.graph.graph_id)
        branch_id = f"policy:{proposal.header.mechanism_id}"
        existing = self.custody.store.delta_for_idempotency(
            current.graph_id,
            branch_id,
            proposal.header.idempotency_key,
        )
        if existing is not None:
            same = (
                existing.metadata.get("policy_proposal_digest") == proposal.digest
                and existing.metadata.get("policy_input_snapshot_digest") == policy_input.digest
            )
            duplicate_check = ConstraintResult(
                constraint_id="idempotency",
                passed=same,
                reason_code="idempotent_replay" if same else "idempotency_mismatch",
                message=(
                    "existing canonical delta is replayed without mutation"
                    if same
                    else "idempotency key already maps to different policy content"
                ),
                evidence_refs=(existing.delta_id,),
            )
            if not same:
                return PolicyProjectionResult(
                    receipt=self._receipt(
                        policy_input,
                        proposal,
                        decision_id,
                        evaluated_at,
                        PolicyDecisionDisposition.REJECT,
                        (duplicate_check,),
                        fallback_reason="idempotency_mismatch",
                    ),
                    delta=None,
                    commit=None,
                )
            prior = self.custody.store.commit_for_delta(existing.graph_id, existing.delta_id)
            resumed: GraphCommitResult | None = None
            if prior is None:
                try:
                    resumed = self.custody.commit(existing)
                    prior = resumed.receipt
                except GraphStoreConflict as error:
                    return PolicyProjectionResult(
                        receipt=self._receipt(
                            policy_input,
                            proposal,
                            decision_id,
                            evaluated_at,
                            PolicyDecisionDisposition.CONFLICT,
                            (
                                duplicate_check,
                                ConstraintResult(
                                    constraint_id="canonical_graph_commit",
                                    passed=False,
                                    reason_code="idempotency_commit_race",
                                    message=str(error),
                                ),
                            ),
                            delta=existing,
                            fallback_reason="idempotency_commit_race",
                        ),
                        delta=existing,
                        commit=None,
                    )
            return PolicyProjectionResult(
                receipt=self._receipt(
                    policy_input,
                    proposal,
                    decision_id,
                    evaluated_at,
                    PolicyDecisionDisposition.REPLAY,
                    (duplicate_check,),
                    delta=existing,
                    graph_commit=prior.to_dict() if prior else {},
                ),
                delta=existing,
                commit=resumed,
            )

        checks = list(self._constraint_checks(policy_input, proposal, current, evaluated_at))
        if any(not item.passed for item in checks):
            return PolicyProjectionResult(
                receipt=self._receipt(
                    policy_input,
                    proposal,
                    decision_id,
                    evaluated_at,
                    PolicyDecisionDisposition.REJECT,
                    checks,
                    fallback_reason="; ".join(
                        item.reason_code for item in checks if not item.passed
                    ),
                ),
                delta=None,
                commit=None,
            )

        try:
            delta = self.delta_builder.build(
                current_snapshot=current,
                policy_input=policy_input,
                proposal=proposal,
                decision_id=decision_id,
                _authorization=_PROJECTOR_AUTHORIZATION,
            )
            preview = self.custody.apply(current, delta)
        except (GraphMutationRejected, TypeError, ValueError) as error:
            details: dict[str, Any] = {"error_type": type(error).__name__}
            if isinstance(error, GraphMutationRejected):
                details["conflicts"] = [item.to_dict() for item in error.conflicts]
            checks.append(
                ConstraintResult(
                    constraint_id="canonical_graph_validation",
                    passed=False,
                    reason_code="graph_constraint_rejected",
                    message=str(error),
                    details=FrozenDict(details),
                )
            )
            return PolicyProjectionResult(
                receipt=self._receipt(
                    policy_input,
                    proposal,
                    decision_id,
                    evaluated_at,
                    PolicyDecisionDisposition.REJECT,
                    checks,
                    fallback_reason="graph_constraint_rejected",
                ),
                delta=None,
                commit=None,
            )

        fanout = Counter(edge.source_node_id for edge in preview.edges)
        fanout_exceeded = max(fanout.values(), default=0) > policy_input.budget.max_fan_out
        checks.append(
            ConstraintResult(
                constraint_id="fanout",
                passed=not fanout_exceeded,
                reason_code="fanout_within_budget" if not fanout_exceeded else "fanout_exceeded",
                message=(
                    "projected graph fan-out is within the policy budget"
                    if not fanout_exceeded
                    else "projected graph fan-out exceeds the policy budget"
                ),
                details=FrozenDict(
                    {
                        "max_projected_fanout": max(fanout.values(), default=0),
                        "max_allowed_fanout": policy_input.budget.max_fan_out,
                    }
                ),
            )
        )
        if fanout_exceeded:
            return PolicyProjectionResult(
                receipt=self._receipt(
                    policy_input,
                    proposal,
                    decision_id,
                    evaluated_at,
                    PolicyDecisionDisposition.REJECT,
                    checks,
                    fallback_reason="fanout_exceeded",
                ),
                delta=None,
                commit=None,
            )

        projected = self._receipt(
            policy_input,
            proposal,
            decision_id,
            evaluated_at,
            PolicyDecisionDisposition.PROJECT,
            checks,
            delta=delta,
        )
        try:
            commit = self.custody.commit(delta, strategy=strategy)
        except GraphStoreConflict as error:
            return PolicyProjectionResult(
                receipt=replace(
                    projected,
                    disposition=PolicyDecisionDisposition.CONFLICT,
                    fallback_profile=proposal.fallback_profile,
                    fallback_reason=f"idempotency_or_commit_race:{error}",
                ),
                delta=delta,
                commit=None,
            )
        dispositions = {
            GraphCommitStatus.COMMITTED: PolicyDecisionDisposition.ACCEPT,
            GraphCommitStatus.REBASED: PolicyDecisionDisposition.REBASE,
            GraphCommitStatus.REPLAYED: PolicyDecisionDisposition.REPLAY,
            GraphCommitStatus.CONFLICTED: PolicyDecisionDisposition.CONFLICT,
            GraphCommitStatus.REPLAN_REQUIRED: PolicyDecisionDisposition.CONFLICT,
        }
        final_disposition = dispositions[commit.receipt.status]
        return PolicyProjectionResult(
            receipt=replace(
                projected,
                disposition=final_disposition,
                graph_commit=FrozenDict(commit.receipt.to_dict()),
                fallback_profile=(
                    proposal.fallback_profile
                    if final_disposition is PolicyDecisionDisposition.CONFLICT
                    else ""
                ),
                fallback_reason=(
                    commit.receipt.status.value
                    if final_disposition is PolicyDecisionDisposition.CONFLICT
                    else ""
                ),
            ),
            delta=delta,
            commit=commit,
        )

    def _constraint_checks(
        self,
        policy_input: PolicyInputSnapshot,
        proposal: TopologyProposalArtifact,
        current,
        evaluated_at: str,
    ) -> Iterable[ConstraintResult]:
        def result(
            identifier: str,
            passed: bool,
            success: str,
            failure: str,
            message: str,
            **details: Any,
        ) -> ConstraintResult:
            return ConstraintResult(
                constraint_id=identifier,
                passed=passed,
                reason_code=success if passed else failure,
                message=message,
                details=FrozenDict(details),
            )

        identity_ok = (
            proposal.input_snapshot_digest == policy_input.digest
            and proposal.base_graph == policy_input.graph
            and proposal.base_graph.revision == current.revision
            and proposal.base_graph.signature == current.signature
            and self._nodes_match_canonical_graph(policy_input, current)
        )
        yield result(
            "snapshot_identity",
            identity_ok,
            "snapshot_current",
            "stale_or_mismatched_snapshot",
            "proposal and policy input are bound to the canonical graph head",
            policy_revision=policy_input.graph.revision,
            current_revision=current.revision,
        )
        not_expired = _time(evaluated_at) <= _time(proposal.expires_at)
        yield result(
            "proposal_expiry",
            not_expired,
            "proposal_current",
            "proposal_expired",
            "proposal expiry evaluated against the recorded decision time",
            expires_at=proposal.expires_at,
            evaluated_at=evaluated_at,
        )
        readiness = {
            item.header.mechanism_id: (item.readiness_stage, item.status)
            for item in policy_input.readiness_refs
        }
        readiness_value = readiness.get(proposal.header.mechanism_id)
        readiness_ok = readiness_value == ("activation_ready", "deterministic_ready")
        yield result(
            "mechanism_readiness",
            readiness_ok,
            "mechanism_deterministic_ready",
            "mechanism_not_activation_ready",
            "only deterministic-ready mechanisms may enter the canonical path",
            mechanism_id=proposal.header.mechanism_id,
            readiness=readiness_value or ("missing", "missing"),
        )

        roles, capabilities = self._requested_registry_values(proposal)
        unknown_roles = sorted(roles - set(policy_input.registered_roles))
        unknown_capabilities = sorted(
            capabilities - set(policy_input.registered_capabilities)
        )
        yield result(
            "registry",
            not unknown_roles and not unknown_capabilities,
            "registry_values_known",
            "unknown_role_or_capability",
            "roles and capabilities must exist in the immutable registry snapshot",
            unknown_roles=unknown_roles,
            unknown_capabilities=unknown_capabilities,
        )

        requested_permissions = {
            item for operation in proposal.operations for item in operation.required_permissions
        }
        operations_without_permission = sorted(
            operation.entity_id
            for operation in proposal.operations
            if not operation.required_permissions
        )
        disallowed_permissions = sorted(
            requested_permissions - set(policy_input.allowed_permissions)
        )
        requested_placements = {
            operation.requested_placement
            for operation in proposal.operations
            if operation.requested_placement
        }
        disallowed_placements = sorted(
            requested_placements - set(policy_input.allowed_placements)
        )
        placements_without_resource = sorted(
            operation.entity_id
            for operation in proposal.operations
            if operation.requested_placement and not operation.resource_id
        )
        yield result(
            "permission_privacy_placement",
            not operations_without_permission
            and not disallowed_permissions
            and not disallowed_placements
            and not placements_without_resource,
            "permission_and_placement_allowed",
            "permission_or_placement_denied",
            "proposal permission and placement requests are checked before projection",
            operations_without_permission=operations_without_permission,
            denied_permissions=disallowed_permissions,
            denied_placements=disallowed_placements,
            placements_without_resource=placements_without_resource,
            privacy_class=policy_input.privacy_class,
        )

        telemetry_failures = []
        observation_by_resource = policy_input.environment.observation_by_resource
        for operation in proposal.operations:
            if not operation.resource_id:
                continue
            observation = observation_by_resource.get(operation.resource_id)
            reason = self._telemetry_failure(
                observation,
                operation,
                policy_input.privacy_class,
                evaluated_at,
            )
            if reason:
                telemetry_failures.append(
                    {"resource_id": operation.resource_id, "reason": reason}
                )
        if policy_input.environment.missing_categories:
            telemetry_failures.append(
                {
                    "resource_id": "",
                    "reason": "missing_categories:"
                    + ",".join(policy_input.environment.missing_categories),
                }
            )
        yield result(
            "capacity_lease_telemetry",
            not telemetry_failures,
            "capacity_and_lease_available",
            "capacity_lease_or_telemetry_invalid",
            "resource telemetry must be fresh, physical, healthy, and sufficient",
            failures=telemetry_failures,
        )

        communication_bytes = sum(item.communication_bytes for item in proposal.operations)
        expected = proposal.expected_outcome
        tokens = int(expected.get("tokens") or 0)
        cost = float(expected.get("cost_usd") or 0)
        time_ms = int(expected.get("time_ms") or 0)
        budget_ok = (
            communication_bytes <= policy_input.budget.max_communication_bytes
            and tokens <= policy_input.budget.remaining_tokens
            and cost <= policy_input.budget.remaining_cost_usd
            and time_ms <= policy_input.budget.remaining_time_ms
        )
        yield result(
            "budget",
            budget_ok,
            "budget_within_limits",
            "budget_exceeded",
            "proposal resource and communication estimates are within remaining budgets",
            communication_bytes=communication_bytes,
            tokens=tokens,
            cost_usd=cost,
            time_ms=time_ms,
        )

        pending_side_effects = tuple(
            str(item)
            for item in (expected.get("pending_side_effects") or ())
            if str(item)
        )
        yield result(
            "pending_side_effect",
            not pending_side_effects,
            "no_pending_side_effect",
            "pending_side_effect_unsettled",
            "topology mutation cannot commit while proposal side effects are pending",
            pending_side_effects=pending_side_effects,
        )

        churn = len(proposal.operations)
        dwell_seconds = (
            _time(evaluated_at) - _time(policy_input.last_topology_change_at)
        ).total_seconds()
        churn_ok = (
            churn <= policy_input.budget.max_topology_churn
            and dwell_seconds >= policy_input.budget.minimum_dwell_seconds
        )
        yield result(
            "churn_and_dwell",
            churn_ok,
            "churn_and_dwell_allowed",
            "churn_or_dwell_limit",
            "topology churn and minimum dwell policy are enforced",
            churn=churn,
            dwell_seconds=dwell_seconds,
        )

    @staticmethod
    def _requested_registry_values(
        proposal: TopologyProposalArtifact,
    ) -> tuple[set[str], set[str]]:
        roles: set[str] = set()
        capabilities: set[str] = set()
        for operation in proposal.operations:
            value = operation.value
            if operation.kind in {
                TopologyOperationKind.ADD_NODE,
                TopologyOperationKind.REPLACE_NODE,
                TopologyOperationKind.SET_NODE_ROLE,
            }:
                role = str(value.get("role") or "")
                if role:
                    roles.add(role)
            if operation.kind in {
                TopologyOperationKind.ADD_NODE,
                TopologyOperationKind.REPLACE_NODE,
                TopologyOperationKind.SET_NODE_CAPABILITIES,
            }:
                capabilities.update(str(item) for item in value.get("capabilities") or ())
            if operation.kind in {
                TopologyOperationKind.ADD_EDGE,
                TopologyOperationKind.REPLACE_EDGE,
            }:
                capabilities.update(
                    str(item) for item in value.get("required_capabilities") or ()
                )
        return roles, capabilities

    @staticmethod
    def _nodes_match_canonical_graph(policy_input: PolicyInputSnapshot, current) -> bool:
        policy_nodes = {item.node_id: item for item in policy_input.nodes}
        if set(policy_nodes) != set(current.node_map):
            return False
        for node_id, current_node in current.node_map.items():
            policy_node = policy_nodes[node_id]
            if (
                policy_node.role != current_node.role
                or policy_node.capabilities != current_node.capabilities
                or policy_node.dependencies != current_node.dependencies
                or policy_node.state != current_node.state.value
                or policy_node.revision != current_node.revision
                or dict(policy_node.labels) != dict(current_node.labels)
                or dict(policy_node.metadata) != dict(current_node.metadata)
            ):
                return False
        return True

    @staticmethod
    def _telemetry_failure(
        observation: TelemetryObservation | None,
        operation,
        privacy_class: str,
        evaluated_at: str,
    ) -> str:
        if observation is None:
            return "missing_observation"
        if _time(evaluated_at) > _time(observation.fresh_until):
            return "stale_observation"
        if observation.missing_fields:
            return "observation_missing_fields"
        if observation.confidence <= 0:
            return "untrusted_observation"
        if not observation.available or not observation.healthy:
            return "resource_unavailable"
        if not observation.lease_available:
            return "lease_unavailable"
        if operation.required_capacity > observation.capacity_available:
            return "capacity_insufficient"
        if privacy_class not in observation.privacy_classes:
            return "privacy_class_denied"
        if (
            operation.requested_placement
            and operation.requested_placement not in observation.allowed_placements
        ):
            return "resource_placement_denied"
        return ""

    @staticmethod
    def _receipt(
        policy_input: PolicyInputSnapshot,
        proposal: TopologyProposalArtifact,
        decision_id: str,
        evaluated_at: str,
        disposition: PolicyDecisionDisposition,
        checks: Iterable[ConstraintResult],
        *,
        delta: BranchGraphDelta | None = None,
        graph_commit: dict[str, Any] | None = None,
        fallback_reason: str = "",
    ) -> PolicyDecisionReceipt:
        receipt_header = ContractHeader(
            contract_id=decision_id,
            created_at=evaluated_at,
            source_event_id=proposal.header.source_event_id,
            correlation_id=proposal.header.correlation_id,
            causation_id=proposal.header.contract_id,
            mechanism_id=proposal.header.mechanism_id,
            mechanism_version=proposal.header.mechanism_version,
            input_version=PolicyInputSnapshot.SCHEMA_VERSION,
            idempotency_key=f"decision:{proposal.header.idempotency_key}",
            configuration_digest=proposal.header.configuration_digest,
        )
        return PolicyDecisionReceipt(
            header=receipt_header,
            decision_id=decision_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            disposition=disposition,
            constraint_results=tuple(checks),
            projected_operations=proposal.operations if delta is not None else (),
            projection_differences=(),
            delta_id=delta.delta_id if delta else "",
            delta_digest=delta.content_digest if delta else "",
            graph_commit=FrozenDict(graph_commit),
            fallback_profile=proposal.fallback_profile if fallback_reason else "",
            fallback_reason=fallback_reason,
        )
