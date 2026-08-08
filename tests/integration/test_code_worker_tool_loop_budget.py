from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state
from zyra_runtime import WorkerRequest
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient


@unittest.skipIf(
    shutil.which("node") is None and shutil.which("bun") is None,
    "TypeScript runtime is required",
)
class CodeWorkerToolLoopBudgetTests(unittest.TestCase):
    def test_tool_contract_assigns_orchestration_to_typescript_and_effects_to_python(self) -> None:
        contract = CodeWorkerSidecarClient(ROOT).tool_loop_contract()

        self.assertEqual(contract["source"], "zyra-typescript-runtime")
        self.assertTrue(contract["schemaValidation"])
        self.assertTrue(contract["readOnlyConcurrent"])
        self.assertTrue(contract["writeSerial"])
        self.assertEqual(contract["resultBudgetOwner"], "typescript")
        # Since the e02 cutover a side effect is owned by the Python tool
        # gateway or by a TypeScript capability, depending on which one owns
        # the route; orchestration stays with TypeScript either way.
        self.assertEqual(
            contract["sideEffectOwner"],
            "python-tool-gateway-or-typescript-capability",
        )

    def test_read_only_tools_batch_and_conflicting_writes_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, workspace = _runtime(tmpdir, "Exercise batching and write conflicts.")
            (workspace / "a.txt").write_text("alpha", encoding="utf-8")
            (workspace / "b.txt").write_text("beta", encoding="utf-8")
            run = runtime.run(
                _request(
                    state,
                    # This case measures batching and write serialization, so the
                    # writes have to reach the gateway.  Under the `default`
                    # mode a workspace edit parks for approval and nothing here
                    # can answer it, which measures the permission runtime
                    # instead of the tool loop.
                    permission_mode="acceptEdits",
                    permission_interactive=False,
                    permission_headless=True,
                    query_turns=[[
                        {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                        {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "one"}},
                        {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "two"}},
                    ]],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            batches = _query_events(run.event_records, "tool_batch_started")
            # How many batches the writes are split across is the runtime's
            # own packing choice.  What this case owns is that reads share one
            # concurrent batch, that nothing non-read-only joins them, and that
            # the conflicting writes land in a declared order.
            self.assertEqual(batches[0]["execution_mode"], "concurrent_read_only")
            self.assertEqual(batches[0]["tool_count"], 2)
            self.assertEqual(
                {item["execution_mode"] for item in batches[1:]},
                {"serial_non_read_only"},
            )
            self.assertTrue(
                any(item.get("conflict_protected") == "true" for item in batches[1:]),
                batches,
            )
            self.assertEqual(run.worker_result.metadata["tool_runtime_completed"], "4")
            self.assertEqual(run.worker_result.metadata["tool_runtime_mutating"], "2")
            self.assertEqual(run.worker_result.metadata["tool_conflict_protected"], "1")
            self.assertEqual((workspace / "same.txt").read_text(encoding="utf-8"), "two")

    def test_adapter_tool_use_formats_do_not_reenter_the_productized_tool_loop(self) -> None:
        """Productization retired the sidecar adapter formats deliberately.

        The CodeWorker used to accept upstream ``session_messages`` and
        opencode ``tool_parts`` and replay them as tool steps.  The productized
        runtime owns one canonical entry -- ``query_turns`` -- and
        ``verify_claude_productization_foundation`` fails the clean-runtime
        probe when ``sidecar_contracts_used`` is true.  Assert the retirement
        holds so an adapter path cannot quietly come back.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, workspace = _runtime(tmpdir, "Bridge session tool uses.")
            (workspace / "alpha.txt").write_text("alpha", encoding="utf-8")
            (workspace / "beta.txt").write_text("beta", encoding="utf-8")
            run = runtime.run(
                _request(
                    state,
                    session_messages=[
                        {"role": "user", "content": "Read both files."},
                        {
                            "role": "assistant",
                            "content": [{
                                "type": "tool_use",
                                "id": "session-alpha",
                                "name": "file_read",
                                "input": {"path": "alpha.txt"},
                            }],
                        },
                    ],
                    opencode_tool_parts=[{
                        "type": "tool-call",
                        "callID": "opencode-beta",
                        "name": "file_read",
                        "input": {"path": "beta.txt"},
                    }],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertEqual(run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertEqual(run.worker_result.metadata["tool_steps"], "0")
            self.assertEqual(
                run.worker_result.metadata["loop"],
                "zyra_typescript_query_engine_runtime",
            )
            completed = _query_events(run.event_records, "tool_call_completed")
            self.assertEqual(
                {item["tool_call_id"] for item in completed} & {"session-alpha", "opencode-beta"},
                set(),
            )

    def test_canonical_query_turns_reach_the_typescript_tool_loop(self) -> None:
        """The canonical replacement for the retired adapter formats."""

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, workspace = _runtime(tmpdir, "Read both files.")
            (workspace / "alpha.txt").write_text("alpha", encoding="utf-8")
            (workspace / "beta.txt").write_text("beta", encoding="utf-8")
            run = runtime.run(
                _request(
                    state,
                    query_turns=[[
                        {"tool_name": "file_read", "arguments": {"path": "alpha.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "beta.txt"}},
                    ]],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertEqual(run.worker_result.metadata["tool_steps"], "2")
            completed = _query_events(run.event_records, "tool_call_completed")
            self.assertEqual(len(completed), 2)
            self.assertTrue(all(item["tool_result"]["ok"] for item in completed))

    def test_large_result_is_externalized_and_routes_budget_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, workspace = _runtime(tmpdir, "Externalize a large result.")
            (workspace / "large.txt").write_text("large-result-" * 500, encoding="utf-8")
            run = runtime.run(
                _request(
                    state,
                    tool_result_budget_chars=250,
                    query_turns=[[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}]],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertEqual(run.worker_result.metadata["tool_result_externalizations"], "1")
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            self.assertEqual(len(_query_events(run.event_records, "tool_result_budget_exceeded")), 1)
            signal = _query_events(run.event_records, "tool_failure_signal")[-1]["signal"]
            self.assertEqual(signal["kind"], "budget_exceeded")
            self.assertEqual(signal["route"], "artifact_externalized")

    def test_schema_failure_is_owned_and_routed_by_typescript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, _ = _runtime(tmpdir, "Reject invalid tool arguments.")
            run = runtime.run(
                _request(
                    state,
                    query_turns=[[{"tool_name": "file_write", "arguments": {"path": "invalid.txt"}}]],
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_schema_errors"], "1")
            signal = _query_events(run.event_records, "tool_failure_signal")[-1]["signal"]
            self.assertEqual(signal["kind"], "schema_error")
            self.assertEqual(signal["route"], "repair_tool_arguments")

    def test_permission_denial_blocks_python_side_effect_and_returns_to_typescript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state, workspace = _runtime(tmpdir, "Deny an interactive shell command.")
            run = runtime.run(
                _request(
                    state,
                    # Nothing in this run can answer an approval prompt, and the
                    # case is about the denial reaching the permission runtime
                    # rather than about parking.  Declaring that lets the
                    # evaluator settle the high-risk ASK as a denial.
                    permission_interactive=False,
                    permission_headless=True,
                    query_turns=[[
                        {
                            "tool_name": "shell",
                            "arguments": {
                                "command": "Set-Content -LiteralPath permission-leak.txt -Value forbidden"
                            },
                        }
                    ]],
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertFalse((workspace / "permission-leak.txt").exists())
            signal = _query_events(run.event_records, "tool_failure_signal")[-1]["signal"]
            self.assertEqual(signal["kind"], "permission_denied")
            self.assertEqual(signal["route"], "permission_runtime")

    def test_disabling_any_required_tool_component_fails_before_mutation(self) -> None:
        for constraint in (
            "disable_tool_registry_runtime",
            "disable_tool_execution_runtime",
            "disable_tool_result_budget_runtime",
            "disable_tool_permission_handoff_runtime",
        ):
            with self.subTest(constraint=constraint), tempfile.TemporaryDirectory() as tmpdir:
                runtime, state, workspace = _runtime(tmpdir, "Disconnect " + constraint)
                run = runtime.run(
                    _request(
                        state,
                        **{
                            constraint: True,
                            "query_turns": [[{
                                "tool_name": "file_write",
                                "arguments": {"path": "must-not-exist.txt", "content": "forbidden"},
                            }]],
                        },
                    )
                )

                self.assertFalse(run.worker_result.ok)
                self.assertEqual(run.worker_result.error, "tool_loop_foundation_disabled")
                self.assertIn(constraint, run.worker_result.metadata["tool_runtime_gate_failures"])
                self.assertFalse((workspace / "must-not-exist.txt").exists())


def _runtime(tmpdir: str, goal: str) -> tuple[CodeWorkerRuntime, object, Path]:
    state = create_task_state(goal)
    workspace = Path(tmpdir) / "workspace"
    workspace.mkdir()
    return (
        CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=Path(tmpdir) / "artifacts",
        ),
        state,
        workspace,
    )


def _request(state: object, **constraints: object) -> WorkerRequest:
    return WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        worker_name="CodeWorkerRuntime",
        constraints=constraints,
    )


def _query_events(events: list[object], phase: str) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for event in events:
        payload = getattr(event, "payload", {})
        query = payload.get("query_session") if isinstance(payload, dict) else None
        if isinstance(query, dict) and query.get("phase") == phase:
            selected.append(query)
    return selected


if __name__ == "__main__":
    unittest.main()
