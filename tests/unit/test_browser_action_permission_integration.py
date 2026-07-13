from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace

from zyra_runtime import WorkerRequest
from zyra_workers.browser_action import (
    ActionIdentity,
    ActionRequest,
    BrowserActionDeadlineRuntime,
    BrowserActionIntegrationError,
    BrowserActionPlanAdapter,
    BrowserNetworkPolicy,
    NetworkPolicyConfig,
    PlanPhase,
    SessionBoundCdpTransport,
    SessionTransportConfig,
    StaticHostResolver,
    default_browser_action_registry,
)


def _action_request(*, deadline_at: str = "", action_id: str = "") -> ActionRequest:
    identity = ActionIdentity(
        run_id="run-integration-unit",
        task_id="task-integration-unit",
        worker_request_id="request-integration-unit",
        session_id="permission-integration-unit",
        browser_session_id="browser-integration-unit",
        step_index=1,
        action_id=action_id,
    )
    return ActionRequest(
        identity=identity,
        action="list_targets",
        arguments={},
        backend="zyra-browser-productized",
        deadline_at=deadline_at,
    )


class _SelectorStore:
    disabled = False


class _TargetRuntime:
    def ensure_valid_focus(self):
        return SimpleNamespace(target_id="target-1", generation=3, url="https://example.test/current")

    def snapshot(self):
        return SimpleNamespace(cdp_sessions=())

    def active_cdp_session(self, *, timeout=0.0):
        return SimpleNamespace(cdp_session_id="active-session")


class _CdpRuntime:
    def __init__(self, response: object | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], str, float]] = []
        self.response = {"ok": True} if response is None else response

    def send(self, method, params, *, cdp_session_id="", timeout_seconds=0.0):
        self.calls.append((method, dict(params), cdp_session_id, timeout_seconds))
        return self.response


class BrowserActionPlanAdapterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = BrowserActionPlanAdapter(default_browser_action_registry(), _SelectorStore())
        self.session = SimpleNamespace(
            session_id="browser-integration-unit",
            canonical_session_id="canonical-integration-unit",
            run_id="run-integration-unit",
            task_id="task-integration-unit",
            status="running",
        )
        self.session_start = SimpleNamespace(ok=True, session=self.session)

    def test_all_steps_are_admitted_and_tool_use_ids_are_stable_action_ids(self) -> None:
        request = WorkerRequest(
            run_id="run-integration-unit",
            task_id="task-integration-unit",
            request_id="request-integration-unit",
            worker_name="BrowserWorker",
        )
        admission = self.adapter.admit(
            request,
            self.session_start,
            [
                {"action": "list_targets", "arguments": {}},
                {"action": "wait", "arguments": {"seconds": 0.05}},
            ],
            permission_session_id="permission-integration-unit",
            target_runtime=_TargetRuntime(),
        )
        self.assertTrue(admission.ok, [item.public_dict() for item in admission.issues])
        plan = admission.require()
        self.assertEqual(len(plan.steps), 2)
        for step in plan.steps:
            self.assertEqual(step.request.metadata["permission_tool_use_id"], step.action_id)

    def test_any_unknown_action_or_bypass_marker_rejects_the_entire_plan(self) -> None:
        request = WorkerRequest(
            run_id="run-integration-unit",
            task_id="task-integration-unit",
            request_id="request-integration-unit",
            worker_name="BrowserWorker",
            constraints={"nested": {"skip_permission": True}},
        )
        admission = self.adapter.admit(
            request,
            self.session_start,
            [
                {"action": "list_targets", "arguments": {}},
                {"action": "raw_cdp", "arguments": {"method": "Browser.close"}},
            ],
            permission_session_id="permission-integration-unit",
            target_runtime=_TargetRuntime(),
        )
        self.assertFalse(admission.ok)
        self.assertIsNone(admission.plan)
        codes = {item.code for item in admission.issues}
        self.assertIn("unknown_browser_action", codes)
        self.assertTrue(any("bypass" in code or "permission" in code for code in codes), codes)

    def test_top_level_windows_sandbox_authority_is_not_a_permission_bypass(self) -> None:
        request = WorkerRequest(
            run_id="run-integration-unit",
            task_id="task-integration-unit",
            request_id="request-windows-sandbox-unit",
            worker_name="BrowserWorker",
            constraints={
                "browser_allow_unsafe_sandbox_bypass": True,
                "browser_plan": [{"action": "list_targets", "arguments": {}}],
            },
        )
        admission = self.adapter.admit(
            request,
            self.session_start,
            request.constraints["browser_plan"],
            permission_session_id="permission-integration-unit",
            target_runtime=_TargetRuntime(),
        )
        self.assertIsNotNone(admission.plan, admission.issues)
        self.assertFalse(admission.issues)

        nested = WorkerRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            request_id="request-nested-sandbox-unit",
            worker_name=request.worker_name,
            constraints={
                "runtime": {"browser_allow_unsafe_sandbox_bypass": True},
                "browser_plan": [{"action": "list_targets", "arguments": {}}],
            },
        )
        nested_admission = self.adapter.admit(
            nested,
            self.session_start,
            nested.constraints["browser_plan"],
            permission_session_id="permission-integration-unit",
            target_runtime=_TargetRuntime(),
        )
        self.assertIsNone(nested_admission.plan)
        self.assertTrue(any(issue.kind.value == "bypass" for issue in nested_admission.issues))


class BrowserActionDeadlineAndTransportTests(unittest.TestCase):
    def test_cancellation_and_elapsed_deadline_fail_before_dispatch(self) -> None:
        runtime = BrowserActionDeadlineRuntime()
        request = _action_request(action_id="cancelled-action")
        deadline = runtime.begin(request)
        runtime.cancel(request.identity.action_id, reason="task cancelled", actor_id="test")
        with self.assertRaises(BrowserActionIntegrationError) as cancelled:
            deadline.checkpoint(PlanPhase.DISPATCH)
        self.assertEqual(cancelled.exception.code, "browser_action_cancelled")
        runtime.finish(request.identity.action_id, failed=True, cancelled=True)

        expired_at = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)).isoformat()
        expired = _action_request(deadline_at=expired_at, action_id="expired-action")
        elapsed = runtime.begin(expired)
        with self.assertRaises(BrowserActionIntegrationError) as timeout:
            elapsed.checkpoint(PlanPhase.SECURITY_PREFLIGHT)
        self.assertEqual(timeout.exception.code, "browser_action_deadline_elapsed")
        runtime.finish(expired.identity.action_id, failed=True)

    def test_session_transport_is_unusable_without_grant_binding_and_caps_payloads(self) -> None:
        cdp = _CdpRuntime()
        transport = SessionBoundCdpTransport(
            cdp,
            _TargetRuntime(),
            browser_session_id="browser-integration-unit",
            config=SessionTransportConfig(maximum_request_bytes=1024),
        )
        with self.assertRaises(BrowserActionIntegrationError) as unbound:
            transport.send("Runtime.evaluate", {"expression": "1+1"})
        self.assertEqual(unbound.exception.code, "browser_transport_unbound")

        deadlines = BrowserActionDeadlineRuntime()
        request = _action_request(action_id="transport-action")
        deadline = deadlines.begin(request)
        transport.bind_action(request.identity.action_id, deadline)
        with self.assertRaises(BrowserActionIntegrationError) as forbidden:
            transport.send("Browser.close", {})
        self.assertEqual(forbidden.exception.code, "browser_cdp_method_denied")
        with self.assertRaises(BrowserActionIntegrationError) as oversized:
            transport.send("Runtime.evaluate", {"expression": "x" * 2048})
        self.assertEqual(oversized.exception.code, "browser_cdp_request_too_large")
        self.assertEqual(cdp.calls, [])
        transport.release_action(request.identity.action_id)
        deadlines.finish(request.identity.action_id, failed=True)

    def test_oversize_cdp_result_is_not_projected_past_the_transport_boundary(self) -> None:
        cdp = _CdpRuntime({"result": "x" * 2048})
        transport = SessionBoundCdpTransport(
            cdp,
            _TargetRuntime(),
            browser_session_id="browser-integration-unit",
            config=SessionTransportConfig(maximum_response_bytes=1024),
        )
        deadlines = BrowserActionDeadlineRuntime()
        request = _action_request(action_id="oversize-result-action")
        deadline = deadlines.begin(request)
        transport.bind_action(request.identity.action_id, deadline)
        with self.assertRaises(BrowserActionIntegrationError) as oversized:
            transport.send("Target.getTargets", {})
        self.assertEqual(oversized.exception.code, "browser_cdp_response_too_large")
        self.assertEqual(len(cdp.calls), 1)
        self.assertEqual(len(transport.outcomes), 1)
        self.assertFalse(transport.outcomes[0].ok)
        self.assertEqual(transport.outcomes[0].error_code, "browser_cdp_response_too_large")
        transport.release_action(request.identity.action_id)
        deadlines.finish(request.identity.action_id, failed=True)

    def test_dns_observation_epoch_is_not_an_approval_identity(self) -> None:
        resolver = StaticHostResolver({"example.test": ["8.8.8.8"]})
        policy = BrowserNetworkPolicy(NetworkPolicyConfig(), resolver)
        first = policy.preflight(action_id="navigate-action", raw_url="https://example.test/a")
        second = policy.preflight(action_id="navigate-action", raw_url="https://example.test/a")
        self.assertNotEqual(first.resolution.epoch, second.resolution.epoch)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(first.binding_digest, second.binding_digest)

        changed = BrowserNetworkPolicy(
            NetworkPolicyConfig(),
            StaticHostResolver({"example.test": ["8.8.4.4"]}),
        ).preflight(action_id="navigate-action", raw_url="https://example.test/a")
        self.assertNotEqual(first.receipt_id, changed.receipt_id)
        self.assertNotEqual(first.binding_digest, changed.binding_digest)


if __name__ == "__main__":
    unittest.main()
