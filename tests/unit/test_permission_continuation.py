from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_runtime.permission.canonical import (  # noqa: E402
    arguments_digest,
    build_request_fingerprint,
)
from zyra_runtime.permission.continuation import (  # noqa: E402
    PermissionContinuationAlreadyClaimed,
    PermissionContinuationConflict,
    PermissionContinuationDisabledError,
    PermissionContinuationIdentityError,
    PermissionContinuationPayloadMissing,
    PermissionContinuationPhase,
    PermissionContinuationRuntime,
    PermissionContinuationStateError,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionResolutionResponse,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from zyra_runtime.permission.store import (  # noqa: E402
    PermissionStateDisabled,
    PermissionStateStore,
)


class PermissionContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2032, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def _store(self, path: Path, *, disabled: bool = False) -> PermissionStateStore:
        return PermissionStateStore(path, clock=lambda: self.now, disabled=disabled)

    def test_permission_store_reclaims_a_fresh_lock_from_a_dead_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "permission.json"
            lock = path.with_name(f".{path.name}.lock")
            exited = subprocess.Popen([sys.executable, "-c", "pass"])
            exited.wait(timeout=10)
            lock.write_text(f"{exited.pid}:1:0", encoding="ascii")
            store = PermissionStateStore(
                path,
                clock=lambda: self.now,
                lock_timeout=0.1,
                stale_lock_seconds=30,
            )

            state = store.read_state()

            self.assertEqual(state["revision"], 0)
            self.assertFalse(lock.exists())

    def _pending_request(
        self,
        store: PermissionStateStore,
        *,
        session_id: str = "session-a",
        request_id: str = "request-a",
        tool_use_id: str = "tool-use-a",
        command: str = "write-secret-marker --not-persisted",
        server_id: str = "server-a",
        version: str = "1.0.0",
        schema_digest: str = "sha256:schema-a",
        ttl_seconds: int = 300,
    ) -> tuple[PermissionRequestRecord, dict[str, object]]:
        arguments: dict[str, object] = {"command": command, "timeout_seconds": 7}
        identity = ToolIdentity(
            namespace="mcp",
            name="shell_remote",
            server_id=server_id,
            version=version,
            schema_digest=schema_digest,
        )
        digest = arguments_digest(arguments)
        fingerprint = build_request_fingerprint(
            identity,
            digest,
            session_id=session_id,
            tool_use_id=tool_use_id,
            run_id="run-a",
            task_id="task-a",
        )
        scope = PermissionScope(
            PermissionScopeKind.ACTION,
            session_id=session_id,
            task_id="task-a",
            run_id="run-a",
            workspace_root=".",
            tool_namespace=identity.namespace,
            tool_name=identity.name,
            server_id=identity.server_id,
            argument_digest=digest,
            request_fingerprint=fingerprint,
            metadata={"exact_identity": True},
        )
        request = PermissionRequestRecord(
            request_id=request_id,
            session_id=session_id,
            task_id="task-a",
            run_id="run-a",
            worker_request_id="worker-request-origin",
            tool_use_id=tool_use_id,
            tool_identity=identity,
            arguments_digest=digest,
            request_fingerprint=fingerprint,
            scope=scope,
            expires_at=(self.now + timedelta(seconds=ttl_seconds)).isoformat(),
            reason_code="risk.shell_review",
            reason="remote shell requires an exact approval",
        )
        store.create_request(request)
        return request, arguments

    def _deliver_and_resolve(
        self,
        store: PermissionStateStore,
        runtime: PermissionContinuationRuntime,
        request: PermissionRequestRecord,
        *,
        effect: PermissionEffect = PermissionEffect.ALLOW,
    ):
        delivered = store.mark_delivered(
            request.request_id,
            expected_request_revision=request.revision,
            channel="api",
        )
        continuation = runtime.deliver(
            delivered,
            expected_record_revision=0,
        )
        response = PermissionResolutionResponse(
            request_id=delivered.request_id,
            session_id=delivered.session_id,
            tool_use_id=delivered.tool_use_id,
            tool_identity=delivered.tool_identity,
            arguments_digest=delivered.arguments_digest,
            request_fingerprint=delivered.request_fingerprint,
            scope=delivered.scope,
            effect=effect,
            actor_id="approver-a",
            expected_revision=delivered.revision,
            channel="api",
            idempotency_key=f"resolution-{delivered.request_id}",
        )
        resolved = store.resolve_request(response)
        ready = runtime.resolution_ready(
            resolved,
            expected_record_revision=continuation.revision,
        )
        return delivered, resolved, ready

    def _replay(
        self,
        request: PermissionRequestRecord,
        arguments: dict[str, object],
        *,
        locator: str = "session-record:session-a:41",
        sequence: int = 41,
        include_raw_arguments: bool = False,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "session_id": request.session_id,
            "task_id": request.task_id,
            "run_id": request.run_id,
            "tool_use_id": request.tool_use_id,
            "tool_identity": request.tool_identity.to_dict(),
            "arguments_digest": request.arguments_digest,
            "request_fingerprint": request.request_fingerprint,
            "scope": request.scope.to_dict(),
            "payload_locator": locator,
            "session_sequence": sequence,
        }
        if include_raw_arguments:
            value.pop("arguments_digest")
            value["arguments"] = copy.deepcopy(arguments)
        return value

    def test_park_is_durable_immutable_and_never_persists_raw_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "permission-state.json"
            store = self._store(state_path)
            request, _ = self._pending_request(store)
            runtime = PermissionContinuationRuntime(store, session_id=request.session_id)

            parked = runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
                metadata={"turn_id": "turn-a", "batch_index": 2},
            )
            reopened = PermissionContinuationRuntime(
                self._store(state_path),
                session_id=request.session_id,
            ).get(request.request_id)

            self.assertEqual(parked, reopened)
            self.assertEqual(reopened.phase, PermissionContinuationPhase.PARKED)
            self.assertFalse(reopened.authorizes_execution)
            serialized = state_path.read_text(encoding="utf-8")
            self.assertNotIn("write-secret-marker", serialized)
            self.assertNotIn('"arguments":', serialized)
            self.assertIn('"permission_continuations"', serialized)
            self.assertIn(request.arguments_digest, serialized)

            # request_id is the idempotency key; changing immutable locator or
            # sequence cannot replace the parked record.
            with self.assertRaises(PermissionContinuationIdentityError):
                runtime.park(
                    request,
                    payload_locator="session-record:session-a:42",
                    session_sequence=42,
                )

    def test_resume_validates_each_exact_identity_field_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "state.json")
            request, arguments = self._pending_request(store)
            runtime = PermissionContinuationRuntime(store, session_id=request.session_id)
            runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, runtime, request)
            valid = self._replay(request, arguments)
            authoritative = self._replay(
                request,
                arguments,
                include_raw_arguments=True,
            )

            forged_values: list[dict[str, object]] = []
            for mutator in (
                lambda item: item.update(session_id="session-forged"),
                lambda item: item.update(task_id="task-forged"),
                lambda item: item.update(run_id="run-forged"),
                lambda item: item.update(tool_use_id="tool-use-forged"),
                lambda item: item["tool_identity"].update(namespace="builtin"),
                lambda item: item["tool_identity"].update(name="other_tool"),
                lambda item: item["tool_identity"].update(server_id="server-forged"),
                lambda item: item["tool_identity"].update(version="9.9.9"),
                lambda item: item["tool_identity"].update(schema_digest="sha256:forged"),
                lambda item: item.update(arguments_digest="sha256:forged"),
                lambda item: item.update(request_fingerprint="sha256:forged"),
                lambda item: item["scope"].update(server_id="server-forged"),
                lambda item: item.update(payload_locator="session-record:session-a:99"),
                lambda item: item.update(session_sequence=99),
            ):
                item = copy.deepcopy(valid)
                mutator(item)
                forged_values.append(item)

            for index, forged in enumerate(forged_values):
                with self.subTest(forgery=index), self.assertRaises(
                    PermissionContinuationIdentityError
                ):
                    runtime.prepare_resume(
                        request.request_id,
                        forged,
                        claimant="query-engine-a",
                        idempotency_key=f"forged-claim-{index}",
                        expected_record_revision=ready.revision,
                        payload_resolver=lambda _: authoritative,
                    )
                self.assertEqual(runtime.get(request.request_id).phase, ready.phase)
                self.assertEqual(runtime.get(request.request_id).revision, ready.revision)

            claim = runtime.prepare_resume(
                request.request_id,
                valid,
                claimant="query-engine-a",
                idempotency_key="valid-claim",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: authoritative,
            )
            self.assertEqual(claim.record.phase, PermissionContinuationPhase.CLAIMED)
            self.assertFalse(claim.authorizes_execution)
            self.assertFalse(claim.record.authorizes_execution)
            self.assertNotIn('"arguments":', json.dumps(claim.to_dict(), sort_keys=True))

    def test_concurrent_process_style_claim_has_one_winner_and_replay_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            store = self._store(state_path)
            request, arguments = self._pending_request(store)
            owner = PermissionContinuationRuntime(store, session_id=request.session_id)
            owner.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, owner, request)
            replay = self._replay(request, arguments)
            authoritative = self._replay(request, arguments, include_raw_arguments=True)
            barrier = threading.Barrier(2)

            def compete(index: int):
                runtime = PermissionContinuationRuntime(
                    self._store(state_path),
                    session_id=request.session_id,
                )

                def resolve(_):
                    barrier.wait(timeout=5)
                    return copy.deepcopy(authoritative)

                try:
                    return runtime.prepare_resume(
                        request.request_id,
                        copy.deepcopy(replay),
                        claimant=f"query-engine-{index}",
                        idempotency_key=f"claim-{index}",
                        expected_record_revision=ready.revision,
                        payload_resolver=resolve,
                    )
                except Exception as error:  # noqa: BLE001 - inspect the single loser.
                    return error

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(compete, range(2)))

            winners = [item for item in outcomes if not isinstance(item, Exception)]
            losers = [item for item in outcomes if isinstance(item, Exception)]
            self.assertEqual(len(winners), 1, outcomes)
            self.assertEqual(len(losers), 1, outcomes)
            self.assertIsInstance(
                losers[0],
                (PermissionContinuationConflict, PermissionContinuationAlreadyClaimed),
            )
            claimed = owner.get(request.request_id)
            self.assertEqual(claimed.phase, PermissionContinuationPhase.CLAIMED)

            with self.assertRaises(PermissionContinuationAlreadyClaimed):
                owner.prepare_resume(
                    request.request_id,
                    replay,
                    claimant="replay",
                    idempotency_key="replay-claim",
                    payload_resolver=lambda _: authoritative,
                )

            completed = owner.complete(
                request.request_id,
                claim_id=claimed.claim_id,
                expected_record_revision=claimed.revision,
                metadata={"tool_result_id": "result-a"},
            )
            self.assertEqual(completed.phase, PermissionContinuationPhase.COMPLETED)
            with self.assertRaises(PermissionContinuationStateError):
                owner.prepare_resume(
                    request.request_id,
                    replay,
                    claimant="terminal-replay",
                    idempotency_key="terminal-replay",
                    payload_resolver=lambda _: authoritative,
                )

    def test_expired_claim_lease_is_reclaimed_without_replaying_old_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            store = self._store(state_path)
            request, arguments = self._pending_request(store, ttl_seconds=120)
            owner = PermissionContinuationRuntime(
                store,
                session_id=request.session_id,
                claim_lease_seconds=1,
            )
            owner.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, owner, request)
            replay = self._replay(request, arguments)
            authoritative = self._replay(
                request,
                arguments,
                include_raw_arguments=True,
            )

            first = owner.prepare_resume(
                request.request_id,
                replay,
                claimant="query-engine-crashed",
                idempotency_key="claim-before-crash",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: authoritative,
            )
            self.assertEqual(first.record.claim_attempt, 1)
            self.assertFalse(first.record.metadata["claim_recovered_after_lease"])

            self.now += timedelta(seconds=2)
            with self.assertRaises(PermissionContinuationStateError):
                owner.complete(
                    request.request_id,
                    claim_id=first.record.claim_id,
                    expected_record_revision=first.record.revision,
                )
            recovery = PermissionContinuationRuntime(
                self._store(state_path),
                session_id=request.session_id,
                claim_lease_seconds=1,
            )
            second = recovery.prepare_resume(
                request.request_id,
                replay,
                claimant="query-engine-recovery",
                idempotency_key="claim-after-lease",
                expected_record_revision=first.record.revision,
                payload_resolver=lambda _: authoritative,
            )

            self.assertNotEqual(second.record.claim_id, first.record.claim_id)
            self.assertEqual(second.record.claim_attempt, 2)
            self.assertTrue(second.record.metadata["claim_recovered_after_lease"])
            with self.assertRaises((PermissionContinuationConflict, PermissionContinuationIdentityError)):
                recovery.complete(
                    request.request_id,
                    claim_id=first.record.claim_id,
                    expected_record_revision=first.record.revision,
                )
            completed = recovery.complete(
                request.request_id,
                claim_id=second.record.claim_id,
                expected_record_revision=second.record.revision,
            )
            self.assertEqual(completed.phase, PermissionContinuationPhase.COMPLETED)

    def test_started_claim_lease_outlives_request_approval_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "state.json")
            request, arguments = self._pending_request(store, ttl_seconds=1)
            runtime = PermissionContinuationRuntime(
                store,
                session_id=request.session_id,
                claim_lease_seconds=10,
            )
            runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, runtime, request)
            replay = self._replay(request, arguments)
            claim = runtime.prepare_resume(
                request.request_id,
                replay,
                claimant="query-engine-long-tool",
                idempotency_key="claim-before-request-expiry",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: self._replay(
                    request,
                    arguments,
                    include_raw_arguments=True,
                ),
            )

            self.now += timedelta(seconds=2)
            self.assertLess(
                datetime.fromisoformat(request.expires_at),
                self.now,
            )
            self.assertGreater(
                datetime.fromisoformat(str(claim.record.claim_expires_at)),
                self.now,
            )
            completed = runtime.complete(
                request.request_id,
                claim_id=claim.record.claim_id,
                expected_record_revision=claim.record.revision,
            )
            self.assertEqual(completed.phase, PermissionContinuationPhase.COMPLETED)

    def test_expired_claim_with_consumed_approval_is_terminal_outcome_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "state.json")
            request, arguments = self._pending_request(store, ttl_seconds=120)
            runtime = PermissionContinuationRuntime(
                store,
                session_id=request.session_id,
                claim_lease_seconds=1,
            )
            runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, resolved, ready = self._deliver_and_resolve(store, runtime, request)
            claim = runtime.prepare_resume(
                request.request_id,
                self._replay(request, arguments),
                claimant="query-engine-before-crash",
                idempotency_key="claim-before-guard-crash",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: self._replay(
                    request,
                    arguments,
                    include_raw_arguments=True,
                ),
            )
            store.commit_decision(
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
                    worker_request_id="query-engine-before-crash",
                    reason_code="approval.restored",
                    reason="consume the exact approval before an injected crash",
                    scope=resolved.scope,
                    request_id=resolved.request_id,
                ),
                consume_approval_request_id=resolved.request_id,
            )

            self.now += timedelta(seconds=2)
            reconciled = runtime.reconcile_expired_claim(
                request.request_id,
                expected_record_revision=claim.record.revision,
            )

            self.assertEqual(reconciled.phase, PermissionContinuationPhase.FAILED)
            self.assertEqual(
                reconciled.failure_code,
                "approval_consumed_outcome_unknown",
            )
            self.assertTrue(reconciled.metadata["execution_outcome_unknown"])
            self.assertTrue(
                reconciled.metadata["recovery_requires_different_action"]
            )
            self.assertEqual(runtime.active(), ())

    def test_released_unexecuted_claim_invalidates_old_owner_and_can_be_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(Path(tmpdir) / "state.json")
            request, arguments = self._pending_request(store)
            runtime = PermissionContinuationRuntime(store, session_id=request.session_id)
            runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, runtime, request)
            replay = self._replay(request, arguments)
            resolver = lambda _: self._replay(  # noqa: E731 - compact deterministic resolver.
                request,
                arguments,
                include_raw_arguments=True,
            )
            first = runtime.prepare_resume(
                request.request_id,
                replay,
                claimant="batch-claim-owner",
                idempotency_key="batch-claim-first",
                expected_record_revision=ready.revision,
                payload_resolver=resolver,
            )
            released = runtime.release_claim(
                request.request_id,
                claim_id=first.record.claim_id,
                expected_record_revision=first.record.revision,
                reason="later batch member failed identity validation",
            )
            self.assertEqual(released.phase, PermissionContinuationPhase.RESOLUTION_READY)
            self.assertEqual(released.claim_id, "")
            with self.assertRaises(
                (PermissionContinuationConflict, PermissionContinuationStateError)
            ):
                runtime.complete(
                    request.request_id,
                    claim_id=first.record.claim_id,
                    expected_record_revision=first.record.revision,
                )

            second = runtime.prepare_resume(
                request.request_id,
                replay,
                claimant="batch-claim-retry",
                idempotency_key="batch-claim-second",
                expected_record_revision=released.revision,
                payload_resolver=resolver,
            )
            self.assertEqual(second.record.claim_attempt, 2)
            self.assertNotEqual(second.record.claim_id, first.record.claim_id)

    def test_snapshot_restore_preserves_ready_identity_without_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / "source.json"
            source_store = self._store(source_path)
            request, arguments = self._pending_request(source_store)
            source = PermissionContinuationRuntime(source_store, session_id=request.session_id)
            source.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            self._deliver_and_resolve(source_store, source, request)
            permission_snapshot = source_store.snapshot(request.session_id)
            continuation_snapshot = source.snapshot()

            serialized = json.dumps(continuation_snapshot, sort_keys=True)
            self.assertNotIn("write-secret-marker", serialized)
            self.assertNotIn('"arguments":', serialized)

            target_path = Path(tmpdir) / "target.json"
            target_store = self._store(target_path)
            target_store.restore_snapshot(permission_snapshot, request.session_id)
            target = PermissionContinuationRuntime(target_store, session_id=request.session_id)
            restored = target.restore(continuation_snapshot)
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].phase, PermissionContinuationPhase.RESOLUTION_READY)

            replay = self._replay(request, arguments)
            claim = target.prepare_resume(
                request.request_id,
                replay,
                claimant="restored-query-engine",
                idempotency_key="restored-claim",
                payload_resolver=lambda _: self._replay(
                    request,
                    arguments,
                    include_raw_arguments=True,
                ),
            )
            self.assertEqual(claim.record.phase, PermissionContinuationPhase.CLAIMED)

    def test_missing_payload_resolver_and_disabled_dependencies_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            store = self._store(state_path)
            request, arguments = self._pending_request(store)
            runtime = PermissionContinuationRuntime(store, session_id=request.session_id)
            runtime.park(
                request,
                payload_locator="session-record:session-a:41",
                session_sequence=41,
            )
            _, _, ready = self._deliver_and_resolve(store, runtime, request)
            replay = self._replay(request, arguments)

            with self.assertRaises(PermissionContinuationPayloadMissing):
                runtime.prepare_resume(
                    request.request_id,
                    replay,
                    claimant="query-engine",
                    idempotency_key="missing-resolver",
                )
            with self.assertRaises(PermissionContinuationPayloadMissing):
                runtime.prepare_resume(
                    request.request_id,
                    replay,
                    claimant="query-engine",
                    idempotency_key="missing-payload",
                    payload_resolver=lambda _: None,
                )
            self.assertEqual(runtime.get(request.request_id).phase, ready.phase)
            self.assertEqual(runtime.get(request.request_id).revision, ready.revision)

            disabled = PermissionContinuationRuntime(
                store,
                session_id=request.session_id,
                disabled=True,
            )
            with self.assertRaises(PermissionContinuationDisabledError):
                disabled.get(request.request_id)

            disabled_store_runtime = PermissionContinuationRuntime(
                self._store(state_path, disabled=True),
                session_id=request.session_id,
            )
            with self.assertRaises(PermissionStateDisabled):
                disabled_store_runtime.get(request.request_id)

    def test_cancel_expire_and_failed_claim_are_durable_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            store = self._store(state_path)
            cancel_request, _ = self._pending_request(
                store,
                request_id="request-cancel",
                tool_use_id="tool-cancel",
            )
            runtime = PermissionContinuationRuntime(store, session_id=cancel_request.session_id)
            cancelled_park = runtime.park(
                cancel_request,
                payload_locator="session-record:session-a:1",
                session_sequence=1,
            )
            cancelled = runtime.cancel(
                cancel_request.request_id,
                reason="user cancelled",
                expected_record_revision=cancelled_park.revision,
            )
            self.assertEqual(cancelled.phase, PermissionContinuationPhase.CANCELLED)

            expire_request, _ = self._pending_request(
                store,
                request_id="request-expire",
                tool_use_id="tool-expire",
                ttl_seconds=1,
            )
            expiring = runtime.park(
                expire_request,
                payload_locator="session-record:session-a:2",
                session_sequence=2,
            )
            self.now += timedelta(seconds=2)
            expired = runtime.expire(
                expire_request.request_id,
                expected_record_revision=expiring.revision,
            )
            self.assertEqual(expired.phase, PermissionContinuationPhase.EXPIRED)

            fail_request, fail_arguments = self._pending_request(
                store,
                request_id="request-fail",
                tool_use_id="tool-fail",
            )
            failed_park = runtime.park(
                fail_request,
                payload_locator="session-record:session-a:3",
                session_sequence=3,
            )
            delivered = store.mark_delivered(
                fail_request.request_id,
                expected_request_revision=fail_request.revision,
                channel="api",
            )
            delivered_cont = runtime.deliver(
                delivered,
                expected_record_revision=failed_park.revision,
            )
            response = PermissionResolutionResponse(
                request_id=delivered.request_id,
                session_id=delivered.session_id,
                tool_use_id=delivered.tool_use_id,
                tool_identity=delivered.tool_identity,
                arguments_digest=delivered.arguments_digest,
                request_fingerprint=delivered.request_fingerprint,
                scope=delivered.scope,
                effect=PermissionEffect.DENY,
                actor_id="approver-a",
                expected_revision=delivered.revision,
                channel="api",
                idempotency_key="resolution-fail",
            )
            resolved = store.resolve_request(response)
            ready = runtime.resolution_ready(
                resolved,
                expected_record_revision=delivered_cont.revision,
            )
            replay = self._replay(
                fail_request,
                fail_arguments,
                locator="session-record:session-a:3",
                sequence=3,
            )
            claim = runtime.prepare_resume(
                fail_request.request_id,
                replay,
                claimant="query-engine",
                idempotency_key="deny-claim",
                expected_record_revision=ready.revision,
                payload_resolver=lambda _: copy.deepcopy(replay),
            )
            self.assertEqual(claim.resolution_effect, PermissionEffect.DENY)
            self.assertFalse(claim.authorizes_execution)
            failed = runtime.fail(
                fail_request.request_id,
                claim_id=claim.claim_id,
                failure_code="permission_denied_projected",
                expected_record_revision=claim.record.revision,
            )
            self.assertEqual(failed.phase, PermissionContinuationPhase.FAILED)
            self.assertEqual(failed.failure_code, "permission_denied_projected")


if __name__ == "__main__":
    unittest.main()
