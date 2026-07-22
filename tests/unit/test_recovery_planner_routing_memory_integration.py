from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from zyra_scheduler.recovery_runtime import (
    ActionPortRegistry,
    AppliedOutcomeVerifier,
    BranchDeltaBuilder,
    BranchRecoveryRuntime,
    BranchResolutionStrategy,
    CallbackActionPort,
    CallbackContinuationDispatch,
    CallbackResumeOwner,
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    CheckpointResumeBridge,
    CheckpointWrite,
    DecisionMode,
    InFlightMessage,
    InMemoryRouteOwner,
    LayeredRouteRuntime,
    MemoryAwareRouteRuntime,
    ObservationDomain,
    PendingRequestInfo,
    PendingWriteState,
    RecoveryAction,
    RecoveryActionRuntime,
    RecoveryComponent,
    RecoveryComponentControl,
    RecoveryComponentDisabled,
    RecoveryContext,
    RecoveryDecisionRuntime,
    RecoveryFeedbackIntegrationRuntime,
    RecoveryIngressRejected,
    RecoveryIngressRuntime,
    RecoveryInvariantAuditor,
    RecoveryPlanStore,
    RecoveryRefs,
    RecoverySemanticRuntime,
    RecoverySignalClassifier,
    RecoverySignalKind,
    RecoveryStateFusionRuntime,
    RouteLayer,
    RouteOwnerRegistry,
    RoutingMemoryFeedback,
    StaticRecoveryContextResolver,
    ExactRecoveryRuntime,
)


def _checkpoint(
    store: RecoveryPlanStore,
    *,
    run_id: str,
    task_id: str,
    session_id: str,
    state: dict[str, object] | None = None,
):
    return CheckpointCommitRuntime(store).commit(CheckpointCommitRequest(
        refs=RecoveryRefs(run_id=run_id, task_id=task_id, session_id=session_id),
        workflow_signature="workflow-v1",
        graph_signature="graph-v1",
        topology_signature="topology-v1",
        owner_refs={"session": "session-owner", "graph": "graph-owner", "topology_revision": 7},
        version_refs={"session": 1, "graph": 1, "topology_revision": 7},
        state_payload=state or {"left": 0, "right": 0, "items": ["base"], "topology_revision": 7},
    ))[0]


class RecoveryPlannerRoutingMemoryIntegrationTests(unittest.TestCase):
    def test_exact_resume_keeps_pending_work_and_never_replays_committed_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "recovery.sqlite3")
            resumes: list[dict[str, object]] = []

            def resume_owner(request: dict[str, object]) -> dict[str, object]:
                resumes.append(copy.deepcopy(request))
                return {"receipt_id": "session-resume-receipt", "changed": True}

            commits = CheckpointCommitRuntime(store)
            bridge = CheckpointResumeBridge(
                store,
                session_owner=CallbackResumeOwner("SessionLifecycleRuntime", resume_owner),
            )
            runtime = ExactRecoveryRuntime(store, commits, bridge)
            refs = RecoveryRefs(run_id="run-exact", task_id="task-exact", session_id="session-exact")
            attempt = runtime.prepare(
                refs=refs,
                step_id="step-publish",
                operation="publish-artifact",
                request={"artifact_id": "artifact-1"},
                idempotency_key="publish-once",
                workflow_signature="workflow-v1",
                graph_signature="graph-v1",
                topology_signature="topology-v1",
                owner_refs={"session": "session-owner", "graph": "graph-owner", "topology_revision": 7},
                version_refs={"session": 1, "graph": 1, "topology_revision": 7},
                state_payload={"progress": 3, "topology_revision": 7},
                pending_writes=(CheckpointWrite(
                    write_id="write-pending",
                    task_key="task-exact",
                    channel="artifact",
                    value={"artifact_id": "artifact-1"},
                    state=PendingWriteState.PENDING,
                ),),
                in_flight_messages=(InFlightMessage(
                    message_id="message-pending",
                    sender_id="worker-a",
                    receiver_id="worker-b",
                    correlation_id="response-publish",
                    payload_ref={"artifact_id": "artifact-1"},
                ),),
                pending_requests=(PendingRequestInfo(
                    request_id="request-pending",
                    request_kind="tool",
                    correlation_id="response-publish",
                    owner="QueryEngine",
                ),),
            )
            calls = 0

            def publish(_: dict[str, object]) -> dict[str, object]:
                nonlocal calls
                calls += 1
                return {"receipt_id": "external-publish-1", "response_id": "response-publish"}

            effect = runtime.execute_effect(attempt, publish)
            crashed = runtime.mark_crashed(effect.attempt, reason="process terminated after external acknowledgement")
            resumed = runtime.resume(
                crashed,
                candidate_step_ids=("step-publish", "step-follow-up"),
            )
            replay = runtime.execute_effect(crashed, publish)
            workset = runtime.recovery_workset(crashed, ("step-publish", "step-follow-up"))
            completed = runtime.complete(
                effect.attempt,
                response_id="response-publish",
                promote_write_ids=("write-pending",),
                state_updates={"progress": 4},
            )

            self.assertEqual(calls, 1)
            self.assertTrue(replay.replayed)
            self.assertFalse(replay.executed)
            self.assertTrue(resumed.effect_step_bypassed)
            self.assertFalse(resumed.response_replayed)
            self.assertTrue(resumed.topology_revision_preserved)
            self.assertEqual(workset["executable_step_ids"], ["step-follow-up"])
            self.assertEqual(workset["pending_requests"][0]["request_id"], "request-pending")
            self.assertEqual(workset["deliverable_messages"][0]["message_id"], "message-pending")
            self.assertEqual(completed.promoted_write_ids, ("write-pending",))
            self.assertEqual(completed.checkpoint.state_payload["progress"], 4)
            self.assertIn("response-publish", completed.checkpoint.processed_response_ids)
            self.assertEqual(len(resumes), 1)

    def test_branch_commit_order_conflict_replan_serialization_and_alias_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "branches.sqlite3")
            base = _checkpoint(
                store,
                run_id="run-branch",
                task_id="task-branch",
                session_id="session-branch",
            )
            branch_z = BranchDeltaBuilder(base, owner="worker-z", branch_id="branch-z")
            branch_z.set("left", 1)
            branch_a = BranchDeltaBuilder(base, owner="worker-a", branch_id="branch-a")
            branch_a.set("right", 2)
            runtime = BranchRecoveryRuntime(store)
            deltas = (branch_z.build(), branch_a.build())
            alias = runtime.alias_audit(deltas)
            result = runtime.commit_batch(deltas)
            self.assertEqual(result.input_order, ("branch-z", "branch-a"))
            self.assertEqual(result.deterministic_order, ("branch-a", "branch-z"))
            self.assertEqual(result.committed_branch_ids, ("branch-a", "branch-z"))
            self.assertEqual(store.checkpoint_head("task-branch").state_payload["left"], 1)
            self.assertEqual(store.checkpoint_head("task-branch").state_payload["right"], 2)
            self.assertEqual(base.state_payload["items"], ["base"])
            self.assertTrue(alias["copy_on_write"])

        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "conflicts.sqlite3")
            base = _checkpoint(
                store,
                run_id="run-conflict",
                task_id="task-conflict",
                session_id="session-conflict",
            )
            left = BranchDeltaBuilder(base, owner="worker-left", branch_id="branch-left")
            right = BranchDeltaBuilder(base, owner="worker-right", branch_id="branch-right")
            left.increment("left", 1)
            right.increment("left", 2)
            result = BranchRecoveryRuntime(store).commit_batch(
                (right.build(), left.build()),
                strategy=BranchResolutionStrategy.REPLAN,
            )
            self.assertEqual(result.committed_branch_ids, ("branch-left",))
            self.assertEqual(result.replan_branch_ids, ("branch-right",))
            self.assertEqual(store.checkpoint_head("task-conflict").state_payload["left"], 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "serialize.sqlite3")
            base = _checkpoint(
                store,
                run_id="run-serialize",
                task_id="task-serialize",
                session_id="session-serialize",
            )
            first = BranchDeltaBuilder(base, owner="worker-a", branch_id="branch-a")
            second = BranchDeltaBuilder(base, owner="worker-b", branch_id="branch-b")
            first.increment("left", 1)
            second.increment("left", 2)
            result = BranchRecoveryRuntime(store).commit_batch(
                (second.build(), first.build()),
                strategy=BranchResolutionStrategy.SERIALIZE,
            )
            self.assertEqual(result.committed_branch_ids, ("branch-a", "branch-b"))
            self.assertEqual(store.checkpoint_head("task-serialize").state_payload["left"], 3)
            self.assertGreater(result.resolutions[1].serialized_after_revision, 0)

    def test_typed_ingress_three_owner_state_fusion_and_fail_closed_component(self) -> None:
        classifier = RecoverySignalClassifier()
        components = RecoveryComponentControl()
        ingress = RecoveryIngressRuntime(classifier, components=components)
        admission = ingress.observe(
            ObservationDomain.API,
            {
                "source": "api_runtime",
                "source_kind": "api_retry_exhausted",
                "error_kind": "api_retry_exhausted",
                "refs": {
                    "run_id": "run-fusion",
                    "task_id": "task-fusion",
                    "session_id": "session-fusion",
                    "request_id": "request-fusion",
                    "provider_id": "provider-a",
                },
                "summary": "bounded API attempts were exhausted",
                "attempt_count": 4,
            },
        )
        base = StaticRecoveryContextResolver(
            session_state={"session_id": "session-fusion", "revision": 4},
            permission_state={"mode": "default", "revision": 8},
            worker_state={"worker_id": "worker-a", "available": True, "revision": 2},
            provider_state={"provider_id": "provider-a", "available": True, "revision": 9},
        )
        fused = RecoveryStateFusionRuntime(base, components=components).fuse(admission.signal)
        self.assertGreaterEqual(len(fused.families), 3)
        self.assertGreaterEqual(fused.context.metadata["state_fusion"]["family_count"], 3)
        self.assertEqual(admission.signal.kind, RecoverySignalKind.API_RETRY_EXHAUSTED)
        with self.assertRaises(RecoveryIngressRejected):
            ingress.observe(
                ObservationDomain.API,
                admission.observation.payload,
                owner="UntrustedTextParser",
            )
        components.disable(RecoveryComponent.CLASSIFIER, "mutation test")
        with self.assertRaises(RecoveryComponentDisabled):
            ingress.observe(ObservationDomain.API, admission.observation.payload)

    def test_route_layers_are_isolated_semantically_gated_and_independently_disableable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "routes.sqlite3")
            feedback = RoutingMemoryFeedback(store)
            components = RecoveryComponentControl()
            owners = RouteOwnerRegistry()
            definitions = (
                (RouteLayer.GRAPH, "commit_id", "graph-old", "graph-new"),
                (RouteLayer.WORKER, "worker_id", "worker-old", "worker-new"),
                (RouteLayer.BACKEND, "backend_id", "backend-old", "backend-new"),
                (RouteLayer.PROVIDER, "provider_id", "provider-old", "provider-new"),
                (RouteLayer.MODEL, "model_id", "model-old", "model-new"),
            )
            for layer, key, before, after in definitions:
                owner = InMemoryRouteOwner(
                    layer,
                    f"{layer.value}-owner",
                    candidates=({key: after, "available": True},),
                    identity_key=key,
                )
                owner.seed("run-routes", "task-routes", {key: before})
                owners.register(owner)
            routes = MemoryAwareRouteRuntime(
                LayeredRouteRuntime(store, owners),
                feedback,
                components=components,
            )
            classifier = RecoverySignalClassifier()
            policy = RecoveryDecisionRuntime(store)
            cases = (
                ("control_runtime", "requirement_changed", {"graph_id": "graph-old", "node_id": "node-1"}, RouteLayer.GRAPH),
                ("worker_handoff", "worker_lost", {"worker_id": "worker-old"}, RouteLayer.WORKER),
                ("backend_runtime", "backend_unavailable", {"backend_id": "backend-old"}, RouteLayer.BACKEND),
                ("provider_runtime", "provider_unavailable", {"provider_id": "provider-old"}, RouteLayer.PROVIDER),
                ("api_runtime", "api_retry_exhausted", {"request_id": "request-old", "provider_id": "provider-new"}, RouteLayer.MODEL),
            )
            plans = {}
            for source, source_kind, refs, expected_layer in cases:
                signal = classifier.classify({
                    "source": source,
                    "source_kind": source_kind,
                    "refs": {"run_id": "run-routes", "task_id": "task-routes", **refs},
                    "summary": f"{expected_layer.value} owner reported failure",
                    "observable_side_effect": False,
                })
                context = RecoveryContext(
                    refs=signal.refs,
                    worker_state={"available": True, "worker_id": "worker-old", "candidates": ["worker-new"]},
                    backend_state={"available": True, "backend_id": "backend-old", "candidates": ["backend-new"]},
                    provider_state={
                        "available": True,
                        "provider_id": "provider-old",
                        "routes": ["provider-new"],
                        "model_id": "model-old",
                        "models": ["model-new"],
                    },
                    metadata={
                        "state_fusion": {
                            "family_count": 3,
                            "families": ["worker", "backend", "provider"],
                        }
                    },
                )
                plan, _ = policy.plan(signal, context, idempotency_key=f"route-{expected_layer.value}")
                RecoverySemanticRuntime().preflight(plan, context).require_accepted()
                plans[expected_layer] = plan
                before = {item.layer: item.digest for item in routes.snapshot("run-routes", "task-routes")}
                decision = routes.apply(
                    plan,
                    plan.decision.selected.action,
                    layers=(expected_layer,),
                )
                after = {item.layer: item.digest for item in routes.snapshot("run-routes", "task-routes")}
                changed = {layer for layer in before if before[layer] != after[layer]}
                self.assertEqual(changed, {expected_layer})
                self.assertEqual(tuple(item.layer for item in decision.changes if item.applied), (expected_layer,))

            components.disable(RecoveryComponent.WORKER_ROUTER, "worker route mutation test")
            with self.assertRaises(RecoveryComponentDisabled):
                routes.apply(
                    plans[RouteLayer.WORKER],
                    RecoveryAction.REROUTE,
                    layers=(RouteLayer.WORKER,),
                )
            self.assertTrue(components.status(RecoveryComponent.GRAPH_ROUTER).usable)
            self.assertTrue(components.status(RecoveryComponent.BACKEND_ROUTER).usable)
            self.assertTrue(components.status(RecoveryComponent.PROVIDER_ROUTER).usable)

    def test_permission_wait_blocks_tool_without_memory_and_compact_resumes_exact_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "semantic-paths.sqlite3")
            feedback = RoutingMemoryFeedback(store)
            routes = LayeredRouteRuntime(store, RouteOwnerRegistry())
            semantics = RecoverySemanticRuntime()
            permission_signal = RecoverySignalClassifier().classify({
                "source": "permission_runtime",
                "source_kind": "permission_ask",
                "refs": {
                    "run_id": "run-permission",
                    "task_id": "task-permission",
                    "request_id": "permission-request-1",
                    "tool_call_id": "tool-call-1",
                },
                "summary": "the tool requires interactive approval",
                "permission_effect": "ask",
            })
            permission_context = RecoveryContext(
                refs=permission_signal.refs,
                mode=DecisionMode.INTERACTIVE,
                session_state={"session_id": "session-permission"},
                permission_state={
                    "pending_request_id": "permission-request-1",
                    "tool_dispatch_allowed": False,
                },
                worker_state={"worker_id": "worker-permission", "available": True},
                metadata={
                    "state_fusion_digest": "fusion-permission-1",
                    "observation_digest": "observation-permission-1",
                    "state_fusion": {
                        "family_count": 3,
                        "families": ["session", "permission", "worker"],
                    }
                },
            )
            permission_plan, _ = RecoveryDecisionRuntime(store).plan(permission_signal, permission_context)
            permission_ports = ActionPortRegistry((CallbackActionPort(
                RecoveryAction.ASK_PERMISSION,
                "ToolPermissionRuntime",
                lambda _: {
                    "status": "deferred",
                    "changed_execution": True,
                    "before": {"tool_dispatch_allowed": False},
                    "after": {
                        "tool_dispatch_allowed": False,
                        "pending_request_id": "permission-request-1",
                    },
                    "receipt_ref": "permission-request-1",
                },
            ),))
            permission_result = RecoveryActionRuntime(
                store,
                routes,
                feedback,
                ports=permission_ports,
                outcome_verifier=AppliedOutcomeVerifier(
                    routes,
                    CallbackContinuationDispatch(
                        "unreachable-continuation",
                        lambda _: self.fail("waiting permission must not dispatch a continuation"),
                    ),
                ),
                semantic_gate=semantics,
            ).execute(permission_plan.plan_id, permission_context)
            self.assertEqual(permission_result.outcome.kind.value, "waiting")
            self.assertFalse(permission_result.routing_memory_id)
            self.assertFalse(permission_result.receipts[0].after["tool_dispatch_allowed"])
            self.assertTrue(RecoveryInvariantAuditor(store).audit_task("task-permission").ok)

            checkpoint = _checkpoint(
                store,
                run_id="run-compact",
                task_id="task-compact",
                session_id="session-compact",
                state={"context_revision": 4, "topology_revision": 7},
            )
            compact_signal = RecoverySignalClassifier().classify({
                "source": "compact_runtime",
                "source_kind": "compact_needed",
                "refs": {
                    "run_id": "run-compact",
                    "task_id": "task-compact",
                    "session_id": "session-compact",
                    "turn_id": "turn-compact",
                    "checkpoint_id": checkpoint.checkpoint_id,
                },
                "summary": "context pressure requires compact and exact restore",
                "partial_output": True,
                "observable_side_effect": True,
            })
            compact_context = RecoveryContext(
                refs=compact_signal.refs,
                session_state={"session_id": "session-compact", "compact_available": True},
                checkpoint_state={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "exact_resume_ready": True,
                    "workflow_signature": "workflow-v1",
                    "graph_signature": "graph-v1",
                    "topology_signature": "topology-v1",
                    "owner_refs": {"session": "session-owner", "graph": "graph-owner", "topology_revision": 7},
                    "version_refs": {"session": 1, "graph": 1, "topology_revision": 7},
                    "candidate_step_ids": ["step-after-compact"],
                },
                permission_state={"mode": "default", "tool_dispatch_allowed": False},
                metadata={
                    "state_fusion_digest": "fusion-compact-1",
                    "observation_digest": "observation-compact-1",
                    "state_fusion": {
                        "family_count": 3,
                        "families": ["session", "checkpoint", "permission"],
                    }
                },
            )
            compact_plan, _ = RecoveryDecisionRuntime(store).plan(compact_signal, compact_context)
            compact_ports = ActionPortRegistry((CallbackActionPort(
                RecoveryAction.COMPACT,
                "SessionCompactRuntime",
                lambda _: {
                    "status": "applied",
                    "changed_execution": True,
                    "before": {"context_revision": 4},
                    "after": {"context_revision": 5, "compacted": True},
                    "receipt_ref": "compact-receipt-1",
                },
            ),))
            resume_bridge = CheckpointResumeBridge(
                store,
                session_owner=CallbackResumeOwner(
                    "SessionLifecycleRuntime",
                    lambda _: {"receipt_id": "session-resume-1", "changed": True},
                ),
            )
            verifier = AppliedOutcomeVerifier(
                routes,
                CallbackContinuationDispatch(
                    "QueryContinuationRuntime",
                    lambda request: {
                        "receipt_id": "compact-continuation-1",
                        "accepted": True,
                        "changed_execution": True,
                        "dispatch_kind": "context_restored",
                        "before": {"context_revision": 4},
                        "after": {"context_revision": 5, "dispatch_id": "turn-compact-resumed"},
                        "canonical_ref": {"dispatch_id": "turn-compact-resumed"},
                    },
                ),
            )
            compact_result = RecoveryActionRuntime(
                store,
                routes,
                feedback,
                ports=compact_ports,
                resume_bridge=resume_bridge,
                outcome_verifier=verifier,
                semantic_gate=semantics,
            ).execute(compact_plan.plan_id, compact_context)
            self.assertEqual(
                tuple(receipt.action for receipt in compact_result.receipts),
                (RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT),
            )
            self.assertTrue(compact_result.applied_proof["applied"])
            self.assertTrue(compact_result.routing_memory_id)
            self.assertEqual(compact_result.receipts[1].checkpoint_receipt.phase.value, "resumed")
            self.assertTrue(RecoveryInvariantAuditor(store).audit_task("task-compact").ok)

    def test_applied_outcome_gate_semantics_and_feedback_change_later_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryPlanStore(Path(tmpdir) / "applied.sqlite3")
            feedback = RoutingMemoryFeedback(store)
            owners = RouteOwnerRegistry()
            worker = InMemoryRouteOwner(
                RouteLayer.WORKER,
                "WorkerPool",
                candidates=({"worker_id": "worker-new", "available": True},),
                identity_key="worker_id",
            )
            worker.seed("run-applied", "task-applied", {"worker_id": "worker-old"})
            owners.register(worker)
            routes = MemoryAwareRouteRuntime(LayeredRouteRuntime(store, owners), feedback)
            continuation_calls: list[dict[str, object]] = []

            def continue_after(request):
                continuation_calls.append(request.to_dict())
                return {
                    "receipt_id": "continuation-worker-new",
                    "accepted": True,
                    "changed_execution": True,
                    "dispatch_kind": "worker_dispatch",
                    "before": {"worker_id": "worker-old"},
                    "after": {"worker_id": "worker-new", "dispatch_id": "dispatch-new"},
                    "canonical_ref": {"dispatch_id": "dispatch-new", "worker_id": "worker-new"},
                }

            verifier = AppliedOutcomeVerifier(
                routes,
                CallbackContinuationDispatch("SchedulerContinuation", continue_after),
            )
            semantics = RecoverySemanticRuntime()
            actions = RecoveryActionRuntime(
                store,
                routes,
                feedback,
                outcome_verifier=verifier,
                semantic_gate=semantics,
            )
            signal = RecoverySignalClassifier().classify({
                "source": "subagent_runtime",
                "source_kind": "subagent_failed",
                "refs": {
                    "run_id": "run-applied",
                    "task_id": "task-applied",
                    "subagent_id": "subagent-a",
                    "worker_id": "worker-old",
                },
                "summary": "subagent failed on the leased worker",
                "observable_side_effect": False,
            })
            context = RecoveryContext(
                refs=signal.refs,
                session_state={"session_id": "session-applied"},
                worker_state={"available": True, "worker_id": "worker-old", "candidates": ["worker-new"]},
                provider_state={"available": True, "provider_id": "provider-a"},
                metadata={"state_fusion": {"family_count": 3, "families": ["session", "worker", "provider"]}},
            )
            plan, _ = RecoveryDecisionRuntime(store).plan(signal, context)
            result = actions.execute(plan.plan_id, context)
            learning = RecoveryFeedbackIntegrationRuntime(store, feedback, routes)
            influence = learning.verify_execution(
                result.plan,
                result,
                context,
                candidates={
                    RouteLayer.WORKER: (
                        {"worker_id": "worker-old", "available": True, "priority": 0},
                        {"worker_id": "worker-new", "available": True, "priority": 0},
                    )
                },
                require_candidate_change=True,
            )
            self.assertTrue(result.applied_proof["applied"])
            self.assertTrue(result.applied_proof["semantic_outcome"]["accepted"])
            self.assertTrue(result.routing_memory_id)
            self.assertEqual(len(continuation_calls), 1)
            self.assertTrue(influence.accepted)
            self.assertTrue(influence.changed_later_decision)
            self.assertIn(result.applied_proof["proof_id"], influence.to_dict()["applied_proof_id"])
            self.assertTrue(learning.task_snapshot("task-applied").consistent)


if __name__ == "__main__":
    unittest.main()
