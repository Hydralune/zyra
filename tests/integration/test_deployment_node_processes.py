from __future__ import annotations

import json
import socket
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil

from zyra_orchestration.deployment.dispatch import DeploymentDispatchRuntime
from zyra_orchestration.deployment.handoff import CheckpointHandoffRuntime
from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    DispatchStatus,
    LifecycleStatus,
    Sensitivity,
    Workload,
    new_id,
    now_iso,
)
from zyra_orchestration.deployment.orchestrator import DeploymentOrchestrator
from zyra_orchestration.deployment.placement import (
    PlacementContext,
    PlacementPolicyRuntime,
)
from zyra_orchestration.deployment.process_manager import DeploymentProcessManager
from zyra_orchestration.deployment.profiles import ProfileCatalog
from zyra_orchestration.deployment.recovery import DeploymentRecoveryRuntime
from zyra_orchestration.deployment.state_store import DeploymentStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port_block(count: int = 3) -> int:
    for base in range(34000, 48000, count):
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                item.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                item.bind(("127.0.0.1", port))
                sockets.append(item)
            return base
        except OSError:
            pass
        finally:
            for item in sockets:
                item.close()
    raise RuntimeError("could not find a free consecutive port block")


def _workload(
    profile: DeploymentProfile,
    *,
    task_id: str,
    run_id: str,
) -> Workload:
    shared = {
        "workload_id": new_id(f"workload_{profile.value}"),
        "task_id": task_id,
        "run_id": run_id,
        "latency_sla_ms": 5_000,
        "cpu_units": 1,
        "memory_mb": 32,
        "provider_required": False,
        "idempotency_key": new_id(f"idempotency_{profile.value}"),
    }
    if profile is DeploymentProfile.DEVICE:
        return Workload(
            **shared,
            operation="deterministic-transform",
            payload={"items": ["b", "a", "a"]},
            sensitivity=Sensitivity.RESTRICTED,
            complexity=2,
            required_capabilities=("local-compute",),
        )
    if profile is DeploymentProfile.EDGE:
        return Workload(
            **shared,
            operation="analyze-text",
            payload={"text": "edge edge semantic deployment"},
            sensitivity=Sensitivity.CONFIDENTIAL,
            complexity=5,
            required_capabilities=("edge-compute",),
        )
    return Workload(
        **shared,
        operation="hash-manifest",
        payload={"entries": {"mode": "cloud", "workload": "high-complexity"}},
        sensitivity=Sensitivity.PUBLIC,
        complexity=10,
        required_capabilities=("cloud-compute",),
    )


def test_three_real_profiles_dispatch_recover_and_restart(tmp_path: Path) -> None:
    base_port = _free_port_block()
    state_root = tmp_path / "deployment-state"
    catalog = ProfileCatalog.defaults(
        PROJECT_ROOT,
        host="127.0.0.1",
        base_port=base_port,
        environment={},
    )
    store = DeploymentStateStore(state_root / "deployment.sqlite3")
    manager = DeploymentProcessManager(
        project_root=PROJECT_ROOT,
        state_root=state_root,
        store=store,
    )
    clients = {}
    original_pids: list[int] = []

    try:
        for profile in DeploymentProfile:
            record, client, health = manager.start_node(catalog.policy(profile))
            clients[profile] = client
            original_pids.append(record.pid)
            assert record.pid != psutil.Process().pid
            assert health["profile"] == profile.value
            assert health["credential_values_exposed"] is False

        assert len(set(original_pids)) == 3
        semantic = {
            profile: client.semantic_readiness()
            for profile, client in clients.items()
        }
        assert all(item["ready"] is True for item in semantic.values())
        cloud_health = clients[DeploymentProfile.CLOUD].health()
        if any(cloud_health["credential_presence"].values()):
            assert semantic[DeploymentProfile.CLOUD]["warnings"] == []
        else:
            assert cloud_health["warnings"] == ["cloud_credential_missing"]
            assert semantic[DeploymentProfile.CLOUD]["status"] in {
                "ready",
                "degraded",
            }

        observations = {
            profile: client.observation()
            for profile, client in clients.items()
        }
        assert len({item.pid for item in observations.values()}) == 3
        assert all(item.network["in_process"] is False for item in observations.values())

        placement = PlacementPolicyRuntime(catalog, store, environment={})
        dispatch = DeploymentDispatchRuntime(store)
        task_id = new_id("task")
        run_id = new_id("run")
        workloads = {
            profile: _workload(profile, task_id=task_id, run_id=run_id)
            for profile in DeploymentProfile
        }
        receipts = {}
        for expected_profile, workload in workloads.items():
            decision = placement.decide(
                workload,
                PlacementContext(observations=observations),
            )
            assert decision.selected_profile is expected_profile
            receipt = dispatch.dispatch(
                workload,
                decision,
                clients[expected_profile],
            )
            receipts[expected_profile] = receipt
            assert receipt.status is DispatchStatus.SUCCEEDED
            assert receipt.profile is expected_profile
            assert receipt.artifact_refs
            assert receipt.checkpoint_ref

            replay = dispatch.dispatch(
                workload,
                decision,
                clients[expected_profile],
            )
            assert replay.attempt_id == receipt.attempt_id
            assert replay.result_digest == receipt.result_digest

        edge_receipt = receipts[DeploymentProfile.EDGE]
        clients[DeploymentProfile.EDGE].inject_fault(
            {"kind": "network-loss", "enabled": True}
        )
        failed_edge = replace(
            edge_receipt,
            status=DispatchStatus.FAILED,
            failure_code="node_network_unavailable",
            completed_at=now_iso(),
        )
        recovery = DeploymentRecoveryRuntime(
            store=store,
            placement=placement,
            dispatch=dispatch,
            handoff=CheckpointHandoffRuntime(
                store,
                canonical_verifier=lambda **_: {"ready": True},
            ),
        ).recover(
            workload=workloads[DeploymentProfile.EDGE],
            failed_receipt=failed_edge,
            observations=observations,
            clients=clients,
            source_client=clients[DeploymentProfile.EDGE],
            unavailable_profiles=frozenset({DeploymentProfile.EDGE}),
        )

        assert recovery.target_profile is not DeploymentProfile.EDGE
        assert recovery.receipt.status is DispatchStatus.SUCCEEDED
        assert recovery.receipt.predecessor_attempt_id == edge_receipt.attempt_id
        assert recovery.handoff is not None
        assert recovery.handoff.verified is True
        assert recovery.handoff.source_checksum
        assert recovery.handoff.import_checksum
        clients[DeploymentProfile.EDGE].inject_fault({"kind": "clear"})

        old_cloud_pid = observations[DeploymentProfile.CLOUD].pid
        clients[DeploymentProfile.CLOUD].shutdown()
        deadline = time.monotonic() + 10
        crashed = None
        while time.monotonic() < deadline:
            crashed = manager.status("profile:cloud")
            if crashed is not None and crashed.status is LifecycleStatus.CRASHED:
                break
            time.sleep(0.1)
        assert crashed is not None
        assert crashed.status is LifecycleStatus.CRASHED

        restarted, restarted_client, health = manager.start_node(
            catalog.policy(DeploymentProfile.CLOUD),
            restart=True,
        )
        clients[DeploymentProfile.CLOUD] = restarted_client
        assert restarted.pid != old_cloud_pid
        assert restarted.restart_count >= 1
        assert health["profile"] == "cloud"
        assert restarted_client.semantic_readiness()["ready"] is True

        events = store.events(limit=10_000)
        event_types = {event["event_type"] for event in events}
        assert "deployment.dispatch_effect_verified" in event_types
        assert "deployment.checkpoint_handoff_completed" in event_types
        assert "deployment.recovery_completed" in event_types
        assert store.integrity_report()["ready"] is True
    finally:
        manager.stop_all(timeout_seconds=5)

    assert all(not psutil.pid_exists(pid) for pid in original_pids)


def test_canonical_checkpoint_owner_retries_transient_projection_visibility() -> None:
    class EventuallyConsistentApi:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _path: str) -> dict[str, object]:
            self.calls += 1
            if self.calls < 3:
                return {
                    "task": {
                        "task_id": "task-stale",
                        "run_id": "run-stale",
                    }
                }
            return {
                "task": {
                    "task_id": "task-canonical",
                    "run_id": "run-canonical",
                }
            }

    api = EventuallyConsistentApi()
    owner = SimpleNamespace(api=api)

    receipt = DeploymentOrchestrator._canonical_checkpoint_ready(
        owner,
        task_id="task-canonical",
        run_id="run-canonical",
        checkpoint_ref="checkpoint-canonical",
    )

    assert receipt["ready"] is True
    assert api.calls == 3
    assert [attempt["ready"] for attempt in receipt["attempts"]] == [
        False,
        False,
        True,
    ]


def test_product_supervisor_api_web_and_control_surface_start_from_clean_state() -> None:
    (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
    base_port = _free_port_block(5)
    with tempfile.TemporaryDirectory(
        prefix="m3-s02b01-product-",
        dir=PROJECT_ROOT / ".tmp",
    ) as selected:
        state_root = Path(selected)
        environment = {
            "ZYRA_DEPLOYMENT_PROFILE_BASE_PORT": str(base_port),
            "ZYRA_API_PORT": str(base_port + 3),
            "ZYRA_WEB_PORT": str(base_port + 4),
        }
        orchestrator = DeploymentOrchestrator(
            PROJECT_ROOT,
            state_root=state_root,
            environment=environment,
            profile_base_port=base_port,
            api_port=base_port + 3,
            web_port=base_port + 4,
        )
        started = None
        try:
            started = orchestrator.start(build_web=False)
            assert started["ready"] is True
            assert started["default_configuration"] is True
            assert started["root_source_runtime_dependency"] is False

            status = orchestrator.status()
            assert status["ready"] is True
            assert status["status"] == "ready"
            assert status["web"]["reachable"] is True
            assert status["api"]["reachable"] is True

            profiles = orchestrator.api.get("/deployment/profiles")
            assert profiles["ready"] is True
            assert len(profiles["catalog"]["profiles"]) == 3

            dispatch = orchestrator.api.post(
                "/deployment/dispatch",
                {
                    "workload": {
                        "task_id": new_id("task"),
                        "run_id": new_id("run"),
                        "operation": "deterministic-transform",
                        "payload": {"items": ["z", "a", "z"]},
                        "sensitivity": "restricted",
                        "complexity": 2,
                        "latency_sla_ms": 5_000,
                        "cpu_units": 1,
                        "memory_mb": 32,
                        "required_capabilities": ["local-compute"],
                    }
                },
                accepted_statuses=(201,),
                idempotency_key=new_id("api-dispatch"),
            )
            assert dispatch["ready"] is True
            assert dispatch["decision"]["selected_profile"] == "device"
            assert dispatch["receipt"]["artifact_refs"]

            events = orchestrator.api.get(
                "/deployment/events",
                query={"event_types": "deployment.dispatch_effect_verified"},
            )
            assert len(events["events"]) == 1
            assert events["events"][0]["profile"] == "device"

            doctor = orchestrator.api.post(
                "/deployment/doctor",
                {
                    "checks": [
                        "runtime",
                        "dependencies",
                        "configuration",
                        "state-store",
                        "ports",
                        "source-boundary",
                        "process-boundary",
                        "partial-startup",
                    ]
                },
                accepted_statuses=(200, 503),
                timeout_seconds=60,
            )
            assert doctor["ready"] is True, doctor

            semantic = orchestrator.semantic_health(
                include_short_task=True,
                fresh_state=True,
            )
            short_probe = next(
                (
                    probe
                    for probe in semantic.get("probes") or ()
                    if probe.get("probe_id") == "short-task"
                ),
                {},
            )
            short_observation = short_probe.get("observations") or {}
            short_details = short_observation.get("details") or {}
            short_receipt = short_details.get("receipt") or {}
            assert semantic["ready"] is True, json.dumps(
                {
                    "blockers": semantic.get("blockers"),
                    "warnings": semantic.get("warnings"),
                    "short_task_failed_assertions": short_details.get(
                        "failed_assertions"
                    ),
                    "short_task_rejections": [
                        assertion
                        for assertion in short_receipt.get("assertions") or ()
                        if assertion.get("accepted") is not True
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            assert semantic["status"] in {"ready", "degraded"}
            assert semantic["short_task_included"] is True
            assert semantic["target_commit"]
            assert semantic["clean_state"]["verified"] is True
            assert orchestrator.store.latest_probe_report()["report_id"] == semantic[
                "report_id"
            ]
        finally:
            if started is not None:
                stopped = orchestrator.stop()
                assert stopped["ready"] is True
                assert stopped["remaining_active"] == []
