from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ...graph_custody import BranchGraphDelta, GraphStateSnapshot
from ..contracts import (
    FrozenDict,
    PolicyInputSnapshot,
    canonical_digest,
    thaw_json,
)
from .catalog import ARGCapabilityBinding, ARGRoleCatalog, ARGRoleProfile


ARG_INPUT_SCHEMA_VERSION = "zyra.arg-input/v1"
_WORD = re.compile(r"[A-Za-z0-9_./:+-]+|[\u3400-\u9fff]")


class ARGInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _terms(*values: Any) -> tuple[str, ...]:
    output: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set, frozenset)):
            output.update(_terms(*value))
            continue
        output.update(item.lower() for item in _WORD.findall(str(value or "")))
    return tuple(sorted(output))


@dataclass(frozen=True, slots=True)
class ARGModelObservation:
    model_id: str
    model_version: str
    input_digest: str
    output_digest: str
    observation_ref: str
    semantic_terms: tuple[str, ...]
    confidence: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "model_id",
            "model_version",
            "input_digest",
            "output_digest",
            "observation_ref",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ARGInputError(
                    "model_observation_invalid",
                    f"model observation {name} is required",
                )
        for name in ("input_digest", "output_digest"):
            digest = str(getattr(self, name))
            if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
                raise ARGInputError(
                    "model_observation_digest_invalid",
                    f"model observation {name} must be SHA-256",
                )
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ARGInputError(
                "model_observation_confidence_invalid",
                "model observation confidence must be between zero and one",
            )
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "semantic_terms", _terms(self.semantic_terms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "observation_ref": self.observation_ref,
            "semantic_terms": list(self.semantic_terms),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ARGEncodedRole:
    profile: ARGRoleProfile
    binding: ARGCapabilityBinding
    cold_start: bool
    phase_affinity: tuple[str, ...]
    obligation_matches: tuple[str, ...]

    @property
    def role_id(self) -> str:
        return self.profile.role_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.profile.role_id,
            "display_name": self.profile.display_name,
            "capabilities": list(self.profile.capabilities),
            "binding": self.binding.to_dict(),
            "cold_start": self.cold_start,
            "phase_affinity": list(self.phase_affinity),
            "obligation_matches": list(self.obligation_matches),
            "semantic_terms": list(self.profile.semantic_terms),
        }


@dataclass(frozen=True, slots=True)
class ARGEncodedInput:
    policy_input: PolicyInputSnapshot
    task_summary: str
    current_graph: GraphStateSnapshot
    branch_delta: BranchGraphDelta | None
    role_catalog_version: str
    role_catalog_digest: str
    eligible_roles: tuple[ARGEncodedRole, ...]
    phase_terms: tuple[str, ...]
    obligation_terms: FrozenDict
    continuity_memory_refs: tuple[str, ...]
    readiness_stage: str
    readiness_status: str
    readiness_report_digest: str
    mechanism_version: str
    configuration_digest: str
    recent_recovery_outcome: FrozenDict = field(default_factory=FrozenDict)
    model_observation: ARGModelObservation | None = None
    schema_version: str = ARG_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ARG_INPUT_SCHEMA_VERSION:
            raise ARGInputError(
                "input_schema_unsupported",
                f"unsupported ARG input schema: {self.schema_version}",
            )
        if not self.eligible_roles:
            raise ARGInputError(
                "input_no_eligible_roles",
                "no capability-, permission-, placement-, and health-eligible role exists",
            )
        object.__setattr__(
            self,
            "eligible_roles",
            tuple(sorted(self.eligible_roles, key=lambda item: item.role_id)),
        )
        object.__setattr__(self, "phase_terms", _terms(self.phase_terms))
        object.__setattr__(self, "obligation_terms", FrozenDict(self.obligation_terms))
        object.__setattr__(
            self,
            "continuity_memory_refs",
            tuple(sorted(set(self.continuity_memory_refs))),
        )
        object.__setattr__(
            self,
            "recent_recovery_outcome",
            FrozenDict(self.recent_recovery_outcome),
        )

    @property
    def branch_delta_digest(self) -> str:
        return self.branch_delta.content_digest if self.branch_delta is not None else ""

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_input_digest": self.policy_input.digest,
            "run_id": self.policy_input.run_id,
            "task_id": self.policy_input.task_id,
            "task_summary": self.task_summary,
            "phase": self.policy_input.phase,
            "requirement_revision": self.policy_input.requirement_revision,
            "unresolved_obligations": list(self.policy_input.unresolved_obligations),
            "phase_terms": list(self.phase_terms),
            "obligation_terms": thaw_json(self.obligation_terms),
            "current_graph": {
                "graph_id": self.current_graph.graph_id,
                "revision": self.current_graph.revision,
                "signature": self.current_graph.signature,
                "commit_id": self.current_graph.commit_id,
                "nodes": [item.to_dict() for item in self.current_graph.nodes],
                "edges": [item.to_dict() for item in self.current_graph.edges],
            },
            "branch_delta": (
                self.branch_delta.to_dict() if self.branch_delta is not None else None
            ),
            "role_catalog_version": self.role_catalog_version,
            "role_catalog_digest": self.role_catalog_digest,
            "eligible_roles": [item.to_dict() for item in self.eligible_roles],
            "allowed_permissions": list(self.policy_input.allowed_permissions),
            "allowed_placements": list(self.policy_input.allowed_placements),
            "environment_digest": self.policy_input.environment.digest,
            "continuity_memory_refs": list(self.continuity_memory_refs),
            "readiness": {
                "stage": self.readiness_stage,
                "status": self.readiness_status,
                "report_digest": self.readiness_report_digest,
                "mechanism_version": self.mechanism_version,
                "configuration_digest": self.configuration_digest,
            },
            "recent_recovery_outcome": thaw_json(self.recent_recovery_outcome),
            "model_observation": (
                self.model_observation.to_dict()
                if self.model_observation is not None
                else None
            ),
        }


class ARGInputEncoder:
    """Validates owner snapshots and freezes one deterministic ARG input."""

    def encode(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        task_summary: str,
        current_graph: GraphStateSnapshot,
        role_catalog: ARGRoleCatalog,
        readiness_stage: str,
        readiness_status: str,
        readiness_report_digest: str,
        mechanism_version: str,
        configuration_digest: str,
        phase_affinity: Mapping[str, Any],
        branch_delta: BranchGraphDelta | None = None,
        recent_recovery_outcome: Mapping[str, Any] | None = None,
        model_observation: ARGModelObservation | None = None,
    ) -> ARGEncodedInput:
        self._validate_graph_binding(policy_input, current_graph)
        self._validate_branch(current_graph, branch_delta)
        self._validate_catalog_binding(policy_input, role_catalog)
        self._validate_readiness_binding(
            policy_input,
            readiness_stage=readiness_stage,
            readiness_status=readiness_status,
            readiness_report_digest=readiness_report_digest,
        )
        created_at = _time(policy_input.header.created_at)
        existing_roles = {item.role for item in current_graph.nodes}
        obligations = {
            item: _terms(item)
            for item in policy_input.unresolved_obligations
        }
        phase_terms = _terms(
            policy_input.phase,
            phase_affinity.get(policy_input.phase.lower(), ()),
        )
        encoded_roles: list[ARGEncodedRole] = []
        for profile in role_catalog.eligible_profiles(
            allowed_permissions=policy_input.allowed_permissions,
            allowed_placements=policy_input.allowed_placements,
        ):
            candidates = []
            for binding in profile.eligible_bindings(
                allowed_permissions=policy_input.allowed_permissions,
                allowed_placements=policy_input.allowed_placements,
            ):
                observation = policy_input.environment.observation_by_resource.get(
                    binding.worker_id
                )
                if observation is None:
                    continue
                if (
                    created_at > _time(observation.fresh_until)
                    or observation.confidence <= 0
                    or not observation.available
                    or not observation.healthy
                    or observation.missing_fields
                    or not observation.lease_available
                ):
                    continue
                candidates.append(binding)
            if not candidates:
                continue
            binding = sorted(
                candidates,
                key=lambda item: (
                    -item.capacity_available,
                    item.worker_id,
                    item.binding_id,
                ),
            )[0]
            profile_terms = set(_terms(profile.semantic_terms, profile.capabilities))
            matches = tuple(
                sorted(
                    obligation
                    for obligation, terms in obligations.items()
                    if profile_terms.intersection(terms)
                )
            )
            encoded_roles.append(
                ARGEncodedRole(
                    profile=profile,
                    binding=binding,
                    cold_start=profile.role_id not in existing_roles,
                    phase_affinity=tuple(sorted(profile_terms.intersection(phase_terms))),
                    obligation_matches=matches,
                )
            )
        if not encoded_roles:
            raise ARGInputError(
                "input_no_fresh_eligible_roles",
                "all eligible role bindings failed capability, permission, placement, health, lease, or freshness checks",
            )

        pre_observation = self.model_observation_input_digest(
            policy_input=policy_input,
            task_summary=task_summary,
            current_graph=current_graph,
            role_catalog=role_catalog,
            branch_delta=branch_delta,
        )
        if model_observation is not None:
            if model_observation.input_digest != pre_observation:
                raise ARGInputError(
                    "model_observation_input_mismatch",
                    "optional model observation is not bound to the frozen ARG input",
                )
            if model_observation.confidence <= 0:
                raise ARGInputError(
                    "model_observation_untrusted",
                    "optional model observation confidence is not positive",
                )

        return ARGEncodedInput(
            policy_input=policy_input,
            task_summary=str(task_summary),
            current_graph=current_graph,
            branch_delta=branch_delta,
            role_catalog_version=role_catalog.catalog_version,
            role_catalog_digest=role_catalog.digest,
            eligible_roles=tuple(encoded_roles),
            phase_terms=phase_terms,
            obligation_terms=FrozenDict(
                {key: list(value) for key, value in obligations.items()}
            ),
            continuity_memory_refs=tuple(
                f"{item.ref_id}:{item.digest}" for item in policy_input.memory_refs
            ),
            readiness_stage=readiness_stage,
            readiness_status=readiness_status,
            readiness_report_digest=readiness_report_digest,
            mechanism_version=mechanism_version,
            configuration_digest=configuration_digest,
            recent_recovery_outcome=FrozenDict(recent_recovery_outcome or {}),
            model_observation=model_observation,
        )

    @staticmethod
    def model_observation_input_digest(
        *,
        policy_input: PolicyInputSnapshot,
        task_summary: str,
        current_graph: GraphStateSnapshot,
        role_catalog: ARGRoleCatalog,
        branch_delta: BranchGraphDelta | None = None,
    ) -> str:
        # This intentionally excludes model output. It is the immutable request
        # digest a fixed gateway must cite before its observation can be used.
        return canonical_digest(
            {
                "policy_input_digest": policy_input.digest,
                "current_graph_signature": current_graph.signature,
                "branch_delta_digest": (
                    branch_delta.content_digest if branch_delta is not None else ""
                ),
                "catalog_digest": role_catalog.digest,
                "roles": [
                    item.to_dict()
                    for item in role_catalog.profiles
                ],
                "task_summary": str(task_summary),
            }
        )

    @staticmethod
    def _validate_graph_binding(
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
    ) -> None:
        graph = policy_input.graph
        if (
            graph.graph_id != current_graph.graph_id
            or graph.run_id != current_graph.run_id
            or graph.revision != current_graph.revision
            or graph.signature != current_graph.signature
            or graph.commit_id != current_graph.commit_id
        ):
            raise ARGInputError(
                "input_graph_drift",
                "PolicyInputSnapshot does not match the immutable canonical graph snapshot",
            )
        policy_nodes = {item.node_id: item for item in policy_input.nodes}
        if set(policy_nodes) != set(current_graph.node_map):
            raise ARGInputError(
                "input_graph_node_drift",
                "PolicyInputSnapshot node set differs from canonical graph",
            )
        for node_id, node in current_graph.node_map.items():
            selected = policy_nodes[node_id]
            if (
                selected.role != node.role
                or selected.capabilities != node.capabilities
                or selected.dependencies != node.dependencies
                or selected.revision != node.revision
                or selected.state != node.state.value
            ):
                raise ARGInputError(
                    "input_graph_node_drift",
                    f"PolicyInputSnapshot node differs from canonical graph: {node_id}",
                )

    @staticmethod
    def _validate_branch(
        current_graph: GraphStateSnapshot,
        branch_delta: BranchGraphDelta | None,
    ) -> None:
        if branch_delta is None:
            return
        if (
            branch_delta.graph_id != current_graph.graph_id
            or branch_delta.run_id != current_graph.run_id
            or branch_delta.base_revision != current_graph.revision
            or branch_delta.base_signature != current_graph.signature
        ):
            raise ARGInputError(
                "input_branch_drift",
                "branch-local delta is not based on the frozen canonical graph",
            )

    @staticmethod
    def _validate_catalog_binding(
        policy_input: PolicyInputSnapshot,
        role_catalog: ARGRoleCatalog,
    ) -> None:
        expected_version = str(
            policy_input.registry_versions.get("arg_role_catalog") or ""
        )
        expected_digest = str(
            policy_input.registry_versions.get("arg_role_catalog_digest") or ""
        )
        if (
            expected_version != role_catalog.catalog_version
            or expected_digest != role_catalog.digest
        ):
            raise ARGInputError(
                "input_catalog_drift",
                "PolicyInputSnapshot is not bound to the supplied role catalog version and digest",
            )
        registered_roles = set(policy_input.registered_roles)
        registered_capabilities = set(policy_input.registered_capabilities)
        for profile in role_catalog.profiles:
            if profile.role_id not in registered_roles:
                raise ARGInputError(
                    "input_catalog_role_unregistered",
                    f"catalog role is absent from the snapshot registry: {profile.role_id}",
                )
            if not set(profile.capabilities).issubset(registered_capabilities):
                raise ARGInputError(
                    "input_catalog_capability_unregistered",
                    f"catalog role has an unregistered capability: {profile.role_id}",
                )

    @staticmethod
    def _validate_readiness_binding(
        policy_input: PolicyInputSnapshot,
        *,
        readiness_stage: str,
        readiness_status: str,
        readiness_report_digest: str,
    ) -> None:
        refs = {
            item.header.mechanism_id: item
            for item in policy_input.readiness_refs
        }
        selected = refs.get("arg_designer")
        if selected is None:
            raise ARGInputError(
                "input_readiness_missing",
                "PolicyInputSnapshot has no ARG readiness reference",
            )
        if (
            selected.readiness_stage != readiness_stage
            or selected.status != readiness_status
            or selected.report_digest != readiness_report_digest
        ):
            raise ARGInputError(
                "input_readiness_drift",
                "PolicyInputSnapshot ARG readiness reference differs from the loaded report",
            )
