from __future__ import annotations

from dataclasses import replace

from zyra_orchestration.graph_custody import (
    BranchGraphDelta,
    GraphDeltaBuilder,
    GraphEdge,
    GraphNode,
)

from .contracts import (
    PolicyInputSnapshot,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
    thaw_json,
)


class PolicyDeltaBuilder:
    """The only policy adapter permitted to invoke the canonical GraphDeltaBuilder."""

    def build(
        self,
        *,
        current_snapshot,
        policy_input: PolicyInputSnapshot,
        proposal: TopologyProposalArtifact,
        decision_id: str,
    ) -> BranchGraphDelta:
        builder = GraphDeltaBuilder(
            current_snapshot,
            branch_id=f"policy:{proposal.header.mechanism_id}",
            actor_id=f"policy-projector:{proposal.header.mechanism_id}",
            causation_id=proposal.header.causation_id,
            correlation_id=proposal.header.correlation_id,
            idempotency_key=proposal.header.idempotency_key,
            metadata={
                "policy_schema": proposal.SCHEMA_VERSION,
                "policy_proposal_id": proposal.proposal_id,
                "policy_proposal_digest": proposal.digest,
                "policy_input_snapshot_id": policy_input.header.contract_id,
                "policy_input_snapshot_digest": policy_input.digest,
                "policy_decision_id": f"decision:{proposal.header.idempotency_key}",
                "policy_path": "TopologyProposalArtifact->constraint_projector->GraphDeltaBuilder",
            },
        )
        for operation in proposal.operations:
            self._append(
                builder,
                operation,
                contract_created_at=proposal.header.created_at,
            )
        built = builder.build()
        mutations = tuple(
            replace(
                mutation,
                mutation_id=(
                    "mutation_policy_"
                    + canonical_digest(
                        (proposal.digest, index, proposal.operations[index].to_dict())
                    )[:24]
                ),
            )
            for index, mutation in enumerate(built.mutations)
        )
        return replace(
            built,
            mutations=mutations,
            delta_id=(
                "delta_policy_"
                + canonical_digest(
                    (
                        built.graph_id,
                        built.branch_id,
                        built.idempotency_key,
                        proposal.digest,
                    )
                )[:24]
            ),
            created_at=proposal.header.created_at,
            content_digest="",
        )

    @staticmethod
    def _append(
        builder: GraphDeltaBuilder,
        operation: TopologyOperation,
        *,
        contract_created_at: str,
    ) -> None:
        value = thaw_json(operation.value)
        expected = operation.expected_entity_revision
        kind = operation.kind
        if kind == TopologyOperationKind.ADD_NODE:
            builder.add_node(
                GraphNode.from_dict(
                    {
                        "created_at": contract_created_at,
                        "updated_at": contract_created_at,
                        "node_id": operation.entity_id,
                        **value,
                    }
                )
            )
        elif kind == TopologyOperationKind.REMOVE_NODE:
            builder.remove_node(operation.entity_id, expected_revision=expected)
        elif kind == TopologyOperationKind.REPLACE_NODE:
            builder.replace_node(
                GraphNode.from_dict(
                    {
                        "created_at": contract_created_at,
                        "updated_at": contract_created_at,
                        "node_id": operation.entity_id,
                        **value,
                    }
                ),
                expected_revision=expected,
            )
        elif kind == TopologyOperationKind.SET_NODE_ROLE:
            builder.set_role(operation.entity_id, str(value.get("role") or ""), expected_revision=expected)
        elif kind == TopologyOperationKind.SET_NODE_CAPABILITIES:
            builder.set_capabilities(
                operation.entity_id,
                value.get("capabilities") or (),
                expected_revision=expected,
            )
        elif kind == TopologyOperationKind.SET_NODE_DEPENDENCIES:
            builder.set_dependencies(
                operation.entity_id,
                value.get("dependencies") or (),
                expected_revision=expected,
            )
        elif kind == TopologyOperationKind.ADD_EDGE:
            builder.add_edge(
                GraphEdge.from_dict(
                    {
                        "created_at": contract_created_at,
                        "updated_at": contract_created_at,
                        "edge_id": operation.entity_id,
                        **value,
                    }
                )
            )
        elif kind == TopologyOperationKind.REMOVE_EDGE:
            builder.remove_edge(operation.entity_id, expected_revision=expected)
        elif kind == TopologyOperationKind.REPLACE_EDGE:
            builder.replace_edge(
                GraphEdge.from_dict(
                    {
                        "created_at": contract_created_at,
                        "updated_at": contract_created_at,
                        "edge_id": operation.entity_id,
                        **value,
                    }
                ),
                expected_revision=expected,
            )
        elif kind == TopologyOperationKind.SET_GRAPH_METADATA:
            builder.set_metadata(operation.entity_id, value.get("value"))
        elif kind == TopologyOperationKind.REMOVE_GRAPH_METADATA:
            builder.remove_metadata(operation.entity_id)
        else:  # pragma: no cover - exhaustive enum guard
            raise ValueError(f"unsupported topology operation: {kind}")
