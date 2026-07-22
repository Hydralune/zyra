from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_scheduler.recovery_runtime import (
    BranchDeltaBuilder,
    CallbackResumeOwner,
    CheckpointCodecError,
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    CheckpointPathError,
    CheckpointResumeBridge,
    CheckpointSignatureMismatch,
    CheckpointWrite,
    DecisionMode,
    DeltaConflictError,
    DeterministicCommitRuntime,
    InFlightMessage,
    PendingWriteState,
    RecoveryAction,
    RecoveryContext,
    RecoveryDecisionRuntime,
    RecoveryPlanStore,
    RecoveryRefs,
    RecoverySignalClassifier,
    RecoverySignalKind,
    RecoverySource,
    ResumeExpectations,
    SafeCheckpointCodec,
    SideEffectFenceRuntime,
)


class RecoveryPlannerRoutingMemoryFoundationTests(unittest.TestCase):
    def test_typed_signal_matrix_and_sealed_permission_policy(self) -> None:
        classifier = RecoverySignalClassifier()
        cases = (
            (RecoverySource.API_RUNTIME, "stream_stall", {"request_id": "request-1"}, RecoverySignalKind.STREAM_STALL),
            (RecoverySource.API_RUNTIME, "prompt_too_long", {"session_id": "session-1"}, RecoverySignalKind.PROMPT_TOO_LONG),
            (RecoverySource.MCP_RUNTIME, "mcp_auth_required", {"mcp_server_id": "mcp-1"}, RecoverySignalKind.MCP_AUTH_REQUIRED),
            (RecoverySource.BACKEND_RUNTIME, "backend_unavailable", {"backend_id": "backend-1"}, RecoverySignalKind.BACKEND_UNAVAILABLE),
            (RecoverySource.SUBAGENT_RUNTIME, "subagent_failed", {"subagent_id": "subagent-1"}, RecoverySignalKind.SUBAGENT_FAILED),
            (RecoverySource.CONTROL_RUNTIME, "requirement_changed", {"graph_id": "graph-1", "node_id": "node-1"}, RecoverySignalKind.REQUIREMENT_CHANGED),
        )
        for source, source_kind, extra_refs, expected in cases:
            with self.subTest(source=source, source_kind=source_kind):
                signal = classifier.classify({
                    "source": source.value,
                    "source_kind": source_kind,
                    "refs": {"run_id": "run-1", "task_id": "task-1", **extra_refs},
                    "summary": "free text says worker-pretend but cannot create identity",
                })
                self.assertEqual(signal.kind, expected)
                self.assertTrue(signal.details["typed_classification"])
                self.assertFalse(signal.details["free_text_identity_inference"])
                self.assertFalse(signal.refs.worker_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "recovery.sqlite3")
            policy = RecoveryDecisionRuntime(store)
            signal = classifier.classify({
                "source": "permission_runtime",
                "source_kind": "permission_ask",
                "refs": {
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "session_id": "session-1",
                    "tool_call_id": "tool-call-1",
                },
                "summary": "permission is required",
            })
            context = RecoveryContext(
                refs=signal.refs,
                mode=DecisionMode.SEALED_AUTONOMOUS,
                checkpoint_state={"available": True, "checkpoint_id": "checkpoint-1"},
            )
            plan, created = policy.plan(signal, context)
            self.assertTrue(created)
            self.assertNotEqual(plan.decision.selected.action, RecoveryAction.ASK_PERMISSION)
            self.assertIn(plan.decision.selected.action, {RecoveryAction.REPLAN, RecoveryAction.ABORT})
            ask = next(item for item in plan.decision.candidates if item.action is RecoveryAction.ASK_PERMISSION)
            self.assertIn("sealed_autonomous_cannot_wait_for_permission", ask.blockers)

    def test_omp_supplement_is_normalized_without_becoming_decision_owner(self) -> None:
        signal = RecoverySignalClassifier().from_omp_receipt({
            "schema": "zyra.omp-recovery-receipt/v1",
            "receipt_id": "omp-receipt-1",
            "signal_kind": "provider_rate_limit",
            "refs": {
                "run_id": "run-omp",
                "task_id": "task-omp",
                "provider_id": "provider-a",
            },
            "summary": "supplementary provider history",
            "retryable": True,
        })
        self.assertEqual(signal.kind, RecoverySignalKind.RATE_LIMITED)
        self.assertEqual(signal.source, RecoverySource.OMP_SUPPLEMENT)
        self.assertTrue(signal.details["supplementary_only"])
        self.assertEqual(signal.details["decision_owner"], "python.RecoveryDecisionRuntime")

    def test_checkpoint_exact_resume_replay_fence_and_safe_codec(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "recovery.sqlite3")
            commit = CheckpointCommitRuntime(store)
            refs = RecoveryRefs(run_id="run-checkpoint", task_id="task-checkpoint", session_id="session-checkpoint")
            checkpoint, receipt, created = commit.commit(CheckpointCommitRequest(
                refs=refs,
                workflow_signature="workflow-v1",
                graph_signature="graph-v1",
                topology_signature="topology-v1",
                owner_refs={"session": "session-owner-1", "graph": "graph-owner-1"},
                version_refs={"session": 3, "graph": 9},
                state_payload={"counter": 1, "nested": {"items": ["a"]}},
                committed_writes=(CheckpointWrite(
                    write_id="write-1",
                    task_key="task-checkpoint",
                    channel="state",
                    value={"counter": 1},
                    state=PendingWriteState.COMMITTED,
                ),),
                in_flight_messages=(
                    InFlightMessage(
                        message_id="message-processed",
                        sender_id="worker-a",
                        receiver_id="worker-b",
                        correlation_id="response-processed",
                        payload_ref={"artifact_id": "artifact-1"},
                        delivered=True,
                    ),
                    InFlightMessage(
                        message_id="message-new",
                        sender_id="worker-a",
                        receiver_id="worker-b",
                        correlation_id="response-new",
                        payload_ref={"artifact_id": "artifact-2"},
                    ),
                ),
                completed_step_ids=("step-complete",),
                processed_response_ids=("response-processed",),
            ))
            self.assertTrue(created)
            self.assertEqual(receipt.content_digest, checkpoint.content_digest)

            owner_calls: list[dict[str, object]] = []

            def resume_owner(request: dict[str, object]) -> dict[str, object]:
                owner_calls.append(request)
                return {"receipt_id": "session-resume-1", "changed": True}

            bridge = CheckpointResumeBridge(
                store,
                session_owner=CallbackResumeOwner("SessionLifecycle", resume_owner),
            )
            expected = ResumeExpectations(
                run_id=refs.run_id,
                task_id=refs.task_id,
                session_id=refs.session_id,
                workflow_signature="workflow-v1",
                graph_signature="graph-v1",
                topology_signature="topology-v1",
                owner_refs={"session": "session-owner-1"},
                version_refs={"session": 3},
                required_completed_step_ids=("step-complete",),
            )
            workset = bridge.prepare(
                checkpoint.checkpoint_id,
                expected,
                candidate_step_ids=("step-complete", "step-new"),
            )
            self.assertEqual(workset.executable_step_ids, ("step-new",))
            self.assertEqual(workset.bypassed_step_ids, ("step-complete",))
            self.assertEqual(tuple(item.message_id for item in workset.deliverable_messages), ("message-new",))
            self.assertEqual(workset.skipped_message_ids, ("message-processed",))

            first, first_owners = bridge.resume(
                checkpoint.checkpoint_id,
                expected,
                candidate_step_ids=("step-complete", "step-new"),
                idempotency_key="resume-once",
            )
            second, second_owners = bridge.resume(
                checkpoint.checkpoint_id,
                expected,
                candidate_step_ids=("step-complete", "step-new"),
                idempotency_key="resume-once",
            )
            self.assertEqual(first.resume_token, second.resume_token)
            self.assertEqual(len(owner_calls), 1)
            self.assertTrue(first_owners[0].changed)
            self.assertFalse(second_owners[0].changed)
            with self.assertRaises(CheckpointSignatureMismatch):
                bridge.prepare(
                    checkpoint.checkpoint_id,
                    ResumeExpectations(
                        run_id=refs.run_id,
                        task_id=refs.task_id,
                        session_id=refs.session_id,
                        workflow_signature="different-workflow",
                        graph_signature="graph-v1",
                        topology_signature="topology-v1",
                    ),
                )

            codec = SafeCheckpointCodec()
            self.assertEqual(codec.decode(codec.encode(checkpoint)).content_digest, checkpoint.content_digest)
            with self.assertRaises(CheckpointCodecError):
                codec.decode(b"\x80\x04pickle")
            with self.assertRaises(CheckpointPathError):
                codec.normalize({"schema": "zyra.recovery-checkpoint/v1", "workspace_path": "../escape"})

    def test_side_effect_execute_once_and_branch_conflict_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "recovery.sqlite3")
            checkpoint, _, _ = CheckpointCommitRuntime(store).commit(CheckpointCommitRequest(
                refs=RecoveryRefs(run_id="run-delta", task_id="task-delta", session_id="session-delta"),
                workflow_signature="workflow-v1",
                graph_signature="graph-v1",
                topology_signature="topology-v1",
                owner_refs={"task": "task-owner"},
                version_refs={"task": 1},
                state_payload={"counter": 1, "nested": {"items": ["base"]}},
            ))

            effect_calls = 0

            def effect(_: dict[str, object]) -> dict[str, object]:
                nonlocal effect_calls
                effect_calls += 1
                return {"receipt_id": "external-effect-1", "value": 42}

            fences = SideEffectFenceRuntime(store)
            first_response, first_fence, first_changed = fences.execute_once(
                run_id="run-delta",
                task_id="task-delta",
                operation="publish-artifact",
                request={"artifact_id": "artifact-1"},
                idempotency_key="effect-once",
                callback=effect,
            )
            replay_response, replay_fence, replay_changed = fences.execute_once(
                run_id="run-delta",
                task_id="task-delta",
                operation="publish-artifact",
                request={"artifact_id": "artifact-1"},
                idempotency_key="effect-once",
                callback=effect,
            )
            self.assertEqual(effect_calls, 1)
            self.assertEqual(replay_response["receipt_ref"], first_response["receipt_id"])
            self.assertTrue(replay_response["replayed"])
            self.assertEqual(first_fence.fence_key, replay_fence.fence_key)
            self.assertTrue(first_changed)
            self.assertFalse(replay_changed)

            first_branch = BranchDeltaBuilder(checkpoint, owner="branch-owner", branch_id="branch-a")
            second_branch = BranchDeltaBuilder(checkpoint, owner="branch-owner", branch_id="branch-b")
            first_branch.increment("counter")
            first_branch.append_unique("nested.items", "a")
            second_branch.increment("counter", 2)
            runtime = DeterministicCommitRuntime(store)
            committed = runtime.commit(first_branch.build())
            self.assertEqual(committed.checkpoint.state_payload["counter"], 2)
            self.assertEqual(committed.checkpoint.state_payload["nested"]["items"], ["base", "a"])
            self.assertEqual(checkpoint.state_payload["nested"]["items"], ["base"])
            with self.assertRaises(DeltaConflictError) as captured:
                runtime.commit(second_branch.build())
            self.assertEqual(captured.exception.conflicts[0].key, "counter")
            self.assertEqual(store.checkpoint_head("task-delta").checkpoint_id, committed.checkpoint.checkpoint_id)


if __name__ == "__main__":
    unittest.main()
