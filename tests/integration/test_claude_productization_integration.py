from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT,
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import (  # noqa: E402
    ClaudeSourceGraphBatch,
    EventContractPhase,
    RuntimePortKind,
    WorkerRequest,
    build_claude_productization_integration_report,
    build_productized_claude_runtime_contracts,
    default_claude_source_graph_crosswalk,
)
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class ExplodingSidecarClient:
    def health(self):
        raise AssertionError("sidecar must not be called by default integration path")

    def runtime_inventory(self):
        raise AssertionError("sidecar must not be called by default integration path")

    def query_contract(self):
        raise AssertionError("sidecar must not be called by default integration path")

    def session_contract(self):
        raise AssertionError("sidecar must not be called by default integration path")

    def tool_loop_contract(self):
        raise AssertionError("sidecar must not be called by default integration path")


class ClaudeProductizationIntegrationTests(unittest.TestCase):
    def test_crosswalk_validates_all_batches_and_downstream_handoffs(self) -> None:
        contracts = build_productized_claude_runtime_contracts(project_root=ROOT)
        report = build_claude_productization_integration_report(
            project_root=ROOT,
            runtime_contracts=contracts,
        )

        self.assertTrue(report.ok, [finding.to_dict() for finding in report.validation.blockers])
        self.assertEqual(report.validation.batch_count, 9)
        self.assertEqual(report.crosswalk.source_pool_target_count, 0)
        self.assertEqual(report.validation.runtime_context_port_count, 18)
        self.assertEqual(report.validation.tool_use_context_port_count, 3)
        self.assertGreaterEqual(report.validation.event_contract_count, 20)
        self.assertGreaterEqual(report.validation.downstream_contract_count, 8)
        self.assertEqual(
            {batch.batch for batch in report.crosswalk.batches},
            set(ClaudeSourceGraphBatch),
        )
        downstream_owners = {contract.owner_slice for contract in report.crosswalk.downstream_contracts}
        self.assertIn("M1-02B", downstream_owners)
        self.assertIn("M1-02C", downstream_owners)
        self.assertIn("M1-02D", downstream_owners)
        self.assertIn("M1-03A", downstream_owners)
        self.assertIn("M1-03B", downstream_owners)
        self.assertIn("M1-03C", downstream_owners)
        self.assertIn("M1-03D", downstream_owners)
        current_targets = {target.path for target in report.crosswalk.current_slice_targets}
        self.assertIn("packages/workers/zyra_workers/code_worker_runtime.py", current_targets)
        self.assertIn("packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py", current_targets)
        self.assertIn("packages/runtime/zyra_runtime/claude_state_custody_runtime.py", current_targets)
        self.assertIn("packages/runtime/zyra_runtime/claude_api_inventory_contract_runtime.py", current_targets)
        self.assertIn("packages/runtime/zyra_runtime/claude_worker_execution_gate_runtime.py", current_targets)
        self.assertEqual(report.metadata()["source_graph_crosswalk_ok"], "true")

    def test_crosswalk_reports_disabled_batch_and_downstream_contracts_as_blockers(self) -> None:
        crosswalk = default_claude_source_graph_crosswalk()
        report = crosswalk.validate(
            project_root=ROOT,
            disabled_batches=[ClaudeSourceGraphBatch.QUERY_TOOL_LOOP.value],
            disable_downstream_contracts=True,
            runtime_contracts=build_productized_claude_runtime_contracts(project_root=ROOT),
        )

        self.assertFalse(report.ok)
        codes = {finding.code for finding in report.blockers}
        self.assertIn("source_graph_batch_disabled", codes)
        self.assertIn("downstream_contracts_disabled", codes)

    def test_code_worker_default_path_emits_source_graph_events_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run CodeWorker with source graph contract.")
            workspace = Path(tmpdir) / "workspace"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "integration/result.txt", "content": "ok"}},
                        {"tool_name": "file_read", "arguments": {"path": "integration/result.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["claude_productization_integration_ok"], "true")
            self.assertEqual(run.worker_result.metadata["source_graph_crosswalk_ok"], "true")
            self.assertEqual(run.worker_result.metadata["source_graph_batch_count"], "9")
            self.assertEqual(run.worker_result.metadata["downstream_contract_count"], "8")
            self.assertEqual(run.worker_result.metadata["runtime_context_assembly_ok"], "true")
            self.assertEqual(run.worker_result.metadata["runtime_context_binding_count"], "18")
            self.assertEqual(run.worker_result.metadata["tool_use_context_binding_count"], "3")
            self.assertEqual(run.worker_result.metadata["source_graph_audit_ok"], "true")
            self.assertEqual(run.worker_result.metadata["source_graph_audit_disconnect_scenarios"], "5")
            self.assertEqual(run.worker_result.metadata["source_graph_audit_state_custody_ok"], "true")
            self.assertEqual(run.worker_result.metadata["source_graph_audit_api_inventory_ok"], "true")
            self.assertEqual(run.worker_result.metadata["worker_execution_gate_ok"], "true")
            self.assertEqual(run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertTrue((workspace / "integration" / "result.txt").exists())
            integration_events = [
                event.payload["claude_productization_integration"]["phase"]
                for event in run.event_records
                if "claude_productization_integration" in event.payload
            ]
            self.assertEqual(integration_events[0], EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY.value)
            self.assertIn(EventContractPhase.RUNTIME_CONTEXT_READY.value, integration_events)
            self.assertIn(EventContractPhase.DOWNSTREAM_CONTRACTS_READY.value, integration_events)
            self.assertIn(EventContractPhase.INTEGRATION_GATE_PASSED.value, integration_events)
            runtime_context_events = [
                event.payload["claude_runtime_context"]["phase"]
                for event in run.event_records
                if "claude_runtime_context" in event.payload
            ]
            self.assertEqual(runtime_context_events, [EventContractPhase.RUNTIME_CONTEXT_READY.value])
            audit_events = [
                event.payload["claude_source_graph_audit"]["phase"]
                for event in run.event_records
                if "claude_source_graph_audit" in event.payload
            ]
            self.assertEqual(audit_events, ["source_graph_audit"])
            gate_events = [
                event.payload["claude_worker_execution_gate"]["phase"]
                for event in run.event_records
                if "claude_worker_execution_gate" in event.payload
            ]
            self.assertEqual(gate_events, ["worker_execution_gate"])
            trace_artifact = next(
                artifact for artifact in run.worker_result.artifacts if artifact.artifact_id == run.worker_result.metadata["trace_artifact_id"]
            )
            trace_text = Path(trace_artifact.uri).read_text(encoding="utf-8")
            self.assertIn("Claude Source Graph Crosswalk", trace_text)
            self.assertIn("Runtime Context Assembly", trace_text)
            self.assertIn("Source Graph Audit", trace_text)
            self.assertIn("Worker Execution Gate", trace_text)
            self.assertIn("batch-01-query-session-context", trace_text)
            self.assertIn("M1-02B.query-session-lifecycle", trace_text)

    def test_disabling_source_graph_blocks_before_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disabled source graph must block.")
            workspace = Path(tmpdir) / "workspace"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "disable_source_graph_crosswalk": True,
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "must not write"}}
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "source_graph_crosswalk_disabled")
            self.assertEqual(run.worker_result.metadata["claude_productization_integration_ok"], "false")
            self.assertFalse((workspace / "blocked.txt").exists())
            phases = [
                event.payload["claude_productization_integration"]["phase"]
                for event in run.event_records
                if "claude_productization_integration" in event.payload
            ]
            self.assertIn(EventContractPhase.INTEGRATION_GATE_BLOCKED.value, phases)

    def test_disabling_required_runtime_context_port_blocks_before_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disabled RuntimeContext source_graph port must block.")
            workspace = Path(tmpdir) / "workspace"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "disabled_runtime_context_ports": [RuntimePortKind.SOURCE_GRAPH.value],
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "blocked-port.txt", "content": "must not write"}}
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "runtime_context_source_graph_port_disabled")
            self.assertEqual(run.worker_result.metadata["source_graph_first_blocker"], "runtime_context_source_graph_port_disabled")
            self.assertFalse((workspace / "blocked-port.txt").exists())

    def test_disabling_tool_use_context_port_blocks_before_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disabled ToolUseContext port must block.")
            workspace = Path(tmpdir) / "workspace"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "disable_tool_use_context_port": True,
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "blocked-tool-port.txt", "content": "must not write"}}
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "runtime_context_tool_registry_port_disabled")
            self.assertFalse((workspace / "blocked-tool-port.txt").exists())

    def test_disabling_worker_execution_gate_blocks_before_query_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disabled worker execution gate must block.")
            workspace = Path(tmpdir) / "workspace"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "disable_worker_execution_gate": True,
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "blocked-gate.txt", "content": "must not write"}}
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "worker_gate_disabled_by_constraint")
            self.assertEqual(run.worker_result.metadata["worker_execution_gate_ok"], "false")
            self.assertEqual(run.worker_result.metadata["source_graph_audit_ok"], "true")
            self.assertFalse((workspace / "blocked-gate.txt").exists())
            self.assertTrue(any("claude_worker_execution_gate" in event.payload for event in run.event_records))

    def test_api_inventory_and_task_path_expose_source_graph_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(Path(tmpdir) / "permissions.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                inventory = _get(base_url, "/workers/code/inventory")
                self.assertEqual(inventory["sourceGraph"]["source_pool_target_count"], 0)
                self.assertEqual(len(inventory["sourceGraph"]["batches"]), 9)
                self.assertGreaterEqual(len(inventory["runtimeContext"]["ports"]), 18)
                self.assertGreaterEqual(inventory["eventContracts"]["event_contract_count"], 20)
                self.assertIn("M1-02B", inventory["downstreamContracts"]["owners"])
                self.assertTrue(inventory["integration"]["ok"])
                self.assertTrue(inventory["sourceGraphAudit"]["ok"])
                self.assertEqual(len(inventory["sourceGraphAudit"]["disconnect_scenarios"]), 5)
                self.assertTrue(inventory["stateCustodyRuntime"]["ok"])
                self.assertTrue(inventory["apiInventoryContract"]["ok"])
                self.assertEqual(inventory["apiInventoryContract"]["metadata"]["api_inventory_contract_route"], "/workers/code/inventory")

                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Run code worker source graph path.", "auto_run": False},
                    expected_status=201,
                )
                task_id = created["task"]["task_id"]
                completed = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "constraints": {
                            "tool_plan": [
                                {
                                    "tool_name": "file_write",
                                    "arguments": {"path": "api-integration/result.txt", "content": "api ok"},
                                }
                            ]
                        }
                    },
                    expected_status=201,
                )
                self.assertTrue(completed["worker_result"]["ok"])
                self.assertEqual(
                    completed["worker_result"]["metadata"]["claude_productization_integration_ok"],
                    "true",
                )
                self.assertEqual(
                    completed["worker_result"]["metadata"]["runtime_context_assembly_ok"],
                    "true",
                )
                self.assertEqual(completed["worker_result"]["metadata"]["source_graph_audit_ok"], "true")
                self.assertEqual(completed["worker_result"]["metadata"]["worker_execution_gate_ok"], "true")
                self.assertTrue(
                    any("claude_productization_integration" in event["payload"] for event in completed["events"])
                )
                self.assertTrue(any("claude_runtime_context" in event["payload"] for event in completed["events"]))
                self.assertTrue(any("claude_source_graph_audit" in event["payload"] for event in completed["events"]))
                self.assertTrue(any("claude_worker_execution_gate" in event["payload"] for event in completed["events"]))

                blocked = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "constraints": {
                            "disabled_runtime_context_ports": ["source_graph"],
                            "tool_plan": [
                                {
                                    "tool_name": "file_write",
                                    "arguments": {"path": "api-integration/blocked.txt", "content": "blocked"},
                                }
                            ],
                        }
                    },
                    expected_status=409,
                )
                self.assertFalse(blocked["worker_result"]["ok"])
                self.assertEqual(blocked["worker_result"]["error"], "runtime_context_source_graph_port_disabled")
                self.assertFalse((Path(tmpdir) / "workspace" / "api-integration" / "blocked.txt").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(base_url + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict, *, expected_status: int = 200) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(f"expected HTTP {expected_status}, got {response.status}: {body}")
            return body
    except HTTPError as error:
        body = json.loads(error.read().decode("utf-8"))
        if error.code != expected_status:
            raise AssertionError(f"expected HTTP {expected_status}, got {error.code}: {body}") from error
        return body


if __name__ == "__main__":
    unittest.main()
