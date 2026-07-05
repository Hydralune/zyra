from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import ArtifactKind, EventRecord, EventType, create_task_state, to_jsonable
from zyra_runtime import (
    ContextSessionRuntime,
    JsonPermissionStore,
    LocalArtifactStore,
    PermissionEffect,
    PermissionOperation,
    PermissionRequestStatus,
    PermissionRule,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolPermissionPolicy,
    default_tool_registry,
    default_worker_descriptors,
)


class RuntimeProtocolTests(unittest.TestCase):
    def test_default_tool_registry_contains_worker_tools(self) -> None:
        names = {tool.name for tool in default_tool_registry().list()}

        self.assertTrue({"file_read", "file_edit", "shell", "browser", "artifact_write"}.issubset(names))

    def test_default_worker_descriptors_point_to_vendored_sources(self) -> None:
        descriptors = {worker.name: worker for worker in default_worker_descriptors()}

        self.assertEqual(descriptors["CodeWorkerRuntime"].source, "vendor/claude-code-best")
        self.assertEqual(descriptors["BrowserWorker"].source, "vendor/browser-use")

    def test_context_session_clear_and_rewind_preserve_trace(self) -> None:
        state = create_task_state("Control visible context.")
        created = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
        )
        clear_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.CONTROL_COMMAND,
            node_id=state.root_node_id,
            payload={"raw": "start fresh"},
        )
        events = [to_jsonable(created), to_jsonable(clear_event)]
        runtime = ContextSessionRuntime(events)

        cleared = runtime.clear(state, clear_event)

        self.assertEqual(cleared["data"]["cleared_visible_events"], 1)
        self.assertEqual(cleared["data"]["session"]["visible_events"], 0)
        self.assertEqual(state.metadata["context_session"]["active_session_id"], cleared["data"]["active_session_id"])

        context_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.CONTROL_COMMAND,
            node_id=state.root_node_id,
            payload={"raw": ""},
        )
        rewind_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.CONTROL_COMMAND,
            node_id=state.root_node_id,
            payload={"raw": "latest"},
        )
        events = [
            to_jsonable(created),
            to_jsonable(clear_event),
            to_jsonable(context_event),
            to_jsonable(rewind_event),
        ]
        rewound = ContextSessionRuntime(events).rewind(state, rewind_event, target="latest")

        self.assertEqual(rewound["summary"], "Context rewound to a prior visible window.")
        self.assertEqual(rewound["data"]["active_session_id"], "session_initial")
        self.assertEqual(rewound["data"]["session"]["visible_events"], 1)
        self.assertEqual(len(state.metadata["context_session"]["snapshots"]), 2)

    def test_permission_policy_denies_outside_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = ToolPermissionPolicy.for_workspace(tmpdir)
            inside = Path(tmpdir) / "allowed.txt"
            outside = Path(tmpdir).parent / "outside.txt"

            self.assertEqual(policy.decide_write(inside).effect, PermissionEffect.ALLOW)
            self.assertEqual(policy.decide_write(outside).effect, PermissionEffect.DENY)

    def test_local_artifact_store_writes_artifact_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Write an artifact.")
            store = LocalArtifactStore(tmpdir)
            artifact = store.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content="artifact body",
                title="Trace summary",
                kind=ArtifactKind.MARKDOWN,
                extension=".md",
                producer_node_id=state.root_node_id,
            )

            self.assertTrue(Path(artifact.uri).exists())
            self.assertEqual(Path(artifact.uri).read_text(encoding="utf-8"), "artifact body")
            self.assertEqual(artifact.kind, ArtifactKind.MARKDOWN)

    def test_local_artifact_store_describes_and_previews_text_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Preview an artifact.")
            store = LocalArtifactStore(tmpdir)
            artifact = store.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content="# Artifact\n\ntrace body",
                title="Trace summary",
                kind=ArtifactKind.TRACE,
                extension=".md",
                producer_node_id=state.root_node_id,
            )

            entry = store.describe(artifact)
            preview = store.read_preview(artifact)

            self.assertTrue(entry["exists"])
            self.assertTrue(entry["previewable"])
            self.assertEqual(preview["content"], "# Artifact\n\ntrace body")
            self.assertFalse(preview["truncated"])

    def test_tool_executor_reads_writes_and_edits_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Use file tools.")
            workspace = Path(tmpdir) / "workspace"
            context = ToolExecutionContext.for_workspace(workspace, Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            write_result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_write",
                    arguments={"path": "notes/todo.txt", "content": "first draft"},
                )
            )
            self.assertTrue(write_result.ok)

            edit_result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_edit",
                    arguments={"path": "notes/todo.txt", "old": "first", "new": "final"},
                )
            )
            self.assertTrue(edit_result.ok)

            read_result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_read",
                    arguments={"path": "notes/todo.txt"},
                )
            )
            self.assertEqual(read_result.output["content"], "final draft")

    def test_web_search_scans_workspace_and_writes_trace_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Search local evidence.")
            workspace = Path(tmpdir) / "workspace"
            docs = workspace / "docs"
            docs.mkdir(parents=True)
            (docs / "notes.md").write_text(
                "# Runtime Notes\n\nZyra supports dynamic requirement changes during execution.",
                encoding="utf-8",
            )
            context = ToolExecutionContext.for_workspace(workspace, Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="web_search",
                    arguments={"query": "requirement changes", "paths": ["docs"], "max_results": 5},
                )
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.output["result_count"], 1)
            self.assertEqual(result.metadata["mode"], "workspace")
            self.assertEqual(len(result.artifacts), 1)
            preview = context.artifact_store.read_preview(result.artifacts[0])
            self.assertIn("Zyra Research Search Results", preview["content"])
            self.assertIn("requirement changes", preview["content"])

    def test_browser_tool_extracts_inline_html_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Extract browser state.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="browser",
                    arguments={
                        "action": "extract_text",
                        "html": "<html><head><title>Inline</title></head><body><h1>Zyra Browser Tool</h1></body></html>",
                    },
                )
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.output["state"]["title"], "Inline")
            self.assertIn("Zyra Browser Tool", result.output["state"]["text_preview"])
            self.assertGreaterEqual(len(result.artifacts), 2)

    def test_web_search_blocks_network_without_explicit_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Block network search.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="web_search",
                    arguments={"query": "zyra", "url": "https://example.com"},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "network_not_allowed")

    def test_trace_tool_requires_event_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Read trace.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="trace",
                    arguments={"limit": 5},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "trace_reader_missing")

    def test_checkpoint_tool_requires_checkpoint_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Read checkpoint.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="checkpoint",
                    arguments={"include_state": True},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "checkpoint_reader_missing")

    def test_tool_executor_denies_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject path traversal.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="file_write",
                    arguments={"path": "../escape.txt", "content": "outside"},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_denied")

    def test_shell_tool_requires_approval_when_policy_asks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Gate shell execution.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="shell",
                    arguments={"command": f'"{sys.executable}" --version'},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")

    def test_shell_tool_runs_after_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run approved shell command.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="shell",
                    arguments={"command": f'"{sys.executable}" -c "print(123)"', "approved": True},
                )
            )

            self.assertTrue(result.ok)
            self.assertIn("123", result.output["stdout"])

    def test_shell_tool_denies_dangerous_fragments_even_when_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject dangerous shell command.")
            context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="shell",
                    arguments={"command": "git reset --hard", "approved": True},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_denied")

    def test_permission_store_rules_allow_matching_shell_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
            store.add_rule(
                PermissionRule(
                    operation=PermissionOperation.SHELL,
                    pattern="print(456)",
                    effect=PermissionEffect.ALLOW,
                    reason="unit test allow",
                )
            )
            state = create_task_state("Use persisted permission rule.")
            context = ToolExecutionContext.for_workspace(
                Path(tmpdir) / "workspace",
                Path(tmpdir) / "artifacts",
                permission_store=store,
            )
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="shell",
                    arguments={"command": f'"{sys.executable}" -c "print(456)"'},
                )
            )

            self.assertTrue(result.ok)
            self.assertIn("456", result.output["stdout"])
            self.assertEqual(result.metadata["permission_effect"], "allow")

    def test_permission_store_records_pending_request_for_shell_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
            state = create_task_state("Record permission request.")
            context = ToolExecutionContext.for_workspace(
                Path(tmpdir) / "workspace",
                Path(tmpdir) / "artifacts",
                permission_store=store,
            )
            executor = ToolExecutor(context)

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    tool_name="shell",
                    arguments={"command": f'"{sys.executable}" --version'},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            requests = store.list_requests(PermissionRequestStatus.PENDING)
            self.assertEqual(len(requests), 1)
            self.assertEqual(result.metadata["permission_request_id"], requests[0].request_id)


if __name__ == "__main__":
    unittest.main()
