from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .contracts import (
    AgentPruneContractError,
    CommunicationEdgeCandidate,
    CommunicationOutcomeObservation,
    EdgeContributionStats,
)


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentPruneContractError(
            f"agentprune_{name}_invalid",
            f"{name} must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise AgentPruneContractError(
            f"agentprune_{name}_timezone_missing",
            f"{name} must include a timezone",
        )
    return parsed.astimezone(UTC)


class CommunicationOutcomeAggregator:
    """Aggregates completed, actual communication receipts by typed edge."""

    def aggregate(
        self,
        *,
        candidates: Iterable[CommunicationEdgeCandidate],
        observations: Iterable[CommunicationOutcomeObservation],
        run_id: str,
        task_id: str,
        completed_before: str,
        maximum_age_seconds: int = 0,
    ) -> tuple[EdgeContributionStats, ...]:
        candidate_values = tuple(
            sorted(candidates, key=lambda item: item.edge_id)
        )
        candidate_map = {item.edge_id: item for item in candidate_values}
        if len(candidate_map) != len(candidate_values):
            raise AgentPruneContractError(
                "agentprune_duplicate_candidate_edge",
                "candidate edge identifiers must be unique",
            )
        if not candidate_values:
            raise AgentPruneContractError(
                "agentprune_candidates_missing",
                "outcome aggregation requires communication candidates",
            )
        cutoff = _timestamp(completed_before, "completed_before")
        grouped: dict[str, list[CommunicationOutcomeObservation]] = {
            edge_id: [] for edge_id in candidate_map
        }
        observation_ids: set[str] = set()
        for item in observations:
            if item.observation_id in observation_ids:
                raise AgentPruneContractError(
                    "agentprune_duplicate_outcome_observation",
                    "outcome observation identifiers must be unique",
                )
            observation_ids.add(item.observation_id)
            if item.run_id != run_id or item.task_id != task_id:
                raise AgentPruneContractError(
                    "agentprune_outcome_scope_drift",
                    "communication outcome belongs to another run or task",
                )
            completed = _timestamp(item.completed_at, "outcome_completed_at")
            if completed >= cutoff:
                raise AgentPruneContractError(
                    "agentprune_outcome_not_prior",
                    "pruning can consume only an earlier completed outcome window",
                )
            if (
                maximum_age_seconds > 0
                and (cutoff - completed).total_seconds()
                > maximum_age_seconds
            ):
                raise AgentPruneContractError(
                    "agentprune_outcome_window_stale",
                    "communication outcome exceeds the configured freshness window",
                )
            candidate = candidate_map.get(item.edge_id)
            if candidate is None:
                raise AgentPruneContractError(
                    "agentprune_outcome_edge_unknown",
                    "communication outcome references an unknown candidate edge",
                )
            if (
                item.source_node_id != candidate.source_node_id
                or item.target_node_id != candidate.target_node_id
                or item.edge_type is not candidate.edge_type
            ):
                raise AgentPruneContractError(
                    "agentprune_outcome_edge_binding_drift",
                    "communication outcome endpoint/type differs from its candidate",
                )
            grouped[item.edge_id].append(item)

        missing = tuple(
            edge_id for edge_id, values in grouped.items() if not values
        )
        if missing:
            raise AgentPruneContractError(
                "agentprune_outcome_coverage_missing",
                "every candidate requires a prior actual outcome: "
                + ", ".join(missing),
            )
        globally_redundant = self._globally_redundant_message_ids(
            tuple(item for values in grouped.values() for item in values)
        )
        return tuple(
            self._edge_stats(
                candidate_map[edge_id],
                grouped[edge_id],
                globally_redundant=globally_redundant,
            )
            for edge_id in sorted(grouped)
        )

    @staticmethod
    def _edge_stats(
        edge: CommunicationEdgeCandidate,
        values: list[CommunicationOutcomeObservation],
        *,
        globally_redundant: set[str],
    ) -> EdgeContributionStats:
        ordered = sorted(
            values,
            key=lambda item: (
                item.completed_at,
                item.round_index,
                item.message_id,
                item.observation_id,
            ),
        )
        delivered = [item for item in ordered if item.delivered]
        payload_first: dict[str, str] = {}
        duplicates = 0
        for item in delivered:
            prior = payload_first.get(item.payload_digest)
            if (
                prior is not None
                or item.redundant_with_message_id
                or item.message_id in globally_redundant
            ):
                duplicates += 1
            else:
                payload_first[item.payload_digest] = item.message_id
        receipts: set[str] = set()
        for item in ordered:
            receipts.add(item.delivery_receipt_ref)
            if item.usage_receipt_ref:
                receipts.add(item.usage_receipt_ref)
            receipts.update(item.causal_refs)
        return EdgeContributionStats(
            edge=edge,
            window_ids=tuple(item.window_id for item in ordered),
            observation_ids=tuple(item.observation_id for item in ordered),
            observed_messages=len(ordered),
            delivered_messages=len(delivered),
            delivered_bytes=sum(item.message_bytes for item in delivered),
            delivered_tokens=sum(item.total_tokens for item in delivered),
            delivered_cost_usd=sum(item.cost_usd for item in delivered),
            evidence_refs=sum(len(item.evidence_refs) for item in delivered),
            utilized_evidence_refs=sum(
                len(item.utilized_evidence_refs) for item in delivered
            ),
            artifact_contributions=sum(bool(item.artifact_refs) for item in delivered),
            verifier_passes=sum(
                item.verifier_result == "passed" for item in delivered
            ),
            verifier_failures=sum(
                item.verifier_result == "failed" for item in delivered
            ),
            failed_deliveries=sum(not item.delivered for item in ordered)
            + sum(item.failure_count for item in ordered),
            retries=sum(item.retry_count for item in ordered),
            duplicate_messages=duplicates,
            malicious_messages=sum(
                item.malicious or item.permission_result == "denied"
                for item in ordered
            ),
            first_round=min(item.round_index for item in ordered),
            last_round=max(item.round_index for item in ordered),
            source_receipt_refs=tuple(receipts),
        )

    @staticmethod
    def _globally_redundant_message_ids(
        values: tuple[CommunicationOutcomeObservation, ...],
    ) -> set[str]:
        first_by_payload: dict[str, str] = {}
        redundant: set[str] = set()
        for item in sorted(
            (value for value in values if value.delivered),
            key=lambda value: (
                value.completed_at,
                value.round_index,
                value.message_id,
            ),
        ):
            if item.payload_digest in first_by_payload:
                redundant.add(item.message_id)
            else:
                first_by_payload[item.payload_digest] = item.message_id
        return redundant


__all__ = ["CommunicationOutcomeAggregator"]
