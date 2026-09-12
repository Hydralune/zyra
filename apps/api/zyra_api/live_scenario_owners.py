from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from zyra_core import (
    ArtifactKind,
    EventRecord,
    EventType,
    PlanNodeStatus,
    TaskState,
    now_iso,
    to_jsonable,
)
from zyra_evaluation.scenario_runner.canonical import digest, new_identity
from zyra_evaluation.scenario_runner.dual_domain import (
    DualDomainOwnerBindings,
    DualDomainScenarioExecutor,
)
from zyra_evaluation.scenario_runner.live_models import (
    DomainInput,
    LiveDomain,
    LiveDomainResult,
    TierKind,
)
from zyra_evaluation.scenario_runner.models import (
    FaultInjection,
    OwnerExecutionResult,
    ScenarioConfiguration,
)
from zyra_runtime import LocalArtifactStore
from zyra_memory import MemoryLayer, MemoryRecord
from zyra_symbolic import apply_failure_injection, apply_requirement_change


_LIVE_EVENT_TYPE_MAP: dict[str, EventType] = {
    "task_created": EventType.TASK_CREATED,
    "topology_mutation": EventType.NODE_UPDATED,
    "permission_decision": EventType.CONSTRAINT_CHECK,
    "resource_decision": EventType.RESOURCE_DECISION,
    "topology_route": EventType.TOPOLOGY_ROUTE,
    "provider_route": EventType.TOPOLOGY_ROUTE,
    "worker_dispatched": EventType.RESOURCE_DECISION,
    "provider_turn": EventType.RESOURCE_DECISION,
    "edge_disconnected": EventType.WORKER_HEALTH,
    "memory_retrieved": EventType.NODE_UPDATED,
    "task_updated": EventType.NODE_UPDATED,
    "analysis_unit_indexed": EventType.NODE_UPDATED,
    "analysis_unit_verified": EventType.EVALUATION,
    "policy_control_committed": EventType.TOPOLOGY_ROUTE,
    "compact_committed": EventType.RECOVERY_PLANNED,
    "fault_observed": EventType.FAILURE_INJECTED,
    "requirement_change": EventType.REQUIREMENT_CHANGE,
    "recovery_planned": EventType.RECOVERY_PLANNED,
    "checkpoint_committed": EventType.RECOVERY_PLANNED,
    "fault_injected": EventType.FAILURE_INJECTED,
    "checkpoint_restored": EventType.RECOVERY_PLANNED,
    "route_migrated": EventType.TOPOLOGY_ROUTE,
    "recovery_applied": EventType.RECOVERY_PLANNED,
    "recovery_completed": EventType.RECOVERY_PLANNED,
    "artifact_written": EventType.ARTIFACT_WRITTEN,
    "verification": EventType.EVALUATION,
    "delivery_committed": EventType.ARTIFACT_WRITTEN,
    # The real curator writes its own typed event in ``curate_memory``.  This
    # stream item is the scenario's causal projection and must not re-dispatch
    # an explicit curator subscription.
    "memory_curator_committed": EventType.NODE_UPDATED,
}

_MAPPED_INJECTION_KINDS: dict[str, str] = {
    "requirement_change": "tool_timeout",
    "tool_exception": "tool_timeout",
    "tool_timeout": "tool_timeout",
    "worker_unavailable": "worker_lost",
    "node_lost": "worker_lost",
    "provider_failure": "model_failure",
    "provider_rate_limit": "model_failure",
    "edge_network_loss": "worker_lost",
    "network_loss": "tool_timeout",
}


class LiveOwnerIntegrationError(RuntimeError):
    """A canonical owner rejected a formal live-scenario operation."""


@dataclass(slots=True)
class _CheckpointBinding:
    checkpoint_id: str
    session_id: str
    workflow_signature: str
    graph_signature: str
    topology_signature: str
    owner_refs: dict[str, Any]
    version_refs: dict[str, Any]
    state_digest: str


def execute_live_owner_chain(
    *,
    scenario_run_id: str,
    configuration: ScenarioConfiguration,
    goal: str,
    policy_decisions: tuple[dict[str, Any], ...],
    cancel_requested: Any,
) -> OwnerExecutionResult:
    """Execute a formal live scenario through existing Zyra state owners."""

    from . import main as api_main

    owner = CanonicalLiveScenarioOwners(
        project_root=api_main.PROJECT_ROOT,
        artifact_root=api_main.artifact_root_path(),
        scratch_root=api_main.sqlite_path().with_name("live-scenario-scratch"),
    )
    bindings = DualDomainOwnerBindings(
        task=owner,
        artifact=owner,
        placement=owner,
        fault=owner,
        source_commit=_source_commit(api_main.PROJECT_ROOT),
        analysis=owner,
    )
    return DualDomainScenarioExecutor(
        project_root=api_main.PROJECT_ROOT,
        artifact_root=api_main.artifact_root_path(),
        scratch_root=api_main.sqlite_path().with_name("live-scenario-scratch"),
        bindings=bindings,
    ).execute(
        scenario_run_id=scenario_run_id,
        configuration=configuration,
        goal=goal,
        policy_decisions=policy_decisions,
        cancel_requested=cancel_requested,
    )


class CanonicalLiveScenarioOwners:
    """Bind live evaluation ports to the established API composition root.

    This adapter owns no task, event, artifact, lease, fault, checkpoint, or
    recovery state.  It projects the scenario protocol into the owners that
    already hold those facts and validates every receipt before returning it.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
        scratch_root: str | Path,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.scratch_root = Path(scratch_root).resolve()
        self.state: TaskState | None = None
        self.configuration: ScenarioConfiguration | None = None
        self.scenario_run_id = ""
        self._artifacts: list[Any] = []
        self._route_history: list[dict[str, Any]] = []
        self._route_worker: dict[str, str] = {}
        self._fault_routes: dict[str, dict[str, Any]] = {}
        self._checkpoints: dict[str, _CheckpointBinding] = {}
        self._tier_values: tuple[dict[str, Any], ...] = ()
        self._provider_values: tuple[dict[str, Any], ...] = ()
        self._fault_events: list[dict[str, Any]] = []
        self._worker_cursor = 0
        self._domain_input: DomainInput | None = None
        self._canonical_event_snapshot: tuple[dict[str, Any], ...] = ()
        self._canonical_analysis_snapshot: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------------
    # Canonical task/event owner port
    # ------------------------------------------------------------------

    def begin(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        goal: str,
        policy_decisions: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        if self.state is not None:
            raise LiveOwnerIntegrationError("live task owner was already initialized")
        state, created_event = api_main.make_task_created_event(goal)
        self.scenario_run_id = scenario_run_id
        self.configuration = configuration
        session_id = f"scenario:{scenario_run_id}"
        state.metadata.update(
            {
                "scenario_run_id": scenario_run_id,
                "scenario_configuration_digest": configuration.configuration_digest,
                "scenario_definition_digest": configuration.definition_digest,
                "scenario_input_digest": configuration.input_digest,
                "scenario_seed": configuration.seed,
                "scenario_profile": configuration.profile.to_dict(),
                "sealed": True,
                "sealed_autonomous": True,
                "formal_benchmark": True,
                "competition_mode": "sealed_autonomous",
                "human_intervention_count": 0,
                "query_session_id": session_id,
                "policy_digest": configuration.policy.policy_digest,
                "policy_decision_ids": [
                    str(item.get("decision_id") or "") for item in policy_decisions
                ],
                "live_owner_binding": {
                    "task": "SQLiteStore.TaskState",
                    "events": "RuntimeEventSpineBridge",
                    "artifacts": "LocalArtifactStore",
                    "placement": "WorkerPoolFoundationRuntime",
                    "fault": "FaultRuntimeApplication",
                    "checkpoint": "RecoveryPlanStore",
                    "recovery": "CheckpointResumeBridge",
                },
            }
        )
        workspace = api_main.get_workspace_manager().create_for_task(
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=session_id,
            worker_id="live-scenario-owner",
            idempotency_key=f"live-scenario-workspace:{scenario_run_id}",
            causation_id=created_event.event_id,
        )
        state.metadata["workspace_ref"] = workspace.projection.to_dict()
        api_main.ensure_default_graph(state)
        canonical_store = api_main.get_store()
        api_main.persist_events(canonical_store, [created_event])
        canonical_store.save_checkpoint(state)
        self.state = state
        return {
            "schema": "zyra.live-task-owner-context/v1",
            "scenario_run_id": scenario_run_id,
            "run_id": state.run_id,
            "task_id": state.task_id,
            "root_node_id": state.root_node_id,
            "task_created_event_id": created_event.event_id,
            "session_id": session_id,
            "workspace_ref": state.metadata["workspace_ref"],
            "started_at": now_iso(),
            "task_owner": "SQLiteStore.TaskState",
            "event_owner": "RuntimeEventSpineBridge",
        }

    def commit_events(
        self,
        *,
        owner_context: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        from . import main as api_main

        state = self._require_state(owner_context)
        records: list[EventRecord] = []
        seen: set[str] = set()
        previous = ""
        for index, value in enumerate(events, start=1):
            event = dict(value)
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in seen:
                raise LiveOwnerIntegrationError(
                    "live event batch contains a missing or duplicate identity"
                )
            seen.add(event_id)
            if str(event.get("run_id") or "") != state.run_id:
                raise LiveOwnerIntegrationError("live event run identity mismatch")
            if str(event.get("task_id") or "") != state.task_id:
                raise LiveOwnerIntegrationError("live event task identity mismatch")
            if int(event.get("sequence") or 0) != index:
                raise LiveOwnerIntegrationError("live event sequence is not contiguous")
            causation = str(
                event.get("causation_id")
                or (event.get("payload") or {}).get("causation_id")
                or ""
            )
            if index > 1 and causation != previous:
                raise LiveOwnerIntegrationError("live event causal chain is broken")
            original_type = str(event.get("event_type") or "")
            selected_type = _LIVE_EVENT_TYPE_MAP.get(original_type)
            if selected_type is None:
                raise LiveOwnerIntegrationError(
                    f"live event type has no canonical projection: {original_type}"
                )
            if original_type in {"analysis_unit_indexed", "analysis_unit_verified"}:
                previous = event_id
                continue
            records.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=selected_type,
                    event_id=event_id,
                    node_id=str(event.get("node_id") or state.root_node_id),
                    created_at=str(event.get("created_at") or now_iso()),
                    payload={
                        **dict(event.get("payload") or {}),
                        "live_event": {
                            "event_type": original_type,
                            "sequence": index,
                            "causation_id": causation,
                            "parent_event_id": str(
                                event.get("parent_event_id") or ""
                            ),
                            "metadata": dict(event.get("metadata") or {}),
                        },
                    },
                )
            )
            previous = event_id
        api_main.persist_events(api_main.get_store(), records)
        stored = {
            str(item.get("event_id") or ""): item
            for item in api_main.get_store().task_events(state.task_id)
        }
        missing = [item.event_id for item in records if item.event_id not in stored]
        if missing:
            raise LiveOwnerIntegrationError(
                f"canonical event owner did not persist events: {missing[:3]}"
            )
        analysis_by_id = {
            str(item.get("memory_id") or ""): dict(item)
            for item in self._canonical_analysis_snapshot
        }
        committed_events: list[dict[str, Any]] = []
        snapshot_events: list[dict[str, Any]] = []
        for event in events:
            source = dict(event)
            if source.get("event_type") in {
                "analysis_unit_indexed",
                "analysis_unit_verified",
            }:
                unit = dict(
                    (source.get("payload") or {}).get("mutation", {}).get(
                        "owner_receipt"
                    )
                    or {}
                )
                selected_memory = analysis_by_id.get(str(unit.get("memory_id") or ""))
                if selected_memory is None:
                    raise LiveOwnerIntegrationError(
                        "analysis event has no canonical memory owner snapshot"
                    )
                receipt = {
                    "schema": "zyra.canonical-analysis-event-owner-receipt/v1",
                    "owner": "SQLiteStore.MemoryRecord",
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "event_id": source["event_id"],
                    "source_event_type": source["event_type"],
                    "source_event_digest": digest(source),
                    "memory_id": unit.get("memory_id"),
                    "analysis_receipt_digest": unit.get("receipt_digest"),
                    "persisted_memory_digest": digest(selected_memory),
                    "persisted_content_digest": digest(
                        selected_memory.get("content") or {}
                    ),
                }
            else:
                selected = stored[str(event["event_id"])]
                receipt = {
                    "schema": "zyra.canonical-event-owner-receipt/v1",
                    "owner": "SQLiteStore.EventRecord",
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "event_id": source["event_id"],
                    "source_event_type": source["event_type"],
                    "persisted_event_type": selected.get("event_type"),
                    "source_event_digest": digest(source),
                    "persisted_event_digest": digest(selected),
                    "persisted_payload_digest": digest(
                        selected.get("payload") or {}
                    ),
                }
                snapshot_events.append(dict(selected))
            receipt["receipt_digest"] = digest(receipt)
            source["owner_receipt"] = receipt
            committed_events.append(source)
        self._canonical_event_snapshot = tuple(snapshot_events)
        state.metadata["live_canonical_event_count"] = len(events)
        state.metadata["live_canonical_event_owner_count"] = len(records)
        state.metadata["live_analysis_owner_count"] = len(analysis_by_id)
        state.metadata["live_causal_root_event_id"] = records[0].event_id
        state.metadata["live_causal_leaf_event_id"] = records[-1].event_id
        api_main.get_store().save_checkpoint(state)
        return tuple(committed_events)

    def canonical_event_snapshot(self) -> tuple[dict[str, Any], ...]:
        from . import main as api_main

        if self.state is None:
            return tuple(dict(item) for item in self._canonical_event_snapshot)
        return tuple(
            dict(item)
            for item in api_main.get_store().task_events(self.state.task_id)
        )

    def canonical_analysis_snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._canonical_analysis_snapshot)

    def commit_analysis_units(
        self,
        *,
        owner_context: Mapping[str, Any],
        values: Sequence[Mapping[str, Any]],
        stage: str,
    ) -> Sequence[Mapping[str, Any]]:
        """Persist distinct source-range analysis facts in the existing memory owner."""

        from . import main as api_main

        state = self._require_state(owner_context)
        records: list[MemoryRecord] = []
        normalized: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        seen_ranges: set[tuple[str, int, int]] = set()
        seen_intervals: dict[str, list[tuple[int, int]]] = {}
        for index, raw in enumerate(values, start=1):
            value = dict(raw)
            unit_id = str(value.get("work_unit_id") or "")
            input_digest = str(value.get("input_digest") or "")
            output_digest = str(value.get("output_digest") or "")
            byte_start = int(value.get("byte_start") or 0)
            byte_end = int(value.get("byte_end") or 0)
            source_locator = str(
                value.get("source_id") or value.get("relative_path") or ""
            )
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
                raise LiveOwnerIntegrationError(
                    "analysis unit is not a distinct source byte range"
                )
            seen_units.add(unit_id)
            seen_ranges.add(source_range)
            seen_intervals.setdefault(source_locator, []).append(
                (byte_start, byte_end)
            )
            content = {
                "work_unit_id": unit_id,
                "kind": str(value.get("kind") or "analysis"),
                "input_digest": input_digest,
                "output_digest": output_digest,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "analysis_index": int(value.get("analysis_index") or index),
                "semantic_mutation": dict(value.get("semantic_mutation") or {}),
                "relative_path": str(value.get("relative_path") or ""),
                "source_id": str(value.get("source_id") or ""),
                "fragment_id": str(value.get("fragment_id") or ""),
                "source_locator": source_locator,
                "stage": stage,
            }
            content_digest = digest(content)
            # Bind the record identity to the run.  Content-only ids collide
            # when two runs analyse identical source bytes: the upsert would
            # keep the first run's run_id/task_id, and the second run's readback
            # (which filters by its own task_id) would find nothing and fail
            # commit/readback verification.
            memory_id = f"analysis-unit:{state.run_id}:{content_digest}"
            records.append(
                MemoryRecord(
                    memory_id=memory_id,
                    run_id=state.run_id,
                    task_id=state.task_id,
                    layer=MemoryLayer.EPISODIC,
                    source_type="distinct_source_range",
                    source_id=unit_id,
                    node_id=state.root_node_id,
                    summary=(
                        f"Verified {stage} source range {byte_start}:{byte_end}"
                    ),
                    content=content,
                    evidence_ids=[unit_id],
                    score=1.0,
                    metadata={
                        "content_digest": content_digest,
                        "owner": "SQLiteStore.MemoryRecord",
                        "formal_long_run": True,
                    },
                )
            )
            normalized.append(
                {
                    **content,
                    "memory_id": memory_id,
                    "content_digest": content_digest,
                }
            )
        api_main.get_store().save_memory_records(records)
        stored = {
            item.memory_id: item
            for item in api_main.get_store().task_memory_records(
                state.task_id,
                MemoryLayer.EPISODIC,
            )
        }
        self._canonical_analysis_snapshot = tuple(
            to_jsonable(stored[item["memory_id"]])
            for item in normalized
            if item["memory_id"] in stored
        )
        receipts: list[dict[str, Any]] = []
        for value in normalized:
            record = stored.get(str(value["memory_id"]))
            readback_verified = bool(
                record is not None
                and record.run_id == state.run_id
                and record.task_id == state.task_id
                and digest(record.content) == value["content_digest"]
                and record.source_id == value["work_unit_id"]
            )
            receipt = {
                "schema": "zyra.analysis-unit-owner-receipt/v1",
                "owner": "SQLiteStore.MemoryRecord",
                "run_id": state.run_id,
                "task_id": state.task_id,
                "work_unit_id": value["work_unit_id"],
                "memory_id": value["memory_id"],
                "kind": value["kind"],
                "input_digest": value["input_digest"],
                "output_digest": value["output_digest"],
                "byte_start": value["byte_start"],
                "byte_end": value["byte_end"],
                "analysis_index": value["analysis_index"],
                "source_locator": value["source_locator"],
                "semantic_mutation": value["semantic_mutation"],
                "content_digest": value["content_digest"],
                "committed": record is not None,
                "readback_verified": readback_verified,
            }
            receipt["receipt_digest"] = digest(receipt)
            receipts.append(receipt)
        if len(receipts) != len(values) or not all(
            item["committed"] and item["readback_verified"] for item in receipts
        ):
            failed = [
                {
                    "work_unit_id": item["work_unit_id"],
                    "memory_id": item["memory_id"],
                    "committed": item["committed"],
                    "readback_verified": item["readback_verified"],
                    "content_digest": item["content_digest"],
                }
                for item in receipts
                if not (item["committed"] and item["readback_verified"])
            ]
            raise LiveOwnerIntegrationError(
                "analysis-unit owner failed commit/readback verification: "
                + json.dumps(
                    {
                        "value_count": len(values),
                        "receipt_count": len(receipts),
                        "failed_count": len(failed),
                        "failed_sample": failed[:5],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return tuple(receipts)

    def curate_memory(
        self,
        *,
        owner_context: Mapping[str, Any],
        result: LiveDomainResult,
        previous_event_id: str,
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(owner_context)
        state.metadata["live_domain_memory"] = {
            "domain": result.domain.value,
            "plan_digest": result.plan.plan_digest,
            "verification_digest": result.verification.receipt_digest,
            "effective_transition_count": len(result.events),
            "fault_ids": [item.injection_id for item in result.faults],
            "artifact_ids": [
                str(item.get("artifact_id") or "") for item in result.artifacts
            ],
            "previous_event_id": previous_event_id,
        }
        state.status = PlanNodeStatus.COMPLETED
        state.updated_at = now_iso()
        api_main.get_store().save_checkpoint(state)
        receipt = api_main.curate_terminal_task(api_main.get_store(), state)
        receipt = dict(receipt or {})
        event_id = new_identity("live-memory-event")
        content_digest = digest(state.metadata["live_domain_memory"])
        state.metadata["live_memory_receipt"] = {
            "receipt": receipt,
            "event_id": event_id,
            "content_digest": content_digest,
        }
        api_main.get_store().save_checkpoint(state)
        return {
            "schema": "zyra.live-memory-curation-receipt/v1",
            "receipt_id": str(
                receipt.get("request_id")
                or receipt.get("job_id")
                or new_identity("live-memory")
            ),
            "event_id": event_id,
            "memory_id": str(
                receipt.get("memory_id")
                or (receipt.get("result") or {}).get("memory_id")
                or f"memory:{content_digest[:24]}"
            ),
            "worker_id": "MemoryCuratorRuntime",
            "state": str(receipt.get("status") or "scheduled"),
            "content_digest": content_digest,
            "owner_receipt": receipt,
        }

    def finalize(
        self,
        *,
        owner_context: Mapping[str, Any],
        success: bool,
        summary: str,
        artifacts: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(owner_context)
        state.status = (
            PlanNodeStatus.COMPLETED if success else PlanNodeStatus.FAILED
        )
        state.updated_at = now_iso()
        state.metadata["live_settlement"] = {
            "summary": summary,
            "success": success,
            "artifact_count": len(artifacts),
            "event_count": len(events),
            "scenario_run_id": self.scenario_run_id,
            "human_intervention_count": 0,
            "settled_at": now_iso(),
        }
        if self._route_history:
            api_main.get_worker_pool_api().finalize_task(
                state,
                success=success,
                summary=summary,
            )
        api_main.get_store().save_checkpoint(state)
        loaded = api_main.get_store().load_task(state.task_id)
        if loaded is None:
            raise LiveOwnerIntegrationError("final task checkpoint is unavailable")
        self.state = loaded
        return to_jsonable(loaded)

    # ------------------------------------------------------------------
    # Canonical artifact owner port
    # ------------------------------------------------------------------

    def publish(
        self,
        *,
        owner_context: Mapping[str, Any],
        source_paths: Sequence[str],
        domain: LiveDomain,
        metadata: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        from . import main as api_main

        state = self._require_state(owner_context)
        store = LocalArtifactStore(self.artifact_root)
        output: list[dict[str, Any]] = []
        for source_value in source_paths:
            source = Path(source_value).resolve()
            if not source.is_file():
                raise LiveOwnerIntegrationError(
                    f"live artifact source is not a file: {source}"
                )
            kind = _artifact_kind(source, metadata)
            artifact = store.write_from_path(
                run_id=state.run_id,
                task_id=state.task_id,
                source_path=source,
                title=f"{domain.value}: {source.name}",
                kind=kind,
                producer_node_id=state.root_node_id,
                metadata={
                    **dict(metadata),
                    "domain": domain.value,
                    "source_name": source.name,
                    "producer_worker_id": "live-scenario-owner",
                    "security_label": "internal",
                    "retention_policy": "submission",
                },
            )
            self._artifacts.append(artifact)
            value = dict(to_jsonable(artifact))
            value["path"] = artifact.uri
            value["sha256"] = str(artifact.metadata.get("sha256") or "")
            output.append(value)
        api_main._attach_artifacts(state, list(self._artifacts))
        api_main.get_store().save_checkpoint(state)
        return tuple(output)

    # ------------------------------------------------------------------
    # Scheduler, tier and provider owner port
    # ------------------------------------------------------------------

    def acquire_route(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        required_capabilities: Sequence[str],
        allowed_tiers: Sequence[TierKind],
        excluded_route_ids: Sequence[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if TierKind.DEVICE not in set(allowed_tiers):
            raise LiveOwnerIntegrationError(
                "API worker pool currently requires a device route for the task owner"
            )
        self._domain_input = domain_input
        return self._acquire_pool_route(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            reason="initial-placement",
            required_capabilities=required_capabilities,
            excluded_route_ids=excluded_route_ids,
            idempotency_key=idempotency_key,
        )

    def migrate_route(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
        reason: str,
        excluded_route_ids: Sequence[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        cached = self._fault_routes.get(reason)
        if cached is not None:
            return dict(cached)
        return self._acquire_pool_route(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            reason=reason,
            required_capabilities=("agent_task", "artifact_return"),
            excluded_route_ids=excluded_route_ids,
            idempotency_key=idempotency_key,
        )

    def execute_tiers(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        # M2-S05-02 intentionally has no authenticated cloud/provider probe.
        # Canonical worker-pool routes and migrations are attested separately.
        return ()

    def execute_providers(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
        capability_count: int,
    ) -> Sequence[Mapping[str, Any]]:
        if capability_count > 0:
            raise LiveOwnerIntegrationError(
                "authenticated provider/model execution is excluded for M2-S05-02"
            )
        return ()

    def disconnected_degradation(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        edge = next(
            (
                item
                for item in self._tier_values
                if item.get("tier") == "edge"
            ),
            dict(route),
        )
        return {
            "schema": "zyra.live-disconnected-degradation/v1",
            "event_id": new_identity("edge-disconnected"),
            "observed": True,
            "safe": True,
            "relabeled_as_cloud": False,
            "route_before": str(edge.get("route_id") or ""),
            "route_after": str(route.get("route_id") or ""),
            "failed_endpoint_id": str(edge.get("endpoint_id") or ""),
            "failed_runtime_id": str(edge.get("runtime_id") or ""),
            "transport_closed_after_task": True,
            "fallback_tier": "device",
            "reason": (
                "deterministic edge/network fault boundary; canonical worker "
                "lease retained without external provider execution"
            ),
            "observed_at": now_iso(),
        }

    # ------------------------------------------------------------------
    # Checkpoint, fault, recovery and re-verification owner port
    # ------------------------------------------------------------------

    def checkpoint(
        self,
        *,
        injection: FaultInjection,
        effective_step: int,
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(scenario_state)
        state.updated_at = now_iso()
        api_main.get_store().save_checkpoint(state)
        state_value = to_jsonable(state)
        state_digest = digest(state_value)
        recovery_api = api_main.get_recovery_runtime_api(api_main.get_store())
        recovery_view = _require_api_response(
            recovery_api.route_get(("tasks", state.task_id, "recovery")),
            expected=(HTTPStatus.OK,),
            operation="inspect recovery checkpoint lineage",
        )
        checkpoint_head = _canonical_checkpoint_head(recovery_view)
        session_id, workflow_signature = _checkpoint_lineage(
            checkpoint_head=checkpoint_head,
            scenario_run_id=self.scenario_run_id,
            configuration_digest=scenario_state.get("configuration_digest"),
            fallback_session_id=str(
                state.metadata.get("query_session_id") or ""
            ),
        )
        graph_signature = digest(
            {
                "graph": state.metadata.get("dynamic_graph_ref"),
                "task_id": state.task_id,
            }
        )
        topology_signature = digest(
            {
                "route": self._current_route(),
                "step": effective_step,
            }
        )
        owner_refs = {
            "task": state.task_id,
            "session": session_id,
            "route": str(self._current_route().get("route_id") or ""),
        }
        version_refs = {
            "task": len(self._checkpoints) + 1,
            "session": 1,
            "route": len(self._route_history),
        }
        response = recovery_api.route_post(
            ("tasks", state.task_id, "recovery", "checkpoints"),
            {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "session_id": session_id,
                "workflow_signature": workflow_signature,
                "graph_signature": graph_signature,
                "topology_signature": topology_signature,
                "owner_refs": owner_refs,
                "version_refs": version_refs,
                "state_payload": {
                    "task_state_digest": state_digest,
                    "input_digest": str(scenario_state.get("input_digest") or ""),
                    "configuration_digest": str(
                        scenario_state.get("configuration_digest") or ""
                    ),
                    "plan_digest": str(scenario_state.get("plan_digest") or ""),
                    "route": dict(self._current_route()),
                    "effective_step": effective_step,
                    "injection_id": injection.injection_id,
                },
                "completed_step_ids": [
                    f"effective-step-{index:06d}"
                    for index in range(1, min(effective_step, 64) + 1)
                ],
                "metadata": {
                    "fault_kind": injection.kind,
                    "scenario_run_id": self.scenario_run_id,
                },
            },
        )
        body = _require_api_response(
            response,
            expected=(HTTPStatus.CREATED, HTTPStatus.OK),
            operation="commit recovery checkpoint",
        )
        checkpoint = dict(body.get("checkpoint") or {})
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if not checkpoint_id:
            raise LiveOwnerIntegrationError(
                "recovery checkpoint owner returned no checkpoint identity"
            )
        binding = _CheckpointBinding(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            workflow_signature=workflow_signature,
            graph_signature=graph_signature,
            topology_signature=topology_signature,
            owner_refs=owner_refs,
            version_refs=version_refs,
            state_digest=state_digest,
        )
        self._checkpoints[injection.injection_id] = binding
        return {
            "schema": "zyra.live-checkpoint-owner-receipt/v1",
            "checkpoint_id": checkpoint_id,
            "run_id": state.run_id,
            "task_id": state.task_id,
            "state_digest": state_digest,
            "owner": "RecoveryPlanStore",
            "committed": True,
            "commit_revision": checkpoint.get("commit_revision"),
            "content_digest": checkpoint.get("content_digest"),
            "signature": checkpoint.get("signature"),
            "owner_receipt": dict(body.get("receipt") or {}),
        }

    def inject(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(scenario_state)
        mapped_kind = _MAPPED_INJECTION_KINDS.get(injection.kind)
        if not mapped_kind:
            raise LiveOwnerIntegrationError(
                f"live fault kind has no canonical injector: {injection.kind}"
            )
        target = _fault_target(
            mapped_kind,
            injection=injection,
            state=state,
            current_route=self._current_route(),
        )
        parameters = {
            key: value
            for key, value in injection.payload.items()
            if key
            in {
                "deadline_ms",
                "elapsed_ms",
                "exit_code",
                "status_code",
                "reason_code",
                "scenario",
            }
        }
        response = api_main.get_fault_runtime_api(api_main.get_store()).route_post(
            ("tasks", state.task_id, "faults", "inject"),
            {
                "kind": mapped_kind,
                "target": target,
                "parameters": parameters,
                "continuation": "recovery_handoff",
                "idempotency_key": (
                    f"live:{self.scenario_run_id}:{injection.injection_id}"
                ),
            },
            task_state=state,
            requested_by="sealed-live-scenario",
        )
        body = _require_api_response(
            response,
            expected=(HTTPStatus.CREATED, HTTPStatus.OK),
            operation="inject canonical fault",
        )
        if body.get("ok") is not True:
            raise LiveOwnerIntegrationError(
                f"fault owner rejected {injection.injection_id}: {body}"
            )
        event_id = str(
            body.get("event_id")
            or body.get("projection_event_id")
            or body.get("signal_event_id")
            or body.get("signal_id")
            or body.get("injection_id")
            or new_identity("fault-owner-event")
        )
        receipt = {
            "schema": "zyra.live-fault-owner-receipt/v1",
            "receipt_id": str(
                body.get("receipt_id")
                or body.get("injection_id")
                or injection.injection_id
            ),
            "event_id": event_id,
            "kind": injection.kind,
            "canonical_injection_kind": mapped_kind,
            "observed": True,
            "target": injection.target,
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "owner": "FaultRuntimeApplication",
            "owner_receipt": body,
            "reason": f"canonical {mapped_kind} boundary observed",
        }
        self._fault_events.append(receipt)
        return receipt

    def recover(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        fault_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(scenario_state)
        binding = self._checkpoints.get(injection.injection_id)
        if binding is None:
            raise LiveOwnerIntegrationError(
                f"checkpoint binding missing for {injection.injection_id}"
            )
        response = api_main.get_recovery_runtime_api(api_main.get_store()).route_post(
            (
                "tasks",
                state.task_id,
                "recovery",
                "checkpoints",
                binding.checkpoint_id,
                "resume",
            ),
            {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "session_id": binding.session_id,
                "workflow_signature": binding.workflow_signature,
                "graph_signature": binding.graph_signature,
                "topology_signature": binding.topology_signature,
                "owner_refs": binding.owner_refs,
                "version_refs": binding.version_refs,
                "candidate_step_ids": [
                    f"effective-step-{index:06d}"
                    for index in range(1, 65)
                ],
                "required_completed_step_ids": ["effective-step-000001"],
                "compact_first": False,
                "rebind_worker": False,
                "rebind_graph": False,
                "idempotency_key": (
                    f"live-resume:{self.scenario_run_id}:{injection.injection_id}"
                ),
            },
        )
        body = _require_api_response(
            response,
            expected=(HTTPStatus.OK,),
            operation="resume recovery checkpoint",
        )
        loaded = api_main.get_store().load_task(state.task_id)
        if loaded is None:
            raise LiveOwnerIntegrationError(
                "canonical task state disappeared during checkpoint resume"
            )
        self.state = loaded
        recovery_event_ids = _recovery_event_ids(body)
        self._apply_recovered_state(
            injection,
            fault_receipt=fault_receipt,
            recovery_event_ids=recovery_event_ids,
            scenario_state=scenario_state,
        )
        restored = api_main.get_store().load_task(state.task_id)
        if restored is None:
            raise LiveOwnerIntegrationError(
                "recovered task state was not checkpointed"
            )
        self.state = restored
        return {
            "schema": "zyra.live-recovery-owner-receipt/v1",
            "receipt_id": str(
                (body.get("receipt") or {}).get("resume_token")
                or new_identity("recovery")
            ),
            "event_ids": recovery_event_ids,
            "state": "recovered",
            "checkpoint_restored": True,
            "checkpoint_id": binding.checkpoint_id,
            "state_digest_before": binding.state_digest,
            "state_digest_after": digest(to_jsonable(restored)),
            "owner": "CheckpointResumeBridge",
            "owner_receipt": body,
            "reason": f"recovered {injection.injection_id}",
        }

    def migrate(
        self,
        *,
        injection: FaultInjection,
        fault_receipt: Mapping[str, Any],
        recovery_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        domain_input = self._domain_input
        if domain_input is None:
            raise LiveOwnerIntegrationError(
                "fault migration has no admitted domain input"
            )
        before = dict(self._current_route())
        reason = str(
            recovery_receipt.get("reason") or f"recovered {injection.injection_id}"
        )
        after = self._acquire_pool_route(
            scenario_run_id=self.scenario_run_id,
            domain_input=domain_input,
            reason=reason,
            required_capabilities=("agent_task", "artifact_return"),
            excluded_route_ids=tuple(
                str(item.get("route_id") or "") for item in self._route_history
            ),
            idempotency_key=(
                f"live-fault-route:{self.scenario_run_id}:{injection.injection_id}"
            ),
        )
        receipt = {
            "schema": "zyra.live-fault-migration-receipt/v1",
            "receipt_id": str(after.get("receipt_id") or ""),
            "lease_id": str(after.get("lease_id") or ""),
            "route_before": str(before.get("route_id") or ""),
            "route_after": str(after.get("route_id") or ""),
            "before": before,
            "after": dict(after),
            "reason": reason,
            "owner": "WorkerPoolFoundationRuntime",
        }
        self._fault_routes[reason] = dict(after)
        return receipt

    def reverify(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        fault_receipt: Mapping[str, Any],
        recovery_receipt: Mapping[str, Any],
        migration_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from . import main as api_main

        state = self._require_state(scenario_state)
        stored = api_main.get_store().load_task(state.task_id)
        if stored is None:
            raise LiveOwnerIntegrationError(
                "post-recovery verifier cannot load canonical task state"
            )
        input_digest = str(scenario_state.get("input_digest") or "")
        state_digest = digest(to_jsonable(stored))
        current_route = dict(self._current_route())
        expected_after = str(migration_receipt.get("route_after") or "")
        route_valid = (
            not expected_after
            or expected_after == str(current_route.get("route_id") or "")
        )
        checkpoint_valid = (
            str(checkpoint.get("checkpoint_id") or "")
            == str(recovery_receipt.get("checkpoint_id") or "")
        )
        valid = bool(
            input_digest
            and state_digest
            and checkpoint_valid
            and route_valid
            and recovery_receipt.get("checkpoint_restored") is True
        )
        return {
            "schema": "zyra.live-post-recovery-verification/v1",
            "receipt_id": new_identity("post-recovery-verification"),
            "event_id": new_identity("post-recovery-verification-event"),
            "valid": valid,
            "input_digest": input_digest,
            "state_digest": state_digest,
            "checkpoint_valid": checkpoint_valid,
            "route_valid": route_valid,
            "route_id": str(current_route.get("route_id") or ""),
            "injection_id": injection.injection_id,
            "owner": "DeterministicLiveRecoveryVerifier",
            "verified_at": now_iso(),
        }

    # ------------------------------------------------------------------
    # Internal owner projections
    # ------------------------------------------------------------------

    def _require_state(self, context: Mapping[str, Any]) -> TaskState:
        if self.state is None:
            raise LiveOwnerIntegrationError("live task owner is not initialized")
        run_id = str(context.get("run_id") or self.state.run_id)
        task_id = str(context.get("task_id") or self.state.task_id)
        if run_id != self.state.run_id or task_id != self.state.task_id:
            raise LiveOwnerIntegrationError("live owner context identity mismatch")
        return self.state

    def _current_route(self) -> dict[str, Any]:
        if not self._route_history:
            return {}
        return dict(self._route_history[-1])

    def _acquire_pool_route(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        reason: str,
        required_capabilities: Sequence[str],
        excluded_route_ids: Sequence[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        from . import main as api_main

        if self.state is None:
            raise LiveOwnerIntegrationError("route requested before task initialization")
        pool_api = api_main.get_worker_pool_api()
        for index in range(0, 8):
            pool_api.ensure_default_local_worker(
                worker_id=f"live-scenario-worker-{index + 1}"
            )
        if self._route_history:
            pool_api.finalize_task(
                self.state,
                success=False,
                summary=f"route replaced after {reason}",
            )
        excluded_workers = {
            self._route_worker[item]
            for item in excluded_route_ids
            if item in self._route_worker
        }
        if self._route_history:
            excluded_workers.add(
                str(self._route_history[-1].get("worker_id") or "")
            )
        selected_worker = ""
        for _ in range(8):
            self._worker_cursor = (self._worker_cursor % 8) + 1
            candidate = f"live-scenario-worker-{self._worker_cursor}"
            if candidate not in excluded_workers:
                selected_worker = candidate
                break
        if not selected_worker:
            raise LiveOwnerIntegrationError(
                "worker pool has no non-excluded live scenario worker"
            )
        supported = {
            "agent_task",
            "artifact_return",
            "code_execution",
            "local_execution",
        }
        requested = tuple(str(item) for item in required_capabilities if str(item))
        effective = tuple(item for item in requested if item in supported)
        if "agent_task" not in effective:
            effective = ("agent_task", *effective)
        acquisition = pool_api.acquire_for_task(
            self.state,
            payload={
                "required_capabilities": list(dict.fromkeys(effective)),
                "locations": ["local"],
                "preferred_worker_ids": [selected_worker],
                "excluded_worker_ids": sorted(excluded_workers),
                "idempotency_key": idempotency_key,
                "ttl_seconds": 3600.0,
                "scenario_run_id": scenario_run_id,
            },
        )
        api_main.get_store().save_checkpoint(self.state)
        route_id = (
            f"worker-pool-route:{acquisition.attempt.attempt_id}:"
            f"{acquisition.lease.backend_id}"
        )
        route = {
            "schema": "zyra.live-scheduler-route/v1",
            "route_id": route_id,
            "lease_id": acquisition.lease.lease_id,
            "worker_id": acquisition.worker.worker_id,
            "backend_id": acquisition.lease.backend_id,
            "provider_id": (
                self.configuration.profile.provider_id
                if self.configuration is not None
                else "managed-provider"
            ),
            "receipt_id": acquisition.attempt.attempt_id,
            "tier": "device",
            "location": "device",
            "reason": reason,
            "requested_capabilities": list(requested),
            "leased_capabilities": list(effective),
            "privacy_class": domain_input.privacy_class.value,
            "maximum_latency_ms": domain_input.maximum_latency_ms,
            "maximum_cost_usd": domain_input.maximum_cost_usd,
            "fence_token_digest": hashlib.sha256(
                acquisition.lease.fence_token.encode("utf-8")
            ).hexdigest(),
            "fence_epoch": acquisition.lease.fence_epoch,
            "attempt_number": acquisition.attempt.attempt_number,
            "simulated": False,
            "owner": "WorkerPoolFoundationRuntime",
            "acquired_at": now_iso(),
        }
        self._route_history.append(route)
        self._route_worker[route_id] = acquisition.worker.worker_id
        return dict(route)

    def _apply_recovered_state(
        self,
        injection: FaultInjection,
        *,
        fault_receipt: Mapping[str, Any],
        recovery_event_ids: Sequence[str],
        scenario_state: Mapping[str, Any],
    ) -> None:
        from . import main as api_main

        if self.state is None:
            raise LiveOwnerIntegrationError("recovered state is unavailable")
        state = self.state
        causation_id = str(
            recovery_event_ids[-1]
            if recovery_event_ids
            else fault_receipt.get("event_id")
            or ""
        )
        if injection.kind == "requirement_change":
            event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                event_type=EventType.REQUIREMENT_CHANGE,
                payload={
                    "requirement": str(
                        injection.payload.get("requirement")
                        or "preserve deterministic live evidence"
                    ),
                    "fault_injection": injection.to_dict(),
                    "causation_id": causation_id,
                },
            )
            apply_requirement_change(state, event)
            requirement = str(event.payload["requirement"])
            if requirement not in state.constraints.requirements:
                state.constraints.requirements.append(requirement)
        else:
            event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                event_type=EventType.FAILURE_INJECTED,
                payload={
                    "failure_kind": injection.kind,
                    "node_id": injection.target or state.root_node_id,
                    "metadata": injection.to_dict(),
                    "causation_id": causation_id,
                },
            )
            apply_failure_injection(state, event)
        state.status = PlanNodeStatus.PENDING
        state.updated_at = now_iso()
        history = state.metadata.setdefault("live_recovery_history", [])
        history.append(
            {
                "injection_id": injection.injection_id,
                "fault_kind": injection.kind,
                "fault_event_id": fault_receipt.get("event_id"),
                "recovery_event_ids": list(recovery_event_ids),
                "input_digest": scenario_state.get("input_digest"),
                "checkpoint_restored": True,
                "recorded_at": now_iso(),
            }
        )
        api_main.get_store().save_checkpoint(state)


def _source_commit(project_root: Path) -> str:
    command = [
        "git",
        "-C",
        str(project_root),
        "rev-parse",
        "HEAD",
    ]
    result = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        ),
    )
    selected = result.stdout.strip()
    if result.returncode != 0 or len(selected) != 40:
        raise LiveOwnerIntegrationError(
            "formal live archive requires an exact source commit"
        )
    return selected


def _fault_target(
    mapped_kind: str,
    *,
    injection: FaultInjection,
    state: TaskState,
    current_route: Mapping[str, Any],
) -> dict[str, Any]:
    base = {
        "session_id": str(state.metadata.get("query_session_id") or ""),
        "node_id": state.root_node_id,
        "source_state_revision": max(1, len(state.metadata.get("live_recovery_history") or ()) + 1),
    }
    if mapped_kind == "worker_lost":
        return {
            **base,
            "worker_id": str(
                current_route.get("worker_id")
                or injection.target
                or "live-worker"
            ),
        }
    if mapped_kind == "model_failure":
        return {
            **base,
            "provider_id": str(injection.target or "live-provider"),
        }
    return {
        **base,
        "tool_call_id": f"live-tool:{injection.injection_id}",
        "tool_name": "live-scenario-boundary",
    }


def _recovery_event_ids(body: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for item in body.get("owner_receipts") or ():
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        ref = payload.get("canonical_ref")
        ref = ref if isinstance(ref, Mapping) else {}
        event_id = str(
            ref.get("event_id")
            or payload.get("event_id")
            or item.get("receipt_ref")
            or ""
        )
        if event_id:
            values.append(event_id)
    receipt = body.get("receipt")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    if not values and receipt.get("resume_token"):
        values.append(str(receipt["resume_token"]))
    if not values:
        raise LiveOwnerIntegrationError(
            "checkpoint resume owner returned no recovery event identity"
        )
    return tuple(dict.fromkeys(values))


def _artifact_kind(
    source: Path,
    metadata: Mapping[str, Any],
) -> ArtifactKind:
    role = str(metadata.get("artifact_role") or "")
    if role in {"research-report", "causal-archive"}:
        return ArtifactKind.REPORT
    if source.suffix.casefold() in {".json", ".jsonl", ".csv"}:
        return ArtifactKind.STRUCTURED_DATA
    if source.suffix.casefold() in {".py", ".ts", ".tsx", ".diff", ".patch"}:
        return ArtifactKind.CODE
    if source.suffix.casefold() in {".md", ".txt"}:
        return ArtifactKind.TEXT
    return ArtifactKind.FILE


def _require_api_response(
    response: Any,
    *,
    expected: Sequence[HTTPStatus],
    operation: str,
) -> dict[str, Any]:
    if response is None:
        raise LiveOwnerIntegrationError(f"{operation} route is unavailable")
    if response.status not in set(expected):
        raise LiveOwnerIntegrationError(
            f"{operation} failed with {int(response.status)}: "
            f"{json.dumps(dict(response.body), ensure_ascii=False, sort_keys=True)}"
        )
    return dict(response.body)


def _canonical_checkpoint_head(
    recovery_view: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if "checkpoint_head" not in recovery_view:
        raise LiveOwnerIntegrationError(
            "canonical recovery view is missing checkpoint_head"
        )
    checkpoint_head = recovery_view["checkpoint_head"]
    if checkpoint_head is None:
        return None
    if not isinstance(checkpoint_head, Mapping):
        raise LiveOwnerIntegrationError(
            "canonical recovery checkpoint_head must be a mapping or null"
        )
    return checkpoint_head


def _checkpoint_lineage(
    *,
    checkpoint_head: Mapping[str, Any] | None,
    scenario_run_id: str,
    configuration_digest: Any,
    fallback_session_id: str,
) -> tuple[str, str]:
    if checkpoint_head is not None:
        session_id = str(checkpoint_head.get("session_id") or "")
        workflow_signature = str(
            checkpoint_head.get("workflow_signature") or ""
        )
        if not session_id or not workflow_signature:
            raise LiveOwnerIntegrationError(
                "canonical recovery checkpoint lineage is incomplete"
            )
        return session_id, workflow_signature
    if not fallback_session_id:
        raise LiveOwnerIntegrationError(
            "initial recovery checkpoint lineage requires a session identity"
        )
    return fallback_session_id, digest(
        {
            "scenario": scenario_run_id,
            "configuration": configuration_digest,
        }
    )


__all__ = [
    "CanonicalLiveScenarioOwners",
    "LiveOwnerIntegrationError",
    "execute_live_owner_chain",
]
