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

    def test_browser_tool_schema_matches_executor_action_contract(self) -> None:
        browser = default_tool_registry().get("browser")

        self.assertIsNotNone(browser)
        assert browser is not None
        self.assertEqual(
            tuple(browser.input_schema["properties"]["action"]["enum"]),
            (
                "open_url",
                "navigate",
                "extract_text",
                "extract",
                "snapshot_state",
                "find_elements",
            ),
        )
        self.assertIn("does not inspect local image or video", browser.purpose)

    def test_default_worker_descriptors_identify_productized_sources(self) -> None:
        descriptors = {worker.name: worker for worker in default_worker_descriptors()}

        self.assertEqual(descriptors["CodeWorkerRuntime"].source, "zyra-claude-productized")
        self.assertEqual(descriptors["CodeWorkerRuntime"].metadata["upstream_source"], "claude-code-best")
        self.assertEqual(descriptors["CodeWorkerRuntime"].metadata["sidecar_required_for_default_path"], "false")
        self.assertEqual(descriptors["BrowserWorker"].source, "zyra-browser-productized")
        self.assertEqual(str(descriptors["BrowserWorker"].kind), "python")
        self.assertEqual(descriptors["BrowserWorker"].metadata["upstream_source"], "browser-use")
        self.assertEqual(descriptors["BrowserWorker"].metadata["sidecar_required_for_default_path"], "false")

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

    def test_local_artifact_store_redacts_secret_like_text_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Persist internal runtime evidence.")
            store = LocalArtifactStore(tmpdir)
            secret = "test-token-material-123456789"
            artifact = store.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content=f'{{"API_KEY":"{secret}","status":"observed"}}',
                title="Internal transcript",
                kind=ArtifactKind.TRACE,
                extension=".json",
                producer_node_id=state.root_node_id,
                redact_secrets=True,
            )

            persisted = Path(artifact.uri).read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertIn("[REDACTED]", persisted)
            self.assertTrue(artifact.metadata["durable_secret_redaction"])
            self.assertGreaterEqual(
                int(artifact.metadata["durable_secret_redaction_count"]),
                1,
            )

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

    def test_tool_executor_requires_grants_for_mutations_but_allows_local_read(self) -> None:
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
            self.assertFalse(write_result.ok)
            self.assertEqual(write_result.error, "permission_required")
            target = workspace / "notes" / "todo.txt"
            self.assertFalse(target.exists())
            target.parent.mkdir(parents=True)
            target.write_text("first draft", encoding="utf-8")

            edit_result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_edit",
                    arguments={"path": "notes/todo.txt", "old": "first", "new": "final"},
                )
            )
            self.assertFalse(edit_result.ok)
            self.assertEqual(edit_result.error, "permission_required")

            read_result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_read",
                    arguments={"path": "notes/todo.txt"},
                )
            )
            self.assertEqual(read_result.output["content"], "first draft")

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

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            self.assertEqual(result.artifacts, [])

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

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            self.assertEqual(result.artifacts, [])

    def test_browser_tool_normalizes_common_model_action_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Read browser state with a natural alias.")
            context = ToolExecutionContext.for_workspace(
                Path(tmpdir) / "workspace",
                Path(tmpdir) / "artifacts",
            )
            executor = ToolExecutor(context)

            for alias, expected in (
                ("open", "open_url"),
                ("view", "snapshot_state"),
                ("capture", "snapshot_state"),
                ("screenshot", "snapshot_state"),
                ("read", "extract_text"),
            ):
                with self.subTest(alias=alias):
                    result = executor._browser(
                        ToolCall(
                            run_id=state.run_id,
                            task_id=state.task_id,
                            node_id=state.root_node_id,
                            tool_name="browser",
                            arguments={
                                "action": alias,
                                "html": "<html><body>browser alias</body></html>",
                            },
                        ),
                        authorized=True,
                    )

                    self.assertTrue(result.ok)
                    self.assertEqual(result.output["action"], expected)
                    self.assertEqual(result.metadata["requested_action"], alias)
                    self.assertEqual(result.metadata["normalized_action"], expected)

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

    def test_shell_tool_rejects_model_supplied_approval_without_execution_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject forged shell approval.")
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

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            self.assertEqual(result.metadata["raw_approved_argument_ignored"], "true")

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

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            self.assertEqual(result.metadata["permission_effect"], "ask")

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
