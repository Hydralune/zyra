from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from zyra_evaluation.m1_hardening.scenario import HttpScenarioTransport, M1MainPathScenarioSuite
from zyra_evaluation.m1_hardening.integration_scenarios import (
    M1IntegrationScenarioSuite,
    ScenarioRuntimeOptions,
)
from zyra_evaluation.m1_hardening.owner_probes import (
    OwnerProbeCatalog,
    ScenarioDisconnectCoordinator,
)
from zyra_evaluation.m1_hardening.contracts import HardeningContext
from zyra_evaluation.m1_hardening.service import AuditOptions, M1HardeningService


ROOT = Path(__file__).resolve().parents[2]


def _fresh_api_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.ZyraRequestHandler


def test_public_api_foundation_scenario_mutates_state_and_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                run, gate = M1MainPathScenarioSuite().run_http_foundation(base_url, timeout_seconds=120)
                status, hardening_status = HttpScenarioTransport(base_url).get("/hardening/m1/status")
                assert status == 200
                assert hardening_status["schema"] == "zyra.m1-hardening-service-status/v1"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

        assert gate.ok, json.dumps(gate.to_dict(), indent=2)
        assert run.task_id
        assert run.run_id
        assert len(run.events_after) > len(run.events_before)
        assert run.disable_evidence["baseline_ok"] is True
        assert run.disable_evidence["error_code"]
        assert run.disable_evidence["fallback_masked"] is False
        assert any(step.step_id == "trace-artifact" and step.ok for step in run.steps)
        assert any(step.step_id == "disable-codeworker" and step.status >= 400 for step in run.steps)
        assert run.task_after.get("artifacts")
        assert (root / "workspace" / "m1-hardening" / "output.txt").exists() is False

        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as audit_directory:
            service = M1HardeningService(
                ROOT,
                source_workspace=ROOT.parent,
                artifact_root=Path(audit_directory),
            )
            outcome = service.audit(
                HardeningContext(
                    project_root=ROOT,
                    workspace_root=ROOT.parent,
                    artifact_root=Path(audit_directory),
                    task=run.task_after,
                    events=run.events_after,
                    options={"scenario_id": run.scenario_id},
                ),
                AuditOptions(
                    baseline_commit="44da53ad8ea909147709857e358b7d16e39f6313",
                    minimum_effective_lines=0,
                    line_audit_head="HEAD",
                    # A git archive intentionally has no .git directory.  The
                    # exact implementation-commit line gate is executed and
                    # recorded separately; keep every behavioural foundation
                    # gate active in cleanroom without fabricating Git state.
                    include_line_audit=os.environ.get("ZYRA_CLEANROOM") != "1",
                    persist=True,
                    run_dynamic_graph_probes=True,
                    run_disable_probes=True,
                ),
                scenario_gate=gate,
                scenario_run=run,
            )
            if not outcome.accepted:
                print(json.dumps(outcome.disposition.to_dict(), indent=2))
                print(
                    json.dumps(
                        {
                            gate.gate_id: {
                                "status": gate.status.value,
                                "findings": [finding.to_dict() for finding in gate.findings],
                            }
                            for gate in outcome.report.gates
                            if not gate.ok
                        },
                        indent=2,
                    )
                )
            assert outcome.accepted, outcome.disposition.to_dict()
            assert outcome.record is not None
            assert service.store.load(outcome.report.report_id, verify=True)["report_id"] == outcome.report.report_id
            assert outcome.report.gate("source-to-target-coverage").ok
            assert outcome.report.gate("m1-state-custody").ok
            assert outcome.report.gate("langgraph-boundary").ok
            disable_gate = outcome.report.gate("disable-module-probe")
            assert disable_gate.ok
            assert disable_gate.metrics["executed_count"] == 1
            assert disable_gate.metrics["live_scenario_disable"]["fallback_masked"] is False


@pytest.mark.parametrize(
    "scenario_id",
    (
        "m1-integration-query-session-context-tool",
        "m1-integration-dangerous-tool-permission",
        "m1-integration-mcp-auth-elicitation",
        "m1-integration-skill-memory-compact-restore",
        "m1-integration-subagent-worker-recovery",
        "m1-integration-api-stream-provider-failover",
    ),
)
def test_public_api_exposes_integration_status_and_executes_each_main_path(
    scenario_id: str,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            transport = HttpScenarioTransport(base_url)
            try:
                status, body = transport.get("/hardening/m1/integration/status")
                assert status == 200
                assert body["schema"] == "zyra.m1-integration-service-status/v1"
                assert len(body["scenario_ids"]) == 6
                assert len(body["owner_probe_ids"]) == 18

                status, missing = transport.get("/hardening/m1/integration/runs/not-present")
                assert status == 404
                assert missing["error"] == "m1_integration_run_not_found"

                suite = M1IntegrationScenarioSuite()
                evidence, gate = suite.execute_http(
                    base_url,
                    scenario_ids=(scenario_id,),
                    options=ScenarioRuntimeOptions(
                        timeout_seconds=120,
                        execute_disconnects=False,
                        final_completion=False,
                    ),
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

        assert gate.ok, json.dumps(
            {
                "gate": gate.to_dict(),
                "failed_steps": [
                    {
                        "scenario_id": item.scenario_id,
                        "step_id": step.step_id,
                        "status": step.status,
                        "error": step.error,
                        "response": step.response,
                    }
                    for item in evidence
                    for step in item.steps
                    if not step.ok
                ],
                "failed_assertions": [
                    assertion.to_dict()
                    for item in evidence
                    for assertion in item.assertions
                    if not assertion.passed
                ],
            },
            indent=2,
        )
        assert len(evidence) == 1
        run = evidence[0]
        assert run.scenario_id == scenario_id
        assert run.task_id
        assert run.run_id
        assert run.final_revision > run.baseline_revision
        assert run.metadata["worker_lease_cleanup"]["ok"] is True
        if scenario_id == "m1-integration-query-session-context-tool":
            assert run.artifacts
        if scenario_id == "m1-integration-subagent-worker-recovery":
            fanout = next(step for step in run.steps if step.step_id == "fanout-subagent")
            assert fanout.status == 201, fanout.response
            assert {
                str(item.get("worker_location") or "")
                for item in fanout.response.get("physical_workers") or ()
            } == {"local", "edge"}
            edge_worker = next(
                item
                for item in fanout.response["physical_workers"]
                if item.get("worker_location") == "edge"
            )
            assert edge_worker["edge_gateway_receipt"]["accepted"] is True
            assert edge_worker["edge_gateway_receipt"]["artifact_refs"]
            assert sys.modules["apps.api.zyra_api.main"]._API_EDGE_CONNECTOR is None
        assert all(step.ok for step in run.steps)


@pytest.mark.parametrize(
    "scenario_id",
    M1IntegrationScenarioSuite().scenario_ids(),
)
def test_public_api_scenarios_execute_real_owner_disconnects_without_lease_exhaustion(
    scenario_id: str,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            api_module = sys.modules["apps.api.zyra_api.main"]
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            coordinator = ScenarioDisconnectCoordinator(
                OwnerProbeCatalog(),
                reset_registry=api_module.m1_owner_probe_reset_registry(),
            )
            try:
                suite = M1IntegrationScenarioSuite()
                evidence, gate = suite.execute_http(
                    base_url,
                    scenario_ids=(scenario_id,),
                    options=ScenarioRuntimeOptions(
                        timeout_seconds=120,
                        execute_disconnects=True,
                        final_completion=False,
                    ),
                    disconnect_executor=coordinator.execute,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

        diagnostic = {
            "gate": gate.to_dict(),
            "disconnect_evidence": [
                {
                    "probe_id": item.get("probe_id"),
                    "capability": item.get("capability"),
                    "status": item.get("status"),
                    "error_code": item.get("error_code"),
                    "error_message": item.get("error_message") or item.get("message"),
                    "baseline_semantic": (item.get("baseline") or {}).get("semantic"),
                    "disabled": item.get("disabled"),
                    "restored_semantic": (item.get("restored") or {}).get("semantic"),
                    "difference": item.get("difference"),
                    "restore_receipt": item.get("restore_receipt"),
                    "post_restore": (item.get("metadata") or {}).get("post_restore"),
                }
                for item in evidence[0].disconnect_evidence
            ],
        }
        receipts = {item["capability"]: item for item in evidence[0].disconnect_evidence}
        expected_capabilities = {
            item.capability for item in suite.definition(scenario_id).disconnects
        }
        assert set(receipts) == expected_capabilities
        assert gate.ok, json.dumps(diagnostic, indent=2)
        assert all(item["status"] == "passed" for item in receipts.values())
        assert all(item.get("expected_failure_observed") or item.get("material_difference") for item in receipts.values())
        assert all(item.get("fallback_masked") is False for item in receipts.values())
        assert all(
            (item.get("isolation") or {}).get("before", {}).get("ok") is True
            and (item.get("isolation") or {}).get("after", {}).get("ok") is True
            for item in receipts.values()
        )


def test_public_api_integration_orchestrator_persists_fail_closed_outcome() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            transport = HttpScenarioTransport(base_url)
            try:
                status, outcome = transport.post(
                    "/hardening/m1/integration",
                    {
                        "baseline_commit": "8065bac109a3bed9ba01e0e92392fec4d05bfca3",
                        "execute_scenarios": False,
                        "execute_disconnects": False,
                        "run_cleanroom": False,
                        "final_completion": False,
                        "persist": True,
                        "benchmark_run_id": "api-benchmark-run",
                        "benchmark_events": [
                            {
                                "event_id": "api-benchmark-event-1",
                                "event_type": "state_changed",
                                "run_id": "api-benchmark-run",
                                "task_id": "api-benchmark-task",
                                "sequence": 1,
                                "causation_id": "api-benchmark-request",
                                "payload": {
                                    "action_id": "api-benchmark-action",
                                    "before_revision": 0,
                                    "after_revision": 1,
                                    "before_digest": "0" * 64,
                                    "after_digest": "1" * 64,
                                    "semantic_family": "state",
                                    "semantic_mutation": True,
                                },
                            }
                        ],
                        "unresolved_requirements": ["test-intentional-external-evidence-gap"],
                    },
                )
                assert status == 409, outcome
                assert outcome["schema"] == "zyra.m1-integration-outcome/v1"
                assert outcome["accepted"] is False
                assert "integration scenarios were not executed" in outcome["limitations"]
                benchmark_gate = next(
                    gate
                    for gate in outcome["gates"]
                    if gate["gate_id"] == "m1-long-horizon-benchmark"
                )
                assert benchmark_gate["metrics"]["transition_count"] == 1
                assert any(path.endswith(".md") for path in outcome["artifact_paths"])
                assert all(Path(path).is_file() for path in outcome["artifact_paths"])
                run_id = outcome["run_id"]

                list_status, listing = transport.get("/hardening/m1/integration/runs")
                assert list_status == 200
                assert any(item["run_id"] == run_id for item in listing["runs"])

                load_status, persisted = transport.get(f"/hardening/m1/integration/runs/{run_id}")
                assert load_status == 200
                assert persisted["content_digest"] == outcome["content_digest"]
                assert persisted["exit_bundle"]["decision"] == "blocked"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)


def test_runtime_event_spine_disconnect_returns_stable_http_failure() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_RUNTIME_EVENT_SPINE_DISABLED": "1",
        }
        with patch.dict(os.environ, environment, clear=False):
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            transport = HttpScenarioTransport(
                f"http://127.0.0.1:{server.server_address[1]}"
            )
            try:
                status, body = transport.post(
                    "/tasks",
                    {"goal": "Fail closed when canonical event custody is unavailable.", "auto_run": False},
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

        assert status == 503
        assert body["error"] == "runtime_event_spine_disabled"
        assert body["fallback"] is False
