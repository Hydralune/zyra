from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from zyra_code_index import CodeIndexConsumer, CodeIndexIntegrationRuntime, CodeIndexQuery
from zyra_core import AgentMessage, AgentRole, MessageIntent, create_task_state
from zyra_runtime import LocalArtifactStore, WorkerRequest
from zyra_workspace import WorkspaceEditPort


class CodeIndexPatchEventIntegrationTests(unittest.TestCase):
    def test_committed_workspace_event_admits_process_job_without_owner_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = {
                "ZYRA_SQLITE_PATH": str(root / "canonical.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(root / "legacy-workspace"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_CODE_INDEX_ROOT": str(root / "code-index"),
                "ZYRA_CODE_INDEX_DISABLED": "false",
            }
            previous = {key: os.environ.get(key) for key in values}
            os.environ.update(values)
            try:
                sys.modules.pop("apps.api.zyra_api.main", None)
                module = importlib.import_module("apps.api.zyra_api.main")
                store = module.SQLiteStore(module.sqlite_path())
                store.initialize()
                state = create_task_state("Patch event must publish code index generation.")
                store.save_checkpoint(state)
                manager = module.get_workspace_manager()
                created = manager.create_for_task(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id="session-patch-event",
                    worker_id="CodeWorkerRuntime",
                    idempotency_key="create-patch-event-workspace",
                )
                module.persist_workspace_events(store, task_id=state.task_id)
                port = WorkspaceEditPort(
                    manager,
                    created.access,
                    worker_id="CodeWorkerRuntime",
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    artifact_store=LocalArtifactStore(root / "artifacts"),
                )
                mutation = port.write_text(
                    "src/event_index.py",
                    "def event_index():\n    return 'canonical patch event process publication'\n",
                    idempotency_key="write-event-index",
                )
                transaction_id = mutation.transaction.transaction_id
                committed_access = mutation.access
                access_before_index_admission = committed_access.to_public_dict()
                projection_before_index_admission = manager.project(
                    committed_access.workspace_id
                )
                persisted = module.persist_workspace_events(store, task_id=state.task_id)
                self.assertTrue(
                    any(
                        event.payload["workspace_event"]["event_type"]
                        == "workspace.patch.committed"
                        for event in persisted
                    )
                )

                projection_after = manager.project(committed_access.workspace_id)
                self.assertEqual(
                    projection_before_index_admission.owner_epoch,
                    projection_after.owner_epoch,
                )
                self.assertEqual(
                    projection_before_index_admission.binding_revision,
                    projection_after.binding_revision,
                )
                self.assertEqual(
                    access_before_index_admission,
                    committed_access.to_public_dict(),
                )
                runtime = module.get_code_index_service().registry.runtime_for_snapshot(
                    committed_access.workspace_id
                )
                integration = CodeIndexIntegrationRuntime(
                    runtime,
                    transaction_resolver=manager.integration_store,
                    allow_inline_worker_for_tests=True,
                )
                selection = integration.select(
                    CodeIndexQuery(
                        task_id=state.task_id,
                        text="canonical patch event publication",
                        consumer=CodeIndexConsumer.EVALUATION,
                        request_id="patch-event-query",
                    )
                )
                self.assertIn("src/event_index.py", selection.selected_files)
                self.assertTrue(runtime.store.has_invalidation(committed_access.workspace_id, transaction_id))
                code_events = [
                    event
                    for event in store.task_events(state.task_id)
                    if event.get("payload", {}).get("code_index")
                ]
                phases = {
                    event["payload"]["code_index"].get("phase") for event in code_events
                }
                self.assertIn("patch_published", phases)

                before_worker_context = manager.project(committed_access.workspace_id)
                worker_context = module._worker_retrieval_context(
                    store,
                    task_id=state.task_id,
                    workspace_manager=manager,
                    workspace_access=committed_access,
                )
                prepared = worker_context.prepare(
                    WorkerRequest(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        worker_name="CodeWorkerRuntime",
                        request_id="api-worker-context-query",
                        messages=[
                            AgentMessage(
                                run_id=state.run_id,
                                task_id=state.task_id,
                                sender_role=AgentRole.USER,
                                receiver_role=AgentRole.WORKER,
                                intent=MessageIntent.REQUEST,
                                content="Inspect canonical patch event publication.",
                            )
                        ],
                    ),
                    session_id="session-worker-context",
                )
                self.assertIn(
                    "src/event_index.py",
                    prepared.constraint_delta["code_index_selected_files"],
                )
                after_worker_context = manager.project(committed_access.workspace_id)
                self.assertEqual(
                    before_worker_context.owner_epoch,
                    after_worker_context.owner_epoch,
                )
                self.assertEqual(
                    before_worker_context.binding_revision,
                    after_worker_context.binding_revision,
                )
                worker_context.finish(
                    prepared,
                    committed=True,
                    terminal_event_ids=("api-worker-context-terminal",),
                    reason="api_helper_verified",
                )
            finally:
                try:
                    module.reset_workspace_manager()
                    module.reset_runtime_event_spine_bridge()
                except (NameError, AttributeError):
                    pass
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
