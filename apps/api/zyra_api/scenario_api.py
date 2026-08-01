from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from zyra_evaluation.scenario_runner import (
    CallbackScenarioExecutionPort,
    OwnerExecutionResult,
    ScenarioRunnerApi,
    ScenarioRunnerService,
    ScenarioRunStore,
)


_LOCK = threading.RLock()
_API: ScenarioRunnerApi | None = None
_KEY: tuple[str, str, str] | None = None


def get_scenario_runner_api() -> ScenarioRunnerApi:
    from . import main as api_main

    global _API, _KEY
    state_path = api_main.sqlite_path().with_name(
        f"{api_main.sqlite_path().stem}.scenario-runs.sqlite3"
    ).resolve()
    artifact_root = api_main.artifact_root_path().resolve()
    project_root = api_main.PROJECT_ROOT.resolve()
    key = (str(state_path), str(artifact_root), str(project_root))
    with _LOCK:
        if _API is not None and _KEY == key:
            return _API
        if _API is not None:
            _API.service.close(wait=False)
        store = ScenarioRunStore(state_path)
        service = ScenarioRunnerService(
            project_root=project_root,
            store=store,
            execution=CallbackScenarioExecutionPort(_execute_owner_chain),
            artifact_root=artifact_root,
            default_preflight_paths={
                "database": str(api_main.sqlite_path().resolve()),
                "cache": str(state_path.parent / "scenario-clean-cache"),
                "index": str(state_path.parent / "scenario-clean-index"),
                "artifact": str(artifact_root),
                "build": str(state_path.parent / "scenario-clean-build"),
            },
            maximum_workers=2,
            auto_reconcile=True,
        )
        _API = ScenarioRunnerApi(service)
        _KEY = key
        return _API


def reset_scenario_runner_api(*, wait: bool = False) -> None:
    global _API, _KEY
    with _LOCK:
        selected = _API
        _API = None
        _KEY = None
    if selected is not None:
        selected.service.close(wait=wait)


def _execute_owner_chain(
    *,
    scenario_run_id: str,
    configuration: Any,
    goal: str,
    policy_decisions: tuple[dict[str, Any], ...],
    cancel_requested: Any,
) -> OwnerExecutionResult:
    from zyra_evaluation.scenario_runner.dual_domain import LIVE_SCENARIO_IDS

    if configuration.scenario_id in LIVE_SCENARIO_IDS:
        from .live_scenario_owners import execute_live_owner_chain

        return execute_live_owner_chain(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            goal=goal,
            policy_decisions=policy_decisions,
            cancel_requested=cancel_requested,
        )

    from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
    from zyra_runtime import LocalArtifactStore
    from zyra_symbolic import apply_failure_injection, apply_requirement_change

    from . import main as api_main

    started_at = api_main.now_iso()
    store = api_main.get_store()
    state, created_event = api_main.make_task_created_event(goal)
    state.metadata.update(
        {
            "scenario_run_id": scenario_run_id,
            "scenario_configuration_digest": configuration.configuration_digest,
            "scenario_definition_digest": configuration.definition_digest,
            "scenario_input_digest": configuration.input_digest,
            "scenario_seed": configuration.seed,
            "scenario_profile": configuration.profile.to_dict(),
            "sealed": configuration.mode.value == "sealed",
            "sealed_autonomous": configuration.mode.value == "sealed",
            "formal_benchmark": configuration.mode.value == "sealed",
            "competition_mode": (
                "sealed_autonomous"
                if configuration.mode.value == "sealed"
                else configuration.mode.value
            ),
            "human_intervention_count": 0,
            "policy_digest": configuration.policy.policy_digest,
            "policy_decision_ids": [
                str(item.get("decision_id") or "") for item in policy_decisions
            ],
        }
    )
    session_id = f"scenario:{scenario_run_id}"
    state.metadata["query_session_id"] = session_id
    workspace = api_main.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=session_id,
        worker_id="scenario-runner",
        idempotency_key=f"scenario-workspace:{scenario_run_id}",
        causation_id=created_event.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    graph_events = api_main.ensure_default_graph(state)
    events: list[Any] = [
        created_event,
        *graph_events,
        *api_main.drain_workspace_events(state.task_id),
    ]
    pool_api = api_main.get_worker_pool_api()
    # Resolve the production composition root before taking the task lease.
    # The resolver replaces the logical bootstrap registration with the
    # deployment-node-backed physical worker.  Acquiring first would leave a
    # live lease on that stale generation and correctly make replacement fail
    # closed.
    execution_context = api_main.graph_execution_context()
    pool_journal = pool_api.pool.store.journal(limit=10000)
    pool_sequence = pool_journal[-1].sequence if pool_journal else 0
    pool_api.acquire_for_task(
        state,
        payload={
            "profile_id": configuration.profile.profile_id,
            "backend_id": configuration.profile.backend_id,
            "provider_id": configuration.profile.provider_id,
            "worker_classes": list(configuration.profile.worker_classes),
            "scenario_run_id": scenario_run_id,
        },
    )
    if cancel_requested():
        raise RuntimeError("scenario cancellation was requested before graph execution")
    events.extend(
        api_main.run_task_graph(
            state,
            execution_context=execution_context,
        )
    )
    pool_api.finalize_task(
        state,
        success=str(state.status) == "completed",
        summary=f"scenario owner graph finished with status {state.status}",
    )
    events.extend(
        event
        for event in pool_api.pool.events.project_after(
            pool_api.pool.store,
            pool_sequence,
        )
        if event.run_id == state.run_id and event.task_id == state.task_id
    )
    fault_receipts: list[dict[str, Any]] = []
    previous_event_id = events[-1].event_id if events else created_event.event_id
    for fault in configuration.faults:
        if fault.kind == "requirement_change":
            injection_event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.REQUIREMENT_CHANGE,
                node_id=state.root_node_id,
                payload={
                    "requirement": str(
                        fault.payload.get("requirement")
                        or "preserve causal evidence"
                    ),
                    "fault_injection": fault.to_dict(),
                    "causation_id": previous_event_id,
                },
            )
            events.append(injection_event)
            effects = apply_requirement_change(state, injection_event)
            events.extend(effects)
            previous_event_id = effects[-1].event_id if effects else injection_event.event_id
            fault_receipts.append(
                {
                    "receipt_id": f"receipt_{fault.injection_id}",
                    "event_type": "recovery_applied",
                    "semantic_effect": "recovery",
                    "stage": "recovery",
                    "node_id": state.root_node_id,
                    "causation_id": injection_event.event_id,
                    "created_at": api_main.now_iso(),
                    "fault": fault.to_dict(),
                    "effect_event_ids": [item.event_id for item in effects],
                }
            )
            continue
        injection_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={
                "failure_kind": fault.kind,
                "node_id": fault.target or state.root_node_id,
                "metadata": fault.to_dict(),
                "causation_id": previous_event_id,
            },
        )
        events.append(injection_event)
        effects = apply_failure_injection(state, injection_event)
        events.extend(effects)
        previous_event_id = effects[-1].event_id if effects else injection_event.event_id
        fault_receipts.extend(
            [
                {
                    "receipt_id": f"fault_{fault.injection_id}",
                    "event_type": "fault_contained",
                    "semantic_effect": "fault",
                    "stage": "fault",
                    "node_id": fault.target or state.root_node_id,
                    "causation_id": injection_event.event_id,
                    "created_at": api_main.now_iso(),
                    "fault": fault.to_dict(),
                    "effect_event_ids": [item.event_id for item in effects],
                },
                {
                    "receipt_id": f"recovery_{fault.injection_id}",
                    "event_type": "recovery_applied",
                    "semantic_effect": "recovery",
                    "stage": "recovery",
                    "node_id": fault.target or state.root_node_id,
                    "causation_id": injection_event.event_id,
                    "created_at": api_main.now_iso(),
                    "fault": fault.to_dict(),
                    "effect_event_ids": [item.event_id for item in effects],
                },
            ]
        )
    artifact_store = LocalArtifactStore(api_main.artifact_root_path())
    artifact = artifact_store.write_text(
        run_id=state.run_id,
        task_id=state.task_id,
        content=json.dumps(
            {
                "schema": "zyra.scenario-owner-settlement/v1",
                "scenario_run_id": scenario_run_id,
                "owner_run_id": state.run_id,
                "task_id": state.task_id,
                "configuration_digest": configuration.configuration_digest,
                "policy_digest": configuration.policy.policy_digest,
                "faults": [item.to_dict() for item in configuration.faults],
                "event_count_before_artifact": len(events),
                "human_intervention_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        title="Scenario owner settlement",
        kind=ArtifactKind.REPORT,
        extension=".json",
        producer_node_id=state.root_node_id,
        metadata={
            "schema": "zyra.scenario-owner-settlement/v1",
            "scenario_run_id": scenario_run_id,
            "producer_worker_id": "scenario-runner",
            "security_label": "internal",
        },
    )
    api_main._attach_artifacts(state, [artifact])
    artifact_event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.ARTIFACT_WRITTEN,
        node_id=state.root_node_id,
        payload={
            "artifact": to_jsonable(artifact),
            "causation_id": previous_event_id,
        },
    )
    events.append(artifact_event)
    api_main.persist_events(store, events)
    store.save_checkpoint(state)
    curator = api_main.curate_terminal_task(store, state)
    final_state = store.load_task(state.task_id) or state
    canonical_events = tuple(store.task_events(state.task_id))
    artifacts = tuple(to_jsonable(item) for item in final_state.artifacts)
    owner_receipts: list[dict[str, Any]] = [
        {
            "receipt_id": f"scheduler_{scenario_run_id}",
            "event_type": "backend_route",
            "semantic_effect": "route",
            "stage": "scheduler",
            "worker_id": "scenario-runner",
            "provider_id": configuration.profile.provider_id,
            "node_id": state.root_node_id,
            "causation_id": created_event.event_id,
            "created_at": api_main.now_iso(),
            "worker_pool": final_state.metadata.get("worker_pool") or {},
            "backend_route": final_state.metadata.get("backend_route") or {},
        },
        {
            "receipt_id": f"memory_{scenario_run_id}",
            "event_type": "memory_curator_committed",
            "semantic_effect": "memory",
            "stage": "memory",
            "worker_id": "Memory",
            "node_id": state.root_node_id,
            "causation_id": artifact_event.event_id,
            "created_at": api_main.now_iso(),
            "curator": curator or {"status": "no_candidate"},
        },
        *fault_receipts,
        {
            "receipt_id": f"artifact_{artifact.artifact_id}",
            "event_type": "artifact_committed",
            "semantic_effect": "artifact",
            "stage": "artifact",
            "node_id": state.root_node_id,
            "causation_id": artifact_event.event_id,
            "created_at": api_main.now_iso(),
            "artifact_id": artifact.artifact_id,
            "sha256": artifact.metadata.get("sha256"),
        },
        {
            "receipt_id": f"verify_{scenario_run_id}",
            "event_type": "verification",
            "semantic_effect": "verification",
            "stage": "verification",
            "worker_id": "Verifier",
            "node_id": state.root_node_id,
            "causation_id": artifact_event.event_id,
            "created_at": api_main.now_iso(),
            "checks": {
                "task_terminal": str(final_state.status) in {"completed", "failed"},
                "canonical_event_count": len(canonical_events),
                "artifact_count": len(artifacts),
                "human_intervention_count": 0,
            },
        },
    ]
    return OwnerExecutionResult(
        owner_run_id=state.run_id,
        task_id=state.task_id,
        task=to_jsonable(final_state),
        events=canonical_events,
        artifacts=artifacts,
        owner_receipts=tuple(owner_receipts),
        started_at=started_at,
        completed_at=api_main.now_iso(),
    )
