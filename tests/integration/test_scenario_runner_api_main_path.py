from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from uuid import uuid4
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from zyra_evaluation.scenario_runner.canonical import digest
from zyra_evaluation.scenario_runner.models import FaultInjection
from zyra_evaluation.scenario_runner.software_delivery import (
    SourceInventoryBuilder,
)


ROOT = Path(__file__).resolve().parents[2]


def test_canonical_event_snapshot_uses_initialized_state_and_prebegin_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.zyra_api import main as api_main
    from apps.api.zyra_api.live_scenario_owners import (
        CanonicalLiveScenarioOwners,
        LiveOwnerIntegrationError,
        _canonical_checkpoint_head,
        _checkpoint_lineage,
    )

    with pytest.raises(
        LiveOwnerIntegrationError,
        match="missing checkpoint_head",
    ):
        _canonical_checkpoint_head({})
    with pytest.raises(
        LiveOwnerIntegrationError,
        match="must be a mapping or null",
    ):
        _canonical_checkpoint_head({"checkpoint_head": []})
    assert _canonical_checkpoint_head({"checkpoint_head": None}) is None

    with pytest.raises(
        LiveOwnerIntegrationError,
        match="canonical recovery checkpoint lineage is incomplete",
    ):
        _checkpoint_lineage(
            checkpoint_head={"session_id": "scenario:incomplete"},
            scenario_run_id="scenario-incomplete",
            configuration_digest="configuration-incomplete",
            fallback_session_id="scenario:fallback-must-not-apply",
        )
    with pytest.raises(
        LiveOwnerIntegrationError,
        match="canonical recovery checkpoint lineage is incomplete",
    ):
        _checkpoint_lineage(
            checkpoint_head={},
            scenario_run_id="scenario-empty-head",
            configuration_digest="configuration-empty-head",
            fallback_session_id="scenario:fallback-must-not-apply",
        )
    assert _checkpoint_lineage(
        checkpoint_head=None,
        scenario_run_id="scenario-initial-lineage",
        configuration_digest="configuration-initial-lineage",
        fallback_session_id="scenario:initial-lineage",
    ) == (
        "scenario:initial-lineage",
        digest(
            {
                "scenario": "scenario-initial-lineage",
                "configuration": "configuration-initial-lineage",
            }
        ),
    )

    owner = CanonicalLiveScenarioOwners(
        project_root=ROOT,
        artifact_root=tmp_path / "artifacts",
        scratch_root=tmp_path / "scratch",
    )
    owner._canonical_event_snapshot = ({"event_id": "cached-event"},)
    assert owner.canonical_event_snapshot() == ({"event_id": "cached-event"},)

    class Store:
        @staticmethod
        def task_events(task_id: str) -> list[dict[str, str]]:
            assert task_id == "task-owner-snapshot"
            return [{"event_id": "persisted-event"}]

    monkeypatch.setattr(api_main, "get_store", lambda: Store())
    owner.state = SimpleNamespace(task_id="task-owner-snapshot")
    assert owner.canonical_event_snapshot() == (
        {"event_id": "persisted-event"},
    )


def test_fault_checkpoint_reuses_canonical_recovery_lineage_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.zyra_api import main as api_main
    from apps.api.zyra_api.live_scenario_owners import CanonicalLiveScenarioOwners

    owner = CanonicalLiveScenarioOwners(
        project_root=ROOT,
        artifact_root=tmp_path / "artifacts",
        scratch_root=tmp_path / "scratch",
    )
    state, _event = api_main.make_task_created_event("checkpoint lineage test")
    owner.state = state
    owner.scenario_run_id = "scenario-checkpoint-lineage"
    state.metadata["query_session_id"] = "scenario:scenario-checkpoint-lineage"
    owner._route_history.append({"route_id": "route-checkpoint-lineage"})

    class Store:
        saved: list[object] = []

        @classmethod
        def save_checkpoint(cls, value: object) -> None:
            cls.saved.append(value)

    committed: dict[str, Any] = {}

    class RecoveryApi:
        @staticmethod
        def route_get(parts: tuple[str, ...]) -> SimpleNamespace:
            assert parts == ("tasks", state.task_id, "recovery")
            return SimpleNamespace(
                status=200,
                body={
                    "checkpoint_head": {
                        "session_id": state.metadata["query_session_id"],
                        "workflow_signature": "canonical-workflow-signature",
                    }
                },
            )

        @staticmethod
        def route_post(
            parts: tuple[str, ...],
            payload: dict[str, Any],
        ) -> SimpleNamespace:
            assert parts == ("tasks", state.task_id, "recovery", "checkpoints")
            committed.update(payload)
            return SimpleNamespace(
                status=201,
                body={
                    "checkpoint": {
                        "checkpoint_id": "checkpoint-lineage-child",
                        "commit_revision": 2,
                        "content_digest": "content-lineage-child",
                        "signature": "signature-lineage-child",
                    },
                    "receipt": {"created": True},
                },
            )

    monkeypatch.setattr(api_main, "get_store", lambda: Store())
    monkeypatch.setattr(
        api_main,
        "get_recovery_runtime_api",
        lambda _store: RecoveryApi(),
    )

    receipt = owner.checkpoint(
        injection=FaultInjection(
            injection_id="fault-checkpoint-lineage",
            stage="recovery",
            kind="tool_timeout",
            after_effective_step=3,
        ),
        effective_step=3,
        scenario_state={
            "run_id": state.run_id,
            "task_id": state.task_id,
            "configuration_digest": "scenario-configuration-signature",
            "input_digest": "input-checkpoint-lineage",
            "plan_digest": "plan-checkpoint-lineage",
        },
    )

    assert Store.saved == [state]
    assert committed["session_id"] == state.metadata["query_session_id"]
    assert committed["workflow_signature"] == "canonical-workflow-signature"
    assert committed["state_payload"]["configuration_digest"] == (
        "scenario-configuration-signature"
    )
    assert receipt["checkpoint_id"] == "checkpoint-lineage-child"


@pytest.mark.parametrize("cancelled", (True, False))
def test_scenario_graph_cancel_or_exception_closes_acquired_lease(
    cancelled: bool,
) -> None:
    from apps.api.zyra_api.scenario_api import _run_leased_task_graph

    class PoolApi:
        def __init__(self) -> None:
            self.acquired = False
            self.finalized: list[dict[str, Any]] = []

        def acquire_for_task(self, state: Any, *, payload: Any) -> None:
            assert state.task_id == "task-lease-close"
            assert payload == {"scenario_run_id": "scenario-lease-close"}
            self.acquired = True

        def finalize_task(self, state: Any, **values: Any) -> None:
            assert state.task_id == "task-lease-close"
            self.finalized.append(dict(values))

    class ApiMain:
        @staticmethod
        def run_task_graph(state: Any, *, execution_context: Any) -> list[Any]:
            assert state.task_id == "task-lease-close"
            assert execution_context == "physical-context"
            raise RuntimeError("graph failed")

    pool_api = PoolApi()
    state = SimpleNamespace(task_id="task-lease-close", status="running")
    with pytest.raises(
        RuntimeError,
        match=(
            "scenario cancellation"
            if cancelled
            else "graph failed"
        ),
    ):
        _run_leased_task_graph(
            api_main=ApiMain,
            pool_api=pool_api,
            state=state,
            payload={"scenario_run_id": "scenario-lease-close"},
            execution_context="physical-context",
            cancel_requested=lambda: cancelled,
        )

    assert pool_api.acquired is True
    assert pool_api.finalized == [
        {
            "success": False,
            "summary": "scenario owner graph aborted before completion",
        }
    ]


@contextmanager
def scenario_api(tmp_path: Path) -> Iterator[str]:
    environment = {
        "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
        "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
        "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
        "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    package_paths = [
        ROOT,
        ROOT / "apps" / "api",
        ROOT / "packages" / "core",
        ROOT / "packages" / "commands",
        ROOT / "packages" / "orchestration",
        ROOT / "packages" / "memory",
        ROOT / "packages" / "runtime",
        ROOT / "packages" / "integrations",
        ROOT / "packages" / "workers",
        ROOT / "packages" / "symbolic",
        ROOT / "packages" / "scheduler",
        ROOT / "packages" / "evaluation",
        ROOT / "packages" / "workspace",
        ROOT / "packages" / "code_index",
    ]
    for path in package_paths:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.api.zyra_api import main as api_main

    api_main = importlib.reload(api_main)
    api_main.reset_scenario_runner_api(wait=True)
    api_main.reset_runtime_event_spine_bridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_scenario_runner_api(wait=True)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def request(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    selected = urllib.request.Request(
        base + path,
        method=method,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "scenario-integration-test",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
            "Idempotency-Key": f"scenario-test-{uuid4().hex}",
        },
    )
    try:
        with urllib.request.urlopen(selected, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_software_inventory_ignores_only_repository_relative_tmp_parts(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / ".tmp" / "clean-checkout"
    source = project_root / "packages" / "example" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    records = SourceInventoryBuilder(project_root=project_root).build(
        ("packages",),
        minimum_work_units=1,
    )

    assert len(records) == 1
    assert records[0].relative_path == "packages/example/runtime.py"


def test_short_sealed_scenario_reaches_real_canonical_owners_and_evidence(
    tmp_path: Path,
) -> None:
    with scenario_api(tmp_path) as base:
        registry_status, registry = request(base, "GET", "/scenarios/registry")
        assert registry_status == 200, registry
        definition = registry["definitions"][0]
        policy = registry["policies"][0]

        create_status, created = request(
            base,
            "POST",
            "/scenarios/runs",
            {
                "scenario_id": definition["scenario_id"],
                "definition_version": definition["version"],
                "profile_id": "foundation.local-sealed",
                "policy_id": policy["policy_id"],
                "policy_digest": policy["policy_digest"],
                "mode": "sealed",
                "input": "new cross-owner foundation input",
                "seed": 25,
                "labels": {"test": "real-api-owner-chain"},
            },
        )
        assert create_status == 201, created
        run_id = created["run"]["scenario_run_id"]
        assert created["run"]["phase"] == "admitted"
        assert created["run"]["preflight_receipt"]["clean"] is True
        assert created["run"]["preflight_receipt"]["new_input"] is True

        start_status, started = request(
            base,
            "POST",
            f"/scenarios/runs/{run_id}/start",
            {"wait": True, "timeout_seconds": 90},
        )
        assert start_status == 200, started
        run = started["run"]
        assert run["phase"] == "succeeded", run.get("failure")
        assert run["human_intervention_count"] == 0
        assert run["operator_intervention_attempt_count"] == 0
        assert run["task_id"]
        assert run["owner_run_id"]
        assert run["verification_receipt"]["valid"] is True

        manifest = run["evidence_manifest"]
        effects = manifest["effective_steps"]["effect_counts"]
        assert {
            "state_mutation",
            "route",
            "memory",
            "permission",
            "fault",
            "recovery",
            "artifact",
            "verification",
        }.issubset(effects)
        assert manifest["claims"]["legacy_demo_fallback"] is False
        assert manifest["claims"]["long_live_scenario_complete"] is False
        assert manifest["source_audit"]["openclaw"] == "excluded_forward_only"
        assert manifest["source_audit"]["valid"] is True

        task_status, task = request(base, "GET", f"/tasks/{run['task_id']}")
        events_status, task_events = request(
            base,
            "GET",
            f"/tasks/{run['task_id']}/events",
        )
        assert task_status == 200
        assert events_status == 200
        assert task["task"]["metadata"]["scenario_run_id"] == run_id
        assert task["task"]["metadata"]["sealed_autonomous"] is True
        assert task["task"]["metadata"]["human_intervention_count"] == 0
        assert task["task"]["metadata"]["worker_pool"]["worker_id"] == (
            "foundation-scenario-worker"
        )
        event_types = {item["event_type"] for item in task_events["events"]}
        assert "task_created" in event_types
        assert "resource_decision" in event_types or "topology_route" in event_types
        assert "failure_injected" in event_types
        assert "artifact_written" in event_types
        execution_receipt = next(
            item["payload"]
            for item in task_events["events"]
            if item.get("payload", {}).get("schema")
            == "zyra.foundation-owner-execution-receipt/v1"
        )
        assert execution_receipt["runtime_worker"] == "ScenarioOwnerChainRuntime"
        assert execution_receipt["provider_called"] is False
        assert execution_receipt["provider_reasoning_required"] is False
        assert all(execution_receipt["checks"].values())
        assert not (
            tmp_path
            / "artifacts"
            / ".provider-control-plane"
            / "provider.sqlite3"
        ).exists()

        evidence_status, evidence = request(
            base,
            "GET",
            f"/scenarios/runs/{run_id}/evidence",
        )
        verify_status, verified = request(
            base,
            "POST",
            f"/scenarios/runs/{run_id}/verify",
            {},
        )
        assert evidence_status == 200
        assert verify_status == 200
        assert evidence["evidence_manifest"]["manifest_digest"]
        assert verified["verification_receipt"]["valid"] is True


def test_live_software_scenario_reaches_canonical_api_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.zyra_api.live_scenario_owners import CanonicalLiveScenarioOwners

    def reject_external_probe(*_: Any, **__: Any) -> tuple[dict[str, Any], ...]:
        raise AssertionError(
            "formal M2-S05-02 path attempted an external tier/provider probe"
        )

    monkeypatch.setattr(
        CanonicalLiveScenarioOwners,
        "execute_tiers",
        reject_external_probe,
    )
    monkeypatch.setattr(
        CanonicalLiveScenarioOwners,
        "execute_providers",
        reject_external_probe,
    )
    root_prefix = "cleanroom-"
    path_padding = max(
        1,
        140 - len(str(tmp_path)) - 1 - len(root_prefix),
    )
    deep_root = tmp_path / (root_prefix + ("x" * path_padding))
    deep_root.mkdir(parents=True)
    with scenario_api(deep_root) as base:
        registry_status, registry = request(base, "GET", "/scenarios/registry")
        assert registry_status == 200, registry
        policy = registry["policies"][0]
        create_status, created = request(
            base,
            "POST",
            "/scenarios/runs",
            {
                "scenario_id": "live.software-delivery",
                "profile_id": "live.heterogeneous-sealed",
                "policy_id": policy["policy_id"],
                "policy_digest": policy["policy_digest"],
                "mode": "sealed",
                "input": (
                    "Implement a new deterministic delivery marker with executable "
                    "tests, a checksum-bound patch, and recovery evidence."
                ),
                "seed": 2502,
                "labels": {"test": "canonical-live-api-owners"},
            },
        )
        assert create_status == 201, created
        run_id = created["run"]["scenario_run_id"]
        start_status, started = request(
            base,
            "POST",
            f"/scenarios/runs/{run_id}/start",
            {"wait": True, "timeout_seconds": 300},
        )
        assert start_status == 200, started
        run = started["run"]
        assert run["phase"] == "succeeded", run.get("failure")
        assert run["verification_receipt"]["valid"] is True
        effective_steps = run["evidence_manifest"]["effective_steps"]
        assert len(effective_steps["admitted_step_ids"]) >= 2_000
        claims = run["evidence_manifest"]["claims"]
        assert claims["long_live_scenario_complete"] is True
        assert claims["external_provider_execution_excluded"] is True
        assert claims["authenticated_provider_cli_invoked"] is False
        assert claims["provider_model_capabilities_complete"] is False
        assert claims["edge_cloud_dispatch_complete"] is False
        task_status, task = request(base, "GET", f"/tasks/{run['task_id']}")
        assert task_status == 200, task
        owner_binding = task["task"]["metadata"]["live_owner_binding"]
        assert owner_binding["events"] == "RuntimeEventSpineBridge"
        assert owner_binding["placement"] == "WorkerPoolFoundationRuntime"
        assert owner_binding["checkpoint"] == "RecoveryPlanStore"
        assert owner_binding["recovery"] == "CheckpointResumeBridge"


def test_dirty_state_replay_policy_mismatch_manual_attempt_and_disable_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with scenario_api(tmp_path) as base:
        status, registry = request(base, "GET", "/scenarios/registry")
        assert status == 200
        policy = registry["policies"][0]
        base_body = {
            "scenario_id": "foundation.short-owner-chain",
            "profile_id": "foundation.local-sealed",
            "policy_id": policy["policy_id"],
            "policy_digest": policy["policy_digest"],
            "mode": "sealed",
            "input": "unique sealed rejection input",
            "seed": 1,
        }
        wrong_status, wrong = request(
            base,
            "POST",
            "/scenarios/runs",
            {**base_body, "input": "wrong policy input", "policy_digest": "0" * 64},
        )
        assert wrong_status == 409
        assert wrong["error"] == "scenario_policy_digest_mismatch"
        assert wrong["fallback"] is False

        create_status, created = request(base, "POST", "/scenarios/runs", base_body)
        assert create_status == 201
        run_id = created["run"]["scenario_run_id"]
        cancel_status, cancelled = request(
            base,
            "POST",
            f"/scenarios/runs/{run_id}/cancel",
            {"reason": "manual sealed mutation"},
        )
        assert cancel_status == 200
        assert cancelled["run"]["phase"] == "failed"
        assert cancelled["run"]["human_intervention_count"] == 0
        assert cancelled["run"]["operator_intervention_attempt_count"] == 1

        replay_status, replay = request(base, "POST", "/scenarios/runs", base_body)
        assert replay_status == 409
        assert replay["error"] == "scenario_input_replayed"

        monkeypatch.setenv("ZYRA_SCENARIO_RUNNER_DISABLED", "1")
        disabled_status, disabled = request(
            base,
            "POST",
            "/scenarios/runs",
            {**base_body, "input": "disabled runner input"},
        )
        assert disabled_status == 503
        assert disabled["error"] == "scenario_runner_disabled"
        assert disabled["fallback"] is False


def test_first_stage_cli_uses_same_registry_and_durable_lifecycle(
    tmp_path: Path,
) -> None:
    with scenario_api(tmp_path) as base:
        registry_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_first_stage_scenarios.py"),
                "--api",
                base,
                "registry",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert registry_result.returncode == 0, registry_result.stderr
        registry = json.loads(registry_result.stdout)
        assert registry["registry_digest"]

        create_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_first_stage_scenarios.py"),
                "--api",
                base,
                "create",
                "--input",
                "new CLI lifecycle input",
                "--seed",
                "75",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert create_result.returncode == 0, create_result.stderr
        created = json.loads(create_result.stdout)
        run_id = created["run"]["scenario_run_id"]
        assert created["run"]["phase"] == "admitted"

        status_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_first_stage_scenarios.py"),
                "--api",
                base,
                "status",
                run_id,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert status_result.returncode == 0, status_result.stderr
        status = json.loads(status_result.stdout)
        assert status["run"]["scenario_run_id"] == run_id
        assert status["run"]["preflight_receipt"]["new_input"] is True
