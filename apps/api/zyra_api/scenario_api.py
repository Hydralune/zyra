from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
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
# This worker executes inside the API process. A restarted API must register
# a fresh identity rather than heartbeat a persisted worker owned by an old PID.
_FOUNDATION_WORKER_ID = f"foundation-scenario-worker-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class _FoundationOwnerWorkerRun:
    worker_result: Any
    event_records: list[Any]


def get_scenario_runner_api() -> ScenarioRunnerApi:
    from . import main as api_main

    global _API, _KEY
    state_path = api_main.sqlite_path().with_name(
        f"{api_main.sqlite_path().stem}.scenario-runs.sqlite3"
    ).resolve()
    artifact_root = api_main.artifact_root_path().resolve()
    project_root = api_main.PROJECT_ROOT.resolve()
    # Formal (sealed) scenarios require a clean starting state.  The live API
    # database and artifact root always carry the running product's rows, so
    # pointing preflight at them would reject every sealed run.  The sealed
    # long-run runner instead verifies a dedicated scenario scratch area that
    # starts absent; mirror that here so a sealed scenario launched from the
    # workbench is admitted under the same contract.  The scratch paths are
    # derived from the scenario store so they stay beside this instance's state.
    scenario_scratch = state_path.parent / f"{state_path.stem}.clean"
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
                "database": str(scenario_scratch / "scenario.sqlite3"),
                "cache": str(scenario_scratch / "cache"),
                "index": str(scenario_scratch / "index"),
                "artifact": str(scenario_scratch / "artifacts"),
                "build": str(scenario_scratch / "build"),
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


def _run_leased_task_graph(
    *,
    api_main: Any,
    pool_api: Any,
    state: Any,
    payload: dict[str, Any],
    execution_context: Any,
    cancel_requested: Any,
) -> tuple[Any, ...]:
    """Execute one graph while making every acquired lease terminal."""

    lease_acquired = False
    graph_finished = False
    try:
        pool_api.acquire_for_task(state, payload=payload)
        lease_acquired = True
        if cancel_requested():
            raise RuntimeError(
                "scenario cancellation was requested before graph execution"
            )
        events = tuple(
            api_main.run_task_graph(
                state,
                execution_context=execution_context,
            )
        )
        graph_finished = True
        return events
    finally:
        if lease_acquired:
            pool_api.finalize_task(
                state,
                success=(
                    graph_finished and str(state.status) == "completed"
                ),
                summary=(
                    "scenario owner graph finished with status "
                    f"{state.status}"
                    if graph_finished
                    else "scenario owner graph aborted before completion"
                ),
            )


def _foundation_graph_execution_context(
    *,
    api_main: Any,
    configuration: Any,
    goal: str,
) -> Any:
    """Bind the sealed foundation scenario to its declared local runtime.

    The short owner-chain scenario is a deterministic control-plane exercise,
    not an open-ended user delivery.  Its registered profile explicitly says
    ``local-only-foundation`` and ``deterministic-owner-chain``.  Routing it
    through the production CodeWorker composition root would silently change
    that contract into a cloud-model workload and make a provider credential
    a prerequisite for a local scenario.

    This context still runs the canonical task graph, ResourceScheduler,
    GraphStateCustody-backed worker lease, and a real execution callback.  The
    callback validates and records the scenario contract; it does not claim to
    reason about or implement an arbitrary user goal.
    """

    from zyra_core import EventRecord, EventType, to_jsonable
    from zyra_orchestration import GraphExecutionContext
    from zyra_runtime import WorkerResult
    from zyra_scheduler import (
        ResourceLocation,
        ResourceScheduler,
        WorkerBackendKind,
        WorkerManifest,
        WorkerPool,
    )

    profile = configuration.profile
    declared = {
        "profile_id": str(profile.profile_id),
        "provider_id": str(profile.provider_id),
        "model_id": str(profile.model_id),
        "backend_id": str(profile.backend_id),
        "dispatch_claim": str(profile.metadata.get("dispatch_claim") or ""),
    }
    expected = {
        "profile_id": "foundation.local-sealed",
        "provider_id": "zyra-local",
        "model_id": "deterministic-owner-chain",
        "backend_id": "local-runtime",
        "dispatch_claim": "local-only-foundation",
    }
    if declared != expected or profile.metadata.get("edge_cloud_claim") is not False:
        raise RuntimeError(
            "foundation scenario execution profile is not the registered "
            "local deterministic contract"
        )

    manifest = WorkerManifest(
        worker_id=_FOUNDATION_WORKER_ID,
        display_name="Foundation scenario owner-chain runtime",
        runtime_worker="ScenarioOwnerChainRuntime",
        location=ResourceLocation.LOCAL,
        backend=WorkerBackendKind.LOCAL_PROCESS,
        capabilities=[
            "agent_task",
            "local_execution",
            "scenario-contract-validation",
            "canonical-owner-chain",
        ],
        tools=[],
        models=["deterministic-owner-chain"],
        sandbox="api-process-scenario-boundary",
        gateway="zyra_api.scenario_api",
        workspace_scope="scenario-workspace",
        privacy_level="sensitive_ok",
        max_concurrency=1,
        latency_ms=1,
        cost_per_1k_tokens=0.0,
        source_modules={
            "zyra": [
                "ScenarioRunnerService",
                "GraphStateCustody",
                "WorkerPoolFoundationRuntime",
            ]
        },
        metadata={
            "dispatch": "in-process deterministic scenario owner contract",
            "provider_reasoning_required": False,
            "scenario_only": True,
        },
    )
    scheduler = ResourceScheduler(WorkerPool((manifest,)))
    goal_digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()

    def topology_policy(state: Any, node: Any, cause_event: Any) -> dict[str, Any]:
        graph_ref = state.metadata.get("dynamic_graph_ref")
        if not isinstance(graph_ref, dict) or not str(graph_ref.get("graph_id") or ""):
            raise RuntimeError(
                "foundation scenario topology is missing its canonical graph commit"
            )
        return {
            "schema": "zyra.foundation-local-topology-policy/v1",
            "committed": True,
            "used_baseline": False,
            "scenario_only": True,
            "canonical_graph_ref": dict(graph_ref),
            "cause_event_id": str(getattr(cause_event, "event_id", "") or ""),
            "node_id": str(getattr(node, "node_id", "") or ""),
            "execution_receipt": {
                "actual_profile_id": profile.profile_id,
                "provider_reasoning_required": False,
            },
        }

    def execute(state: Any, node: Any) -> tuple[_FoundationOwnerWorkerRun, str]:
        worker_pool = state.metadata.get("worker_pool")
        workspace_ref = state.metadata.get("workspace_ref")
        checks = {
            "scenario_id_bound": (
                str(state.metadata.get("scenario_run_id") or "") != ""
            ),
            "configuration_digest_bound": (
                state.metadata.get("scenario_configuration_digest")
                == configuration.configuration_digest
            ),
            "definition_digest_bound": (
                state.metadata.get("scenario_definition_digest")
                == configuration.definition_digest
            ),
            "input_digest_bound": (
                state.metadata.get("scenario_input_digest")
                == configuration.input_digest
            ),
            "goal_digest_bound": (
                hashlib.sha256(state.user_goal.encode("utf-8")).hexdigest()
                == goal_digest
            ),
            "sealed_zero_human_contract": (
                state.metadata.get("sealed_autonomous") is True
                and state.metadata.get("human_intervention_count") == 0
            ),
            "workspace_bound": (
                isinstance(workspace_ref, dict)
                and bool(str(workspace_ref.get("workspace_id") or ""))
            ),
            "local_worker_lease_bound": (
                isinstance(worker_pool, dict)
                and worker_pool.get("worker_id") == _FOUNDATION_WORKER_ID
                and bool(str(worker_pool.get("lease_id") or ""))
            ),
        }
        if not all(checks.values()):
            failed = sorted(key for key, value in checks.items() if not value)
            raise RuntimeError(
                "foundation scenario execution contract failed: " + ",".join(failed)
            )
        receipt = {
            "schema": "zyra.foundation-owner-execution-receipt/v1",
            "scenario_run_id": str(state.metadata["scenario_run_id"]),
            "run_id": state.run_id,
            "task_id": state.task_id,
            "node_id": node.node_id,
            "worker_id": _FOUNDATION_WORKER_ID,
            "runtime_worker": "ScenarioOwnerChainRuntime",
            "profile_id": profile.profile_id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "backend_id": profile.backend_id,
            "provider_called": False,
            "provider_reasoning_required": False,
            "goal_digest": goal_digest,
            "configuration_digest": configuration.configuration_digest,
            "checks": checks,
            "semantic_effect": "state_mutation",
            "stage": "execute",
        }
        request_id = f"foundation-owner:{state.task_id}:{node.node_id}"
        result = WorkerResult(
            request_id=request_id,
            ok=True,
            summary=(
                "Validated the sealed local scenario contract and executed its "
                "canonical owner-chain boundary."
            ),
            events=[receipt],
            metadata={
                "runtime_worker": "ScenarioOwnerChainRuntime",
                "scenario_only": "true",
                "provider_called": "false",
                "goal_digest": goal_digest,
            },
        )
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                **receipt,
                "worker_result": to_jsonable(result),
            },
        )
        return (
            _FoundationOwnerWorkerRun(
                worker_result=result,
                event_records=[event],
            ),
            "ScenarioOwnerChainRuntime",
        )

    return GraphExecutionContext.from_paths(
        project_root=api_main.PROJECT_ROOT,
        workspace_root=api_main.tool_workspace_path(),
        artifact_root=api_main.artifact_root_path(),
        topology_policy_trigger=topology_policy,
        resource_scheduler=scheduler,
        physical_execution_runner=execute,
    )


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
    api_main.persist_events(store, [created_event])
    events: list[Any] = [
        *graph_events,
        *api_main.drain_workspace_events(state.task_id),
    ]
    pool_api = api_main.get_worker_pool_api()
    # The short foundation profile is deliberately local and deterministic.
    # Give it a dedicated API-owned worker identity so a prior production task
    # cannot leave the legacy ``local-code-worker`` id bound to a cloud node.
    pool_api.ensure_default_local_worker(worker_id=_FOUNDATION_WORKER_ID)
    execution_context = _foundation_graph_execution_context(
        api_main=api_main,
        configuration=configuration,
        goal=goal,
    )
    if configuration.mode.value == "sealed":
        api_main.prepare_phase2_loopx_pre_control(
            state,
            causation_id=created_event.event_id,
        )
    pool_journal = pool_api.pool.store.journal(limit=10000)
    pool_sequence = pool_journal[-1].sequence if pool_journal else 0
    events.extend(
        _run_leased_task_graph(
            api_main=api_main,
            pool_api=pool_api,
            state=state,
            payload={
                "profile_id": configuration.profile.profile_id,
                "backend_id": configuration.profile.backend_id,
                "provider_id": configuration.profile.provider_id,
                "worker_classes": list(configuration.profile.worker_classes),
                "scenario_run_id": scenario_run_id,
                "required_capabilities": ["agent_task", "local_execution"],
                "locations": ["local"],
                "preferred_worker_ids": [_FOUNDATION_WORKER_ID],
                "idempotency_key": f"foundation-scenario-lease:{scenario_run_id}",
            },
            execution_context=execution_context,
            cancel_requested=cancel_requested,
        )
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
