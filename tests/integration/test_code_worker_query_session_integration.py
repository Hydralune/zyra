from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


# Nothing in an integration run can answer an approval prompt, and a workspace
# edit under the `default` mode parks for one.  Every case here is about the
# query session, not about permission, so declare the absent approver once.
_HEADLESS = {
    "permission_mode": "acceptEdits",
    "permission_interactive": False,
    "permission_headless": True,
}


class CodeWorkerQuerySessionIntegrationTests(unittest.TestCase):
    """The query-session entry the productized CodeWorker actually owns.

    Productization replaced the Python "query entry packet / handoff custody"
    layer with the canonical TypeScript query engine.  Its phases
    (``query_entry_packet_ready``, ``query_started``, ``query_cancelled`` ...)
    and its metadata (``query_session_integration_ok``,
    ``query_entry_block_reason`` ...) are gone, and the request-level session
    controls it read are inert.  Operator control now travels as a canonical
    control command, which is what these cases exercise.
    """

    def test_default_path_reaches_the_typescript_query_engine_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "raw_input": "Create and read through query entry.",
                    **_HEADLESS,
                    "query_turns": [
                        [
                            {"tool_name": "file_write", "arguments": {"path": "entry/result.txt", "content": "ok"}},
                            {"tool_name": "file_read", "arguments": {"path": "entry/result.txt"}},
                        ]
                    ],
                },
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["loop"], "zyra_typescript_query_engine_runtime")
            self.assertEqual(metadata["canonical_runtime_owner"], "typescript")
            self.assertEqual(metadata["python_query_engine_fallback"], "false")
            self.assertEqual(metadata["query_session_consistent"], "true")
            self.assertTrue(metadata["query_session_resume_token"])

            phases = _query_phases(run)
            for phase in (
                "session_started",
                "stream_request_start",
                "tool_call_started",
                "tool_call_completed",
                "session_completed",
            ):
                self.assertIn(phase, phases)
            # The session has to open before the model stream, and the stream
            # before any tool effect.
            self.assertLess(
                phases.index("session_started"),
                phases.index("stream_request_start"),
            )
            self.assertLess(
                phases.index("stream_request_start"),
                phases.index("tool_call_started"),
            )

    def test_cancel_control_command_blocks_the_engine_before_any_side_effect(self) -> None:
        """Operator cancel is a canonical control command, not a constraint.

        The value of this case is that cancel lands *before* the model stream
        and before the tool effect -- a cancel observed only after the write
        would not be a cancel.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "control_commands": [{"name": "cancel"}],
                    "raw_input": "This should not enter QueryEngine.",
                    **_HEADLESS,
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "x"}}]],
                },
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "user_cancelled")
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["control_command_count"], "1")
            self.assertEqual(metadata["control_command_failed"], "0")
            self.assertEqual(metadata["canonical_control_owner"], "typescript")
            self.assertEqual(metadata["control_state_revision"], "1")
            phases = _query_phases(run)
            self.assertNotIn("stream_request_start", phases)
            self.assertNotIn("tool_call_started", phases)
            self.assertFalse((Path(tmpdir) / "workspace" / "blocked.txt").exists())

    def test_retired_request_level_session_controls_are_inert(self) -> None:
        """These constraints no longer gate anything, so say so out loud.

        ``cancel_session``, ``interrupt_session``, ``disable_query_entry_packet``,
        ``expected_context_fingerprint`` and ``resume_session_id`` belonged to
        the retired Python query-entry layer.  A caller who still passes one
        gets an ordinary run.  Pinning that keeps a stale flag from reading as
        a live safeguard -- use ``control_commands`` for operator control.
        """

        retired = (
            {"cancel_session": True, "cancel_reason": "operator cancelled"},
            {"interrupt_session": True, "interrupt_reason": "operator interrupted"},
            {"disable_query_entry_packet": True},
            {"expected_context_fingerprint": "definitely-stale-context"},
            {"resume_session_id": "query:does-not-exist:task"},
        )
        for constraints in retired:
            with self.subTest(constraints=sorted(constraints)):
                with tempfile.TemporaryDirectory() as tmpdir:
                    run = _run_worker(
                        tmpdir,
                        {
                            **constraints,
                            "raw_input": "Retired controls do not gate the runtime.",
                            **_HEADLESS,
                            "query_turns": [[{
                                "tool_name": "file_write",
                                "arguments": {"path": "not-blocked.txt", "content": "x"},
                            }]],
                        },
                    )

                    self.assertTrue(run.worker_result.ok, run.worker_result.error)
                    self.assertIn("stream_request_start", _query_phases(run))
                    self.assertTrue(
                        (Path(tmpdir) / "workspace" / "not-blocked.txt").exists()
                    )
                    self.assertNotIn(
                        "query_entry_block_reason",
                        run.worker_result.metadata,
                    )

    def test_runtime_state_checkpoint_is_written_and_reloadable(self) -> None:
        """The durable replacement for the retired query-checkpoint record."""

        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "raw_input": "Checkpoint before query.",
                    **_HEADLESS,
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "checkpoint.txt", "content": "ok"}}]],
                },
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["runtime_state_ok"], "true")
            self.assertEqual(metadata["query_session_checkpoint_ready"], "true")
            self.assertGreater(int(metadata["runtime_state_mutations"]), 0)
            checkpoint = Path(metadata["runtime_state_checkpoint_path"])
            self.assertTrue(checkpoint.is_file())
            self.assertTrue(json.loads(checkpoint.read_text(encoding="utf-8")))
            self.assertTrue(metadata["query_session_snapshot_artifact_id"])
            self.assertTrue(metadata["query_session_transcript_artifact_id"])


class CodeWorkerQuerySessionIntegrationApiTests(unittest.TestCase):
    def test_session_integration_endpoint_returns_typescript_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                payload = _get(base_url, "/workers/code/session-integration?q=Inspect%20query%20entry")

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["canonicalOwner"], "typescript")
                self.assertEqual(payload["source"], "zyra-typescript-runtime")
                self.assertTrue(payload["budgets"]["toolResultBudget"])
                self.assertTrue(payload["compactRuntime"]["postCompactRestore"])
                self.assertEqual(
                    payload["sessionContract"]["snapshotVersion"],
                    "zyra.typescript-query-session.v1",
                )
                self.assertFalse(payload["sessionContract"]["pythonProjectionIsCanonical"])
                self.assertTrue(payload["defaultRoute"])
                self.assertFalse(payload["fallbackUsed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                from apps.api.zyra_api.main import reset_api_product_bootstrap
                from apps.api.zyra_api.experiment_api import reset_experiment_api

                reset_experiment_api(wait=True)
                reset_api_product_bootstrap()
                gc.collect()


def _run_worker(tmpdir: str, constraints: dict[str, Any]) -> Any:
    state = create_task_state("query session integration")
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
        constraints=constraints,
    )
    return runtime.run(request)


def _query_phases(run: Any) -> list[str]:
    return [
        event.payload["query_session"]["phase"]
        for event in run.event_records
        if isinstance(event.payload, dict) and "query_session" in event.payload
    ]


def _store_records(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
