from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in [ROOT / "packages" / "core", ROOT / "packages" / "runtime"]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_runtime.permission.api import (  # noqa: E402
    PermissionApiAuthenticationError,
    PermissionApiAuthorizationError,
    PermissionApiFacade,
    PermissionApiOperation,
    permission_api_error_response,
)
from zyra_runtime.permission.control_plane import (  # noqa: E402
    PermissionControlForbidden,
    PermissionControlIdentityError,
    PermissionControlPlane,
    PermissionQuery,
)
from zyra_runtime.permission.continuation import (  # noqa: E402
    PermissionContinuationPhase,
    PermissionContinuationRuntime,
)
from zyra_runtime.permission.custody import (  # noqa: E402
    PermissionSessionCustodyBinding,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionMode,
    PermissionRequestStatus,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from zyra_runtime.permission.runtime import ToolPermissionRuntime  # noqa: E402
from zyra_runtime.permission.transports import (  # noqa: E402
    CallbackPermissionTransport,
    PermissionTransportDescriptor,
    PermissionTransportFailureMode,
    PermissionTransportKind,
    PermissionTransportRegistry,
)


class PermissionControlPlaneIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "permission-state.json"
        self.session_id = "permission-integration-session"
        self.run_id = "permission-integration-run"
        self.task_id = "permission-integration-task"
        self.binding = PermissionSessionCustodyBinding(
            session_id=self.session_id,
            run_id=self.run_id,
            task_id=self.task_id,
            workspace_root=str(self.workspace),
        )
        self.plane = PermissionControlPlane.from_path(self.state_path)
        self.receipt = self.plane.custody_store.claim(self.binding)
        self.internal = self.plane.authority_from_receipt(
            self.receipt,
            actor_id="runtime",
            channel=PermissionTransportKind.STRUCTURED_IO,
        )
        self.api = self.plane.authority_from_custody(
            binding=self.binding,
            custody_token=self.receipt.token,
            actor_id="api-operator",
            channel=PermissionTransportKind.API,
        )

    def evaluation(
        self,
        *,
        tool_use_id: str = "shell-call-1",
        command: str = "python -c \"print('CONTROL-SECRET-7421')\"",
    ) -> PermissionEvaluationRequest:
        return PermissionEvaluationRequest(
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            worker_request_id="worker-request-1",
            turn_id="turn-1",
            node_id="node-1",
            tool_use_id=tool_use_id,
            tool_identity=ToolIdentity(namespace="builtin", name="shell"),
            arguments={"command": command},
            operation="execute",
            mode=PermissionMode.DEFAULT,
            workspace_root=str(self.workspace),
            principal_id="principal-a",
            interactive=True,
            requires_interaction=True,
            attributes={"capabilities": ["shell"]},
        )

    def create_request(self, evaluation: PermissionEvaluationRequest | None = None):
        return self.plane.create(
            self.internal,
            evaluation or self.evaluation(),
            reason_code="shell.requires-approval",
            reason="shell action requires an exact approval",
            metadata={"test_path": "real-control-plane"},
        ).request

    def response(self, *, effect: str, key: str, **extra: object) -> dict[str, object]:
        return {
            "effect": effect,
            "idempotency_key": key,
            **extra,
        }

    def test_create_deliver_resolve_retry_is_durable_and_contains_no_raw_arguments(self) -> None:
        request = self.create_request()
        self.assertIsNotNone(request)

        delivered = self.plane.deliver(
            self.api,
            request.request_id,
            channel=PermissionTransportKind.API,
        )
        self.assertTrue(delivered.ok)
        self.assertEqual(delivered.request.phase.value, "delivered")
        self.assertEqual(len(delivered.delivery_receipts), 1)

        resolved = self.plane.resolve(
            self.api,
            request.request_id,
            self.response(effect="allow", key="approval-1"),
        )
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.request.status, PermissionRequestStatus.APPROVED)
        self.assertFalse(resolved.metadata["execution_grant_issued"])
        self.assertEqual(resolved.request.resolved_by, "api-operator")
        self.assertEqual(resolved.request.resolution_channel, "api")

        retry = self.plane.prepare_retry(self.api, request.request_id)
        self.assertTrue(retry.ok)
        self.assertFalse(retry.retry.to_dict()["raw_arguments_included"])
        self.plane.verify_retry_replay(self.api, retry.retry.retry_id, self.evaluation())
        with self.assertRaises(PermissionControlIdentityError):
            self.plane.verify_retry_replay(
                self.api,
                retry.retry.retry_id,
                self.evaluation(command="python -c \"print('forged')\""),
            )

        state_text = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("CONTROL-SECRET-7421", state_text)
        self.assertNotIn(self.receipt.token, state_text)
        self.assertIn(request.arguments_digest, state_text)
        self.assertEqual(
            self.plane.query(
                self.api,
                PermissionQuery(session_id=self.session_id),
            ).total,
            1,
        )

    def test_authenticated_capability_echo_is_rejected_before_state_or_event_mutation(self) -> None:
        request = self.create_request()
        secret_response = self.response(
            effect="allow",
            key="secret-echo-must-not-resolve",
            reason=f"operator copied {self.receipt.token}",
            metadata={"note": self.receipt.token},
        )

        with self.assertRaises(PermissionControlForbidden):
            self.plane.resolve(self.api, request.request_id, secret_response)
        secret_key_response = self.response(
            effect="allow",
            key="secret-key-must-not-resolve",
            metadata={self.receipt.token: "persist-me"},
        )
        with self.assertRaises(PermissionControlForbidden):
            self.plane.resolve(self.api, request.request_id, secret_key_response)
        with self.assertRaises(PermissionControlForbidden):
            self.plane.authority_from_custody(
                binding=self.binding,
                custody_token=self.receipt.token,
                actor_id="api-operator",
                channel=PermissionTransportKind.API,
                metadata={self.receipt.token: "authority-smuggle"},
            )

        pending = self.plane.get_request(self.api, request.request_id)
        self.assertEqual(pending.status, PermissionRequestStatus.PENDING)
        state_text = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn(self.receipt.token, state_text)
        self.assertNotIn("secret-echo-must-not-resolve", state_text)
        self.assertNotIn("secret-key-must-not-resolve", state_text)
        self.assertNotIn(self.receipt.token, json.dumps(self.api.to_dict()))
        self.assertNotIn(self.receipt.token, repr(self.api))

    def test_control_lifecycle_advances_the_same_durable_continuation(self) -> None:
        request = self.create_request()
        continuation = PermissionContinuationRuntime(
            self.plane.state_store,
            session_id=self.session_id,
        )
        parked = continuation.park(
            request,
            payload_locator="session://permission-integration/turn-1/tool-1",
            session_sequence=1,
            metadata={"raw_arguments_included": False},
        )
        self.assertEqual(parked.phase, PermissionContinuationPhase.PARKED)

        delivered = self.plane.deliver(self.api, request.request_id)
        self.assertEqual(delivered.metadata["continuation_phase"], "delivered")
        self.assertEqual(
            continuation.get(request.request_id).phase,
            PermissionContinuationPhase.DELIVERED,
        )

        resolved = self.plane.resolve(
            self.api,
            request.request_id,
            self.response(effect="allow", key="continuation-approval"),
        )
        self.assertEqual(resolved.metadata["continuation_phase"], "resolution_ready")
        ready = continuation.get(request.request_id)
        self.assertEqual(ready.phase, PermissionContinuationPhase.RESOLUTION_READY)
        self.assertFalse(ready.authorizes_execution)
        resolved_envelope = resolved.events[0].payload["query_session"]["permission_runtime"]
        self.assertEqual(resolved_envelope["cause_event_id"], delivered.events[0].event_id)
        links = self.plane.state_store.read_state()["metadata"]["permission_integration"][
            "event_links"
        ][request.request_id]
        self.assertEqual(
            [item["phase"] for item in links],
            ["request_created", "request_delivered", "request_resolved"],
        )

    def test_cancel_abort_and_expire_sync_continuation_events_and_recovery(self) -> None:
        continuation = PermissionContinuationRuntime(
            self.plane.state_store,
            session_id=self.session_id,
        )
        session_sequence = 0

        def create_and_park(tool_use_id: str, *, ttl_seconds: float = 300.0):
            nonlocal session_sequence
            session_sequence += 1
            request = self.plane.create(
                self.internal,
                self.evaluation(tool_use_id=tool_use_id),
                reason_code="shell.requires-approval",
                reason="exercise terminal control lifecycle",
                ttl_seconds=ttl_seconds,
            ).request
            continuation.park(
                request,
                payload_locator=f"session://terminal/{tool_use_id}",
                session_sequence=session_sequence,
            )
            return request

        cancelled = create_and_park("terminal-cancel")
        cancel_result = self.plane.cancel(
            self.api,
            cancelled.request_id,
            reason="operator cancelled exact request",
        )
        self.assertEqual(cancel_result.request.phase.value, "cancelled")
        self.assertEqual(
            continuation.get(cancelled.request_id).phase,
            PermissionContinuationPhase.CANCELLED,
        )

        aborted = create_and_park("terminal-abort")
        abort_result = self.plane.abort(
            self.internal,
            aborted.request_id,
            reason="runtime aborted exact request",
        )
        self.assertEqual(abort_result.request.phase.value, "aborted")
        self.assertEqual(
            continuation.get(aborted.request_id).phase,
            PermissionContinuationPhase.CANCELLED,
        )

        expiring = create_and_park("terminal-expire", ttl_seconds=0.001)
        time.sleep(0.02)
        expire_result = next(
            result
            for result in self.plane.expire_due(self.internal)
            if result.request.request_id == expiring.request_id
        )
        self.assertEqual(expire_result.request.phase.value, "expired")
        self.assertEqual(
            continuation.get(expiring.request_id).phase,
            PermissionContinuationPhase.EXPIRED,
        )

        expected_kinds = {
            "permission_request_cancelled",
            "permission_request_aborted",
            "permission_request_expired",
        }
        observed_kinds = {
            result.events[0]
            .payload["query_session"]["permission_runtime"]["kind"]
            for result in (cancel_result, abort_result, expire_result)
        }
        self.assertEqual(observed_kinds, expected_kinds)
        for result in (cancel_result, abort_result, expire_result):
            recovery = result.events[1].payload["query_session"]["permission_runtime"]
            self.assertEqual(recovery["kind"], "recovery_input")
            self.assertEqual(recovery["cause_event_id"], result.events[0].event_id)

    def test_approved_guard_retry_has_a_causal_edge_from_control_resolution(self) -> None:
        evaluation = self.evaluation(tool_use_id="causal-shell")
        first_runtime = ToolPermissionRuntime.for_session(
            session_id=self.session_id,
            state_path=self.state_path,
            workspace_root=self.workspace,
            custody_fingerprint=self.receipt.custody_fingerprint,
        )
        pending = first_runtime.guard(evaluation)
        self.assertEqual(pending.decision.effect, PermissionEffect.ASK)
        request = pending.pending_request
        delivered = self.plane.deliver(self.api, request.request_id)
        resolved = self.plane.resolve(
            self.api,
            request.request_id,
            self.response(effect="allow", key="causal-approval"),
        )

        retry_runtime = ToolPermissionRuntime.for_session(
            session_id=self.session_id,
            state_path=self.state_path,
            workspace_root=self.workspace,
            custody_fingerprint=self.receipt.custody_fingerprint,
        )
        allowed = retry_runtime.guard(evaluation)
        self.assertEqual(allowed.decision.effect, PermissionEffect.ALLOW)
        evaluation_envelope = allowed.events[0].payload["query_session"]["permission_runtime"]
        self.assertEqual(evaluation_envelope["cause_event_id"], resolved.events[0].event_id)
        self.assertNotEqual(evaluation_envelope["cause_event_id"], delivered.events[0].event_id)

    def test_concurrent_resolve_has_one_winner_and_duplicate_does_not_mutate_pending(self) -> None:
        request = self.create_request()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []
        errors: list[Exception] = []

        def resolve(key: str) -> None:
            try:
                barrier.wait(timeout=5)
                outcomes.append(
                    PermissionControlPlane.from_path(self.state_path).resolve(
                        PermissionControlPlane.from_path(self.state_path).authority_from_custody(
                            binding=self.binding,
                            custody_token=self.receipt.token,
                            actor_id=f"actor-{key}",
                            channel=PermissionTransportKind.API,
                        ),
                        request.request_id,
                        self.response(effect="allow", key=key),
                    )
                )
            except Exception as error:  # noqa: BLE001 - asserted below.
                errors.append(error)

        first = threading.Thread(target=resolve, args=("race-a",))
        second = threading.Thread(target=resolve, args=("race-b",))
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        accepted = [item for item in outcomes if item.ok]
        rejected = [item for item in outcomes if not item.ok]
        self.assertFalse(errors)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(
            self.plane.get_request(self.api, request.request_id).status,
            PermissionRequestStatus.APPROVED,
        )

    def test_deny_installs_exact_guard_and_same_action_does_not_prompt_again(self) -> None:
        evaluation = self.evaluation(tool_use_id="denied-shell")
        request = self.create_request(evaluation)
        denied = self.plane.resolve(
            self.api,
            request.request_id,
            self.response(effect="deny", key="deny-once"),
        )
        self.assertTrue(denied.ok)
        self.assertEqual(denied.request.status, PermissionRequestStatus.DENIED)
        self.assertEqual(denied.rule.effect, PermissionEffect.DENY)
        self.assertGreaterEqual(len(denied.events), 2)

        runtime = ToolPermissionRuntime.for_session(
            session_id=self.session_id,
            state_path=self.state_path,
            workspace_root=self.workspace,
            custody_fingerprint=self.receipt.custody_fingerprint,
        )
        guarded = runtime.guard(evaluation)
        self.assertEqual(guarded.decision.effect, PermissionEffect.DENY)
        self.assertIsNone(guarded.pending_request)
        self.assertIsNone(guarded.execution_grant)

        different = runtime.guard(
            self.evaluation(
                tool_use_id="different-shell",
                command="python -c \"print('different')\"",
            )
        )
        self.assertEqual(different.decision.effect, PermissionEffect.ASK)
        self.assertIsNotNone(different.pending_request)

    def test_transport_and_identity_are_server_owned(self) -> None:
        request = self.create_request()
        facade = PermissionApiFacade(
            self.plane,
            workspace_root=self.workspace,
        )
        with self.assertRaises(PermissionApiAuthorizationError):
            facade.deliver_request(
                self.api,
                request.request_id,
                {"channel": "gateway"},
            )
        with self.assertRaises(PermissionApiAuthorizationError):
            facade.resolve_request(
                self.api,
                request.request_id,
                self.response(
                    effect="allow",
                    key="spoofed",
                    channel="hook",
                    actor_id="policy-engine",
                ),
            )

        resolved = facade.resolve_request(
            self.api,
            request.request_id,
            {
                "effect": "allow",
                "idempotency_key": "server-stamped",
            },
        )
        self.assertEqual(resolved.body["result"]["request"]["resolution_channel"], "api")
        self.assertEqual(resolved.body["result"]["request"]["resolved_by"], "api-operator")

    def test_bridge_callback_receives_redacted_envelope_and_durable_request_survives_delivery(self) -> None:
        envelopes: list[object] = []
        registry = PermissionTransportRegistry(
            (
                CallbackPermissionTransport(
                    descriptor=PermissionTransportDescriptor(
                        transport_id="test-bridge",
                        name="test bridge",
                        kind=PermissionTransportKind.BRIDGE,
                        enabled=True,
                        priority=1,
                        failure_mode=PermissionTransportFailureMode.FAIL_CLOSED,
                        authoritative_response_channel=True,
                        max_pending=8,
                        source="test-deployment",
                    ),
                    callback=lambda envelope: envelopes.append(envelope)
                    or {"accepted": True, "remote_reference": "bridge-message-1"},
                ),
            )
        )
        plane = PermissionControlPlane(self.plane.state_store, transport_registry=registry)
        bridge = plane.authority_from_custody(
            binding=self.binding,
            custody_token=self.receipt.token,
            actor_id="bridge-principal",
            channel=PermissionTransportKind.BRIDGE,
        )
        request = plane.create(
            bridge,
            self.evaluation(tool_use_id="bridge-shell"),
            reason_code="bridge.ask",
            reason="bridge delivery",
        ).request
        delivered = plane.deliver(
            bridge,
            request.request_id,
            transport_id="test-bridge",
        )

        self.assertTrue(delivered.ok)
        self.assertEqual(len(envelopes), 1)
        projected = envelopes[0].to_dict()
        encoded = json.dumps(projected, sort_keys=True)
        self.assertNotIn("CONTROL-SECRET-7421", encoded)
        self.assertNotIn(self.receipt.token, encoded)
        self.assertFalse(projected["raw_arguments_included"])
        self.assertEqual(
            plane.get_request(bridge, request.request_id).phase.value,
            "delivered",
        )

    def test_service_attested_sdk_request_creation_uses_same_queue_and_rejects_bad_token(self) -> None:
        facade = PermissionApiFacade(
            self.plane,
            workspace_root=self.workspace,
            service_token="sdk-service-token",
        )
        payload = {
            "worker_request_id": "sdk-worker",
            "turn_id": "sdk-turn",
            "tool_use_id": "sdk-shell-call",
            "tool_identity": {"namespace": "sdk", "name": "shell"},
            "arguments": {"command": "python -c \"print('sdk')\""},
            "attributes": {"capabilities": ["shell"]},
            "reason_code": "sdk.ask",
        }
        with self.assertRaises(PermissionApiAuthenticationError):
            facade.create_request(
                self.api,
                payload,
                service_token="wrong",
            )

        created = facade.create_request(
            self.api,
            payload,
            service_token="sdk-service-token",
        )
        self.assertEqual(created.status.value, 201)
        request = created.body["result"]["request"]
        self.assertEqual(request["tool_identity"]["namespace"], "sdk")
        self.assertEqual(request["status"], "pending")
        self.assertFalse(created.body["result"]["metadata"].get("execution_grant_issued", False))
        self.assertEqual(self.plane.query(self.api).total, 1)

    def test_session_custody_is_required_and_token_is_only_returned_once(self) -> None:
        other_session = "new-session"
        facade = PermissionApiFacade(
            PermissionControlPlane.from_path(self.root / "other-state.json"),
            workspace_root=self.workspace,
        )
        opened = facade.open_session(
            session_id=other_session,
            run_id="new-run",
            task_id="new-task",
        )
        token = opened.body["session"]["bearer_token"]
        self.assertTrue(token)
        self.assertEqual(opened.headers["Cache-Control"], "no-store, max-age=0")
        self.assertNotIn(token, (self.root / "other-state.json").read_text(encoding="utf-8"))

        resumed = facade.resume_session(
            session_id=other_session,
            run_id="new-run",
            task_id="new-task",
            custody_token=token,
            actor_id="api-operator",
        )
        self.assertNotIn("bearer_token", resumed.body["session"])
        self.assertFalse(resumed.body["session"]["bearer_token_included"])
        error = permission_api_error_response(
            PermissionApiOperation.SESSION_RESUME,
            PermissionControlForbidden("forbidden"),
        )
        self.assertEqual(int(error.status), 403)

    def test_mode_mutation_is_revisioned_safe_and_sealed_is_sticky(self) -> None:
        changed = self.plane.update_mode(
            self.api,
            PermissionMode.DONT_ASK,
            expected_mode_revision=0,
        )
        self.assertTrue(changed.ok)
        self.assertEqual(changed.metadata["mode"]["previous_mode"], "default")
        self.assertEqual(changed.metadata["mode"]["revision"], 1)

        with self.assertRaises(PermissionControlForbidden):
            self.plane.update_mode(self.api, PermissionMode.AUTO)
        sealed = self.plane.update_mode(
            self.api,
            PermissionMode.SEALED,
            expected_mode_revision=1,
        )
        self.assertEqual(sealed.metadata["mode"]["previous_mode"], "dont_ask")
        with self.assertRaises(PermissionControlForbidden):
            self.plane.update_mode(self.api, PermissionMode.DEFAULT)
        self.assertEqual(self.plane.session_mode(self.session_id), "sealed")

    def test_rule_creation_cannot_broaden_beyond_exact_action(self) -> None:
        facade = PermissionApiFacade(self.plane, workspace_root=self.workspace)
        exact_scope = PermissionScope(
            kind=PermissionScopeKind.ACTION,
            session_id=self.session_id,
            task_id=self.task_id,
            run_id=self.run_id,
            workspace_root=str(self.workspace),
            tool_namespace="builtin",
            tool_name="shell",
            argument_digest=self.evaluation().arguments_digest,
            request_fingerprint=self.evaluation().request_fingerprint,
        )
        created = facade.create_rule(
            self.api,
            {
                "effect": "deny",
                "scope": exact_scope.to_dict(),
                "namespace_pattern": "builtin",
                "tool_pattern": "shell",
                "reason": "exact session deny",
            },
        )
        self.assertTrue(created.ok)
        self.assertEqual(created.body["result"]["rule"]["source"], "session")

        broad_scope = PermissionScope(
            kind=PermissionScopeKind.SESSION,
            session_id=self.session_id,
        )
        with self.assertRaises(PermissionControlForbidden):
            facade.create_rule(
                self.api,
                {
                    "effect": "allow",
                    "scope": broad_scope.to_dict(),
                    "tool_pattern": "*",
                },
            )


if __name__ == "__main__":
    unittest.main()
