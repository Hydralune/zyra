from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


def _phases(run: object) -> list[str]:
    return [
        event.payload["query_session"]["phase"]
        for event in run.event_records
        if isinstance(event.payload, dict)
        and isinstance(event.payload.get("query_session"), dict)
    ]


class CodeWorkerQuerySessionFoundationTests(unittest.TestCase):
    """The session foundation the productized CodeWorker actually owns.

    Productization moved the query session from a Python "session foundation"
    layer -- with its own seed, input-processor, context-assembly and session
    store gates -- into the canonical TypeScript query engine.  The phases and
    metadata that layer emitted no longer exist, and its
    ``disable_code_worker_*`` constraints are inert.  These cases assert the
    lifecycle and the fail-closed gates the runtime has today.
    """

    def _runtime(self, tmpdir: str) -> tuple[CodeWorkerRuntime, object]:
        return (
            CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            ),
            create_task_state("session foundation integration"),
        )

    def _request(self, state: object, **constraints: object) -> WorkerRequest:
        return WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints=constraints,
        )

    def test_code_worker_default_path_emits_query_session_lifecycle_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime, state = self._runtime(tmpdir)
            run = runtime.run(self._request(
                state,
                raw_input="Create and read a file through the session foundation.",
                # Nothing here can answer an approval, so the workspace edit
                # would park instead of exercising the lifecycle.
                permission_mode="acceptEdits",
                permission_interactive=False,
                permission_headless=True,
                query_turns=[[
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "foundation/result.txt", "content": "ok"},
                    },
                    {"tool_name": "file_read", "arguments": {"path": "foundation/result.txt"}},
                ]],
            ))

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            phases = _phases(run)
            for phase in (
                "session_started",
                "stream_request_start",
                "turn_started",
                "tool_call_started",
                "tool_call_completed",
                "turn_completed",
                "session_completed",
                "query_session_snapshot",
            ):
                self.assertIn(phase, phases)

            metadata = run.worker_result.metadata
            self.assertEqual(metadata["loop"], "zyra_typescript_query_engine_runtime")
            self.assertEqual(metadata["query_contract_source"], "zyra-claude-productized")
            self.assertEqual(metadata["canonical_runtime_owner"], "typescript")
            # The session must be durable and self-consistent, and it must not
            # have been produced by a Python re-implementation of the engine.
            self.assertEqual(metadata["query_session_consistent"], "true")
            self.assertEqual(metadata["query_session_checkpoint_ready"], "true")
            self.assertEqual(metadata["python_query_engine_fallback"], "false")
            self.assertEqual(metadata["python_session_projection_canonical"], "false")
            self.assertEqual(metadata["query_plan_ok"], "true")
            self.assertEqual(metadata["query_plan_tool_steps"], "2")
            self.assertTrue(metadata["query_session_id"])
            self.assertTrue(metadata["query_session_snapshot_artifact_id"])
            self.assertTrue(metadata["query_session_transcript_artifact_id"])
            self.assertEqual(metadata["runtime_state_ok"], "true")
            self.assertTrue(Path(metadata["runtime_state_checkpoint_path"]).exists())

    def test_code_worker_blocks_before_the_loop_when_a_durable_component_is_disabled(self) -> None:
        """Each required durable permission component still fails closed.

        The gate has to fire before the model request, otherwise a worker with
        no decision log or rule store could still reach a side effect.
        """

        gates = {
            "disable_tool_permission_runtime": "E02CapabilityCoordinator",
            "disable_permission_rule_store": "PermissionRuleStore",
            "disable_permission_request_queue": "PermissionRequestQueue",
            "disable_permission_decision_log": "PermissionDecisionLog",
        }
        for constraint, component in gates.items():
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory() as tmpdir:
                    runtime, state = self._runtime(tmpdir)
                    run = runtime.run(self._request(
                        state,
                        **{constraint: True},
                        raw_input="This should not reach QueryEngine.",
                        permission_mode="acceptEdits",
                        permission_interactive=False,
                        permission_headless=True,
                        query_turns=[[{
                            "tool_name": "file_write",
                            "arguments": {"path": "blocked.txt", "content": "no"},
                        }]],
                    ))

                    self.assertFalse(run.worker_result.ok)
                    self.assertEqual(
                        run.worker_result.error,
                        "tool_loop_foundation_disabled",
                    )
                    phases = _phases(run)
                    self.assertIn("tool_loop_foundation_disabled", phases)
                    self.assertNotIn("stream_request_start", phases)
                    self.assertNotIn("tool_call_started", phases)
                    self.assertFalse(
                        (Path(tmpdir) / "workspace" / "blocked.txt").exists()
                    )

    def test_retired_session_foundation_constraints_are_inert_rather_than_silently_partial(self) -> None:
        """The retired gates must not look like they still protect anything.

        ``disable_code_worker_session_store`` and its siblings were part of the
        Python session foundation.  They are no longer wired to anything, so a
        caller passing one gets a normal run -- assert that plainly instead of
        letting a stale flag read as a live safeguard.
        """

        for constraint in (
            "disable_code_worker_session_store",
            "disable_context_assembly",
            "disable_query_input_processor",
        ):
            with self.subTest(constraint=constraint):
                with tempfile.TemporaryDirectory() as tmpdir:
                    runtime, state = self._runtime(tmpdir)
                    run = runtime.run(self._request(
                        state,
                        **{constraint: True},
                        raw_input="Retired flags do not gate the runtime.",
                        permission_mode="acceptEdits",
                        permission_interactive=False,
                        permission_headless=True,
                        query_turns=[[{
                            "tool_name": "file_write",
                            "arguments": {"path": "retired.txt", "content": "ok"},
                        }]],
                    ))

                    self.assertTrue(run.worker_result.ok, run.worker_result.error)
                    self.assertIn("stream_request_start", _phases(run))
                    self.assertNotIn(
                        "code_worker_session_seed_ok",
                        run.worker_result.metadata,
                    )


if __name__ == "__main__":
    unittest.main()
