from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "workspace",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_runtime.provider_control_plane import ProviderControlPlanePortError
from zyra_scheduler.backend_registry import (
    BackendDefinition,
    BackendDispatchError,
    BackendDispatchRuntime,
    BackendFailureKind,
    BackendKind,
    BackendLocation,
    BackendRecoveryIntent,
    BackendRegistry,
    BackendRegistryStore,
    BackendResourceLimits,
    BackendSelectionRequest,
    WorkspacePolicy,
)


class BackendRegistryTests(unittest.TestCase):
    def test_callable_dispatch_persists_opaque_provider_route_reference(self) -> None:
        with self._runtime() as fixture:
            outcome = fixture["runtime"].dispatch_callable(
                fixture["request"],
                lambda envelope: {
                    "backend_id": envelope.backend_id,
                    "provider_route_id": envelope.provider_route_id,
                },
                idempotency_key="dispatch-success",
            )

            self.assertEqual(outcome.value["backend_id"], "primary")
            self.assertEqual(outcome.value["provider_route_id"], "provider_route_immutable")
            self.assertFalse(outcome.backend_changed)
            self.assertEqual(len(outcome.attempts), 1)
            self.assertEqual(outcome.attempts[0].outcome, "succeeded")
            envelope = outcome.final_envelope.to_dict()
            self.assertEqual(envelope["provider_route_id"], "provider_route_immutable")
            serialized = str(envelope).lower()
            self.assertNotIn("api_key", serialized)
            self.assertNotIn("credential_secret", serialized)
            self.assertNotIn("authorization", serialized)
            self.assertNotIn("model_id", serialized)
            self.assertNotIn("provider_id", serialized)

    def test_backend_unavailable_changes_backend_but_not_provider_route(self) -> None:
        with self._runtime() as fixture:
            observed: list[tuple[str, str | None]] = []

            def operation(envelope):
                observed.append((envelope.backend_id, envelope.provider_route_id))
                if envelope.backend_id == "primary":
                    raise BackendDispatchError(
                        BackendFailureKind.BACKEND_UNAVAILABLE,
                        "primary backend is down",
                        retryable=True,
                        recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                        backend_id=envelope.backend_id,
                    )
                return "recovered"

            outcome = fixture["runtime"].dispatch_callable(
                fixture["request"],
                operation,
                idempotency_key="dispatch-failover",
            )

            self.assertEqual(outcome.value, "recovered")
            self.assertEqual(
                observed,
                [
                    ("primary", "provider_route_immutable"),
                    ("fallback", "provider_route_immutable"),
                ],
            )
            self.assertTrue(outcome.backend_changed)
            self.assertEqual(len(outcome.attempts), 2)
            self.assertEqual(outcome.attempts[0].failure_kind, BackendFailureKind.BACKEND_UNAVAILABLE)
            self.assertTrue(
                any(event.event_type == "backend.failover.committed" for event in outcome.events)
            )
            failover_event = next(
                event
                for event in outcome.events
                if event.event_type == "backend.failover.committed"
            )
            self.assertFalse(failover_event.payload["provider_route_changed"])
            self.assertTrue(failover_event.payload["backend_changed"])

    def test_provider_failure_never_rotates_backend(self) -> None:
        with self._runtime() as fixture:
            observed: list[str] = []

            def operation(envelope):
                observed.append(envelope.backend_id)
                raise ProviderControlPlanePortError(
                    "provider_unavailable",
                    "model endpoint is unavailable",
                    detail={"outputObserved": False},
                )

            with self.assertRaises(BackendDispatchError) as raised:
                fixture["runtime"].dispatch_callable(
                    fixture["request"],
                    operation,
                    idempotency_key="provider-failure",
                )

            self.assertEqual(raised.exception.kind, BackendFailureKind.PROVIDER_FAILURE)
            self.assertEqual(
                raised.exception.recovery_intent,
                BackendRecoveryIntent.CHANGE_PROVIDER_ROUTE,
            )
            self.assertEqual(observed, ["primary"])
            events = fixture["store"].events(run_id="run-1", task_id="task-1")
            self.assertTrue(
                any(event.event_type == "provider.dispatch.failover_required" for event in events)
            )
            self.assertFalse(
                any(event.event_type == "backend.failover.committed" for event in events)
            )

    def test_missing_workspace_fails_selection_before_callable(self) -> None:
        with self._runtime(create_workspace=False) as fixture:
            called = False

            def operation(_envelope):
                nonlocal called
                called = True
                return None

            with self.assertRaises(BackendDispatchError) as raised:
                fixture["runtime"].dispatch_callable(
                    fixture["request"],
                    operation,
                    idempotency_key="missing-workspace",
                )

            self.assertEqual(raised.exception.kind, BackendFailureKind.BACKEND_UNAVAILABLE)
            self.assertFalse(called)

    def test_partial_output_timeout_is_reconcile_only(self) -> None:
        with self._runtime(turn_timeout_seconds=0.05) as fixture:
            def operation(_envelope):
                import time

                time.sleep(0.1)
                return "side effect already completed"

            with self.assertRaises(BackendDispatchError) as raised:
                fixture["runtime"].dispatch_callable(
                    fixture["request"],
                    operation,
                    idempotency_key="late-timeout",
                    interruptible=False,
                )

            self.assertEqual(raised.exception.kind, BackendFailureKind.TURN_TIMEOUT)
            self.assertTrue(raised.exception.output_observed)
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(raised.exception.recovery_intent, BackendRecoveryIntent.RECONCILE)

    def _runtime(
        self,
        *,
        create_workspace: bool = True,
        turn_timeout_seconds: float = 2.0,
    ):
        return _RuntimeFixture(
            create_workspace=create_workspace,
            turn_timeout_seconds=turn_timeout_seconds,
        )


class _RuntimeFixture:
    def __init__(self, *, create_workspace: bool, turn_timeout_seconds: float) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.artifacts = root / "artifacts"
        if create_workspace:
            self.workspace.mkdir()
        self.artifacts.mkdir()
        self.store = BackendRegistryStore(root / "backend.sqlite3")
        self.registry = BackendRegistry(
            self.store,
            lease_seconds=10.0,
            attempt_limit=3,
            failure_threshold=2,
        )
        for backend_id, priority in (("primary", 100), ("fallback", 10)):
            self.registry.register(
                BackendDefinition(
                    backend_id=backend_id,
                    display_name=backend_id,
                    kind=BackendKind.LOCAL_PROCESS,
                    location=BackendLocation.LOCAL,
                    runtime_worker="CodeWorkerRuntime",
                    capabilities=("code-change",),
                    workspace_policy=WorkspacePolicy(require_existing=True),
                    limits=BackendResourceLimits(
                        maximum_concurrency=1,
                        turn_timeout_seconds=turn_timeout_seconds,
                    ),
                    priority=priority,
                )
            )
        self.runtime = BackendDispatchRuntime(self.registry)
        self.request = BackendSelectionRequest(
            run_id="run-1",
            task_id="task-1",
            node_id="node-1",
            runtime_worker="CodeWorkerRuntime",
            preferred_backend_id="primary",
            required_capabilities=("code-change",),
            allowed_locations=(BackendLocation.LOCAL,),
            excluded_backend_ids=(),
            workspace_root=str(self.workspace),
            artifact_root=str(self.artifacts),
            provider_route_id="provider_route_immutable",
            provider_route_checksum="route-checksum-immutable",
            provider_catalog_revision=7,
            provider_credential_version=3,
            provider_credential_fingerprint="credential-fingerprint-immutable",
            provider_transport_id="transport-openai-sse",
            m0_execution_ref="worker_request:request-1",
            turn_id="turn-1",
        )

    def __enter__(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "request": self.request,
            "store": self.store,
            "registry": self.registry,
        }

    def __exit__(self, *_: object) -> None:
        self.store.close()
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
