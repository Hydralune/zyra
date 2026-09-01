from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from zyra_orchestration.deployment.dispatch import DeploymentDispatchRuntime
from zyra_orchestration.deployment.errors import ProcessUnavailable
from zyra_orchestration.deployment.handoff import CheckpointHandoffRuntime
from zyra_orchestration.deployment.http_client import ProductHttpClient
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
from zyra_orchestration.deployment.semantic_health import SemanticHealthRuntime
from zyra_orchestration.deployment.state_store import DeploymentStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _SequencedReadinessApi:
    def __init__(self, values: list[Mapping[str, Any] | BaseException]) -> None:
        self.values = list(values)
        self.calls = 0

    def get(self, path: str) -> dict[str, Any]:
        assert path == "/runtime/readiness"
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, BaseException):
            raise value
        return dict(value)


def _semantic_runtime(api: _SequencedReadinessApi) -> SemanticHealthRuntime:
    runtime = object.__new__(SemanticHealthRuntime)
    runtime.api = api
    runtime._runtime_readiness_lock = threading.RLock()
    runtime._runtime_readiness_cache = None
    runtime._runtime_readiness_attempts = ()
    return runtime


def test_deployment_node_exits_when_its_supervisor_dies_abruptly(
    tmp_path: Path,
) -> None:
    helper = "\n".join(
        (
            "import json, os, sys",
            "from pathlib import Path",
            "from zyra_orchestration.deployment import DeploymentOrchestrator, DeploymentProfile",
            "root = Path(sys.argv[1]).resolve()",
            "orchestrator = DeploymentOrchestrator(root)",
            "policy = orchestrator.catalog.policy(DeploymentProfile.DEVICE)",
            "record, client, health = orchestrator.processes.start_node(policy)",
            "print(json.dumps({'pid': record.pid, 'create_time': record.process_create_time, 'port': policy.port}), flush=True)",
            "os._exit(0)",
        )
    )
    environment = dict(os.environ)
    environment["ZYRA_STATE_ROOT"] = str(
        (tmp_path / "abrupt-supervisor-state").resolve()
    )
    environment["ZYRA_DEPLOYMENT_PROFILE_BASE_PORT"] = str(
        _free_port_block()
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            helper,
            str(PROJECT_ROOT),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=45)
    assert process.returncode == 0, stderr
    identity = json.loads(stdout.strip().splitlines()[-1])
    node_pid = int(identity["pid"])
    node_create_time = float(identity["create_time"])
    port = int(identity["port"])

    def same_node_alive() -> bool:
        try:
            observed = psutil.Process(node_pid)
            return bool(
                observed.is_running()
                and observed.status() != psutil.STATUS_ZOMBIE
                and abs(observed.create_time() - node_create_time) < 0.01
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    deadline = time.monotonic() + 15
    while same_node_alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        assert same_node_alive() is False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        if same_node_alive():
            observed = psutil.Process(node_pid)
            observed.terminate()
            try:
                observed.wait(timeout=5)
            except psutil.TimeoutExpired:
                observed.kill()


def test_runtime_readiness_retries_retryable_transport_reset_and_caches(
    monkeypatch,
) -> None:
    api = _SequencedReadinessApi(
        [
            ProcessUnavailable(
                "product_http_unreachable",
                "connection reset",
                retryable=True,
            ),
            {"ready": True, "canonical_owners": {"domains": []}},
        ]
    )
    runtime = _semantic_runtime(api)
    monkeypatch.setattr(
        "zyra_orchestration.deployment.semantic_health.time.sleep",
        lambda _seconds: None,
    )

    first = runtime._runtime_readiness()
    second = runtime._runtime_readiness()

    assert first == second
    assert first["ready"] is True
    assert api.calls == 2
    assert runtime._runtime_readiness_attempts == (
        {
            "attempt": 1,
            "ready": False,
            "error": "product_http_unreachable",
            "retryable": True,
        },
        {
            "attempt": 2,
            "ready": True,
            "error": "",
            "retryable": False,
        },
    )
    observation = runtime._probe_runtime_owners()
    assert observation["transport_retried"] is True
    assert observation["transport_attempts"] == list(
        runtime._runtime_readiness_attempts
    )


def test_runtime_readiness_does_not_retry_non_retryable_failure(
    monkeypatch,
) -> None:
    error = ProcessUnavailable(
        "runtime_owner_contract_invalid",
        "invalid readiness contract",
        retryable=False,
    )
    api = _SequencedReadinessApi([error])
    runtime = _semantic_runtime(api)
    monkeypatch.setattr(
        "zyra_orchestration.deployment.semantic_health.time.sleep",
        lambda _seconds: pytest.fail("non-retryable failure must not sleep"),
    )

    with pytest.raises(ProcessUnavailable) as caught:
        runtime._runtime_readiness()

    assert caught.value is error
    assert api.calls == 1
    assert error.details["runtime_readiness_attempts"] == [
        {
            "attempt": 1,
            "ready": False,
            "error": "runtime_owner_contract_invalid",
            "retryable": False,
        }
    ]


def test_runtime_readiness_exhausts_exactly_three_retryable_attempts(
    monkeypatch,
) -> None:
    errors = [
        ProcessUnavailable(
            "product_http_unreachable",
            f"connection reset {attempt}",
            retryable=True,
        )
        for attempt in range(1, 4)
    ]
    api = _SequencedReadinessApi(errors)
    runtime = _semantic_runtime(api)
    monkeypatch.setattr(
        "zyra_orchestration.deployment.semantic_health.time.sleep",
        lambda _seconds: None,
    )

    with pytest.raises(ProcessUnavailable) as caught:
        runtime._runtime_readiness()

    assert caught.value is errors[-1]
    assert api.calls == 3
    assert [
        attempt["attempt"]
        for attempt in errors[-1].details["runtime_readiness_attempts"]
    ] == [1, 2, 3]


def test_product_http_client_marks_connection_reset_retryable(
    monkeypatch,
) -> None:
    class ResetConnection:
        closed = False

        def request(self, *_args, **_kwargs) -> None:
            raise ConnectionResetError(10054, "connection reset")

        def close(self) -> None:
            self.closed = True

    connection = ResetConnection()
    monkeypatch.setattr(
        "zyra_orchestration.deployment.http_client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ProcessUnavailable) as caught:
        ProductHttpClient("http://127.0.0.1:16393").get(
            "/runtime/readiness"
        )

    assert caught.value.code == "product_http_unreachable"
    assert caught.value.retryable is True
    assert caught.value.operation == "GET /runtime/readiness"
    assert caught.value.details["error"].startswith("ConnectionResetError:")
    assert connection.closed is True


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
            assert record.pid == int(health["runtime_identity"]["pid"])
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

        # The execution request occupies one HTTP handler while the product
        # progress channel is polled through another authenticated request.
        # This guards the cross-process concurrency property required by live
        # provider output, independent of provider availability in this test.
        concurrent_workload = _workload(
            DeploymentProfile.DEVICE,
            task_id=new_id("task_concurrent_runtime_events"),
            run_id=new_id("run_concurrent_runtime_events"),
        )
        concurrent_attempt_id = new_id("attempt_concurrent_runtime_events")
        concurrent_response: list[dict[str, Any]] = []
        concurrent_errors: list[BaseException] = []
        clients[DeploymentProfile.DEVICE].inject_fault(
            {"kind": "latency", "latency_ms": 750}
        )

        def execute_concurrent_workload() -> None:
            try:
                concurrent_response.append(
                    clients[DeploymentProfile.DEVICE].execute(
                        {
                            "schema": "zyra.deployment-node-execution-request/v1",
                            "attempt_id": concurrent_attempt_id,
                            "idempotency_key": (
                                concurrent_workload.idempotency_key
                            ),
                            "workload": concurrent_workload.semantic_dict(),
                        },
                        timeout_seconds=5,
                    )
                )
            except BaseException as error:
                concurrent_errors.append(error)

        concurrent_thread = threading.Thread(
            target=execute_concurrent_workload,
            daemon=True,
        )
        concurrent_thread.start()
        active_deadline = time.monotonic() + 3
        while time.monotonic() < active_deadline:
            if (
                int(
                    clients[DeploymentProfile.DEVICE]
                    .health()
                    .get("resource", {})
                    .get("active_dispatches", 0)
                )
                > 0
            ):
                break
            time.sleep(0.02)
        assert concurrent_thread.is_alive()
        runtime_page_started = time.monotonic()
        runtime_page = clients[DeploymentProfile.DEVICE].runtime_events(
            attempt_id=concurrent_attempt_id,
        )
        runtime_page_elapsed = time.monotonic() - runtime_page_started
        assert runtime_page["schema"] == (
            "zyra.deployment-node-runtime-events/v1"
        )
        assert runtime_page["events"] == []
        assert runtime_page["durable"] is False
        assert concurrent_thread.is_alive()
        assert runtime_page_elapsed < 0.5
        concurrent_thread.join(timeout=5)
        clients[DeploymentProfile.DEVICE].inject_fault({"kind": "clear"})
        assert concurrent_errors == []
        assert concurrent_response[0]["status"] == "succeeded"
        assert clients[DeploymentProfile.DEVICE].runtime_events(
            attempt_id=concurrent_attempt_id,
            release=True,
        )["released"] is True

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

            structural = orchestrator.semantic_health(
                include_short_task=False,
                fresh_state=False,
            )
            assert structural["ready"] is True, structural
            assert structural["status"] == "degraded"
            assert structural["short_task_included"] is False
            assert structural["short_task_id"] == ""
            assert not any(
                probe.get("probe_id") == "short-task"
                for probe in structural.get("probes") or ()
            )
            assert any(
                str(item).endswith(
                    "cloud_provider_credential_missing_fail_closed"
                )
                for item in structural["warnings"]
            )

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
            # Product startup remains available without a paid provider, but
            # the semantic task must now fail closed: Phase 2 execution is no
            # longer allowed to replace model reasoning with a template.
            assert semantic["ready"] is False
            assert semantic["status"] == "blocked"
            assert "short-task:task_execution_failed" in semantic["blockers"]
            assert any(
                str(item).endswith(
                    "cloud_provider_credential_missing_fail_closed"
                )
                for item in semantic["warnings"]
            )
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


def test_one_shot_cli_lifecycle_keeps_nodes_alive_across_restart() -> None:
    (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
    base_port = _free_port_block(5)
    with tempfile.TemporaryDirectory(
        prefix="p2-r01-cli-lifecycle-",
        dir=PROJECT_ROOT / ".tmp",
        ignore_cleanup_errors=True,
    ) as selected:
        state_root = Path(selected)
        environment = {
            **os.environ,
            "ZYRA_DEPLOYMENT_PROFILE_BASE_PORT": str(base_port),
            "ZYRA_API_PORT": str(base_port + 3),
            "ZYRA_WEB_PORT": str(base_port + 4),
        }
        command = [
            sys.executable,
            "-m",
            "zyra_orchestration.deployment.cli",
            "--project-root",
            str(PROJECT_ROOT),
            "--state-root",
            str(state_root),
        ]

        def invoke(action: str, *extra: str) -> dict[str, Any]:
            completed = subprocess.run(
                [*command, action, *extra],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
            value = json.loads(completed.stdout)
            assert isinstance(value, dict)
            return value

        started = False
        try:
            assert invoke("start", "--no-build-web")["ready"] is True
            started = True
            time.sleep(1.5)
            first_status = invoke("status")
            assert first_status["ready"] is True
            assert all(
                profile["reachable"] is True
                for profile in first_status["profiles"].values()
            )

            assert invoke("restart")["ready"] is True
            time.sleep(1.5)
            restarted_status = invoke("status")
            assert restarted_status["ready"] is True
            assert all(
                profile["reachable"] is True
                for profile in restarted_status["profiles"].values()
            )
        finally:
            if started:
                stopped = invoke("stop")
                assert stopped["ready"] is True
                assert stopped["remaining_active"] == []
