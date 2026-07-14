from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from zyra_runtime import (  # noqa: E402
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolExecutionRuntime,
    ToolLoopScheduler,
    ToolRegistryRuntime,
    ToolResultBudgetRuntime,
    ToolRuntimeDisabledError,
    ToolUseContext,
    WorkerRequest,
)
from zyra_runtime.permission.grants import (  # noqa: E402
    ExecutionGrantBinding,
    ExecutionGrantStore,
    canonical_arguments_digest,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionResolutionResponse,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity as PermissionToolIdentity,
)
from zyra_runtime.permission.request_queue import PermissionResolutionCode  # noqa: E402
from zyra_runtime.permission.runtime import (  # noqa: E402
    PermissionRuntimeIdentityError,
    PermissionRuntimeConfig,
    ToolPermissionRuntime,
    ToolPermissionRuntimeDisabledError,
)
from zyra_runtime.permission.store import (  # noqa: E402
    PermissionIdentityMismatch,
    PermissionStateConflict,
    PermissionStateStore,
)
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class CodeWorkerPermissionRuntimeFoundationTests(unittest.TestCase):
    @staticmethod
    def _evaluation_request(
        *,
        state: object,
        call: ToolCall,
        session_id: str,
        workspace: Path,
    ) -> PermissionEvaluationRequest:
        return PermissionEvaluationRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=session_id,
            worker_request_id="worker-request-a",
            node_id=state.root_node_id,
            tool_use_id=call.tool_call_id,
            tool_identity=PermissionToolIdentity(
                namespace="builtin",
                name=call.tool_name,
            ),
            arguments=dict(call.arguments),
            workspace_root=str(workspace),
        )

    @staticmethod
    def _permission_event_kinds(events: list[object]) -> list[tuple[int, str]]:
        output: list[tuple[int, str]] = []
        for index, event in enumerate(events):
            payload = getattr(event, "payload", {})
            query_session = payload.get("query_session") if isinstance(payload, dict) else None
            permission = (
                query_session.get("permission_runtime")
                if isinstance(query_session, dict)
                else None
            )
            if isinstance(permission, dict) and permission.get("kind"):
                output.append((index, str(permission["kind"])))
        return output

    @staticmethod
    def _allow_response(
        pending: object,
        *,
        idempotency_key: str,
        channel: str = "user",
    ) -> PermissionResolutionResponse:
        return PermissionResolutionResponse(
            request_id=pending.request_id,
            session_id=pending.session_id,
            tool_use_id=pending.tool_use_id,
            tool_identity=pending.tool_identity,
            arguments_digest=pending.arguments_digest,
            request_fingerprint=pending.request_fingerprint,
            scope=pending.scope,
            effect=PermissionEffect.ALLOW,
            actor_id="test-user",
            expected_revision=pending.revision,
            channel=channel,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _query_phases(events: list[object], phase: str) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for event in events:
            payload = getattr(event, "payload", {})
            query_session = payload.get("query_session") if isinstance(payload, dict) else None
            if isinstance(query_session, dict) and query_session.get("phase") == phase:
                output.append(query_session)
        return output

    def test_model_supplied_approved_flag_cannot_create_shell_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Ignore model-supplied permission approval.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "raw-approved-must-not-exist.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('raw-approved-must-not-exist.txt').write_text('unsafe', encoding='utf-8')",
                ]
            )
            executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                )
            )

            result = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="shell",
                    arguments={"command": command, "approved": True},
                )
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.error, "permission_required")
            self.assertFalse(marker.exists())

    def test_direct_workspace_mutations_without_execution_grant_have_zero_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Mutations require a runtime execution grant.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            existing = workspace / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            created = workspace / "created.txt"
            executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                )
            )

            write = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_write",
                    arguments={"path": created.name, "content": "unsafe"},
                )
            )
            edit = executor.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="file_edit",
                    arguments={
                        "path": existing.name,
                        "old": "before",
                        "new": "after",
                    },
                )
            )

            self.assertFalse(write.ok)
            self.assertFalse(edit.ok)
            self.assertIn(write.error, {"permission_required", "permission_grant_required"})
            self.assertIn(edit.error, {"permission_required", "permission_grant_required"})
            self.assertFalse(created.exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "before")

    def test_real_shell_executor_consumes_exact_grant_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Consume an exact permission grant once.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "grant-once.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "p=Path('grant-once.txt'); "
                    "p.write_text((p.read_text(encoding='utf-8') if p.exists() else '') + 'x', encoding='utf-8')",
                ]
            )
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": command},
            )
            runtime = ToolPermissionRuntime.for_session(
                session_id="session-a",
                state_path=Path(tmpdir) / "permission" / "state.json",
                workspace_root=workspace,
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="session-a",
                workspace=workspace,
            )
            asked = runtime.guard(request)
            pending = asked.pending_request
            resolved = runtime.resolve(
                PermissionResolutionResponse(
                    request_id=pending.request_id,
                    session_id=pending.session_id,
                    tool_use_id=pending.tool_use_id,
                    tool_identity=pending.tool_identity,
                    arguments_digest=pending.arguments_digest,
                    request_fingerprint=pending.request_fingerprint,
                    scope=pending.scope,
                    effect=PermissionEffect.ALLOW,
                    actor_id="test-authority",
                    expected_revision=pending.revision,
                    channel="test",
                    idempotency_key="shell-grant-once",
                )
            )
            self.assertTrue(resolved.accepted, resolved.to_dict())
            guarded = runtime.guard(request)
            self.assertTrue(guarded.allowed, guarded.to_dict())
            grant = guarded.execution_grant
            executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                ),
                permission_authority=runtime,
            )

            first = executor.execute(
                call,
                permission_grant=grant,
            )
            replay = executor.execute(
                call,
                permission_grant=grant,
            )

            self.assertTrue(first.ok, f"{first.summary}: {first.metadata}")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertFalse(replay.ok)
            self.assertEqual(replay.error, "permission_grant_invalid")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_execution_grant_cannot_cross_workspace_and_wrong_workspace_does_not_consume_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Bind a write grant to one exact workspace.")
            workspace_a = Path(tmpdir) / "workspace-a"
            workspace_b = Path(tmpdir) / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            (workspace_a / "workspace-bound.txt").write_text("before", encoding="utf-8")
            (workspace_b / "workspace-bound.txt").write_text("before", encoding="utf-8")
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="file_edit",
                arguments={
                    "path": "workspace-bound.txt",
                    "old": "before",
                    "new": "authorized",
                },
            )
            runtime = ToolPermissionRuntime.for_session(
                session_id="workspace-session",
                state_path=Path(tmpdir) / "permission" / "state.json",
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="workspace-session",
                workspace=workspace_a,
            )
            guarded = runtime.guard(
                request,
                workspace_state={
                    "workspace_root": str(workspace_a),
                    "workspace_scoped": True,
                    "path_validated": True,
                    "read_before_write": True,
                    "baseline_current": True,
                    "bounded_change": True,
                },
            )
            self.assertTrue(guarded.allowed)
            wrong_executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace_b,
                    artifact_root=Path(tmpdir) / "artifacts-b",
                ),
                permission_authority=runtime,
            )
            correct_executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace_a,
                    artifact_root=Path(tmpdir) / "artifacts-a",
                ),
                permission_authority=runtime,
            )

            wrong = wrong_executor.execute(
                call,
                permission_grant=guarded.execution_grant,
            )
            correct = correct_executor.execute(
                call,
                permission_grant=guarded.execution_grant,
            )

            self.assertFalse(wrong.ok)
            self.assertEqual(wrong.error, "permission_grant_invalid")
            self.assertEqual(
                (workspace_b / "workspace-bound.txt").read_text(encoding="utf-8"),
                "before",
            )
            self.assertTrue(correct.ok, correct.summary)
            self.assertEqual(
                (workspace_a / "workspace-bound.txt").read_text(encoding="utf-8"),
                "authorized",
            )

    def test_sealed_runtime_denies_shell_with_recovery_and_zero_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Sealed permission recovery is autonomous.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "sealed-must-not-exist.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('sealed-must-not-exist.txt').write_text('unsafe', encoding='utf-8')",
                ]
            )
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": command},
            )
            runtime = ToolPermissionRuntime.for_session(
                session_id="sealed-session",
                state_path=Path(tmpdir) / "permission" / "state.json",
                config=PermissionRuntimeConfig(
                    mode="sealed",
                    interactive=False,
                    headless=True,
                ),
            )

            result = runtime.guard(
                self._evaluation_request(
                    state=state,
                    call=call,
                    session_id="sealed-session",
                    workspace=workspace,
                )
            )

            self.assertEqual(result.effect, PermissionEffect.DENY)
            self.assertIsNone(result.execution_grant)
            self.assertIsNone(result.pending_request)
            self.assertIsNotNone(result.decision.recovery_input)
            self.assertEqual(runtime.metadata()["permission_runtime_human_intervention_count"], "0")
            kinds = [
                event.payload["query_session"]["permission_runtime"]["kind"]
                for event in result.events
            ]
            self.assertEqual(
                kinds[:3],
                ["permission_evaluation_started", "permission_decision", "recovery_input"],
            )
            self.assertFalse(marker.exists())

    def test_exact_approval_survives_snapshot_then_authorizes_one_real_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Restore exact approval into the active session.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "restored-approval-once.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "p=Path('restored-approval-once.txt'); "
                    "p.write_text((p.read_text(encoding='utf-8') if p.exists() else '') + 'x', encoding='utf-8')",
                ]
            )
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": command},
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="approval-session",
                workspace=workspace,
            )
            runtime = ToolPermissionRuntime.for_session(
                session_id="approval-session",
                state_path=Path(tmpdir) / "before" / "state.json",
            )
            asked = runtime.guard(request)
            self.assertTrue(asked.ask_pending)
            pending = asked.pending_request
            forged = PermissionResolutionResponse(
                request_id=pending.request_id,
                session_id=pending.session_id,
                tool_use_id=pending.tool_use_id,
                tool_identity=replace(pending.tool_identity, server_id="forged-server"),
                arguments_digest=pending.arguments_digest,
                request_fingerprint=pending.request_fingerprint,
                scope=pending.scope,
                effect=PermissionEffect.ALLOW,
                actor_id="test-authority",
                expected_revision=pending.revision,
                channel="test",
                idempotency_key="forged-response",
            )
            rejected = runtime.resolve(forged)
            self.assertEqual(rejected.code, PermissionResolutionCode.IDENTITY_MISMATCH)
            exact = replace(
                forged,
                tool_identity=pending.tool_identity,
                idempotency_key="exact-response",
            )
            accepted = runtime.resolve(exact)
            self.assertTrue(accepted.accepted)

            snapshot = runtime.snapshot()
            serialized = json.dumps(snapshot, sort_keys=True)
            self.assertNotIn("restored-approval-once.txt", serialized)
            restored = ToolPermissionRuntime.for_session(
                session_id="approval-session",
                state_path=Path(tmpdir) / "after" / "state.json",
                restored_snapshot=snapshot,
            )
            authorized = restored.guard(request)
            self.assertTrue(authorized.allowed)
            self.assertTrue(authorized.restored_approval)
            executor = ToolExecutor(
                ToolExecutionContext.for_workspace(
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                ),
                permission_authority=restored,
            )

            first = executor.execute(
                call,
                permission_grant=authorized.execution_grant,
            )
            replay = executor.execute(
                call,
                permission_grant=authorized.execution_grant,
            )

            self.assertTrue(first.ok, first.summary)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertFalse(replay.ok)
            self.assertEqual(replay.error, "permission_grant_invalid")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            asked_again = restored.guard(request)
            self.assertTrue(asked_again.ask_pending)
            self.assertFalse(asked_again.allowed)

    def test_session_hijack_cannot_guard_or_restore_another_sessions_permission_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject permission session hijacking.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="file_read",
                arguments={"path": "missing.txt"},
            )
            owner = ToolPermissionRuntime.for_session(
                session_id="session-owner",
                state_path=Path(tmpdir) / "owner" / "state.json",
            )
            hijacked_request = self._evaluation_request(
                state=state,
                call=call,
                session_id="session-attacker",
                workspace=workspace,
            )

            with self.assertRaises(PermissionRuntimeIdentityError):
                owner.guard(hijacked_request)

            snapshot = owner.snapshot()
            with self.assertRaises(PermissionIdentityMismatch):
                ToolPermissionRuntime.for_session(
                    session_id="session-attacker",
                    state_path=Path(tmpdir) / "attacker" / "state.json",
                    restored_snapshot=snapshot,
                )

    def test_disabling_any_permission_authority_component_fails_closed(self) -> None:
        cases = {
            "disabled": "ToolPermissionRuntime",
            "disable_rule_store": "PermissionRuleStore",
            "disable_request_queue": "PermissionRequestQueue",
            "disable_decision_log": "PermissionDecisionLog",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            for field, component in cases.items():
                with self.subTest(field=field):
                    with self.assertRaises(ToolPermissionRuntimeDisabledError) as captured:
                        ToolPermissionRuntime.for_session(
                            session_id=f"disabled-{field}",
                            state_path=Path(tmpdir) / field / "state.json",
                            config=PermissionRuntimeConfig(**{field: True}),
                        )
                    self.assertEqual(captured.exception.component, component)

    def test_code_worker_default_read_uses_permission_guard_before_real_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Read through the default permission main path.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "notes.txt").write_text("permission-main-path", encoding="utf-8")
            artifact_root = Path(tmpdir) / "artifacts"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {"tool_name": "file_read", "arguments": {"path": "notes.txt"}}
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            permission_events = self._permission_event_kinds(run.event_records)
            kinds = [kind for _, kind in permission_events]
            self.assertNotIn("permission_evaluation_started", kinds)
            self.assertIn("permission_decision", kinds)
            self.assertIn("permission_execution_grant_issued", kinds)
            self.assertIn("permission_execution_grant_consumed", kinds)
            projected = {
                event.payload["query_session"]["permission_runtime"]["kind"]: event
                for event in run.event_records
                if isinstance(event.payload.get("query_session", {}).get("permission_runtime"), dict)
                and event.payload["query_session"]["permission_runtime"].get("kind")
            }
            decision_event = projected["permission_decision"]
            issued_event = projected["permission_execution_grant_issued"]
            consumed_event = projected["permission_execution_grant_consumed"]
            self.assertIn(
                '"canonical_policy_owner": "typescript"',
                json.dumps(decision_event.payload, sort_keys=True),
            )
            self.assertEqual(
                issued_event.payload["query_session"]["permission_runtime"]["cause_event_id"],
                decision_event.event_id,
            )
            self.assertEqual(
                consumed_event.payload["query_session"]["permission_runtime"]["cause_event_id"],
                issued_event.event_id,
            )
            tool_result_index = next(
                index
                for index, event in enumerate(run.event_records)
                if "tool_result" in getattr(event, "payload", {})
            )
            self.assertLess(
                next(index for index, kind in permission_events if kind == "permission_decision"),
                tool_result_index,
            )
            tool_started_index = next(
                index
                for index, event in enumerate(run.event_records)
                if (
                    getattr(event, "payload", {})
                    .get("query_session", {})
                    .get("phase")
                    == "tool_call_started"
                )
            )
            self.assertLess(tool_started_index, tool_result_index)
            self.assertLess(
                next(index for index, kind in permission_events if kind == "permission_decision"),
                tool_result_index,
            )
            self.assertLess(
                next(index for index, kind in permission_events if kind == "permission_execution_grant_consumed"),
                tool_result_index,
            )
            tool_result = next(
                event.payload["tool_result"]
                for event in run.event_records
                if "tool_result" in event.payload
            )
            self.assertTrue(tool_result["ok"])
            self.assertIn("permission-main-path", json.dumps(tool_result["output"]))
            self.assertTrue((artifact_root / ".permission" / "state.json").exists())

            snapshot_id = run.worker_result.metadata["query_session_snapshot_artifact_id"]
            snapshot_artifact = next(
                item
                for item in run.worker_result.artifacts
                if item.artifact_id == snapshot_id
            )
            snapshot = json.loads(Path(snapshot_artifact.uri).read_text(encoding="utf-8"))
            permission_snapshot = (
                snapshot.get("permission_runtime")
                or snapshot.get("session_snapshot", {}).get("permission_runtime")
                or snapshot.get("runtime_state", {}).get("permission_runtime")
                or snapshot.get("metadata", {}).get("permission_runtime")
            )
            self.assertIsInstance(permission_snapshot, dict)
            self.assertEqual(permission_snapshot["runtime_id"], "zyra-tool-permission-runtime")
            self.assertEqual(permission_snapshot["metrics"]["permission_runtime_decisions"], "1")
            self.assertEqual(
                permission_snapshot["metrics"]["permission_runtime_human_intervention_count"],
                "0",
            )
            checkpoint_path = Path(
                run.worker_result.metadata["runtime_state_checkpoint_path"]
            )
            checkpoint_records = [
                json.loads(line)
                for line in checkpoint_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            checkpoint = next(
                item
                for item in reversed(checkpoint_records)
                if item["record_type"] == "runtime_state_checkpoint"
            )
            checkpoint_permission = checkpoint["payload"]["runtime_state"]["permission_runtime"]
            self.assertEqual(
                checkpoint_permission["runtime_id"],
                "zyra-tool-permission-runtime",
            )
            self.assertEqual(
                checkpoint_permission["session_id"],
                permission_snapshot["session_id"],
            )

    def test_code_worker_sealed_shell_ignores_raw_approval_and_emits_recovery_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Sealed worker must recover without a human.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "worker-sealed-must-not-exist.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('worker-sealed-must-not-exist.txt').write_text('unsafe', encoding='utf-8')",
                ]
            )
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
                    "permission_mode": "sealed",
                    "tool_plan": [
                        {
                            "tool_name": "shell",
                            "arguments": {"command": command, "approved": True},
                        }
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertFalse(marker.exists())
            permission_events = self._permission_event_kinds(run.event_records)
            kinds = [kind for _, kind in permission_events]
            self.assertIn("permission_decision", kinds)
            self.assertIn("recovery_input", kinds)
            self.assertNotIn("permission_execution_grant_issued", kinds)
            self.assertNotIn("permission_execution_grant_consumed", kinds)
            self.assertEqual(
                len(self._query_phases(run.event_records, "tool_call_started")),
                1,
            )
            tool_result = next(
                event.payload["tool_result"]
                for event in run.event_records
                if "tool_result" in event.payload
            )
            self.assertEqual(tool_result["error"], "permission_denied")
            self.assertEqual(tool_result["metadata"]["human_intervention_count"], "0")
            self.assertEqual(tool_result["metadata"]["raw_approved_argument_ignored"], "true")
            recovery_event = next(
                event
                for event in run.event_records
                if (
                    event.payload.get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    == "recovery_input"
                )
            )
            recovery_payload = recovery_event.payload["query_session"]["permission_runtime"]
            self.assertEqual(recovery_payload["payload"]["human_intervention_count"], 0)

    def test_permission_denial_limit_aborts_query_engine_even_with_continue_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Denial circuit breaker must terminate the loop.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "denial-limit-must-not-exist.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('denial-limit-must-not-exist.txt').write_text('unsafe', encoding='utf-8')",
                ]
            )
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
                    "permission_mode": "sealed",
                    "continue_on_error": True,
                    "tool_plan": [
                        {"tool_name": "shell", "arguments": {"command": command}}
                        for _ in range(4)
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertFalse(marker.exists())
            decisions = [
                event
                for event in run.event_records
                if (
                    event.payload.get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    == "permission_decision"
                )
            ]
            tool_results = [
                event.payload["tool_result"]
                for event in run.event_records
                if "tool_result" in event.payload
            ]
            self.assertEqual(len(decisions), 3)
            self.assertEqual(len(tool_results), 3)
            self.assertEqual(tool_results[-1]["metadata"]["permission_abort_loop"], "true")
            self.assertEqual(
                tool_results[-1]["metadata"]["permission_reason_code"],
                "denial.limit_abort",
            )
            self.assertEqual(run.worker_result.metadata["permission_runtime_denials"], "3")

    def test_code_worker_default_shell_creates_exact_pending_ask_without_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Interactive worker must create an exact pending ask.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "worker-ask-must-not-exist.txt"
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('worker-ask-must-not-exist.txt').write_text('unsafe', encoding='utf-8')",
                ]
            )
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
                        {
                            "tool_name": "shell",
                            "arguments": {"command": command, "approved": True},
                        }
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertFalse(marker.exists())
            kinds = [kind for _, kind in self._permission_event_kinds(run.event_records)]
            self.assertIn("permission_decision", kinds)
            self.assertIn("permission_request_created", kinds)
            self.assertNotIn("permission_execution_grant_issued", kinds)
            self.assertEqual(
                len(self._query_phases(run.event_records, "tool_call_started")),
                1,
            )
            tool_result = next(
                event.payload["tool_result"]
                for event in run.event_records
                if "tool_result" in event.payload
            )
            self.assertEqual(tool_result["error"], "permission_approval_required")
            self.assertEqual(tool_result["metadata"]["raw_approved_argument_ignored"], "true")
            pending = tool_result["output"]["pending_request"]
            self.assertEqual(pending["session_id"], run.worker_result.metadata["query_session_id"])
            self.assertEqual(pending["tool_use_id"], tool_result["tool_call_id"])
            self.assertEqual(pending["tool_identity"]["name"], "shell")
            self.assertTrue(pending["arguments_digest"].startswith("sha256:"))
            self.assertTrue(pending["request_fingerprint"].startswith("sha256:"))
            self.assertTrue(pending["scope"]["request_fingerprint"])
            self.assertTrue(pending["expires_at"])

    def test_code_worker_fails_when_permission_runtime_rule_store_or_queue_is_disconnected(self) -> None:
        cases = (
            ("disable_tool_permission_runtime", "ToolPermissionRuntime"),
            ("disable_permission_rule_store", "PermissionRuleStore"),
            ("disable_permission_request_queue", "PermissionRequestQueue"),
        )
        for field, component in cases:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmpdir:
                state = create_task_state(f"Disconnect {component}.")
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                marker = workspace / "disconnected-must-not-exist.txt"
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
                        field: True,
                        "tool_plan": [
                            {
                                "tool_name": "file_write",
                                "arguments": {
                                    "path": marker.name,
                                    "content": "unsafe",
                                },
                            }
                        ],
                    },
                )

                run = runtime.run(request)

                self.assertFalse(run.worker_result.ok)
                self.assertEqual(run.worker_result.error, "tool_loop_foundation_disabled")
                self.assertEqual(
                    run.worker_result.metadata["tool_foundation_disabled_component"],
                    component,
                )
                self.assertFalse(marker.exists())
                self.assertEqual(
                    len(self._query_phases(run.event_records, "tool_loop_foundation_disabled")),
                    1,
                )

    def test_code_worker_session_custody_requires_secret_and_never_projects_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission session custody cannot be selected by id alone.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "proof.txt").write_text("custody", encoding="utf-8")
            artifact_root = Path(tmpdir) / "artifacts"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )

            def request(*, token: str = "") -> WorkerRequest:
                constraints = {
                    "session_id": "permission-custody-session",
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "proof.txt"}}],
                }
                if token:
                    constraints["session_custody_token"] = token
                return WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints=constraints,
                )

            first = runtime.run(request())
            token = first.session_custody_token
            self.assertTrue(first.worker_result.ok)
            self.assertGreater(len(token), 40)
            projected = json.dumps(
                {
                    "events": [event.payload for event in first.event_records],
                    "metadata": first.worker_result.metadata,
                },
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            self.assertNotIn(token, (artifact_root / ".permission" / "state.json").read_text(encoding="utf-8"))

            stolen = runtime.run(request())
            self.assertFalse(stolen.worker_result.ok)
            self.assertEqual(stolen.worker_result.error, "session_custody_required")
            self.assertEqual(self._permission_event_kinds(stolen.event_records), [])

            resumed = runtime.run(request(token=token))
            self.assertTrue(resumed.worker_result.ok)
            self.assertEqual(resumed.worker_result.metadata["permission_session_custody_verified"], "true")

            wrong_workspace = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "other-workspace",
                artifact_root=artifact_root,
            ).run(request(token=token))
            self.assertFalse(wrong_workspace.worker_result.ok)
            self.assertEqual(wrong_workspace.worker_result.error, "session_custody_scope_mismatch")

    def test_worker_constraints_cannot_enable_bypass_or_auto_permission_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Untrusted constraints cannot widen deployment permission policy.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            run = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            ).run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "permission_mode": "bypassPermissions",
                        "permission_bypass_available": True,
                        "permission_auto_available": True,
                        "tool_plan": [
                            {
                                "tool_name": "browser",
                                "arguments": {
                                    "action": "extract_text",
                                    "html": "<html><body>must remain pending</body></html>",
                                },
                            }
                        ],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["permission_runtime_mode"], "default")
            self.assertEqual(run.worker_result.metadata["permission_runtime_allows"], "0")
            self.assertEqual(run.worker_result.metadata["permission_runtime_asks"], "1")
            self.assertEqual(
                len(self._query_phases(run.event_records, "tool_call_started")),
                1,
            )

    def test_finite_standing_allow_rule_is_atomically_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Finite standing permission rules must not overrun max_uses.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = ToolPermissionRuntime.for_session(
                session_id="finite-rule-session",
                state_path=Path(tmpdir) / "state.json",
                workspace_root=workspace,
            )
            runtime.rule_store.add(
                PermissionRuleRecord(
                    effect=PermissionEffect.ALLOW,
                    source=PermissionRuleSource.SESSION,
                    scope=PermissionScope(
                        kind=PermissionScopeKind.SESSION,
                        session_id="finite-rule-session",
                    ),
                    tool_pattern="browser",
                    reason="one exact session use for the test",
                    max_uses=1,
                )
            )

            first_call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="browser",
                arguments={"html": "<p>one</p>"},
            )
            second_call = replace(first_call, tool_call_id="second-browser-call")
            first = runtime.guard(
                self._evaluation_request(
                    state=state,
                    call=first_call,
                    session_id="finite-rule-session",
                    workspace=workspace,
                )
            )
            second = runtime.guard(
                self._evaluation_request(
                    state=state,
                    call=second_call,
                    session_id="finite-rule-session",
                    workspace=workspace,
                )
            )

            self.assertTrue(first.allowed)
            self.assertTrue(second.ask_pending)
            stored = runtime.rule_store.list(effective=False, include_inactive=True)
            self.assertEqual(stored[0].use_count, 1)
            self.assertFalse(stored[0].is_active())

    def test_global_finite_allow_rule_is_shared_across_frozen_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Global finite permission use is not once per session.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            state_path = Path(tmpdir) / "state.json"
            store = PermissionStateStore(state_path)
            store.add_global_rule(
                PermissionRuleRecord(
                    rule_id="global-browser-once",
                    effect=PermissionEffect.ALLOW,
                    source=PermissionRuleSource.POLICY,
                    scope=PermissionScope(PermissionScopeKind.GLOBAL),
                    tool_pattern="browser",
                    reason="one use across all sessions",
                    max_uses=1,
                )
            )
            runtime_a = ToolPermissionRuntime.for_session(
                session_id="global-finite-a",
                state_path=state_path,
                workspace_root=workspace,
            )
            runtime_b = ToolPermissionRuntime.for_session(
                session_id="global-finite-b",
                state_path=state_path,
                workspace_root=workspace,
            )
            call_a = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="browser",
                arguments={"html": "<p>one</p>"},
            )
            call_b = replace(call_a, tool_call_id="global-finite-call-b")

            first = runtime_a.guard(
                self._evaluation_request(
                    state=state,
                    call=call_a,
                    session_id="global-finite-a",
                    workspace=workspace,
                )
            )
            second = runtime_b.guard(
                self._evaluation_request(
                    state=state,
                    call=call_b,
                    session_id="global-finite-b",
                    workspace=workspace,
                )
            )

            self.assertTrue(first.allowed)
            self.assertFalse(second.allowed)
            self.assertTrue(second.ask_pending)
            persisted = store.read_state()
            self.assertEqual(persisted["global_rules"][0]["use_count"], 1)
            self.assertEqual(
                persisted["session_overlays"]["global-finite-a"]["base_rules"][0]["use_count"],
                1,
            )
            self.assertEqual(
                persisted["session_overlays"]["global-finite-b"]["base_rules"][0]["use_count"],
                1,
            )

    def test_resolved_approval_claim_is_durable_across_fresh_runtimes_and_old_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Resolved approval is a durable one-execution capability.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            state_path = Path(tmpdir) / "state.json"
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": "echo durable-claim"},
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="durable-approval-session",
                workspace=workspace,
            )
            owner = ToolPermissionRuntime.for_session(
                session_id="durable-approval-session",
                state_path=state_path,
                workspace_root=workspace,
            )
            asked = owner.guard(request)
            resolved = owner.resolve(
                self._allow_response(
                    asked.pending_request,
                    idempotency_key="durable-approval",
                )
            )
            self.assertTrue(resolved.accepted)
            before_claim = owner.snapshot()
            fresh_a = ToolPermissionRuntime.for_session(
                session_id="durable-approval-session",
                state_path=state_path,
                workspace_root=workspace,
            )
            fresh_b = ToolPermissionRuntime.for_session(
                session_id="durable-approval-session",
                state_path=state_path,
                workspace_root=workspace,
            )

            claimed = fresh_a.guard(request)
            replay = fresh_b.guard(request)

            self.assertTrue(claimed.allowed)
            self.assertTrue(claimed.restored_approval)
            claimed_decision_event = next(
                event
                for event in claimed.events
                if event.payload["query_session"]["permission_runtime"]["kind"]
                == "permission_decision"
            )
            self.assertEqual(
                claimed_decision_event.payload["query_session"]["permission_runtime"]["payload"][
                    "human_intervention_count"
                ],
                1,
            )
            self.assertFalse(replay.allowed)
            self.assertTrue(replay.ask_pending)
            self.assertEqual(fresh_a.metadata()["permission_runtime_consumed_approvals"], "1")
            self.assertEqual(fresh_a.metadata()["permission_runtime_human_intervention_count"], "1")
            restored_old = ToolPermissionRuntime.for_session(
                session_id="durable-approval-session",
                state_path=state_path,
                restored_snapshot=before_claim,
                workspace_root=workspace,
            )
            after_old_restore = restored_old.guard(request)
            self.assertFalse(after_old_restore.allowed)
            self.assertTrue(after_old_restore.ask_pending)
            claim_record = next(
                record
                for record in restored_old.request_queue.list()
                if record.request_id == asked.pending_request.request_id
            )
            self.assertTrue(claim_record.metadata["execution_claim_decision_id"])

    def test_two_fresh_runtimes_cannot_concurrently_claim_one_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Only one runtime may win an approval claim CAS.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            state_path = Path(tmpdir) / "state.json"
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": "echo concurrent-claim"},
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="concurrent-approval-session",
                workspace=workspace,
            )
            owner = ToolPermissionRuntime.for_session(
                session_id="concurrent-approval-session",
                state_path=state_path,
                workspace_root=workspace,
            )
            asked = owner.guard(request)
            self.assertTrue(
                owner.resolve(
                    self._allow_response(
                        asked.pending_request,
                        idempotency_key="concurrent-approval",
                    )
                ).accepted
            )
            runtimes = [
                ToolPermissionRuntime.for_session(
                    session_id="concurrent-approval-session",
                    state_path=state_path,
                    workspace_root=workspace,
                )
                for _ in range(2)
            ]
            barrier = threading.Barrier(2)
            for runtime in runtimes:
                original = runtime._approved_request_for

                def synchronized(candidate: object, *, _original=original):
                    selected = _original(candidate)
                    barrier.wait(timeout=5)
                    return selected

                runtime._approved_request_for = synchronized

            outcomes: list[object] = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(runtime.guard, request) for runtime in runtimes]
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except Exception as error:  # noqa: BLE001 - the loser must fail closed.
                        outcomes.append(error)

            allowed = [item for item in outcomes if getattr(item, "allowed", False)]
            conflicts = [item for item in outcomes if isinstance(item, PermissionStateConflict)]
            self.assertEqual(len(allowed), 1)
            self.assertEqual(len(conflicts), 1)
            invalidations = [
                event
                for runtime in runtimes
                for event in runtime.grant_store.audit_events()
                if event.event_type == "execution_grant_invalidated"
            ]
            self.assertEqual(len(invalidations), 1)

    def test_resolved_approval_cannot_issue_after_request_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Approval must be used before its exact request expires.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                arguments={"command": "echo expires"},
            )
            runtime = ToolPermissionRuntime.for_session(
                session_id="expiring-approval-session",
                state_path=Path(tmpdir) / "state.json",
                workspace_root=workspace,
                config=PermissionRuntimeConfig(approval_ttl_seconds=0.5),
            )
            request = self._evaluation_request(
                state=state,
                call=call,
                session_id="expiring-approval-session",
                workspace=workspace,
            )
            asked = runtime.guard(request)
            resolved = runtime.resolve(
                self._allow_response(
                    asked.pending_request,
                    idempotency_key="expiring-approval",
                )
            )
            self.assertTrue(resolved.accepted)
            import time as _time

            _time.sleep(0.55)
            expired = runtime.guard(request)

            self.assertFalse(expired.allowed)
            self.assertTrue(expired.ask_pending)
            self.assertEqual(runtime.metadata()["permission_runtime_consumed_approvals"], "0")

    def test_executor_rejects_caller_supplied_validator_and_freezes_context_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Executor authority is dispatcher-owned and workspace-frozen.")
            workspace_a = Path(tmpdir) / "workspace-a"
            workspace_b = Path(tmpdir) / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            marker_a = workspace_a / "marker.txt"
            marker_b = workspace_b / "marker.txt"
            context = ToolExecutionContext.for_workspace(
                workspace_root=workspace_a,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            direct = ToolExecutor(context)
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="file_write",
                arguments={"path": "marker.txt", "content": "unsafe"},
            )

            with self.assertRaises(TypeError):
                direct.execute(
                    call,
                    permission_grant=object(),
                    grant_validator=lambda *_: True,
                )
            self.assertFalse(marker_a.exists())
            self.assertFalse(marker_b.exists())

            runtime = ToolPermissionRuntime.for_session(
                session_id="frozen-executor-session",
                state_path=Path(tmpdir) / "state.json",
                workspace_root=workspace_a,
            )
            edit_target = workspace_a / "existing.txt"
            edit_target.write_text("before", encoding="utf-8")
            edit_call = replace(
                call,
                tool_name="file_edit",
                tool_call_id="frozen-edit-call",
                arguments={"path": "existing.txt", "old": "before", "new": "after"},
            )
            guarded = runtime.guard(
                self._evaluation_request(
                    state=state,
                    call=edit_call,
                    session_id="frozen-executor-session",
                    workspace=workspace_a,
                ),
                workspace_state={
                    "workspace_scoped": True,
                    "path_validated": True,
                    "read_before_write": True,
                    "baseline_current": True,
                    "bounded_change": True,
                },
            )
            self.assertTrue(guarded.allowed)
            bound = ToolExecutor(context, permission_authority=runtime)
            context.workspace_root = workspace_b
            executed = bound.execute(
                edit_call,
                permission_grant=guarded.execution_grant,
            )
            self.assertTrue(executed.ok, executed.summary)
            self.assertEqual(edit_target.read_text(encoding="utf-8"), "after")
            self.assertFalse((workspace_b / "existing.txt").exists())

    def test_restore_clamps_auto_in_plan_to_current_deployment_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            permissive = ToolPermissionRuntime.for_session(
                session_id="mode-clamp-session",
                state_path=state_path,
                config=PermissionRuntimeConfig(
                    mode="default",
                    auto_available=True,
                    use_auto_in_plan=True,
                ),
            )
            permissive.mode_runtime.transition("plan")
            self.assertTrue(permissive.mode_runtime.auto_active)
            snapshot = permissive.snapshot()

            clamped = ToolPermissionRuntime.for_session(
                session_id="mode-clamp-session",
                state_path=Path(tmpdir) / "restored.json",
                restored_snapshot=snapshot,
                config=PermissionRuntimeConfig(
                    mode="plan",
                    auto_available=True,
                    use_auto_in_plan=False,
                ),
            )

            self.assertEqual(clamped.mode_runtime.mode.value, "plan")
            self.assertFalse(clamped.mode_runtime.auto_active)
            self.assertFalse(clamped.mode_runtime.snapshot()["use_auto_in_plan"])

    def test_disabled_budget_dependency_is_detected_before_permission_or_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Execution preflight precedes every side effect.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            marker = workspace / "budget-disabled.txt"
            context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            materialization = ToolRegistryRuntime(context.registry).materialize(
                worker_request_id="worker-preflight",
                session_id="session-preflight",
                workspace_root=workspace,
            )
            scheduler = ToolLoopScheduler(materialization.to_registry())
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-preflight",
                turn_index=1,
                steps=[
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": marker.name, "content": "must not exist"},
                    }
                ],
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-preflight",
                session_id="session-preflight",
                turn_id="turn-preflight",
                turn_index=1,
                materialization=materialization,
            )

            with self.assertRaises(ToolRuntimeDisabledError):
                ToolExecutionRuntime(
                    context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=100, disabled=True),
                ).execute_batch(plan.batches[0], tool_context=tool_context, max_workers=1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
