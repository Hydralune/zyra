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


class CodeWorkerContextCompactApiFoundationTests(unittest.TestCase):
    def test_default_path_emits_compact_restore_model_stream_retry_and_budget_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise context compact and CodeWorker API foundation.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("compact-api-foundation-" * 180, encoding="utf-8")
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
                    "raw_input": "Read the large file and preserve next-turn compact state.",
                    "query_context_budget_chars": 900,
                    "tool_result_budget_chars": 7000,
                    "force_compact_restore": True,
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                    "restore_files": ["large.txt"],
                    "invoked_skills": ["codeworker-api-foundation"],
                    "active_plan": "Continue with the compacted file analysis on the next turn.",
                    "mcp_instruction_deltas": {"workspace": "Keep workspace restore instructions after compact."},
                    "deferred_tools": ["file_write"],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["codeworker_api_foundation_ok"], "true")
            self.assertEqual(metadata["compact_restore_ok"], "true")
            self.assertEqual(metadata["model_stream_ok"], "true")
            self.assertEqual(metadata["model_stream_watchdog_ok"], "true")
            self.assertEqual(metadata["api_retry_ok"], "true")
            self.assertEqual(metadata["runtime_budget_state_ok"], "true")
            self.assertEqual(metadata["runtime_budget_replay_ok"], "true")
            self.assertEqual(metadata["context_epoch_ok"], "true")
            self.assertEqual(metadata["compact_restore_policy_ok"], "true")
            self.assertEqual(metadata["api_retry_playbook_ok"], "true")
            self.assertEqual(metadata["compact_state_projection_ok"], "true")
            self.assertTrue(metadata["compact_restore_boundary_id"])
            self.assertTrue(metadata["compact_restore_contract_id"])
            self.assertGreaterEqual(int(metadata["compact_restore_segments"]), 4)
            self.assertGreaterEqual(int(metadata["compact_restore_preserved_segments"]), 1)
            self.assertGreaterEqual(int(metadata["model_stream_frames"]), 4)
            self.assertGreaterEqual(int(metadata["context_epoch_restore_epochs"]), 1)
            self.assertGreaterEqual(int(metadata["runtime_budget_replay_mutations"]), 4)
            self.assertGreaterEqual(int(metadata["compact_state_projection_sections"]), 11)
            self.assertEqual(metadata["api_retry_status"], "not_needed")
            self.assertGreater(int(metadata["runtime_budget_state_context_used_chars"]), 0)
            self.assertIn("RuntimeBudgetState", metadata["codeworker_api_foundation_chain"])
            self.assertEqual(len(_query_phases(run.event_records, "compact_restore_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "compact_restore_policy")), 1)
            self.assertGreaterEqual(
                len(_query_phases(run.event_records, "compact_boundary_created"))
                + len(_query_phases(run.event_records, "compact_needed")),
                1,
            )
            self.assertEqual(len(_query_phases(run.event_records, "next_turn_restore_contract")), 1)
            self.assertGreaterEqual(len(_query_phases(run.event_records, "model_stream_frame")), 4)
            self.assertEqual(len(_query_phases(run.event_records, "model_stream_watchdog")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "context_epoch_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "api_retry_report")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "api_retry_playbook")), 1)
            self.assertEqual(len(_query_phases(run.event_records, "runtime_budget_replay")), 1)
            replay = _query_phases(run.event_records, "runtime_budget_replay")[0]["runtime_budget_replay"]
            replay_finding_codes = {finding["code"] for finding in replay["findings"]}
            self.assertNotIn("RESTORE_BEFORE_COMPACT", replay_finding_codes)
            compact_sequences = [
                node["sequence"] for node in replay["nodes"] if node["kind"] == "compact_boundary"
            ]
            restore_sequences = [
                node["sequence"] for node in replay["nodes"] if node["kind"] == "next_turn_restore"
            ]
            self.assertTrue(compact_sequences)
            self.assertTrue(restore_sequences)
            self.assertLess(min(compact_sequences), min(restore_sequences))
            self.assertEqual(len(_query_phases(run.event_records, "codeworker_api_foundation")), 1)
            api_audit = _query_phases(run.event_records, "codeworker_api_foundation_audit")[0][
                "codeworker_api_foundation_audit"
            ]
            self.assertEqual(api_audit["status"], "pass")
            self.assertTrue(all(item["reached_by_default_path"] for item in api_audit["reachability"]))
            self.assertTrue(all(item["ok"] for item in api_audit["reachability"]))
            self.assertTrue(any(item["disabled_changes_result"] is False for item in api_audit["reachability"]))
            self.assertTrue(
                all(item["disable_semantics_required"] is False for item in api_audit["reachability"])
            )
            self.assertEqual(len(_query_phases(run.event_records, "compact_state_projection")), 1)

    def test_rate_limit_retry_and_model_fallback_are_real_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise model API retry and fallback.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "probe.txt").write_text("fallback probe", encoding="utf-8")
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
                    "raw_input": "Trigger model unavailable fallback but keep tool execution real.",
                    "query_context_budget_chars": 1400,
                    "force_compact_restore": True,
                    "model_stream_error_kind": "model_unavailable",
                    "api_retry_fallback_models": "zyra-fallback-a,zyra-fallback-b",
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "probe.txt"}}],
                    "restore_files": ["README.md"],
                    "invoked_skills": ["api-retry"],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["model_stream_ok"], "false")
            self.assertEqual(metadata["model_stream_error_kind"], "model_unavailable")
            self.assertEqual(metadata["api_retry_ok"], "true")
            self.assertEqual(metadata["api_retry_status"], "fallback_selected")
            self.assertEqual(metadata["api_retry_fallback_used"], "true")
            self.assertEqual(metadata["api_retry_final_model"], "zyra-fallback-a")
            self.assertEqual(metadata["model_stream_watchdog_ok"], "true")
            self.assertEqual(metadata["model_stream_watchdog_status"], "recovered")
            self.assertGreaterEqual(int(metadata["runtime_budget_state_retry_count"]), 1)
            self.assertEqual(metadata["api_retry_playbook_ok"], "true")
            self.assertEqual(metadata["api_retry_playbook_status"], "recovered")
            self.assertEqual(metadata["runtime_budget_replay_ok"], "true")
            self.assertEqual(metadata["codeworker_api_foundation_ok"], "true")
            self.assertEqual(metadata["compact_state_projection_ok"], "true")
            api_retry = _query_phases(run.event_records, "api_retry_report")[0]["api_retry"]
            self.assertEqual(api_retry["attempts"][0]["decision"], "retry_fallback_model")
            self.assertEqual(api_retry["attempts"][0]["fallback_model"], "zyra-fallback-a")
            retry_playbook = _query_phases(run.event_records, "api_retry_playbook")[0]["api_retry_playbook"]
            self.assertEqual(retry_playbook["fallback_count"], 1)
            model_report = _query_phases(run.event_records, "model_stream_report")[0]["model_stream"]
            self.assertEqual(model_report["error_kind"], "model_unavailable")

    def test_core_runtime_disconnects_block_codeworker_api_foundation(self) -> None:
        cases = (
            ("disable_compact_restore_runtime", "compact_restore_ok"),
            ("disable_model_stream_runtime", "model_stream_ok"),
            ("disable_runtime_budget_state", "runtime_budget_state_ok"),
        )
        for constraint_name, metadata_key in cases:
            with self.subTest(constraint_name=constraint_name), tempfile.TemporaryDirectory() as tmpdir:
                state = create_task_state(f"Disable {constraint_name}.")
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                (workspace / "probe.txt").write_text("disconnect probe", encoding="utf-8")
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
                        "force_compact_restore": True,
                        "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "probe.txt"}}],
                    },
                )

                run = runtime.run(request)

                self.assertFalse(run.worker_result.ok)
                self.assertEqual(run.worker_result.metadata[metadata_key], "false")
                self.assertEqual(run.worker_result.metadata["codeworker_api_foundation_ok"], "false")
                self.assertEqual(run.worker_result.metadata["compact_state_projection_ok"], "false")
                self.assertIn("codeworker_api_foundation", run.worker_result.metadata["tool_runtime_gate_failures"])
                self.assertEqual(len(_query_phases(run.event_records, "codeworker_api_foundation")), 1)
                self.assertEqual(len(_query_phases(run.event_records, "runtime_budget_replay")), 1)
                self.assertFalse((workspace / "should-not-exist.txt").exists())

    def test_compact_state_api_endpoint_returns_live_projection(self) -> None:
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
                payload = _get(
                    base_url,
                    "/workers/code/compact-state?query_context_budget_chars=900&force_compact_restore=true",
                )
                compact_state = payload["compact_state"]
                self.assertEqual(compact_state["ok"], "true")
                self.assertEqual(compact_state["compact_restore_policy_status"], "ready")
                self.assertIn(compact_state["api_retry_playbook_status"], {"ready", "recovered"})
                self.assertIn(compact_state["runtime_budget_replay_status"], {"ready", "degraded"})
                self.assertTrue(payload["compact_state_projection"]["ok"])
                for section in ("compact_restore_policy", "api_retry_playbook", "runtime_budget_replay"):
                    self.assertIn(section, payload["compact_state_projection"])
                    self.assertTrue(payload["compact_state_projection"][section]["ok"])
                phases = [
                    event["payload"]["query_session"]["phase"]
                    for event in payload["events"]
                    if isinstance(event.get("payload", {}).get("query_session"), dict)
                ]
                self.assertIn("compact_restore_policy", phases)
                self.assertIn("api_retry_playbook", phases)
                self.assertIn("runtime_budget_replay", phases)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _query_phases(events: list[object], phase: str) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for event in events:
        payload = getattr(event, "payload", {})
        query_session = payload.get("query_session") if isinstance(payload, dict) else None
        if isinstance(query_session, dict) and query_session.get("phase") == phase:
            matches.append(query_session)
    return matches


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
