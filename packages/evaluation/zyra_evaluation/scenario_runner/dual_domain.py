from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonicalize, digest, file_digest, new_identity, utc_now
from .domain_verification import ResearchDeliveryVerifier, SoftwareDeliveryVerifier
from .errors import conflict, invalid, unavailable
from .fault_campaign import (
    FaultCampaignRuntime,
    FaultOwnerPort,
    FaultSchedule,
    campaign_events,
    campaign_summary,
)
from .live_archive import CausalArchiveBuilder, raw_metric_samples, runtime_environment
from .live_models import (
    ActionKind,
    DomainVerification,
    FaultObservation,
    LiveDomain,
    LiveDomainResult,
    ProviderObservation,
    TierObservation,
)
from .models import OwnerExecutionResult, ScenarioConfiguration
from .placement_evidence import (
    PlacementEvidenceRuntime,
    PlacementOwnerPort,
    PlacementRun,
    placement_events,
)
from .research_delivery import (
    ResearchDeliveryRuntime,
    ResearchExecutionPayload,
    research_input_from_configuration,
)
from .software_delivery import (
    SoftwareDeliveryRuntime,
    SoftwareExecutionPayload,
    software_input_from_configuration,
)


LIVE_SOFTWARE_SCENARIO_ID = "live.software-delivery"
LIVE_RESEARCH_SCENARIO_ID = "live.cross-source-research"
LIVE_SCENARIO_IDS = frozenset(
    {
        LIVE_SOFTWARE_SCENARIO_ID,
        LIVE_RESEARCH_SCENARIO_ID,
    }
)


class TaskOwnerPort(Protocol):
    def begin(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        goal: str,
        policy_decisions: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

    def commit_events(
        self,
        *,
        owner_context: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]: ...

    def curate_memory(
        self,
        *,
        owner_context: Mapping[str, Any],
        result: LiveDomainResult,
        previous_event_id: str,
    ) -> Mapping[str, Any]: ...

    def finalize(
        self,
        *,
        owner_context: Mapping[str, Any],
        success: bool,
        summary: str,
        artifacts: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


class ArtifactOwnerPort(Protocol):
    def publish(
        self,
        *,
        owner_context: Mapping[str, Any],
        source_paths: Sequence[str],
        domain: LiveDomain,
        metadata: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]: ...


class PreExecutionPolicyPort(Protocol):
    def execute(
        self,
        *,
        owner_context: Mapping[str, Any],
        configuration: ScenarioConfiguration,
        goal: str,
    ) -> Mapping[str, Any]: ...


class AnalysisUnitOwnerPort(Protocol):
    def commit_analysis_units(
        self,
        *,
        owner_context: Mapping[str, Any],
        values: Sequence[Mapping[str, Any]],
        stage: str,
    ) -> Sequence[Mapping[str, Any]]: ...


class CallbackTaskOwnerPort:
    def __init__(
        self,
        *,
        begin: Callable[..., Mapping[str, Any]],
        commit_events: Callable[..., Sequence[Mapping[str, Any]]],
        curate_memory: Callable[..., Mapping[str, Any]],
        finalize: Callable[..., Mapping[str, Any]],
    ) -> None:
        self._begin = begin
        self._commit_events = commit_events
        self._curate_memory = curate_memory
        self._finalize = finalize

    def begin(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._begin(**kwargs)

    def commit_events(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
        return self._commit_events(**kwargs)

    def curate_memory(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._curate_memory(**kwargs)

    def finalize(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._finalize(**kwargs)


class CallbackArtifactOwnerPort:
    def __init__(self, publish: Callable[..., Sequence[Mapping[str, Any]]]) -> None:
        self._publish = publish

    def publish(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
        return self._publish(**kwargs)


class UnboundTaskOwnerPort:
    def _fail(self) -> Mapping[str, Any]:
        raise unavailable(
            "live_task_owner_unbound",
            "Live scenario is not bound to the canonical task/event owner.",
            phase="dual-domain",
        )

    def begin(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def commit_events(self, **_: Any) -> Sequence[Mapping[str, Any]]:
        self._fail()
        return ()

    def curate_memory(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def finalize(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()


class UnboundArtifactOwnerPort:
    def publish(self, **_: Any) -> Sequence[Mapping[str, Any]]:
        raise unavailable(
            "live_artifact_owner_unbound",
            "Live scenario is not bound to the canonical artifact owner.",
            phase="dual-domain",
        )


@dataclass(frozen=True, slots=True)
class DualDomainExecutionOptions:
    require_real_tiers: bool
    require_real_providers: bool
    minimum_effective_transitions: int = 2_000
    maximum_effective_transitions: int = 10_000
    requirement_change: str = ""

    @classmethod
    def from_configuration(
        cls,
        configuration: ScenarioConfiguration,
    ) -> "DualDomainExecutionOptions":
        profile = configuration.profile.metadata
        required_tiers = profile.get("require_real_tiers")
        if required_tiers is None:
            required_tiers = profile.get("edge_cloud_claim")
        required_providers = profile.get("require_real_providers")
        if required_providers is None:
            required_providers = profile.get("provider_model_claim")
        return cls(
            require_real_tiers=required_tiers is True,
            require_real_providers=required_providers is True,
            minimum_effective_transitions=max(
                2_000,
                int(profile.get("minimum_effective_transitions") or 2_000),
            ),
            maximum_effective_transitions=min(
                configuration.profile.maximum_effective_steps,
                int(profile.get("maximum_effective_transitions") or 10_000),
            ),
            requirement_change=str(
                configuration.metadata.get("requirement_change")
                or "Retain deterministic recovery and re-verification evidence."
            ),
        )


@dataclass(frozen=True, slots=True)
class DualDomainOwnerBindings:
    task: TaskOwnerPort
    artifact: ArtifactOwnerPort
    placement: PlacementOwnerPort
    fault: FaultOwnerPort
    source_commit: str
    pre_execution_policy: PreExecutionPolicyPort | None = None
    analysis: AnalysisUnitOwnerPort | None = None


class CanonicalEventBuilder:
    def __init__(self, *, run_id: str, task_id: str) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self._sequence = 0
        self._previous = ""
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def previous_event_id(self) -> str:
        return self._previous

    def append_existing(self, values: Sequence[Mapping[str, Any]]) -> None:
        for value in values:
            event = dict(value)
            event_id = str(event.get("event_id") or "")
            if not event_id:
                raise conflict(
                    "live_event_identity_missing",
                    "Owner event lacks canonical identity.",
                    phase="dual-domain-events",
                )
            if str(event.get("run_id") or "") != self.run_id:
                raise conflict(
                    "live_event_run_mismatch",
                    "Owner event is outside the live run partition.",
                    phase="dual-domain-events",
                    detail={"event_id": event_id},
                )
            if str(event.get("task_id") or "") != self.task_id:
                raise conflict(
                    "live_event_task_mismatch",
                    "Owner event is outside the live task partition.",
                    phase="dual-domain-events",
                    detail={"event_id": event_id},
                )
            event["sequence"] = self._sequence + 1
            if self._previous:
                event["causation_id"] = self._previous
                event["parent_event_id"] = self._previous
                payload = event.get("payload")
                payload = dict(payload) if isinstance(payload, Mapping) else {}
                payload["causation_id"] = self._previous
                event["payload"] = payload
            self._sequence += 1
            self._events.append(event)
            self._previous = event_id

    def emit(
        self,
        *,
        event_type: str,
        effect: str,
        stage: str,
        mutation: Mapping[str, Any],
        worker_id: str = "",
        provider_id: str = "",
        node_id: str = "root",
        metadata: Mapping[str, Any] | None = None,
        event_id: str = "",
    ) -> dict[str, Any]:
        self._sequence += 1
        # Event identity must be unique per run, not just per (content, index).
        # Several live events carry a fixed mutation payload (for example the
        # topology_mutation that always advertises the same node/edge set), so
        # deriving the id from content and sequence alone lets two runs emit
        # the same id into the shared runtime event spine and fail the append
        # with EVENT_ID_CONFLICT.  Bind the run identity into the digest.
        selected_event_id = event_id or (
            f"live-event-{self._sequence:06d}-"
            f"{digest((self.run_id, self.task_id, event_type, stage, mutation, self._sequence))[:16]}"
        )
        event = {
            "event_id": selected_event_id,
            "event_type": event_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": node_id,
            "sequence": self._sequence,
            "causation_id": self._previous,
            "parent_event_id": self._previous,
            "created_at": utc_now(),
            "payload": {
                "semantic_effect": effect,
                "mutation": canonicalize(mutation),
                "causation_id": self._previous,
            },
            "metadata": {
                "stage": stage,
                "semantic_effect": effect,
                "worker_id": worker_id,
                "provider_id": provider_id,
                **dict(metadata or {}),
            },
        }
        self._events.append(event)
        self._previous = selected_event_id
        return event

    def work_units(
        self,
        values: Sequence[Mapping[str, Any]],
        *,
        worker_id: str,
        provider_id: str,
        stage: str,
    ) -> None:
        seen_units: set[str] = set()
        seen_ranges: set[tuple[str, int, int]] = set()
        seen_intervals: dict[str, list[tuple[int, int]]] = {}
        for index, value in enumerate(values, start=1):
            unit_id = str(value.get("work_unit_id") or f"unit-{index:06d}")
            if value.get("schema") != "zyra.analysis-unit-owner-receipt/v1":
                raise conflict(
                    "live_work_unit_owner_receipt_missing",
                    "Long-run work units require an existing owner receipt.",
                    phase="dual-domain-events",
                    detail={"work_unit_id": unit_id, "analysis_index": index},
                )
            unsigned = dict(value)
            receipt_digest = str(unsigned.pop("receipt_digest", ""))
            if (
                receipt_digest != digest(unsigned)
                or value.get("owner") != "SQLiteStore.MemoryRecord"
                or value.get("committed") is not True
                or value.get("readback_verified") is not True
            ):
                raise conflict(
                    "live_work_unit_owner_receipt_invalid",
                    "Long-run work-unit owner receipt failed integrity or readback.",
                    phase="dual-domain-events",
                    detail={"work_unit_id": unit_id, "analysis_index": index},
                )
            input_digest = str(value.get("input_digest") or "")
            output_digest = str(value.get("output_digest") or "")
            byte_start = int(value.get("byte_start") or 0)
            byte_end = int(value.get("byte_end") or 0)
            source_locator = str(value.get("source_locator") or "")
            source_range = (source_locator, byte_start, byte_end)
            overlaps_existing = any(
                byte_start < existing_end and byte_end > existing_start
                for existing_start, existing_end in seen_intervals.get(
                    source_locator,
                    (),
                )
            )
            if (
                not unit_id
                or unit_id in seen_units
                or not source_locator
                or source_range in seen_ranges
                or overlaps_existing
                or len(input_digest) != 64
                or len(output_digest) != 64
                or byte_end <= byte_start
            ):
                raise conflict(
                    "live_work_unit_not_distinct",
                    "Long-run work units must bind distinct, non-empty source byte ranges.",
                    phase="dual-domain-events",
                    detail={"work_unit_id": unit_id, "analysis_index": index},
                )
            seen_units.add(unit_id)
            seen_ranges.add(source_range)
            seen_intervals.setdefault(source_locator, []).append(
                (byte_start, byte_end)
            )
            indexed = self.emit(
                event_type="analysis_unit_indexed",
                effect="memory",
                stage=stage,
                worker_id=worker_id,
                provider_id=provider_id,
                mutation={
                    "work_unit_id": unit_id,
                    "operation": str(value.get("kind") or "analyze"),
                    "input_digest": input_digest,
                    "byte_start": byte_start,
                    "byte_end": byte_end,
                    "source_locator": source_locator,
                    "analysis_index": int(value.get("analysis_index") or index),
                    "semantic_mutation": canonicalize(
                        value.get("semantic_mutation") or {}
                    ),
                    "owner_receipt": canonicalize(value),
                    "state": "owner-indexed",
                },
                metadata={
                    "work_unit_id": unit_id,
                    "owner_binding_required": True,
                    "distinct_source_range": True,
                },
            )
            self.emit(
                event_type="analysis_unit_verified",
                effect="verification",
                stage=stage,
                worker_id=worker_id,
                provider_id=provider_id,
                mutation={
                    "work_unit_id": unit_id,
                    "operation": str(value.get("kind") or "analyze"),
                    "input_digest": input_digest,
                    "output_digest": output_digest,
                    "byte_start": byte_start,
                    "byte_end": byte_end,
                    "source_locator": source_locator,
                    "index_revision": int(value.get("analysis_index") or index),
                    "caused_by": indexed["event_id"],
                    "owner_receipt": canonicalize(value),
                    "state": "digest-verified",
                },
                metadata={
                    "work_unit_id": unit_id,
                    "owner_binding_required": True,
                    "distinct_source_range": True,
                },
            )


class DualDomainScenarioExecutor:
    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
        scratch_root: str | Path,
        bindings: DualDomainOwnerBindings,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.scratch_root = Path(scratch_root).resolve(strict=False)
        self.bindings = bindings

    def execute(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        goal: str,
        policy_decisions: Sequence[Mapping[str, Any]],
        cancel_requested: Callable[[], bool],
    ) -> OwnerExecutionResult:
        if configuration.scenario_id not in LIVE_SCENARIO_IDS:
            raise invalid(
                "dual_domain_scenario_unknown",
                "Dual-domain executor received an unowned scenario.",
                phase="dual-domain",
                detail={"scenario_id": configuration.scenario_id},
            )
        options = DualDomainExecutionOptions.from_configuration(configuration)
        self._require_options(configuration, options)
        owner_context = dict(
            self.bindings.task.begin(
                scenario_run_id=scenario_run_id,
                configuration=configuration,
                goal=goal,
                policy_decisions=policy_decisions,
            )
        )
        owner_run_id = str(owner_context.get("run_id") or "")
        task_id = str(owner_context.get("task_id") or "")
        root_node_id = str(owner_context.get("root_node_id") or "root")
        if not owner_run_id or not task_id:
            raise conflict(
                "live_task_owner_identity_missing",
                "Task owner did not return a canonical run/task identity.",
                phase="dual-domain",
            )
        started_at = str(owner_context.get("started_at") or utc_now())
        policy_evidence: dict[str, Any] = {}
        if self.bindings.pre_execution_policy is not None:
            policy_evidence = dict(
                self.bindings.pre_execution_policy.execute(
                    owner_context=owner_context,
                    configuration=configuration,
                    goal=goal,
                )
            )
            if (
                policy_evidence.get("schema")
                != "zyra.phase2-sealed-inline-policy/v1"
                or policy_evidence.get("ready") is not True
                or policy_evidence.get("run_id") != owner_run_id
                or policy_evidence.get("task_id") != task_id
            ):
                raise conflict(
                    "live_inline_policy_invalid",
                    "The pre-execution strongest policy did not return a run-bound ready receipt.",
                    phase="dual-domain-policy",
                )
        domain_input = self._domain_input(configuration)
        placement_runtime = PlacementEvidenceRuntime(owner=self.bindings.placement)
        placement = placement_runtime.execute(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            require_real_tiers=options.require_real_tiers,
            require_real_providers=options.require_real_providers,
        )
        route = self._domain_route(placement)
        if policy_evidence:
            route["phase2_policy_binding"] = canonicalize(
                {
                    "policy_profile": policy_evidence.get("policy_profile"),
                    "decision_id": policy_evidence.get("decision_id"),
                    "operator_ref": policy_evidence.get("operator_ref"),
                    "resource_decision_id": policy_evidence.get(
                        "resource_decision_id"
                    ),
                    "lease_id": policy_evidence.get("lease_id"),
                    "attempt_id": policy_evidence.get("attempt_id"),
                    "physical_receipt_digest": policy_evidence.get(
                        "physical_receipt_digest"
                    ),
                    "receipt_digest": policy_evidence.get("receipt_digest"),
                    "consumed_by": "dual-domain-route-and-domain-runtime",
                }
            )
        if cancel_requested():
            raise conflict(
                "live_cancelled_before_domain_execution",
                "Live scenario was cancelled before domain execution.",
                phase="dual-domain",
            )
        payload = self._execute_domain(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            domain_input=domain_input,
            route=route,
            options=options,
            cancel_requested=cancel_requested,
        )
        if policy_evidence and payload.metadata.get(
            "inline_policy_receipt_digest"
        ) != policy_evidence.get("receipt_digest"):
            raise conflict(
                "live_inline_policy_not_consumed",
                "Domain runtime did not bind the strongest policy receipt.",
                phase="dual-domain-policy",
            )
        builder = CanonicalEventBuilder(run_id=owner_run_id, task_id=task_id)
        builder.emit(
            event_type="task_created",
            effect="state_mutation",
            stage="task",
            node_id=root_node_id,
            mutation={
                "scenario_run_id": scenario_run_id,
                "scenario_id": configuration.scenario_id,
                "configuration_digest": configuration.configuration_digest,
                "input_digest": configuration.input_digest,
                "domain": domain_input.domain.value,
                "state": "running",
            },
        )
        if policy_evidence:
            builder.emit(
                event_type="policy_control_committed",
                effect="topology",
                stage="pre-execution-policy",
                node_id=root_node_id,
                mutation={
                    "policy_profile": policy_evidence.get("policy_profile"),
                    "decision_id": policy_evidence.get("decision_id"),
                    "operator_ref": policy_evidence.get("operator_ref"),
                    "resource_decision_id": policy_evidence.get(
                        "resource_decision_id"
                    ),
                    "lease_id": policy_evidence.get("lease_id"),
                    "attempt_id": policy_evidence.get("attempt_id"),
                    "receipt_digest": policy_evidence.get("receipt_digest"),
                    "state": "consumed-before-domain-execution",
                },
            )
        builder.emit(
            event_type="topology_mutation",
            effect="topology",
            stage="topology",
            node_id=root_node_id,
            mutation={
                "topology_revision": 1,
                "added_nodes": [
                    "domain-planner",
                    "domain-worker",
                    "fault-observer",
                    "deterministic-verifier",
                    "artifact-publisher",
                ],
                "added_edges": [
                    "planner->worker",
                    "worker->fault-observer",
                    "worker->verifier",
                    "verifier->artifact-publisher",
                ],
                "dynamic": True,
            },
        )
        builder.append_existing(
            placement_events(
                placement,
                run_id=owner_run_id,
                task_id=task_id,
                starting_sequence=builder.sequence,
                previous_event_id=builder.previous_event_id,
            )
        )
        builder.emit(
            event_type="memory_retrieved",
            effect="memory",
            stage="memory",
            node_id=root_node_id,
            mutation={
                "query_digest": digest(domain_input.request_text),
                "plan_digest": payload.plan.plan_digest,
                "selected_context": [
                    configuration.definition_digest,
                    configuration.policy.policy_digest,
                ],
                "state": "context-bound",
            },
        )
        for action_index, result in enumerate(payload.action_results, start=1):
            action = payload.plan.action(result.action_id)
            builder.emit(
                event_type="task_updated",
                effect=action.expected_effect,
                stage=action.stage,
                worker_id=result.worker_id,
                provider_id=result.provider_id,
                mutation={
                    "action_id": result.action_id,
                    "action_kind": action.kind.value,
                    "action_state": result.state.value,
                    "action_index": action_index,
                    "input_digest": result.input_digest,
                    "output_digest": result.output_digest,
                    "route_id": result.route_id,
                    "tier": result.tier.value,
                },
            )
        if self.bindings.analysis is None:
            raise conflict(
                "live_analysis_owner_unbound",
                "Formal long-run analysis requires the canonical memory owner.",
                phase="dual-domain-events",
            )
        analysis_receipts = tuple(
            dict(item)
            for item in self.bindings.analysis.commit_analysis_units(
                owner_context=owner_context,
                values=payload.work_units,
                stage=(
                    "code-index"
                    if domain_input.domain is LiveDomain.SOFTWARE_DELIVERY
                    else "source-index"
                ),
            )
        )
        builder.work_units(
            analysis_receipts,
            worker_id=str(route.get("worker_id") or ""),
            provider_id=str(route.get("provider_id") or ""),
            stage=(
                "code-index"
                if domain_input.domain is LiveDomain.SOFTWARE_DELIVERY
                else "source-index"
            ),
        )
        fault_runtime = FaultCampaignRuntime(
            owner=self.bindings.fault,
            schedule=FaultSchedule.for_domain(
                domain_input.domain,
                overrides=configuration.faults,
            ),
        )
        executions = fault_runtime.drain(
            effective_step=builder.sequence,
            plan=payload.plan,
            scenario_state={
                **owner_context,
                "route": route,
                "route_id": str(route.get("route_id") or ""),
                "input_digest": domain_input.input_digest,
                "configuration_digest": configuration.configuration_digest,
                "plan_digest": payload.plan.plan_digest,
                "event_count": builder.sequence,
                "last_event_id": builder.previous_event_id,
            },
            completed_action_ids=tuple(
                item.action_id for item in payload.action_results
            ),
        )
        builder.append_existing(
            campaign_events(
                executions,
                run_id=owner_run_id,
                task_id=task_id,
                starting_sequence=builder.sequence,
                previous_event_id=builder.previous_event_id,
            )
        )
        fault_receipt = fault_runtime.require_formal()
        observations = fault_runtime.observations
        migration_reasons = [
            item.reason
            for item in observations
            if item.route_before and item.route_after and item.route_before != item.route_after
        ]
        for reason in migration_reasons:
            placement = placement_runtime.migrate(
                placement,
                scenario_run_id=scenario_run_id,
                domain_input=domain_input,
                reason=reason,
            )
        self._bind_requirement_change(payload, observations)
        preliminary_artifacts = self._publish_domain_artifacts(
            owner_context=owner_context,
            payload=payload,
            domain=domain_input.domain,
            scenario_run_id=scenario_run_id,
        )
        for artifact in preliminary_artifacts:
            builder.emit(
                event_type="artifact_written",
                effect="artifact",
                stage="artifact",
                node_id=root_node_id,
                mutation={
                    "artifact_id": artifact.get("artifact_id"),
                    "sha256": artifact.get("sha256")
                    or (artifact.get("metadata") or {}).get("sha256"),
                    "uri": artifact.get("uri") or artifact.get("path"),
                    "state": "owner-committed",
                },
            )
        builder.emit(
            event_type="verification",
            effect="verification",
            stage="verification",
            node_id=root_node_id,
            mutation={
                "verifier": (
                    "SoftwareDeliveryVerifier"
                    if domain_input.domain is LiveDomain.SOFTWARE_DELIVERY
                    else "ResearchDeliveryVerifier"
                ),
                "state": "executing",
                "input_digest": domain_input.input_digest,
                "artifact_count": len(preliminary_artifacts),
            },
        )
        builder.emit(
            event_type="delivery_committed",
            effect="delivery",
            stage="delivery",
            node_id=root_node_id,
            mutation={
                "delivery_id": f"delivery:{scenario_run_id}",
                "artifact_ids": [
                    str(item.get("artifact_id") or "")
                    for item in preliminary_artifacts
                ],
                "configuration_digest": configuration.configuration_digest,
                "state": "committed-for-verification",
            },
        )
        if not (
            options.minimum_effective_transitions
            <= len(builder.events)
            <= options.maximum_effective_transitions
        ):
            raise conflict(
                "live_transition_budget_invalid",
                "Dual-domain event stream is outside the formal transition budget.",
                phase="dual-domain",
                detail={
                    "observed": len(builder.events),
                    "minimum": options.minimum_effective_transitions,
                    "maximum": options.maximum_effective_transitions,
                },
            )
        verification = self._verify_domain(
            payload=payload,
            domain_input=domain_input,
            artifacts=preliminary_artifacts,
            events=builder.events,
            faults=observations,
            placement=placement,
            options=options,
        )
        verification.require_valid()
        provisional = LiveDomainResult(
            domain=domain_input.domain,
            plan=payload.plan,
            action_results=payload.action_results,
            faults=observations,
            verification=verification,
            tier_observations=placement.tiers,
            provider_observations=placement.providers,
            artifacts=tuple(dict(item) for item in preliminary_artifacts),
            events=builder.events,
            owner_receipts=(),
            started_at=payload.started_at,
            completed_at=payload.completed_at,
            metadata={
                "require_real_tiers": options.require_real_tiers,
                "require_real_providers": options.require_real_providers,
                "scenario_run_id": scenario_run_id,
                "configuration_digest": configuration.configuration_digest,
                "inline_policy_receipt_digest": policy_evidence.get(
                    "receipt_digest"
                ),
            },
        )
        provisional.require_formal()
        memory_receipt = dict(
            self.bindings.task.curate_memory(
                owner_context=owner_context,
                result=provisional,
                previous_event_id=builder.previous_event_id,
            )
        )
        builder.emit(
            event_type="memory_curator_committed",
            effect="memory",
            stage="memory",
            worker_id=str(memory_receipt.get("worker_id") or "Memory"),
            node_id=root_node_id,
            mutation={
                "receipt_id": memory_receipt.get("receipt_id"),
                "memory_id": memory_receipt.get("memory_id"),
                "state": memory_receipt.get("state") or "committed",
                "content_digest": memory_receipt.get("content_digest"),
            },
            event_id=str(memory_receipt.get("event_id") or ""),
        )
        raw_samples = raw_metric_samples(
            scenario_run_id=scenario_run_id,
            owner_run_id=owner_run_id,
            task_id=task_id,
            events=builder.events,
            result=provisional,
        )
        owner_receipts = self._owner_receipts(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            domain_result=provisional,
            placement=placement,
            fault_receipt=fault_receipt,
            memory_receipt=memory_receipt,
        )
        committed_events = tuple(
            dict(item)
            for item in self.bindings.task.commit_events(
                owner_context=owner_context,
                events=builder.events,
            )
        )
        self._require_committed_events(
            requested=builder.events,
            committed=committed_events,
            owner_run_id=owner_run_id,
            task_id=task_id,
        )
        archive = CausalArchiveBuilder(
            artifact_root=self.artifact_root,
            source_commit=self.bindings.source_commit,
        ).build(
            scenario_run_id=scenario_run_id,
            owner_run_id=owner_run_id,
            task_id=task_id,
            configuration_digest=configuration.configuration_digest,
            input_digest=configuration.input_digest,
            policy_digest=configuration.policy.policy_digest,
            domain=domain_input.domain,
            events=committed_events,
            artifacts=preliminary_artifacts,
            owner_receipts=owner_receipts,
            fault_receipts=tuple(item.to_dict() for item in executions),
            placement_receipt=placement.to_dict(),
            verification=verification,
            raw_samples=raw_samples,
            environment={
                "scenario_id": configuration.scenario_id,
                "profile": configuration.profile.to_dict(),
                "fault_schedule": fault_runtime.schedule.to_dict(),
                "runtime": runtime_environment(),
            },
        )
        archive_artifacts = tuple(
            self.bindings.artifact.publish(
                owner_context=owner_context,
                source_paths=(archive.manifest_path,),
                domain=domain_input.domain,
                metadata={
                    "scenario_run_id": scenario_run_id,
                    "artifact_role": "causal-archive",
                    "archive_id": archive.archive_id,
                    "archive_digest": archive.manifest_digest,
                },
            )
        )
        all_artifacts = (
            *preliminary_artifacts,
            *(dict(item) for item in archive_artifacts),
        )
        final_result = LiveDomainResult(
            domain=domain_input.domain,
            plan=payload.plan,
            action_results=payload.action_results,
            faults=observations,
            verification=verification,
            tier_observations=placement.tiers,
            provider_observations=placement.providers,
            artifacts=tuple(dict(item) for item in all_artifacts),
            events=committed_events,
            owner_receipts=owner_receipts,
            started_at=payload.started_at,
            completed_at=utc_now(),
            metadata={
                **provisional.metadata,
                "causal_archive": archive.to_dict(),
                "raw_sample_digest": digest(raw_samples),
                "fault_campaign_digest": fault_receipt["receipt_digest"],
                "placement_run_digest": placement.run_digest,
            },
        )
        final_result.require_formal()
        task = dict(
            self.bindings.task.finalize(
                owner_context=owner_context,
                success=True,
                summary=(
                    f"{domain_input.domain.value} completed with "
                    f"{len(committed_events)} effective canonical transitions"
                ),
                artifacts=all_artifacts,
                events=committed_events,
            )
        )
        return OwnerExecutionResult(
            owner_run_id=owner_run_id,
            task_id=task_id,
            task={
                **task,
                "live_domain": final_result.summary(),
                "domain_verification": verification.to_dict(),
                "fault_campaign": fault_receipt,
                "placement": placement.to_dict(),
                "causal_archive": archive.to_dict(),
                "raw_samples": list(raw_samples),
                "human_intervention_count": 0,
            },
            events=committed_events,
            artifacts=tuple(dict(item) for item in all_artifacts),
            owner_receipts=owner_receipts,
            started_at=started_at,
            completed_at=utc_now(),
        )

    def _domain_input(self, configuration: ScenarioConfiguration) -> Any:
        metadata = {
            **dict(configuration.profile.metadata),
            **dict(configuration.metadata),
        }
        if configuration.scenario_id == LIVE_SOFTWARE_SCENARIO_ID:
            return software_input_from_configuration(
                configuration.input_text,
                project_root=self.project_root,
                metadata=metadata,
            )
        return research_input_from_configuration(
            configuration.input_text,
            project_root=self.project_root,
            metadata=metadata,
        )

    def _execute_domain(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        domain_input: Any,
        route: Mapping[str, Any],
        options: DualDomainExecutionOptions,
        cancel_requested: Callable[[], bool],
    ) -> SoftwareExecutionPayload | ResearchExecutionPayload:
        domain_root = self.scratch_root / scenario_run_id / domain_input.domain.value
        artifact_root = (
            self.artifact_root
            / "live-scenario-inputs"
            / scenario_run_id
            / domain_input.domain.value
        )
        if domain_input.domain is LiveDomain.SOFTWARE_DELIVERY:
            return SoftwareDeliveryRuntime(
                project_root=self.project_root,
                workspace_root=domain_root,
                artifact_root=artifact_root,
            ).execute(
                domain_input=domain_input,
                seed=configuration.seed,
                route=route,
                requirement_change=options.requirement_change,
            )
        return ResearchDeliveryRuntime(
            project_root=self.project_root,
            artifact_root=artifact_root,
        ).execute(
            domain_input=domain_input,
            seed=configuration.seed,
            route=route,
            cancel_requested=cancel_requested,
        )

    def _publish_domain_artifacts(
        self,
        *,
        owner_context: Mapping[str, Any],
        payload: SoftwareExecutionPayload | ResearchExecutionPayload,
        domain: LiveDomain,
        scenario_run_id: str,
    ) -> tuple[dict[str, Any], ...]:
        paths = list(payload.artifact_paths)
        if domain is LiveDomain.CROSS_SOURCE_RESEARCH:
            report_path = next(
                path for path in paths if Path(path).name == "research-report.json"
            )
            paths.remove(report_path)
            published = [
                dict(item)
                for item in self.bindings.artifact.publish(
                    owner_context=owner_context,
                    source_paths=tuple(paths),
                    domain=domain,
                    metadata={
                        "scenario_run_id": scenario_run_id,
                        "artifact_role": "research-evidence",
                        "input_digest": payload.plan.domain_input.input_digest,
                    },
                )
            ]
            payload.report["artifact_ids"] = [
                str(item.get("artifact_id") or "") for item in published
            ]
            Path(report_path).write_text(
                json.dumps(
                    payload.report,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
            published.extend(
                dict(item)
                for item in self.bindings.artifact.publish(
                    owner_context=owner_context,
                    source_paths=(report_path,),
                    domain=domain,
                    metadata={
                        "scenario_run_id": scenario_run_id,
                        "artifact_role": "research-report",
                        "input_digest": payload.plan.domain_input.input_digest,
                    },
                )
            )
            return tuple(published)
        published = tuple(
            dict(item)
            for item in self.bindings.artifact.publish(
                owner_context=owner_context,
                source_paths=tuple(paths),
                domain=domain,
                metadata={
                    "scenario_run_id": scenario_run_id,
                    "artifact_role": "software-evidence",
                    "input_digest": payload.plan.domain_input.input_digest,
                },
            )
        )
        artifact_ids = [
            str(item.get("artifact_id") or "") for item in published
        ]
        for receipt in payload.patch_receipt.get("requirement_receipts") or ():
            if isinstance(receipt, dict):
                receipt["evidence_refs"] = artifact_ids
        return published

    def _verify_domain(
        self,
        *,
        payload: SoftwareExecutionPayload | ResearchExecutionPayload,
        domain_input: Any,
        artifacts: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        faults: Sequence[FaultObservation],
        placement: PlacementRun,
        options: DualDomainExecutionOptions,
    ) -> DomainVerification:
        if isinstance(payload, SoftwareExecutionPayload):
            workspace_root = (
                self.scratch_root
                / str(
                    payload.plan.domain_input.metadata.get("scenario_run_id")
                    or ""
                )
            )
            actual_workspace = Path(
                next(
                    item["cwd"]
                    for item in payload.command_receipts
                    if item.get("cwd")
                )
            ).parent
            return SoftwareDeliveryVerifier(
                artifact_root=self.artifact_root,
                workspace_root=actual_workspace,
                project_root=self.project_root,
            ).verify(
                domain_input=domain_input,
                plan=payload.plan,
                action_results=payload.action_results,
                artifacts=artifacts,
                command_receipts=payload.command_receipts,
                patch_receipt=payload.patch_receipt,
                events=events,
                faults=faults,
                tiers=placement.tiers,
                providers=placement.providers,
                require_real_tiers=options.require_real_tiers,
                require_real_providers=options.require_real_providers,
            )
        return ResearchDeliveryVerifier(
            artifact_root=self.artifact_root
        ).verify(
            domain_input=domain_input,
            plan=payload.plan,
            action_results=payload.action_results,
            artifacts=artifacts,
            sources=payload.sources,
            claims=payload.claims,
            citations=payload.citations,
            report=payload.report,
            events=events,
            faults=faults,
            tiers=placement.tiers,
            providers=placement.providers,
            require_real_tiers=options.require_real_tiers,
            require_real_providers=options.require_real_providers,
        )

    @staticmethod
    def _bind_requirement_change(
        payload: SoftwareExecutionPayload | ResearchExecutionPayload,
        faults: Sequence[FaultObservation],
    ) -> None:
        selected = next(
            (
                item
                for item in faults
                if item.kind.value == "requirement_change"
            ),
            None,
        )
        if selected is None:
            raise conflict(
                "live_requirement_change_observation_missing",
                "Dual-domain scenario requires an observed requirement change.",
                phase="dual-domain",
            )
        if isinstance(payload, SoftwareExecutionPayload):
            receipt = payload.patch_receipt.get("requirement_change")
            if isinstance(receipt, dict):
                receipt["event_id"] = selected.fault_event_id
                receipt["reverification_id"] = selected.verifier_event_id

    @staticmethod
    def _domain_route(placement: PlacementRun) -> dict[str, Any]:
        route = dict(placement.initial_route)
        route.setdefault(
            "route_id",
            route.get("routeId"),
        )
        route.setdefault(
            "lease_id",
            route.get("leaseId"),
        )
        route.setdefault(
            "worker_id",
            route.get("workerId"),
        )
        route.setdefault(
            "receipt_id",
            route.get("receiptId"),
        )
        return route

    @staticmethod
    def _owner_receipts(
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        domain_result: LiveDomainResult,
        placement: PlacementRun,
        fault_receipt: Mapping[str, Any],
        memory_receipt: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        previous = str(domain_result.events[-1].get("event_id") or "")
        receipts: list[dict[str, Any]] = [
            {
                "receipt_id": f"live-route-{scenario_run_id}",
                "event_type": "backend_route",
                "semantic_effect": "route",
                "stage": "scheduler",
                "worker_id": str(
                    placement.initial_route.get("worker_id")
                    or placement.initial_route.get("workerId")
                    or ""
                ),
                "provider_id": str(
                    placement.initial_route.get("provider_id") or ""
                ),
                "causation_id": previous,
                "created_at": utc_now(),
                "route": placement.initial_route,
                "placement_run_digest": placement.run_digest,
            },
            {
                "receipt_id": str(
                    memory_receipt.get("receipt_id")
                    or f"live-memory-{scenario_run_id}"
                ),
                "event_type": "memory_curator_committed",
                "semantic_effect": "memory",
                "stage": "memory",
                "worker_id": str(memory_receipt.get("worker_id") or "Memory"),
                "causation_id": previous,
                "created_at": utc_now(),
                "memory": dict(memory_receipt),
            },
            {
                "receipt_id": f"live-fault-campaign-{scenario_run_id}",
                "event_type": "recovery_applied",
                "semantic_effect": "recovery",
                "stage": "recovery",
                "causation_id": previous,
                "created_at": utc_now(),
                "fault_campaign": dict(fault_receipt),
            },
            {
                "receipt_id": f"live-domain-verification-{scenario_run_id}",
                "event_type": "verification",
                "semantic_effect": "verification",
                "stage": "verification",
                "worker_id": domain_result.verification.verifier_id,
                "causation_id": previous,
                "created_at": utc_now(),
                "verification": domain_result.verification.to_dict(),
            },
            {
                "receipt_id": f"live-domain-delivery-{scenario_run_id}",
                "event_type": "delivery_committed",
                "semantic_effect": "delivery",
                "stage": "delivery",
                "causation_id": previous,
                "created_at": utc_now(),
                "artifact_ids": [
                    str(item.get("artifact_id") or "")
                    for item in domain_result.artifacts
                ],
                "scenario_id": configuration.scenario_id,
                "configuration_digest": configuration.configuration_digest,
                "human_intervention_count": 0,
            },
        ]
        return tuple(receipts)

    @staticmethod
    def _require_options(
        configuration: ScenarioConfiguration,
        options: DualDomainExecutionOptions,
    ) -> None:
        if configuration.mode.value != "sealed":
            raise conflict(
                "live_scenario_requires_sealed_mode",
                "Formal dual-domain scenarios require sealed mode.",
                phase="dual-domain",
            )
        if options.minimum_effective_transitions < 2_000:
            raise invalid(
                "live_transition_minimum_invalid",
                "Formal dual-domain minimum cannot be below 2,000.",
                phase="dual-domain",
            )
        if (
            options.maximum_effective_transitions
            < options.minimum_effective_transitions
        ):
            raise invalid(
                "live_transition_budget_invalid",
                "Maximum transition budget is below the formal minimum.",
                phase="dual-domain",
            )
        disabled = [
            name
            for name, environment_name in (
                ("scheduler", "ZYRA_WORKER_POOL_INTEGRATION_DISABLED"),
                ("memory", "ZYRA_MEMORY_RETRIEVAL_DISABLED"),
                ("recovery", "ZYRA_DISABLE_RECOVERY_RUNTIME"),
                ("low_entropy", "ZYRA_TARGETED_COMMUNICATION_DISABLED"),
                ("verifier", "ZYRA_SCENARIO_DOMAIN_VERIFIER_DISABLED"),
            )
            if _disabled(environment_name)
        ]
        if disabled:
            raise unavailable(
                "live_component_disabled",
                "Formal dual-domain scenario has no fallback for disabled owners.",
                phase="dual-domain",
                detail={"disabled": disabled},
            )

    @staticmethod
    def _require_committed_events(
        *,
        requested: Sequence[Mapping[str, Any]],
        committed: Sequence[Mapping[str, Any]],
        owner_run_id: str,
        task_id: str,
    ) -> None:
        if len(requested) != len(committed):
            raise conflict(
                "live_event_commit_count_mismatch",
                "Canonical event owner committed another event count.",
                phase="dual-domain",
                detail={
                    "requested": len(requested),
                    "committed": len(committed),
                },
            )
        requested_ids = [str(item.get("event_id") or "") for item in requested]
        committed_ids = [str(item.get("event_id") or "") for item in committed]
        if requested_ids != committed_ids:
            raise conflict(
                "live_event_commit_identity_mismatch",
                "Canonical event owner changed event identity or order.",
                phase="dual-domain",
            )
        for item in committed:
            if (
                str(item.get("run_id") or "") != owner_run_id
                or str(item.get("task_id") or "") != task_id
            ):
                raise conflict(
                    "live_event_commit_partition_mismatch",
                    "Canonical event owner committed an event to another partition.",
                    phase="dual-domain",
                    detail={"event_id": item.get("event_id")},
                )


def live_domain_projection(owner: OwnerExecutionResult) -> dict[str, Any]:
    task = owner.task
    summary = task.get("live_domain")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    verification = task.get("domain_verification")
    verification = (
        dict(verification) if isinstance(verification, Mapping) else {}
    )
    placement = task.get("placement")
    placement = dict(placement) if isinstance(placement, Mapping) else {}
    archive = task.get("causal_archive")
    archive = dict(archive) if isinstance(archive, Mapping) else {}
    event_effects = Counter(
        str((item.get("metadata") or {}).get("semantic_effect") or "")
        for item in owner.events
    )
    return {
        "schema": "zyra.live-domain-projection/v1",
        "owner_run_id": owner.owner_run_id,
        "task_id": owner.task_id,
        "domain": summary.get("domain"),
        "effective_transition_count": len(owner.events),
        "effect_counts": dict(event_effects),
        "fault_count": summary.get("fault_count"),
        "recovered_fault_count": summary.get("recovered_fault_count"),
        "tier_counts": summary.get("tier_counts"),
        "provider_model_counts": summary.get("provider_model_counts"),
        "verification_valid": verification.get("valid"),
        "verification_digest": verification.get("receipt_digest"),
        "placement_valid": (
            (placement.get("verification") or {}).get("valid")
            if isinstance(placement.get("verification"), Mapping)
            else False
        ),
        "placement_digest": placement.get("run_digest"),
        "causal_archive_id": archive.get("archive_id"),
        "causal_archive_digest": archive.get("manifest_digest"),
        "artifact_count": len(owner.artifacts),
        "human_intervention_count": 0,
    }


def compare_live_domains(
    left: OwnerExecutionResult,
    right: OwnerExecutionResult,
) -> dict[str, Any]:
    left_projection = live_domain_projection(left)
    right_projection = live_domain_projection(right)
    domains = {
        str(left_projection.get("domain") or ""),
        str(right_projection.get("domain") or ""),
    }
    required = {
        LiveDomain.SOFTWARE_DELIVERY.value,
        LiveDomain.CROSS_SOURCE_RESEARCH.value,
    }
    result = {
        "schema": "zyra.dual-domain-comparison/v1",
        "domains": sorted(domains),
        "dual_domain_complete": domains == required,
        "both_verified": (
            left_projection.get("verification_valid") is True
            and right_projection.get("verification_valid") is True
        ),
        "combined_effective_transition_count": (
            int(left_projection.get("effective_transition_count") or 0)
            + int(right_projection.get("effective_transition_count") or 0)
        ),
        "both_zero_human": (
            int(left_projection.get("human_intervention_count") or 0) == 0
            and int(right_projection.get("human_intervention_count") or 0) == 0
        ),
        "archive_digests_distinct": (
            left_projection.get("causal_archive_digest")
            != right_projection.get("causal_archive_digest")
        ),
        "left": left_projection,
        "right": right_projection,
    }
    result["comparison_digest"] = digest(result)
    return result


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "ArtifactOwnerPort",
    "CallbackArtifactOwnerPort",
    "CallbackTaskOwnerPort",
    "CanonicalEventBuilder",
    "DualDomainExecutionOptions",
    "DualDomainOwnerBindings",
    "DualDomainScenarioExecutor",
    "LIVE_RESEARCH_SCENARIO_ID",
    "LIVE_SCENARIO_IDS",
    "LIVE_SOFTWARE_SCENARIO_ID",
    "TaskOwnerPort",
    "UnboundArtifactOwnerPort",
    "UnboundTaskOwnerPort",
    "compare_live_domains",
    "live_domain_projection",
]
