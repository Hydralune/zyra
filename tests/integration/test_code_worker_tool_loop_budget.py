from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import (  # noqa: E402
    JsonPermissionStore,
    ToolSemanticEffectReport,
    ToolSemanticFinding,
    ToolSemanticSeverity,
    ToolSemanticSurface,
    WorkerRequest,
)
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient  # noqa: E402


@unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
class CodeWorkerToolLoopBudgetTests(unittest.TestCase):
    def test_sidecar_tool_loop_contract_maps_claude_code_budget_sources(self) -> None:
        contract = CodeWorkerSidecarClient(ROOT).tool_loop_contract()

        self.assertEqual(contract["source"], "claude-code-best")
        self.assertEqual(contract["ownerUnit"], "M1-02C")
        self.assertTrue(contract["inventoryExists"])
        self.assertIn("src/services/tools/toolExecution.ts", contract["sourceFiles"])
        self.assertIn("src/services/tools/toolOrchestration.ts", contract["sourceFiles"])
        self.assertIn("src/utils/toolResultStorage.ts", contract["sourceFiles"])
        self.assertIn("src/utils/ShellCommand.ts", contract["sourceFiles"])
        self.assertTrue(contract["toolInterface"]["hasInputSchema"])
        self.assertTrue(contract["toolInterface"]["hasConcurrencyFlag"])
        self.assertTrue(contract["executionPipeline"]["hasPermissionGate"])
        self.assertTrue(contract["executionPipeline"]["hasSchemaValidation"])
        self.assertTrue(contract["executionPipeline"]["hasLargeResultExternalization"])
        self.assertTrue(contract["scheduling"]["readOnlyConcurrent"])
        self.assertTrue(contract["scheduling"]["writeSerial"])
        self.assertTrue(contract["resultBudget"]["hasMaxResultSizeChars"])
        self.assertTrue(contract["shellRuntime"]["hasProcessLifecycle"])
        self.assertTrue(contract["shellRuntime"]["hasReadOnlyCommandValidation"])
        self.assertTrue(contract["sandboxRuntime"]["hasSandboxAdapter"])
        self.assertTrue(contract["failureSignals"]["hasDenialLimits"])
        self.assertEqual(
            contract["zyraRuntimeMapping"]["toolLoop"],
            "packages/runtime/zyra_runtime/tool_loop.py",
        )

    def test_runtime_batches_read_only_tools_and_serializes_conflicting_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise read-only batching and write conflict protection.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "a.txt").write_text("alpha", encoding="utf-8")
            (workspace / "b.txt").write_text("beta", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "query_turns": [
                        [
                            {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                            {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                            {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "one"}},
                            {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "two"}},
                        ]
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            batches = _query_phases(run.event_records, "tool_batch_started")
            self.assertEqual([batch["execution_mode"] for batch in batches], [
                "concurrent_read_only",
                "serial_non_read_only",
                "serial_non_read_only",
            ])
            self.assertEqual(batches[0]["tool_count"], 2)
            self.assertEqual(batches[2]["conflict_protected"], "true")
            self.assertEqual(run.worker_result.metadata["tool_conflict_protected"], "1")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_owner_unit"], "M1-02C")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_read_only_concurrent"], "true")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_write_serial"], "true")
            self.assertEqual(run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertEqual((workspace / "same.txt").read_text(encoding="utf-8"), "two")

    def test_session_assistant_tool_use_and_opencode_part_enter_real_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Session assistant tool_use drives QueryEngine.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "alpha.txt").write_text("alpha-session", encoding="utf-8")
            (workspace / "beta.txt").write_text("beta-opencode", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_messages": [
                        {"role": "user", "content": "Read the two files from the active session."},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "session-tu-alpha",
                                    "name": "file_read",
                                    "input": {"path": "alpha.txt"},
                                }
                            ],
                        },
                    ],
                    "opencode_tool_parts": [
                        {
                            "type": "tool-call",
                            "callID": "opencode-tu-beta",
                            "name": "file_read",
                            "input": {"path": "beta.txt"},
                        }
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_session_bridge_valid_tool_uses"], "2")
            self.assertEqual(run.worker_result.metadata["tool_session_bridge_assistant_message_tool_uses"], "1")
            self.assertEqual(run.worker_result.metadata["tool_session_bridge_opencode_tool_uses"], "1")
            self.assertEqual(run.worker_result.metadata["tool_steps"], "2")
            accepted = _query_phases(run.event_records, "assistant_tool_use_accepted")
            self.assertEqual(len(accepted), 2)
            self.assertTrue(all(item["through_runtime"] == "ToolExecutionRuntime" for item in accepted))
            self.assertIn("session-tu-alpha", {item["assistant_tool_use_id"] for item in accepted})
            self.assertIn("opencode-tu-beta", {item["assistant_tool_use_id"] for item in accepted})
            tool_results = [event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload]
            self.assertEqual(len(tool_results), 2)
            self.assertEqual(run.worker_result.metadata["tool_result_context_projections"], "2")
            self.assertEqual(run.worker_result.metadata["tool_result_context_appended_messages"], "2")
            self.assertEqual(run.worker_result.metadata["tool_execution_timeline_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_result_replay_index_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_result_replay_index_tool_calls"], "2")
            self.assertEqual(run.worker_result.metadata["tool_readiness_matrix_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_effect_fingerprint_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_integration_ok"], "true")
            self.assertEqual(
                run.worker_result.metadata["tool_integration_requirement_session_tool_use_to_runtime"],
                "pass",
            )
            self.assertEqual(
                run.worker_result.metadata["tool_integration_requirement_tool_result_session_append"],
                "pass",
            )
            self.assertEqual(
                run.worker_result.metadata["tool_integration_requirement_opencode_or_hermes_effect"],
                "pass",
            )
            self.assertEqual(len(_query_phases(run.event_records, "tool_result_session_appended")), 2)
            self.assertEqual(len(_query_phases(run.event_records, "tool_result_context_projected")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_execution_timeline")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_result_replay_index")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_readiness_matrix")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_effect_fingerprint")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_integration_audit")), 1)

    def test_session_tool_use_without_upstream_id_keeps_stable_result_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Session assistant tool_use without upstream id.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "alpha.txt").write_text("alpha-no-id", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_messages": [
                        {"role": "user", "content": "Read alpha from the active session."},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "file_read",
                                    "input": {"path": "alpha.txt"},
                                }
                            ],
                        },
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            accepted = _query_phases(run.event_records, "assistant_tool_use_accepted")
            self.assertEqual(len(accepted), 1)
            self.assertTrue(accepted[0]["assistant_tool_use_id"])
            self.assertEqual(accepted[0]["tool_call_id"], accepted[0]["assistant_tool_use_id"])
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertEqual(tool_result["tool_call_id"], accepted[0]["tool_call_id"])
            projected = _query_phases(run.event_records, "tool_result_context_projected")[0]["tool_result_context"]
            self.assertEqual(projected["projections"][0]["tool_call_id"], accepted[0]["tool_call_id"])
            self.assertEqual(run.worker_result.metadata["tool_semantic_effect_ok"], "true")
            self.assertEqual(
                run.worker_result.metadata["tool_integration_requirement_session_tool_use_to_runtime"],
                "pass",
            )

    def test_runtime_externalizes_large_tool_result_and_emits_budget_watchdog_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Externalize large tool result.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("budget-" * 160, encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_result_budget_chars": 140,
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_result_externalizations"], "1")
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            budget_events = _query_phases(run.event_records, "tool_result_budget_exceeded")
            failure_events = _query_phases(run.event_records, "tool_failure_signal")
            watchdog_events = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(len(budget_events), 1)
            self.assertEqual(len(failure_events), 1)
            self.assertEqual(len(watchdog_events), 1)
            self.assertEqual(failure_events[0]["signal"]["kind"], "budget_exceeded")
            self.assertEqual(watchdog_events[0]["watchdog_signal"]["route"], "artifact_externalized")
            self.assertEqual(run.worker_result.metadata["tool_result_context_budgeted"], "1")
            self.assertEqual(run.worker_result.metadata["tool_result_context_raw_output_blocked"], "1")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_result_context_artifact_refs"]), 1)
            self.assertEqual(run.worker_result.metadata["tool_budget_chain_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_budget_chain_budgeted"], "1")
            self.assertEqual(run.worker_result.metadata["tool_result_replay_index_ok"], "true")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_result_replay_index_externalized"]), 1)
            self.assertEqual(run.worker_result.metadata["tool_effect_fingerprint_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_integration_requirement_budgeted_result_context"], "pass")
            projected = _query_phases(run.event_records, "tool_result_context_projected")[0]["tool_result_context"]
            self.assertEqual(projected["budgeted_count"], 1)
            self.assertEqual(projected["raw_output_blocked_count"], 1)
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertTrue(tool_result["output"]["truncated"])
            artifact_id = tool_result["output"]["full_output_artifact_id"]
            artifact = next(item for item in run.worker_result.artifacts if item.artifact_id == artifact_id)
            self.assertIn("budget-", json.loads(Path(artifact.uri).read_text(encoding="utf-8"))["content"])

    def test_runtime_converts_schema_error_to_failure_and_watchdog_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Schema error signals.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "continue_on_error": True,
                    "tool_plan": [{"tool_name": "file_write", "arguments": {"path": "bad.txt"}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_schema_errors"], "1")
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            failures = _query_phases(run.event_records, "tool_failure_signal")
            watchdogs = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(failures[0]["signal"]["kind"], "schema_error")
            self.assertEqual(watchdogs[0]["watchdog_signal"]["route"], "repair_tool_arguments")
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertFalse(tool_result["ok"])
            self.assertEqual(tool_result["error"], "schema_error")

    def test_runtime_converts_permission_denial_to_watchdog_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission denial signals.")
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "continue_on_error": True,
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": str(outside)}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            failures = _query_phases(run.event_records, "tool_failure_signal")
            watchdogs = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(failures[0]["signal"]["kind"], "permission_denied")
            self.assertEqual(watchdogs[0]["watchdog_signal"]["route"], "permission_runtime")
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertFalse(tool_result["ok"])
            self.assertEqual(tool_result["error"], "permission_denied")


class CodeWorkerToolLoopFoundationRuntimeTests(unittest.TestCase):
    def test_default_worker_path_emits_tool_foundation_context_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise tool foundation events.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "readme.txt").write_text("hello", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {"tool_name": "file_read", "arguments": {"path": "readme.txt"}},
                        {"tool_name": "file_write", "arguments": {"path": "done.txt", "content": "ok"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_foundation_owner_unit"], "M1-02C")
            self.assertEqual(run.worker_result.metadata["tool_foundation_audit_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_foundation_audit_status"], "pass")
            self.assertEqual(run.worker_result.metadata["tool_foundation_replay_artifact_written"], "true")
            self.assertEqual(run.worker_result.metadata["tool_permission_handoff_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_permission_handoff_questions"], "0")
            self.assertEqual(run.worker_result.metadata["tool_budget_policy_ok"], "true")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_budget_policy_entries"]), 2)
            self.assertEqual(run.worker_result.metadata["tool_streaming_ok"], "true")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_streaming_frames"]), 6)
            self.assertEqual(run.worker_result.metadata["tool_continuation_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_continuation_missing_messages"], "0")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_continuation_pairs"]), 2)
            self.assertEqual(run.worker_result.metadata["tool_concurrency_ok"], "true")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_concurrency_batches"]), 1)
            self.assertEqual(run.worker_result.metadata["tool_failure_policy_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_failure_policy_status"], "clean")
            self.assertEqual(run.worker_result.metadata["tool_output_store_status"], "ready")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_output_store_entries"]), 2)
            self.assertEqual(run.worker_result.metadata["tool_output_store_artifact_written"], "true")
            self.assertEqual(run.worker_result.metadata["tool_source_coverage_ok"], "true")
            self.assertIn("opencode", run.worker_result.metadata["tool_source_coverage_repos"])
            self.assertEqual(run.worker_result.metadata["tool_cleanroom_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_cleanroom_status"], "pass")
            self.assertEqual(run.worker_result.metadata["tool_execution_timeline_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_budget_chain_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_continuation_packet_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_replay_state_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_semantic_effect_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_result_replay_index_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_source_effects_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_readiness_matrix_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_effect_fingerprint_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_contract_gate_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_contract_gate_status"], "pass")
            self.assertEqual(run.worker_result.metadata["tool_settlement_all_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_settlement_reports"], "1")
            self.assertEqual(run.worker_result.metadata["tool_registry_active_count"], "9")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_use_context_modifiers"]), 4)
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_foundation_persisted_artifacts"]), 5)
            self.assertEqual(len(_query_phases(run.event_records, "tool_registry_materialized")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_foundation_audit")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_foundation_persisted")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_permission_handoff")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_budget_policy")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_streaming_report")), 1)
            self.assertGreaterEqual(len(_query_phases(run.event_records, "tool_stream_frame")), 6)
            self.assertEqual(len(_query_phases(run.event_records, "tool_continuation_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_concurrency_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_failure_policy")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_output_store_persisted")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_source_coverage")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_cleanroom_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_execution_timeline")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_budget_chain")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_continuation_packet")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_replay_state")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_semantic_effects")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_result_replay_index")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_source_effects")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_readiness_matrix")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_effect_fingerprint")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_contract_gate")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_registry_settled")), 2)
            self.assertGreaterEqual(len(_query_phases(run.event_records, "tool_context_modifier_applied")), 4)
            self.assertTrue(run.worker_result.metadata["tool_foundation_receipt_log_artifact_id"])
            self.assertTrue(run.worker_result.metadata["tool_foundation_context_snapshot_artifact_id"])
            self.assertTrue(run.worker_result.metadata["tool_output_store_artifact_id"])
            self.assertEqual((workspace / "done.txt").read_text(encoding="utf-8"), "ok")

    def test_default_worker_path_fails_when_core_tool_foundation_runtime_is_disabled(self) -> None:
        cases = (
            ("disable_tool_registry_runtime", "ToolRegistryRuntime"),
            ("disable_tool_execution_runtime", "ToolExecutionRuntime"),
            ("disable_tool_result_budget_runtime", "ToolResultBudgetRuntime"),
        )
        for constraint_name, component in cases:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmpdir:
                state = create_task_state(f"Disable {component}.")
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                runtime = CodeWorkerRuntime(
                    project_root=ROOT,
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                )
                request = WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        constraint_name: True,
                        "tool_plan": [{"tool_name": "file_write", "arguments": {"path": "nope.txt", "content": "no"}}],
                    },
                )

                run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "tool_loop_foundation_disabled")
            self.assertEqual(run.worker_result.metadata["tool_foundation_disabled_component"], component)
            self.assertFalse((workspace / "nope.txt").exists())
            disabled_events = _query_phases(run.event_records, "tool_loop_foundation_disabled")
            self.assertEqual(len(disabled_events), 1)

    def test_permission_handoff_pending_appends_question_and_blocks_shell_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission pending session bridge.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            permission_store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                permission_store=permission_store,
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "continue_on_error": True,
                    "session_messages": [
                        {"role": "user", "content": "Try the shell command after asking permission."},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "session-tu-shell",
                                    "name": "shell",
                                    "input": {
                                        "command": "cmd.exe /c echo blocked > should_not_exist.txt",
                                        "timeout_seconds": 5,
                                    },
                                }
                            ],
                        },
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertFalse((workspace / "should_not_exist.txt").exists())
            self.assertEqual(run.worker_result.metadata["tool_permission_handoff_questions"], "1")
            self.assertEqual(run.worker_result.metadata["tool_permission_session_questions"], "1")
            self.assertEqual(run.worker_result.metadata["tool_permission_session_pending"], "1")
            self.assertGreaterEqual(int(run.worker_result.metadata["tool_permission_checkpoint_pending"]), 1)
            self.assertEqual(run.worker_result.metadata["tool_permission_checkpoint_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_semantic_effect_ok"], "true")
            self.assertEqual(run.worker_result.metadata["tool_result_context_permission_required"], "1")
            self.assertEqual(run.worker_result.metadata["tool_integration_requirement_permission_pending_handoff"], "pass")
            self.assertEqual(len(permission_store.list_requests()), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_permission_question_appended")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "tool_permission_pending_blocker")), 1)
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertFalse(tool_result["ok"])
            self.assertEqual(tool_result["error"], "permission_required")

    def test_permission_handoff_disconnect_changes_default_tool_loop_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disable permission handoff.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                permission_store=JsonPermissionStore(Path(tmpdir) / "permissions.json"),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "disable_tool_permission_handoff_runtime": True,
                    "session_messages": [
                        {"role": "user", "content": "Read the file."},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "session-tu-disabled",
                                    "name": "file_read",
                                    "input": {"path": "missing.txt"},
                                }
                            ],
                        },
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "tool_loop_foundation_disabled")
            self.assertEqual(run.worker_result.metadata["tool_foundation_disabled_component"], "ToolPermissionHandoffRuntime")
            self.assertEqual(len(_query_phases(run.event_records, "tool_loop_foundation_disabled")), 1)

    def test_runtime_gate_failure_changes_worker_result_not_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Runtime gate failure should fail worker result.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "readme.txt").write_text("hello", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={"tool_plan": [{"tool_name": "file_read", "arguments": {"path": "readme.txt"}}]},
            )

            def blocked_semantic_report(self, **kwargs):
                return ToolSemanticEffectReport(
                    report_id="semantic-blocked",
                    owner_unit="M1-02C",
                    runtime_id="test-runtime",
                    session_id=str(kwargs["session_id"]),
                    worker_request_id=str(kwargs["worker_request_id"]),
                    workspace_root=str(kwargs["workspace_root"]),
                    effects=(),
                    findings=(
                        ToolSemanticFinding(
                            code="FORCED_SEMANTIC_BLOCK",
                            severity=ToolSemanticSeverity.BLOCKER,
                            surface=ToolSemanticSurface.RESULT_CONTEXT,
                            message="forced semantic blocker",
                        ),
                    ),
                )

            with patch(
                "zyra_runtime.claude_query_engine_runtime.ToolSemanticEffectRuntime.build_report",
                blocked_semantic_report,
            ):
                run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "tool_runtime_gate_failed")
            self.assertEqual(run.worker_result.metadata["tool_semantic_effect_ok"], "false")
            self.assertEqual(run.worker_result.metadata["tool_runtime_gate_ok"], "false")
            self.assertIn("tool_semantic_effects", run.worker_result.metadata["tool_runtime_gate_failures"])
            self.assertEqual(len(_query_phases(run.event_records, "tool_runtime_gate_failed")), 1)


def _query_phases(event_records, phase: str) -> list[dict]:
    return [
        event.payload["query_session"]
        for event in event_records
        if event.payload.get("query_session", {}).get("phase") == phase
    ]


if __name__ == "__main__":
    unittest.main()
