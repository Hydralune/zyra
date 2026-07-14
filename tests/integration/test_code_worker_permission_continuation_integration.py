from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
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
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_runtime.claude_session_store import (  # noqa: E402
    CodeWorkerSessionStore,
    CodeWorkerSessionStoreReceipt,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionResolutionResponse,
)
from zyra_runtime.permission.control_plane import PermissionControlPlane  # noqa: E402
from zyra_runtime.permission.continuation import (  # noqa: E402
    PermissionContinuationPhase,
    PermissionContinuationRuntime,
)
from zyra_runtime.permission.custody import PermissionSessionCustodyBinding  # noqa: E402
from zyra_runtime.permission.request_queue import PermissionRequestQueue  # noqa: E402
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_runtime.permission.transports import PermissionTransportKind  # noqa: E402
from zyra_runtime.tool_runtime_streaming import ToolStreamingRuntime  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class CodeWorkerPermissionContinuationIntegrationTests(unittest.TestCase):
    @staticmethod
    def _command(marker_name: str, content: str = "executed") -> str:
        return subprocess.list2cmdline(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({marker_name!r}).write_text({content!r}, encoding='utf-8')",
            ]
        )

    @staticmethod
    def _request(
        state: Any,
        *,
        session_id: str,
        constraints: dict[str, Any],
        custody_token: str = "",
    ) -> WorkerRequest:
        selected = {"session_id": session_id, **constraints}
        if custody_token:
            selected["session_custody_token"] = custody_token
        return WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints=selected,
        )

    @staticmethod
    def _query_phases(run: Any, prefix: str = "") -> list[dict[str, Any]]:
        phases: list[dict[str, Any]] = []
        for event in run.event_records:
            value = event.payload.get("query_session")
            if not isinstance(value, dict):
                continue
            phase = str(value.get("phase") or "")
            if not prefix or phase.startswith(prefix):
                phases.append(value)
        return phases

    @staticmethod
    def _resolve_only_pending(
        state_path: Path,
        *,
        session_id: str,
        idempotency_key: str,
        effect: PermissionEffect = PermissionEffect.ALLOW,
        custody_token: str = "",
    ) -> str:
        queue = PermissionRequestQueue(
            PermissionStateStore(state_path),
            session_id=session_id,
        )
        pending = queue.pending()
        if len(pending) != 1:
            raise AssertionError(f"expected one pending request, found {len(pending)}")
        request = pending[0]
        response = PermissionResolutionResponse(
                request_id=request.request_id,
                session_id=request.session_id,
                tool_use_id=request.tool_use_id,
                tool_identity=request.tool_identity,
                arguments_digest=request.arguments_digest,
                request_fingerprint=request.request_fingerprint,
                scope=request.scope,
                effect=effect,
                actor_id="integration-test-user",
                expected_revision=request.revision,
                channel="api" if custody_token else "user",
                idempotency_key=idempotency_key,
            )
        if custody_token:
            plane = PermissionControlPlane(PermissionStateStore(state_path))
            authority = plane.authority_from_custody(
                binding=PermissionSessionCustodyBinding(
                    session_id=request.session_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    workspace_root=request.scope.workspace_root,
                ),
                custody_token=custody_token,
                actor_id="integration-test-user",
                channel=PermissionTransportKind.API,
            )
            result = plane.resolve(authority, request.request_id, response.to_dict())
            if not result.ok:
                raise AssertionError(result.to_dict())
            return request.request_id
        outcome = queue.resolve(response)
        if not outcome.accepted:
            raise AssertionError(outcome.to_dict())
        return request.request_id

    @staticmethod
    def _latest_runtime_state(checkpoint_path: str) -> dict[str, Any]:
        records = [
            json.loads(line)
            for line in Path(checkpoint_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        checkpoint = next(
            item
            for item in reversed(records)
            if item.get("record_type") == "runtime_state_checkpoint"
        )
        return checkpoint["payload"]["runtime_state"]

    @staticmethod
    def _run_with_permission_clock_offset(
        runtime: CodeWorkerRuntime,
        request: WorkerRequest,
        *,
        seconds: float,
    ) -> Any:
        """Run with a future permission clock while preserving immutable state."""

        original_init = PermissionStateStore.__init__

        def initialize_with_offset(
            store_self: PermissionStateStore,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            kwargs["clock"] = lambda: (
                datetime.now(timezone.utc) + timedelta(seconds=seconds)
            )
            original_init(store_self, *args, **kwargs)

        with patch.object(
            PermissionStateStore,
            "__init__",
            new=initialize_with_offset,
        ):
            return runtime.run(request)

    def test_ask_parks_a_durable_barrier_and_uses_deployment_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("ASK must park the tool loop.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            command = self._command("ask-side-effect-must-not-exist.txt")

            run = runtime.run(
                self._request(
                    state,
                    session_id="continuation-barrier-session",
                    constraints={
                        "continue_on_error": True,
                        "permission_extension_registry": {
                            "replace_with": "untrusted-request-hook"
                        },
                        "query_turns": [
                            [
                                {
                                    "tool_name": "shell",
                                    "tool_call_id": "ask-shell-use-1",
                                    "arguments": {"command": command},
                                }
                            ],
                            [
                                {
                                    "tool_name": "file_write",
                                    "arguments": {
                                        "path": "later-turn-must-not-exist.txt",
                                        "content": "unsafe",
                                    },
                                }
                            ],
                        ],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_suspended")
            self.assertEqual(run.worker_result.metadata["query_turns"], "1")
            self.assertEqual(run.worker_result.metadata["tool_steps"], "1")
            self.assertEqual(
                run.worker_result.metadata["permission_continuation_suspended"],
                "true",
            )
            self.assertEqual(
                run.worker_result.metadata["permission_extension_registry_id"],
                "zyra-deployment-permission-extensions",
            )
            self.assertEqual(
                run.worker_result.metadata["permission_extension_request_configurable"],
                "false",
            )
            self.assertGreaterEqual(
                int(run.worker_result.metadata["permission_extension_hook_count"]),
                2,
            )
            self.assertFalse((workspace / "ask-side-effect-must-not-exist.txt").exists())
            self.assertFalse((workspace / "later-turn-must-not-exist.txt").exists())

            parked = self._query_phases(run, "permission_continuation_parked")
            self.assertEqual(len(parked), 1)
            self.assertEqual(parked[0]["tool_call_id"], "ask-shell-use-1")
            state_path = artifact_root / ".permission" / "state.json"
            persisted_text = state_path.read_text(encoding="utf-8")
            self.assertNotIn("ask-side-effect-must-not-exist.txt", persisted_text)
            persisted = json.loads(persisted_text)
            continuations = persisted["metadata"]["permission_continuations"]["records"]
            self.assertEqual(len(continuations), 1)
            continuation = next(iter(continuations.values()))
            self.assertEqual(continuation["phase"], "parked")
            self.assertEqual(continuation["tool_use_id"], "ask-shell-use-1")
            self.assertFalse(
                continuation["metadata"]["raw_arguments_persisted_in_permission_state"]
            )

            runtime_state = self._latest_runtime_state(
                run.worker_result.metadata["runtime_state_checkpoint_path"]
            )
            safe_snapshot = json.dumps(
                runtime_state["permission_continuation"],
                sort_keys=True,
            )
            self.assertNotIn("ask-side-effect-must-not-exist.txt", safe_snapshot)
            self.assertEqual(len(runtime_state["permission_continuation_payloads"]), 1)

    def test_permission_state_path_is_a_deployment_owned_single_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "proof.txt").write_text("owned", encoding="utf-8")
            artifact_root = root / "artifacts"
            deployment_state = root / "deployment" / "permission-state.json"
            request_selected_state = root / "untrusted" / "permission-state.json"
            state = create_task_state("Permission state path is deployment-owned.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
                permission_state_path=deployment_state,
            )

            run = runtime.run(
                self._request(
                    state,
                    session_id="deployment-state-owner-session",
                    constraints={
                        "permission_state_path": str(request_selected_state),
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "arguments": {"path": "proof.txt"},
                            }
                        ],
                    },
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertTrue(deployment_state.exists())
            self.assertFalse(request_selected_state.exists())
            self.assertFalse(
                (artifact_root / ".permission" / "state.json").exists()
            )
            persisted = PermissionStateStore(deployment_state).read_state()
            custody = persisted["metadata"]["session_custody"]["records"]
            self.assertIn("deployment-state-owner-session", custody)

    def test_custody_capability_echo_in_tool_arguments_is_rejected_before_query_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "safe.txt").write_text("safe", encoding="utf-8")
            artifact_root = root / "artifacts"
            state = create_task_state("Custody capabilities are not tool data.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "codeworker-capability-echo-session"
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "arguments": {"path": "safe.txt"},
                            }
                        ]
                    },
                )
            )
            token = first.session_custody_token
            self.assertTrue(token)
            target = workspace / "must-not-write-token.txt"

            rejected = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=token,
                    constraints={
                        "operator_note": token,
                        "nested": {token: "secret-key", "note": token},
                        "tool_plan": [
                            {
                                "tool_name": "file_write",
                                "arguments": {
                                    "path": target.name,
                                    "content": token,
                                },
                            }
                        ],
                    },
                )
            )

            self.assertFalse(rejected.worker_result.ok)
            self.assertEqual(
                rejected.worker_result.error,
                "permission_custody_capability_echo",
            )
            self.assertFalse(target.exists())
            self.assertEqual(rejected.worker_result.metadata["tool_steps"], "0")
            projected = json.dumps(
                {
                    "result": rejected.worker_result.metadata,
                    "events": [event.payload for event in rejected.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            for path in artifact_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_exact_approved_replay_reenters_guard_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("Approved continuation must replay exactly once.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-approved-session"
            command = self._command("approved-exactly-once.txt", "approved")
            plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "approved-shell-use-1",
                        "arguments": {"command": command},
                    }
                ]
            }

            first = runtime.run(
                self._request(state, session_id=session_id, constraints=plan)
            )
            custody_token = first.session_custody_token
            self.assertEqual(first.worker_result.error, "permission_suspended")
            self.assertGreater(len(custody_token), 40)
            self.assertNotIn(custody_token, json.dumps(first.safe_session_metadata()))
            private_envelope = first.private_api_session_envelope()
            self.assertTrue(private_envelope["session_custody_token_included"])
            self.assertEqual(private_envelope["session_custody_token"], custody_token)

            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-exact-replay",
            )
            second = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=plan,
                    custody_token=custody_token,
                )
            )

            self.assertTrue(second.worker_result.ok, second.worker_result.error)
            self.assertEqual(
                (workspace / "approved-exactly-once.txt").read_text(encoding="utf-8"),
                "approved",
            )
            continuation_phases = {
                item["phase"]: item
                for item in self._query_phases(second, "permission_continuation_")
            }
            self.assertIn("permission_continuation_claimed", continuation_phases)
            self.assertIn("permission_continuation_finished", continuation_phases)
            self.assertEqual(
                continuation_phases["permission_continuation_finished"][
                    "continuation_phase"
                ],
                "completed",
            )
            self.assertEqual(
                continuation_phases["permission_continuation_finished"]["request_id"],
                request_id,
            )
            started = self._query_phases(second, "tool_call_started")
            self.assertEqual([item["tool_call_id"] for item in started], ["approved-shell-use-1"])

            permission_kinds = [
                event.payload.get("query_session", {})
                .get("permission_runtime", {})
                .get("kind")
                for event in second.event_records
            ]
            self.assertIn("permission_execution_grant_issued", permission_kinds)
            self.assertIn("permission_execution_grant_consumed", permission_kinds)
            persisted = PermissionStateStore(state_path).read_state()
            record = next(
                item
                for item in persisted["metadata"]["permission_continuations"]["records"].values()
                if item["request_id"] == request_id
            )
            self.assertEqual(record["phase"], "completed")
            self.assertTrue(record["metadata"]["permission_guard_reentered"])
            self.assertTrue(record["metadata"]["execution_grant_required"])
            self.assertEqual(
                second.worker_result.metadata[
                    "permission_continuation_payload_count"
                ],
                "0",
            )

            self.assertFalse(second.session_custody_created)
            self.assertEqual(second.session_custody_token, "")
            self.assertFalse(
                second.private_api_session_envelope()["session_custody_token_included"]
            )
            projected = json.dumps(
                {
                    "first_events": [event.payload for event in first.event_records],
                    "second_events": [event.payload for event in second.event_records],
                    "first_metadata": first.worker_result.metadata,
                    "second_metadata": second.worker_result.metadata,
                    "first_artifacts": first.worker_result.artifacts,
                    "second_artifacts": second.worker_result.artifacts,
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(custody_token, projected)
            for path in artifact_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(custody_token.encode("utf-8"), path.read_bytes())

    def test_live_claim_blocks_other_worker_then_expired_lease_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("A live continuation claim fences the exact tool call.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-live-claim-session"
            marker = workspace / "claim-fence-marker.txt"
            plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "claim-fence-shell-use",
                        "arguments": {"command": self._command(marker.name)},
                    }
                ]
            }
            first = runtime.run(
                self._request(state, session_id=session_id, constraints=plan)
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-live-claim",
                custody_token=first.session_custody_token,
            )
            checkpoint = self._latest_runtime_state(
                first.worker_result.metadata["runtime_state_checkpoint_path"]
            )
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
                claim_lease_seconds=30,
            )
            ready = continuation.get(request_id)
            payload = checkpoint["permission_continuation_payloads"][ready.payload_locator]
            other_claim = continuation.prepare_resume(
                request_id,
                payload,
                claimant="other-worker-still-owns",
                idempotency_key="other-worker-live-claim",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: payload,
            )

            blocked = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertFalse(blocked.worker_result.ok)
            self.assertEqual(
                blocked.worker_result.error,
                "permission_continuation_claim_in_progress",
            )
            self.assertFalse(marker.exists())
            self.assertEqual(
                continuation.get(request_id).claim_id,
                other_claim.record.claim_id,
            )

            # Deterministically inject lease expiry after proving the live
            # claim blocks.  A one-second wall-clock lease made this race with
            # normal QueryEngine startup under parallel CI load.
            def expire_claim_lease(value: dict[str, Any]) -> None:
                records = value["metadata"]["permission_continuations"]["records"]
                current = next(
                    item for item in records.values() if item["request_id"] == request_id
                )
                current["claim_expires_at"] = "2000-01-01T00:00:00+00:00"
                current["revision"] = int(current["revision"]) + 1
                current.setdefault("metadata", {})["fault_injected_lease_expiry"] = True

            PermissionStateStore(state_path).mutate(expire_claim_lease)
            recovered = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertTrue(recovered.worker_result.ok, recovered.worker_result.error)
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed")
            completed = continuation.get(request_id)
            self.assertEqual(completed.phase, PermissionContinuationPhase.COMPLETED)
            self.assertEqual(completed.claim_attempt, 2)

    def test_unconsumed_claim_and_approval_expiry_release_different_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "unconsumed-claim-recovery.txt").write_text(
                "safe-recovery",
                encoding="utf-8",
            )
            artifact_root = root / "artifacts"
            state = create_task_state("An unconsumed expired claim cannot orphan recovery.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-unconsumed-expired-claim-session"
            dangerous_plan = {
                "permission_approval_ttl_seconds": 300,
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "unconsumed-expired-shell-use",
                        "arguments": {
                            "command": self._command(
                                "unconsumed-expired-must-not-exist.txt"
                            )
                        },
                    }
                ],
            }
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=dangerous_plan,
                )
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-before-unconsumed-claim-expiry",
                custody_token=first.session_custody_token,
            )
            checkpoint = self._latest_runtime_state(
                first.worker_result.metadata["runtime_state_checkpoint_path"]
            )
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
                claim_lease_seconds=1,
            )
            ready = continuation.get(request_id)
            payload = checkpoint["permission_continuation_payloads"][
                ready.payload_locator
            ]
            continuation.prepare_resume(
                request_id,
                payload,
                claimant="crashed-before-permission-guard",
                idempotency_key="unconsumed-claim-before-crash",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: payload,
            )

            recovery = self._run_with_permission_clock_offset(
                runtime,
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "unconsumed-claim-recovery-read",
                                "arguments": {"path": "unconsumed-claim-recovery.txt"},
                            }
                        ]
                    },
                ),
                seconds=301,
            )

            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertFalse((workspace / "unconsumed-expired-must-not-exist.txt").exists())
            expired = continuation.get(request_id)
            self.assertEqual(expired.phase, PermissionContinuationPhase.EXPIRED)
            self.assertFalse(expired.metadata["execution_outcome_unknown"])
            self.assertEqual(
                expired.metadata["terminal_reason"],
                "unconsumed_approval_expired_after_claim_lease",
            )
            self.assertEqual(
                recovery.worker_result.metadata["permission_continuation_payload_count"],
                "0",
            )

    def test_permission_payload_wal_recovers_when_final_checkpoint_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("Permission payload write-ahead survives checkpoint failure.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-wal-recovery-session"
            marker = workspace / "wal-recovery-marker.txt"
            plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "wal-recovery-shell-use",
                        "arguments": {"command": self._command(marker.name)},
                    }
                ]
            }

            def fail_checkpoint(
                store: CodeWorkerSessionStore,
                **kwargs: Any,
            ) -> CodeWorkerSessionStoreReceipt:
                return CodeWorkerSessionStoreReceipt(
                    ok=False,
                    session_id=str(kwargs.get("session_id") or ""),
                    worker_request_id=str(kwargs.get("worker_request_id") or ""),
                    path=str(store.session_path(str(kwargs.get("session_id") or ""))),
                    appended_records=(),
                    error="injected_checkpoint_failure",
                )

            with patch.object(
                CodeWorkerSessionStore,
                "append_runtime_state",
                new=fail_checkpoint,
            ):
                first = runtime.run(
                    self._request(state, session_id=session_id, constraints=plan)
                )

            self.assertFalse(first.worker_result.ok)
            self.assertEqual(first.worker_result.error, "injected_checkpoint_failure")
            self.assertFalse(marker.exists())
            session_log = Path(first.worker_result.metadata["runtime_state_checkpoint_path"])
            self.assertIn(
                '"record_type": "permission_continuation_payload"',
                session_log.read_text(encoding="utf-8"),
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-wal-recovery",
                custody_token=first.session_custody_token,
            )

            recovered = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertTrue(recovered.worker_result.ok, recovered.worker_result.error)
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed")
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
            )
            self.assertEqual(
                continuation.get(request_id).phase,
                PermissionContinuationPhase.COMPLETED,
            )
            self.assertEqual(
                recovered.worker_result.metadata["permission_continuation_payload_count"],
                "0",
            )

    def test_tombstone_failure_defers_cleanup_without_reporting_tool_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("A cleanup failure cannot turn a committed tool into failure.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-tombstone-deferred-session"
            marker = workspace / "tombstone-success-marker.txt"
            plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "tombstone-shell-use",
                        "arguments": {"command": self._command(marker.name)},
                    }
                ]
            }
            first = runtime.run(
                self._request(state, session_id=session_id, constraints=plan)
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-tombstone-deferred",
                custody_token=first.session_custody_token,
            )

            def fail_tombstone(
                store: CodeWorkerSessionStore,
                **kwargs: Any,
            ) -> CodeWorkerSessionStoreReceipt:
                return CodeWorkerSessionStoreReceipt(
                    ok=False,
                    session_id=str(kwargs.get("session_id") or ""),
                    worker_request_id=str(kwargs.get("worker_request_id") or ""),
                    path=str(store.session_path(str(kwargs.get("session_id") or ""))),
                    appended_records=(),
                    error="injected_tombstone_failure",
                )

            with patch.object(
                CodeWorkerSessionStore,
                "append_permission_continuation_tombstone",
                new=fail_tombstone,
            ):
                completed_run = runtime.run(
                    self._request(
                        state,
                        session_id=session_id,
                        constraints=plan,
                        custody_token=first.session_custody_token,
                    )
                )

            self.assertTrue(completed_run.worker_result.ok, completed_run.worker_result.error)
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed")
            self.assertIn(
                "permission_continuation_cleanup_deferred",
                [
                    item.get("phase")
                    for item in self._query_phases(completed_run)
                ],
            )
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
            )
            self.assertEqual(
                continuation.get(request_id).phase,
                PermissionContinuationPhase.COMPLETED,
            )

            reconciled = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertFalse(reconciled.worker_result.ok)
            self.assertEqual(reconciled.worker_result.error, "permission_suspended")
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed")
            session_log = Path(reconciled.worker_result.metadata["runtime_state_checkpoint_path"])
            self.assertIn(
                '"record_type": "permission_continuation_tombstone"',
                session_log.read_text(encoding="utf-8"),
            )

    def test_expired_pending_approval_releases_barrier_for_different_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "expiry-recovery-proof.txt").write_text(
                "safe-after-expiry",
                encoding="utf-8",
            )
            artifact_root = root / "artifacts"
            state = create_task_state("An expired ASK cannot orphan the session barrier.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-expired-ask-session"
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints={
                        "permission_approval_ttl_seconds": 300,
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "expired-ask-shell-use",
                                "arguments": {
                                    "command": self._command(
                                        "expired-ask-must-not-exist.txt"
                                    )
                                },
                            }
                        ],
                    },
                )
            )
            self.assertEqual(first.worker_result.error, "permission_suspended")
            state_path = artifact_root / ".permission" / "state.json"
            pending = PermissionRequestQueue(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).pending()
            self.assertEqual(len(pending), 1)
            request_id = pending[0].request_id

            recovery = self._run_with_permission_clock_offset(
                runtime,
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "expiry-recovery-read-use",
                                "arguments": {"path": "expiry-recovery-proof.txt"},
                            }
                        ]
                    },
                ),
                seconds=301,
            )

            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertFalse((workspace / "expired-ask-must-not-exist.txt").exists())
            request = PermissionRequestQueue(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertIsNotNone(request)
            self.assertEqual(str(request.phase), "expired")
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertEqual(continuation.phase, PermissionContinuationPhase.EXPIRED)
            self.assertEqual(
                recovery.worker_result.metadata["permission_continuation_payload_count"],
                "0",
            )
            started = self._query_phases(recovery, "tool_call_started")
            self.assertEqual(
                [item["tool_call_id"] for item in started],
                ["expiry-recovery-read-use"],
            )

    def test_resolved_but_unused_approval_expires_before_different_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "resolved-expiry-proof.txt").write_text(
                "safe-after-unused-approval",
                encoding="utf-8",
            )
            artifact_root = root / "artifacts"
            state = create_task_state("An unused resolved approval still has a TTL.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-resolved-expiry-session"
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints={
                        "permission_approval_ttl_seconds": 300,
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "resolved-expiry-shell-use",
                                "arguments": {
                                    "command": self._command(
                                        "resolved-expiry-must-not-exist.txt"
                                    )
                                },
                            }
                        ],
                    },
                )
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-before-unused-expiry",
                custody_token=first.session_custody_token,
            )

            resolved = PermissionRequestQueue(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertIsNotNone(resolved)
            self.assertEqual(str(resolved.phase), "resolved")
            recovery = self._run_with_permission_clock_offset(
                runtime,
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "resolved-expiry-recovery-read-use",
                                "arguments": {"path": "resolved-expiry-proof.txt"},
                            }
                        ]
                    },
                ),
                seconds=301,
            )

            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertFalse((workspace / "resolved-expiry-must-not-exist.txt").exists())
            request = PermissionRequestQueue(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertIsNotNone(request)
            self.assertEqual(str(request.phase), "resolved")
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertEqual(continuation.phase, PermissionContinuationPhase.EXPIRED)
            self.assertEqual(
                recovery.worker_result.metadata["permission_continuation_payload_count"],
                "0",
            )
            started = self._query_phases(recovery, "tool_call_started")
            self.assertEqual(
                [item["tool_call_id"] for item in started],
                ["resolved-expiry-recovery-read-use"],
            )

    def test_consumed_approval_with_unknown_outcome_is_fenced_before_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "unknown-outcome-recovery-proof.txt").write_text(
                "safe-recovery",
                encoding="utf-8",
            )
            artifact_root = root / "artifacts"
            state = create_task_state("Unknown execution outcome must forbid exact replay.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-outcome-unknown-session"
            marker = workspace / "unknown-outcome-must-not-run.txt"
            dangerous_plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "unknown-outcome-shell-use",
                        "arguments": {"command": self._command(marker.name)},
                    }
                ]
            }
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=dangerous_plan,
                )
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-before-unknown-outcome",
                custody_token=first.session_custody_token,
            )
            checkpoint = self._latest_runtime_state(
                first.worker_result.metadata["runtime_state_checkpoint_path"]
            )
            state_store = PermissionStateStore(state_path)
            continuation = PermissionContinuationRuntime(
                state_store,
                session_id=session_id,
                claim_lease_seconds=1,
            )
            ready = continuation.get(request_id)
            payload = checkpoint["permission_continuation_payloads"][
                ready.payload_locator
            ]
            claim = continuation.prepare_resume(
                request_id,
                payload,
                claimant="query-engine-crashed-after-guard",
                idempotency_key="claim-before-unknown-outcome",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: payload,
            )
            resolved = PermissionRequestQueue(
                state_store,
                session_id=session_id,
            ).get(request_id)
            self.assertIsNotNone(resolved)
            state_store.commit_decision(
                PermissionDecisionRecord(
                    effect=PermissionEffect.ALLOW,
                    mode=resolved.mode,
                    request_fingerprint=resolved.request_fingerprint,
                    arguments_digest=resolved.arguments_digest,
                    tool_use_id=resolved.tool_use_id,
                    tool_identity=resolved.tool_identity,
                    session_id=resolved.session_id,
                    task_id=resolved.task_id,
                    run_id=resolved.run_id,
                    worker_request_id="query-engine-crashed-after-guard",
                    reason_code="approval.restored",
                    reason="consume approval before injected process loss",
                    scope=resolved.scope,
                    request_id=resolved.request_id,
                ),
                consume_approval_request_id=resolved.request_id,
            )

            time.sleep(1.1)
            recovery = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "unknown-outcome-recovery-read-use",
                                "arguments": {
                                    "path": "unknown-outcome-recovery-proof.txt"
                                },
                            }
                        ]
                    },
                )
            )

            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertFalse(marker.exists())
            fenced = continuation.get(request_id)
            self.assertEqual(fenced.phase, PermissionContinuationPhase.FAILED)
            self.assertEqual(
                fenced.failure_code,
                "approval_consumed_outcome_unknown",
            )
            self.assertTrue(fenced.metadata["execution_outcome_unknown"])
            self.assertNotEqual(fenced.claim_id, "")
            self.assertEqual(fenced.claim_id, claim.record.claim_id)
            self.assertEqual(
                recovery.worker_result.metadata["permission_continuation_payload_count"],
                "0",
            )
            started = self._query_phases(recovery, "tool_call_started")
            self.assertEqual(
                [item["tool_call_id"] for item in started],
                ["unknown-outcome-recovery-read-use"],
            )

    def test_real_side_effect_before_lost_receipt_is_never_reauthorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "ambiguous-recovery-proof.txt").write_text(
                "safe-recovery",
                encoding="utf-8",
            )
            artifact_root = root / "artifacts"
            state = create_task_state("A lost receipt cannot authorize a duplicate side effect.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-real-outcome-unknown-session"
            marker = workspace / "ambiguous-side-effect-count.txt"
            command = self._command(marker.name, "executed-once")
            dangerous_plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "ambiguous-shell-use",
                        "arguments": {"command": command},
                    }
                ]
            }
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=dangerous_plan,
                )
            )
            state_path = artifact_root / ".permission" / "state.json"
            request_id = self._resolve_only_pending(
                state_path,
                session_id=session_id,
                idempotency_key="approve-before-lost-receipt",
                custody_token=first.session_custody_token,
            )

            original_continuation_init = PermissionContinuationRuntime.__init__

            def short_claim_lease(
                runtime_self: PermissionContinuationRuntime,
                *args: Any,
                **kwargs: Any,
            ) -> None:
                kwargs["claim_lease_seconds"] = 1
                original_continuation_init(runtime_self, *args, **kwargs)

            with patch.object(
                PermissionContinuationRuntime,
                "__init__",
                new=short_claim_lease,
            ):
                ambiguous = runtime.run(
                    self._request(
                        state,
                        session_id=session_id,
                        constraints={
                            **dangerous_plan,
                            "simulate_typescript_host_loss_after_tool_side_effect": True,
                        },
                        custody_token=first.session_custody_token,
                    )
                )

            self.assertFalse(ambiguous.worker_result.ok)
            self.assertEqual(
                ambiguous.worker_result.error,
                "permission_continuation_execution_ambiguous",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed-once")
            time.sleep(1.1)

            same_id = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=dangerous_plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertFalse(same_id.worker_result.ok)
            self.assertEqual(
                same_id.worker_result.error,
                "permission_continuation_outcome_unknown",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed-once")
            self.assertEqual(
                PermissionRequestQueue(
                    PermissionStateStore(state_path),
                    session_id=session_id,
                ).pending(),
                (),
            )

            new_id_same_action = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "ambiguous-shell-use-new-id",
                                "arguments": {"command": command},
                            }
                        ]
                    },
                )
            )
            self.assertFalse(new_id_same_action.worker_result.ok)
            self.assertEqual(
                new_id_same_action.worker_result.error,
                "permission_continuation_outcome_unknown",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed-once")
            self.assertEqual(
                PermissionRequestQueue(
                    PermissionStateStore(state_path),
                    session_id=session_id,
                ).pending(),
                (),
            )

            recovery = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "ambiguous-different-recovery-read",
                                "arguments": {"path": "ambiguous-recovery-proof.txt"},
                            }
                        ]
                    },
                )
            )
            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed-once")
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(state_path),
                session_id=session_id,
            ).get(request_id)
            self.assertEqual(continuation.phase, PermissionContinuationPhase.FAILED)
            self.assertEqual(
                continuation.failure_code,
                "approval_consumed_outcome_unknown",
            )

    def test_tampered_exact_replay_is_rejected_before_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("Tampered continuation must fail closed.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-tamper-session"
            original = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "tamper-shell-use-1",
                        "arguments": {"command": self._command("original-must-not-exist.txt")},
                    }
                ]
            }
            first = runtime.run(
                self._request(state, session_id=session_id, constraints=original)
            )
            self._resolve_only_pending(
                artifact_root / ".permission" / "state.json",
                session_id=session_id,
                idempotency_key="approve-before-tamper",
            )
            tampered = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "tamper-shell-use-1",
                        "arguments": {"command": self._command("tampered-must-not-exist.txt")},
                    }
                ]
            }

            second = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=tampered,
                    custody_token=first.session_custody_token,
                )
            )

            self.assertFalse(second.worker_result.ok)
            self.assertEqual(
                second.worker_result.error,
                "permission_continuation_replay_rejected",
            )
            self.assertFalse((workspace / "original-must-not-exist.txt").exists())
            self.assertFalse((workspace / "tampered-must-not-exist.txt").exists())
            self.assertNotIn(
                "permission_execution_grant_issued",
                [
                    event.payload.get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    for event in second.event_records
                ],
            )
            rejected = self._query_phases(
                second,
                "permission_continuation_replay_rejected",
            )
            self.assertEqual(len(rejected), 1)

    def test_denied_replay_stays_blocked_but_a_different_recovery_action_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "recovery-proof.txt").write_text("safe", encoding="utf-8")
            artifact_root = root / "artifacts"
            state = create_task_state("Denied continuation permits only a different recovery.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            session_id = "continuation-denied-session"
            denied_plan = {
                "tool_plan": [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "denied-shell-use-1",
                        "arguments": {
                            "command": self._command("denied-must-not-exist.txt")
                        },
                    }
                ]
            }
            first = runtime.run(
                self._request(state, session_id=session_id, constraints=denied_plan)
            )
            self._resolve_only_pending(
                artifact_root / ".permission" / "state.json",
                session_id=session_id,
                idempotency_key="deny-exact-replay",
                effect=PermissionEffect.DENY,
                custody_token=first.session_custody_token,
            )
            exact = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints=denied_plan,
                    custody_token=first.session_custody_token,
                )
            )
            self.assertFalse(exact.worker_result.ok)
            self.assertEqual(exact.worker_result.error, "permission_denied")
            self.assertFalse((workspace / "denied-must-not-exist.txt").exists())
            self.assertNotIn(
                "permission_execution_grant_issued",
                [
                    event.payload.get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    for event in exact.event_records
                ],
            )
            continuation = PermissionContinuationRuntime(
                PermissionStateStore(artifact_root / ".permission" / "state.json"),
                session_id=session_id,
            )
            records = continuation.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].phase, PermissionContinuationPhase.CANCELLED)
            self.assertEqual(continuation.active(), ())

            recovery = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "denied-shell-use-1",
                                "arguments": {"path": "recovery-proof.txt"},
                            }
                        ]
                    },
                )
            )
            self.assertTrue(recovery.worker_result.ok, recovery.worker_result.error)
            self.assertEqual(
                recovery.worker_result.metadata[
                    "permission_continuation_payload_count"
                ],
                "0",
            )
            started = self._query_phases(recovery, "tool_call_started")
            self.assertEqual([item["tool_call_id"] for item in started], ["denied-shell-use-1"])

    def test_branch_resume_does_not_inherit_source_permission_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "branch-proof.txt").write_text("branch-safe", encoding="utf-8")
            artifact_root = root / "artifacts"
            state = create_task_state("Branch context cannot inherit permission authority.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
            )
            source_session = "continuation-source-session"
            first = runtime.run(
                self._request(
                    state,
                    session_id=source_session,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "source-pending-shell-use",
                                "arguments": {
                                    "command": self._command(
                                        "source-pending-must-not-exist.txt"
                                    )
                                },
                            }
                        ]
                    },
                )
            )
            self.assertEqual(first.worker_result.error, "permission_suspended")

            branch = runtime.run(
                self._request(
                    state,
                    session_id="continuation-target-branch-session",
                    constraints={
                        "resume_session_id": source_session,
                        "resume_session_custody_token": first.session_custody_token,
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "tool_call_id": "branch-safe-read-use",
                                "arguments": {"path": "branch-proof.txt"},
                            }
                        ],
                    },
                )
            )

            self.assertTrue(branch.worker_result.ok, branch.worker_result.error)
            self.assertEqual(
                branch.worker_result.metadata["permission_continuation_pending"],
                "0",
            )
            started = self._query_phases(branch, "tool_call_started")
            self.assertEqual([item["tool_call_id"] for item in started], ["branch-safe-read-use"])
            self.assertFalse((workspace / "source-pending-must-not-exist.txt").exists())

    def test_persisted_mode_wins_and_direct_resume_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            artifact_root = root / "artifacts"
            state = create_task_state("Permission state owner controls session mode.")
            state_store = PermissionStateStore(
                artifact_root / ".permission" / "state.json"
            )
            session_id = "persisted-sealed-session"

            def persist_sealed(value: dict[str, Any]) -> None:
                integration = value.setdefault("metadata", {}).setdefault(
                    "permission_integration",
                    {},
                )
                integration.setdefault("session_modes", {})[session_id] = {
                    "session_id": session_id,
                    "mode": "sealed",
                    "revision": 1,
                    "bypass_available": False,
                    "auto_available": False,
                }

            state_store.mutate(persist_sealed)
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
                permission_bypass_available=True,
                permission_auto_available=True,
            )
            sealed = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints={
                        "permission_mode": "bypassPermissions",
                        "permission_bypass_available": True,
                        "permission_auto_available": True,
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "persisted-mode-shell-use",
                                "arguments": {
                                    "command": self._command("sealed-must-not-exist.txt")
                                },
                            }
                        ],
                    },
                )
            )
            self.assertFalse(sealed.worker_result.ok)
            self.assertEqual(sealed.worker_result.metadata["permission_runtime_mode"], "sealed")
            self.assertEqual(
                sealed.worker_result.metadata["permission_mode_from_state_owner"],
                "sealed",
            )
            self.assertFalse((workspace / "sealed-must-not-exist.txt").exists())
            self.assertEqual(
                sealed.worker_result.metadata["permission_continuation_pending"],
                "0",
            )

            direct_root = root / "direct-artifacts"
            direct = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=direct_root,
            ).run(
                self._request(
                    state,
                    session_id="direct-resume-field-session",
                    constraints={
                        "permission_continuation_resume": {
                            "request_id": "forged-request",
                            "claim_id": "forged-claim",
                            "arguments": {"secret": "must-not-project"},
                        },
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "arguments": {
                                    "command": self._command("direct-must-not-exist.txt")
                                },
                            }
                        ],
                    },
                )
            )
            self.assertFalse(direct.worker_result.ok)
            self.assertEqual(
                direct.worker_result.error,
                "permission_continuation_api_resolver_required",
            )
            self.assertFalse((workspace / "direct-must-not-exist.txt").exists())
            serialized_events = json.dumps(
                [event.payload for event in direct.event_records],
                sort_keys=True,
            )
            self.assertNotIn("must-not-project", serialized_events)
            self.assertTrue(
                direct.private_api_session_envelope()["session_custody_token_included"]
            )

    def test_persisted_mode_update_overrides_an_older_runtime_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "proof.txt").write_text("proof", encoding="utf-8")
            artifact_root = root / "artifacts"
            state = create_task_state("Durable mode update wins over old snapshot.")
            session_id = "mode-update-after-checkpoint-session"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifact_root,
                permission_bypass_available=True,
            )
            first = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "arguments": {"path": "proof.txt"},
                            }
                        ]
                    },
                )
            )
            self.assertTrue(first.worker_result.ok, first.worker_result.error)
            state_store = PermissionStateStore(
                artifact_root / ".permission" / "state.json"
            )

            def persist_sealed(value: dict[str, Any]) -> None:
                integration = value.setdefault("metadata", {}).setdefault(
                    "permission_integration",
                    {},
                )
                integration.setdefault("session_modes", {})[session_id] = {
                    "session_id": session_id,
                    "mode": "sealed",
                    "revision": 1,
                    "bypass_available": False,
                    "auto_available": False,
                }

            state_store.mutate(persist_sealed)
            second = runtime.run(
                self._request(
                    state,
                    session_id=session_id,
                    custody_token=first.session_custody_token,
                    constraints={
                        "permission_mode": "bypassPermissions",
                        "tool_plan": [
                            {
                                "tool_name": "shell",
                                "tool_call_id": "mode-update-shell-use",
                                "arguments": {
                                    "command": self._command(
                                        "mode-update-must-not-exist.txt"
                                    )
                                },
                            }
                        ],
                    },
                )
            )

            self.assertFalse(second.worker_result.ok)
            self.assertEqual(second.worker_result.metadata["permission_runtime_mode"], "sealed")
            self.assertEqual(
                second.worker_result.metadata["permission_mode_from_state_owner"],
                "sealed",
            )
            self.assertFalse((workspace / "mode-update-must-not-exist.txt").exists())
            attached = self._query_phases(second, "permission_runtime_attached")
            self.assertEqual(len(attached), 1)
            self.assertTrue(
                attached[0]["permission_mode_reconciliation"]["changed"]
            )


if __name__ == "__main__":
    unittest.main()
