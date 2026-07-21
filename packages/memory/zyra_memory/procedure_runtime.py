from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from zyra_core import EventRecord, EventType, now_iso

from .curator_integration_models import (
    CuratorConsumer,
    CuratorOutcomeKind,
    CuratorOutcomeState,
)
from .curator_integration_store import CuratorIntegrationStore
from .procedure_miner import (
    ProcedureMinerPolicy,
    ProcedureMiningResult,
    ReusableProcedureMiner,
)
from .procedure_models import (
    PROCEDURE_MINING_PROTOCOL,
    PROCEDURE_PROTOCOL,
    ProcedureApplicability,
    ProcedureConsumer,
    ProcedureContractError,
    ProcedureMiningDisposition,
    ProcedureValidationStatus,
    ReusableProcedure,
    stable_digest,
    stable_id,
    unique_strings,
)
from .procedure_store import (
    ProcedureClaim,
    ProcedureProjectionReceipt,
    ReusableProcedureStore,
)


@dataclass(frozen=True, slots=True)
class ProcedureQuery:
    task_id: str
    consumer: ProcedureConsumer
    goal: str = ""
    languages: tuple[str, ...] = ()
    workspace_kinds: tuple[str, ...] = ()
    available_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    provider_capabilities: tuple[str, ...] = ()
    minimum_confidence: float = 0.0
    validated_only: bool = True
    limit: int = 50
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> ProcedureQuery:
        if not self.task_id.strip():
            raise ProcedureContractError(
                "procedure_query_task_required",
                "procedure query task_id is required",
            )
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ProcedureContractError(
                "procedure_query_confidence",
                "procedure query minimum confidence must be between zero and one",
            )
        if self.limit < 1 or self.limit > 10_000:
            raise ProcedureContractError(
                "procedure_query_limit",
                "procedure query limit must be between 1 and 10000",
            )
        return self

    @classmethod
    def from_mapping(
        cls,
        task_id: str,
        value: Mapping[str, Any],
        *,
        default_consumer: ProcedureConsumer,
    ) -> ProcedureQuery:
        return cls(
            task_id=task_id,
            consumer=ProcedureConsumer(
                str(value.get("consumer") or default_consumer.value)
            ),
            goal=str(value.get("goal") or "").strip(),
            languages=unique_strings(value.get("languages") or ()),
            workspace_kinds=unique_strings(value.get("workspace_kinds") or ()),
            available_tools=unique_strings(value.get("available_tools") or ()),
            denied_tools=unique_strings(value.get("denied_tools") or ()),
            artifact_kinds=unique_strings(value.get("artifact_kinds") or ()),
            provider_capabilities=unique_strings(
                value.get("provider_capabilities") or ()
            ),
            minimum_confidence=float(value.get("minimum_confidence", 0.0)),
            validated_only=bool(value.get("validated_only", True)),
            limit=int(value.get("limit", 50)),
            metadata=dict(value.get("metadata") or {}),
        ).validated()


@dataclass(frozen=True, slots=True)
class ProcedureMatch:
    procedure: ReusableProcedure
    score: float
    matched_goal: bool
    matched_languages: tuple[str, ...]
    matched_workspace_kinds: tuple[str, ...]
    matched_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    forbidden_tools_present: tuple[str, ...]
    matched_artifact_kinds: tuple[str, ...]
    matched_provider_capabilities: tuple[str, ...]
    applicable: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        projection = {
            "procedure": self.procedure.to_dict(),
            "score": self.score,
            "matched_goal": self.matched_goal,
            "matched_languages": list(self.matched_languages),
            "matched_workspace_kinds": list(self.matched_workspace_kinds),
            "matched_tools": list(self.matched_tools),
            "missing_tools": list(self.missing_tools),
            "forbidden_tools_present": list(self.forbidden_tools_present),
            "matched_artifact_kinds": list(self.matched_artifact_kinds),
            "matched_provider_capabilities": list(
                self.matched_provider_capabilities
            ),
            "applicable": self.applicable,
            "reasons": list(self.reasons),
            "03c_resolution_required": True,
            "procedure_can_invoke_skill": False,
        }
        return projection


@dataclass(frozen=True, slots=True)
class ProcedureQueryResult:
    query_id: str
    task_id: str
    consumer: ProcedureConsumer
    matches: tuple[ProcedureMatch, ...]
    created_at: str
    query_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROCEDURE_PROTOCOL,
            "query_id": self.query_id,
            "task_id": self.task_id,
            "consumer": self.consumer.value,
            "matches": [item.to_dict() for item in self.matches],
            "created_at": self.created_at,
            "query_digest": self.query_digest,
            "canonical_curator_owner": "CuratorIntegrationStore",
            "procedure_projection_owner": "ReusableProcedureStore",
            "skill_invocation_owner": "03C SkillCoordinator",
        }


@dataclass(frozen=True, slots=True)
class ProcedureRuntimeStatus:
    task_id: str
    source_outcome_count: int
    mined_outcome_count: int
    pending_outcome_count: int
    rejected_receipt_count: int
    procedure_state_counts: Mapping[str, int]
    routing_visible_count: int
    recovery_visible_count: int
    context_visible_count: int
    store_status: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROCEDURE_MINING_PROTOCOL,
            "task_id": self.task_id,
            "source_outcome_count": self.source_outcome_count,
            "mined_outcome_count": self.mined_outcome_count,
            "pending_outcome_count": self.pending_outcome_count,
            "rejected_receipt_count": self.rejected_receipt_count,
            "procedure_state_counts": dict(self.procedure_state_counts),
            "routing_visible_count": self.routing_visible_count,
            "recovery_visible_count": self.recovery_visible_count,
            "context_visible_count": self.context_visible_count,
            "store_status": dict(self.store_status),
            "disable_env": "ZYRA_DISABLE_REUSABLE_PROCEDURE_MINER",
            "model_can_activate": False,
            "static_document_can_activate": False,
        }


class ReusableProcedureRuntime:
    """06B curator -> procedure store -> routing/recovery/context read path."""

    def __init__(
        self,
        *,
        canonical_store: Any,
        curator_store: CuratorIntegrationStore,
        procedure_store: ReusableProcedureStore,
        policy: ProcedureMinerPolicy | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
    ) -> None:
        self.canonical_store = canonical_store
        self.curator_store = curator_store
        self.procedure_store = procedure_store
        self.event_sink = event_sink
        self.miner = ReusableProcedureMiner(
            canonical_store=canonical_store,
            procedure_store=procedure_store,
            policy=policy,
            event_sink=self._procedure_event,
        )

    def mine_task(
        self,
        task_id: str,
        *,
        outcome_ids: Sequence[str] = (),
        limit: int = 1000,
    ) -> tuple[ProcedureMiningResult, ...]:
        selected_ids = set(unique_strings(outcome_ids))
        outcomes = self.curator_store.outcomes(
            task_id=task_id,
            kinds=(CuratorOutcomeKind.SKILL_CANDIDATE,),
            states=(CuratorOutcomeState.PUBLISHED,),
            canonical_only=True,
            limit=max(1, min(int(limit), 100_000)),
        )
        eligible = tuple(
            outcome
            for outcome in outcomes
            if (
                not selected_ids or outcome.outcome_id in selected_ids
            )
            and outcome.deterministic_validation
            and CuratorConsumer.SKILL_MEMORY in outcome.target_consumers
        )
        if selected_ids:
            found = {outcome.outcome_id for outcome in eligible}
            missing = selected_ids - found
            if missing:
                raise KeyError(
                    "requested curator outcomes are unavailable or ineligible: "
                    + ", ".join(sorted(missing))
                )
        return self.miner.mine_many(eligible)

    def query(self, query_value: ProcedureQuery) -> ProcedureQueryResult:
        query = query_value.validated()
        states = (
            (ProcedureValidationStatus.VALIDATED,)
            if query.validated_only
            else (
                ProcedureValidationStatus.CANDIDATE,
                ProcedureValidationStatus.VALIDATED,
            )
        )
        candidates = self.procedure_store.list(
            task_id=query.task_id,
            states=states,
            consumers=(query.consumer,),
            minimum_confidence=query.minimum_confidence,
            limit=max(query.limit * 8, query.limit),
        )
        matches = tuple(
            sorted(
                (self._match(query, procedure) for procedure in candidates),
                key=lambda item: (
                    not item.applicable,
                    -item.score,
                    -item.procedure.success_count,
                    item.procedure.procedure_id,
                ),
            )[: query.limit]
        )
        created_at = now_iso()
        query_projection = {
            "task_id": query.task_id,
            "consumer": query.consumer.value,
            "goal": query.goal,
            "languages": list(query.languages),
            "workspace_kinds": list(query.workspace_kinds),
            "available_tools": list(query.available_tools),
            "denied_tools": list(query.denied_tools),
            "artifact_kinds": list(query.artifact_kinds),
            "provider_capabilities": list(query.provider_capabilities),
            "minimum_confidence": query.minimum_confidence,
            "validated_only": query.validated_only,
            "limit": query.limit,
        }
        query_digest = stable_digest(query_projection)
        return ProcedureQueryResult(
            query_id=stable_id("procedure-query", query_projection),
            task_id=query.task_id,
            consumer=query.consumer,
            matches=matches,
            created_at=created_at,
            query_digest=query_digest,
        )

    def routing(self, task_id: str, value: Mapping[str, Any]) -> ProcedureQueryResult:
        return self.query(
            ProcedureQuery.from_mapping(
                task_id,
                value,
                default_consumer=ProcedureConsumer.ROUTING,
            )
        )

    def recovery(self, task_id: str, value: Mapping[str, Any]) -> ProcedureQueryResult:
        return self.query(
            ProcedureQuery.from_mapping(
                task_id,
                value,
                default_consumer=ProcedureConsumer.RECOVERY,
            )
        )

    def context(self, task_id: str, value: Mapping[str, Any]) -> ProcedureQueryResult:
        return self.query(
            ProcedureQuery.from_mapping(
                task_id,
                value,
                default_consumer=ProcedureConsumer.CONTEXT,
            )
        )

    def claim(
        self,
        procedure_id: str,
        *,
        consumer: ProcedureConsumer,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> ProcedureClaim:
        return self.procedure_store.claim(
            procedure_id,
            consumer=consumer,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def record_projection(
        self,
        claim: ProcedureClaim,
        *,
        state: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcedureProjectionReceipt:
        return self.procedure_store.project(
            claim,
            state=state,
            metadata=metadata,
        )

    def status(self, task_id: str) -> ProcedureRuntimeStatus:
        outcomes = self.curator_store.outcomes(
            task_id=task_id,
            kinds=(CuratorOutcomeKind.SKILL_CANDIDATE,),
            states=(CuratorOutcomeState.PUBLISHED,),
            canonical_only=True,
            limit=100_000,
        )
        procedures = self.procedure_store.list(
            task_id=task_id,
            states=tuple(ProcedureValidationStatus),
            limit=100_000,
        )
        receipts = self.procedure_store.receipts(task_id=task_id, limit=100_000)
        mined_ids = {item.provenance.curator_outcome_id for item in procedures}
        counts = {
            state.value: sum(1 for item in procedures if item.state is state)
            for state in ProcedureValidationStatus
        }
        return ProcedureRuntimeStatus(
            task_id=task_id,
            source_outcome_count=len(outcomes),
            mined_outcome_count=len(mined_ids),
            pending_outcome_count=sum(
                1 for outcome in outcomes if outcome.outcome_id not in mined_ids
            ),
            rejected_receipt_count=sum(
                1
                for receipt in receipts
                if receipt.disposition is ProcedureMiningDisposition.REJECTED
            ),
            procedure_state_counts=counts,
            routing_visible_count=sum(1 for item in procedures if item.routable),
            recovery_visible_count=sum(1 for item in procedures if item.recoverable),
            context_visible_count=sum(
                1
                for item in procedures
                if item.state is ProcedureValidationStatus.VALIDATED
                and ProcedureConsumer.CONTEXT in item.consumers
            ),
            store_status=self.procedure_store.status(task_id),
        )

    def export_for_typescript(
        self,
        task_id: str,
        *,
        consumers: Sequence[ProcedureConsumer] = (
            ProcedureConsumer.CONTEXT,
            ProcedureConsumer.ROUTING,
            ProcedureConsumer.RECOVERY,
        ),
        limit: int = 1000,
    ) -> tuple[Mapping[str, Any], ...]:
        procedures = self.procedure_store.list(
            task_id=task_id,
            states=(ProcedureValidationStatus.VALIDATED,),
            consumers=consumers,
            limit=limit,
        )
        return tuple(self._typescript_projection(item) for item in procedures)

    def _match(
        self,
        query: ProcedureQuery,
        procedure: ReusableProcedure,
    ) -> ProcedureMatch:
        applicability = procedure.applicability
        available = set(query.available_tools)
        denied = set(query.denied_tools)
        required = set(applicability.required_tools)
        forbidden = set(applicability.forbidden_tools)
        missing_tools = tuple(sorted(required - available)) if available else ()
        forbidden_present = tuple(sorted((forbidden & available) | (required & denied)))
        matched_tools = tuple(sorted(required & available))
        languages = tuple(sorted(set(query.languages) & set(applicability.languages)))
        workspaces = tuple(
            sorted(set(query.workspace_kinds) & set(applicability.workspace_kinds))
        )
        artifacts = tuple(
            sorted(
                set(query.artifact_kinds)
                & set(applicability.required_artifact_kinds)
            )
        )
        providers = tuple(
            sorted(
                set(query.provider_capabilities)
                & set(applicability.provider_capabilities)
            )
        )
        matched_goal = _goal_matches(query.goal, applicability)
        applicable = not missing_tools and not forbidden_present
        if query.languages and applicability.languages and not languages:
            applicable = False
        if (
            query.workspace_kinds
            and applicability.workspace_kinds
            and not workspaces
        ):
            applicable = False
        if (
            applicability.required_artifact_kinds
            and query.artifact_kinds
            and len(artifacts) < len(applicability.required_artifact_kinds)
        ):
            applicable = False
        if query.goal and applicability.goal_patterns and not matched_goal:
            applicable = False
        reasons: list[str] = []
        if matched_goal:
            reasons.append("goal_pattern_match")
        if matched_tools:
            reasons.append("required_tools_available")
        if languages:
            reasons.append("language_match")
        if workspaces:
            reasons.append("workspace_kind_match")
        if artifacts:
            reasons.append("artifact_kind_match")
        if providers:
            reasons.append("provider_capability_match")
        if missing_tools:
            reasons.append("required_tools_missing")
        if forbidden_present:
            reasons.append("forbidden_tool_present")
        if not reasons:
            reasons.append("provenance_only_match")
        score = procedure.confidence
        score += 0.08 if matched_goal else 0.0
        score += min(0.08, 0.02 * len(matched_tools))
        score += 0.03 if languages else 0.0
        score += 0.03 if workspaces else 0.0
        score += 0.02 if artifacts else 0.0
        score += 0.02 if providers else 0.0
        score -= min(0.4, 0.1 * len(missing_tools))
        score -= min(0.5, 0.2 * len(forbidden_present))
        if not applicable:
            score *= 0.4
        return ProcedureMatch(
            procedure=procedure,
            score=round(max(0.0, min(score, 1.0)), 6),
            matched_goal=matched_goal,
            matched_languages=languages,
            matched_workspace_kinds=workspaces,
            matched_tools=matched_tools,
            missing_tools=missing_tools,
            forbidden_tools_present=forbidden_present,
            matched_artifact_kinds=artifacts,
            matched_provider_capabilities=providers,
            applicable=applicable,
            reasons=tuple(reasons),
        )

    def _procedure_event(self, value: Mapping[str, Any]) -> None:
        if self.event_sink is None:
            return
        event_type = str(value.get("event_type") or "memory.procedure_mined")
        event_kind = (
            EventType.SYSTEM_NOTICE
            if not hasattr(EventType, "MEMORY_PROCEDURE_MINED")
            else getattr(EventType, "MEMORY_PROCEDURE_MINED")
        )
        self.event_sink(
            EventRecord(
                event_id=stable_id(
                    "event",
                    event_type,
                    value.get("task_id"),
                    value.get("aggregate_id"),
                    value.get("payload_digest"),
                ),
                run_id=str(value.get("run_id") or ""),
                task_id=str(value.get("task_id") or ""),
                event_type=event_kind,
                payload={
                    "schema": "zyra.memory-procedure-event.v1",
                    "event_type": event_type,
                    "aggregate_id": value.get("aggregate_id"),
                    "causation_id": value.get("causation_id"),
                    "correlation_id": value.get("correlation_id"),
                    "procedure": dict(value.get("payload") or {}),
                    "payload_digest": value.get("payload_digest"),
                    "canonical_owner": "ReusableProcedureStore",
                },
            )
        )

    @staticmethod
    def _typescript_projection(
        procedure: ReusableProcedure,
    ) -> Mapping[str, Any]:
        value = procedure.to_dict(include_evidence=False)
        steps = [
            {
                "stepId": step["step_id"],
                "ordinal": step["ordinal"],
                "action": step["action"],
                "toolName": step["tool_name"],
                "inputShapeDigest": step["input_shape_digest"],
                "expectedEffect": step["expected_effect"],
                "successEvidenceIds": step["success_evidence_ids"],
                "artifactKinds": step["artifact_kinds"],
                "retryable": step["retryable"],
                "failureRoutes": step["failure_routes"],
                "metadata": step["metadata"],
            }
            for step in value["steps"]
        ]
        applicability = value["applicability"]
        provenance = value["provenance"]
        projection: dict[str, Any] = {
            "procedureId": value["procedure_id"],
            "protocol": PROCEDURE_PROTOCOL,
            "name": value["name"],
            "summary": value["summary"],
            "state": value["state"],
            "revision": value["revision"],
            "steps": steps,
            "applicability": {
                "languages": applicability["languages"],
                "workspaceKinds": applicability["workspace_kinds"],
                "goalPatterns": applicability["goal_patterns"],
                "requiredTools": applicability["required_tools"],
                "forbiddenTools": applicability["forbidden_tools"],
                "requiredArtifactKinds": applicability[
                    "required_artifact_kinds"
                ],
                "providerCapabilities": applicability[
                    "provider_capabilities"
                ],
                "minimumTrust": applicability["minimum_trust"],
                "constraints": applicability["constraints"],
            },
            "provenance": {
                "curatorOutcomeId": provenance["curator_outcome_id"],
                "curatorJobId": provenance["curator_job_id"],
                "curatorDecisionId": provenance["curator_decision_id"],
                "evidenceBundleId": provenance["evidence_bundle_id"],
                "evidenceDigest": provenance["evidence_digest"],
                "memoryId": provenance["memory_id"],
                "memoryRevision": provenance["memory_revision"],
                "runId": provenance["run_id"],
                "taskId": provenance["task_id"],
                "sessionIds": provenance["session_ids"],
                "toolCallIds": provenance["tool_call_ids"],
                "artifactIds": provenance["artifact_ids"],
                "skillVersions": [
                    {
                        "skillId": item["skill_id"],
                        "skillName": item["skill_name"],
                        "registryRevision": item["registry_revision"],
                        "descriptorDigest": item["descriptor_digest"],
                        "bodyDigest": item["body_digest"],
                        "resourceDigests": item["resource_digests"],
                        "sourceRevision": item["source_revision"],
                    }
                    for item in provenance["skill_versions"]
                ],
                "memoryEventIds": provenance["memory_event_ids"],
            },
            "consumers": value["consumers"],
            "confidence": value["confidence"],
            "successCount": value["success_count"],
            "failureCount": value["failure_count"],
            "validatedAt": value["validated_at"],
            "createdAt": value["created_at"],
            "updatedAt": value["updated_at"],
            "supersedesProcedureId": value["supersedes_procedure_id"],
            "procedureDigest": "",
            "metadata": value["metadata"],
        }
        projection["procedureDigest"] = typescript_projection_digest(
            {
                key: item
                for key, item in projection.items()
                if key != "procedureDigest"
            }
        )
        return projection


def typescript_projection_digest(value: Mapping[str, Any]) -> str:
    """Hash JSON using the integer-number shape produced by JSON.stringify.

    Python preserves ``1.0`` while JavaScript serializes the same JSON number as
    ``1``.  Procedure confidence commonly reaches exactly one, so normalizing
    integral finite floats is part of the cross-runtime admission contract.
    """

    payload = json.dumps(
        _typescript_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _typescript_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _typescript_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_typescript_json_value(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _goal_matches(goal: str, applicability: ProcedureApplicability) -> bool:
    if not goal or not applicability.goal_patterns:
        return not applicability.goal_patterns
    import re

    for pattern in applicability.goal_patterns:
        try:
            if re.search(pattern, goal):
                return True
        except re.error:
            continue
    return False


__all__ = [
    "ProcedureMatch",
    "ProcedureQuery",
    "ProcedureQueryResult",
    "ProcedureRuntimeStatus",
    "ReusableProcedureRuntime",
    "typescript_projection_digest",
]
