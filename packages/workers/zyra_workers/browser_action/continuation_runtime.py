from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zyra_core import now_iso, to_jsonable
from zyra_runtime.permission.continuation import (
    PermissionContinuationAlreadyClaimed,
    PermissionContinuationClaim,
    PermissionContinuationError,
    PermissionContinuationIdentityError,
    PermissionContinuationPayloadMissing,
    PermissionContinuationPhase,
    PermissionContinuationRecord,
    PermissionContinuationReplay,
    PermissionContinuationRuntime,
    PermissionContinuationStateError,
)
from zyra_runtime.permission.models import (
    PermissionEffect,
    PermissionRequestPhase,
    PermissionRequestRecord,
)
from zyra_runtime.permission.canonical import arguments_digest, canonical_arguments_json

from .gateway import AuthorizedBrowserAction, PreparedBrowserAction
from .integration_models import (
    BrowserActionIntegrationError,
    BrowserActionPlan,
    PendingActionCheckpoint,
    PlanPhase,
    create_checkpoint_id,
)
from .models import digest_value, stable_id


@dataclass(frozen=True, slots=True)
class BrowserContinuationPayload:
    locator: str
    request_id: str
    replay: Mapping[str, Any]
    application: Mapping[str, Any]
    payload_digest: str
    created_at: str

    def __post_init__(self) -> None:
        if not self.locator or not self.request_id or not self.payload_digest:
            raise ValueError("browser continuation payload identity is incomplete")
        object.__setattr__(self, "replay", copy.deepcopy(dict(self.replay)))
        object.__setattr__(self, "application", copy.deepcopy(dict(self.application)))
        expected = digest_value(
            {
                "locator": self.locator,
                "request_id": self.request_id,
                "replay": self.replay,
                "application": self.application,
                "created_at": self.created_at,
            }
        )
        if self.payload_digest != expected:
            raise PermissionContinuationIdentityError("browser continuation payload digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        locator: str,
        request_id: str,
        replay: Mapping[str, Any],
        application: Mapping[str, Any],
    ) -> "BrowserContinuationPayload":
        created_at = now_iso()
        value = {
            "locator": locator,
            "request_id": request_id,
            "replay": copy.deepcopy(dict(replay)),
            "application": copy.deepcopy(dict(application)),
            "created_at": created_at,
        }
        return cls(**value, payload_digest=digest_value(value))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserContinuationPayload":
        return cls(
            locator=str(value.get("locator") or ""),
            request_id=str(value.get("request_id") or ""),
            replay=dict(value.get("replay") or {}),
            application=dict(value.get("application") or {}),
            payload_digest=str(value.get("payload_digest") or ""),
            created_at=str(value.get("created_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-action.continuation-payload.v1",
            "locator": self.locator,
            "request_id": self.request_id,
            "replay": copy.deepcopy(dict(self.replay)),
            "application": copy.deepcopy(dict(self.application)),
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
        }


class BrowserContinuationPayloadStore:
    """04A-session-owned write-ahead payloads outside permission state.

    The authoritative 03A continuation record contains only an immutable
    locator and argument digest.  Exact arguments and browser binding material
    live here under the browser session state root.  This prevents a second
    permission owner while retaining restart-safe exact replay validation.
    """

    def __init__(self, root: str | Path, *, disabled: bool = False) -> None:
        self.root = Path(root).resolve(strict=False)
        self.disabled = disabled
        self._lock = threading.RLock()
        self._writes = 0
        self._reads = 0
        self._tombstones = 0
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def write(self, payload: BrowserContinuationPayload) -> Path:
        self._ensure_available()
        target = self._path(payload.locator)
        encoded = json.dumps(payload.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        with self._lock:
            if target.exists():
                existing = self.read(payload.locator)
                if canonical_arguments_json(existing.to_dict()) != canonical_arguments_json(payload.to_dict()):
                    raise PermissionContinuationIdentityError(
                        "browser continuation locator already owns another payload"
                    )
                return target
            temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            self._writes += 1
        return target

    def read(self, locator: str) -> BrowserContinuationPayload:
        self._ensure_available()
        target = self._path(locator)
        with self._lock:
            if not target.is_file() or target.is_symlink():
                raise PermissionContinuationPayloadMissing(
                    "browser continuation payload is missing or not a regular file"
                )
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PermissionContinuationPayloadMissing(
                    f"browser continuation payload could not be read: {type(exc).__name__}"
                ) from exc
            if not isinstance(raw, Mapping):
                raise PermissionContinuationPayloadMissing("browser continuation payload is not an object")
            payload = BrowserContinuationPayload.from_dict(raw)
            if payload.locator != locator:
                raise PermissionContinuationIdentityError("browser continuation locator/path mismatch")
            self._reads += 1
            return payload

    def resolve_replay(self, record: PermissionContinuationRecord) -> Mapping[str, Any] | None:
        try:
            payload = self.read(record.payload_locator)
        except PermissionContinuationPayloadMissing:
            return None
        if payload.request_id != record.request_id:
            raise PermissionContinuationIdentityError("browser continuation payload belongs to another request")
        return payload.replay

    def application_payload(self, record: PermissionContinuationRecord) -> Mapping[str, Any]:
        return self.read(record.payload_locator).application

    def tombstone(self, locator: str, *, reason: str, claim_id: str = "") -> None:
        self._ensure_available()
        target = self._path(locator)
        with self._lock:
            if not target.exists():
                return
            tombstone = target.with_suffix(".tombstone.json")
            value = {
                "schema": "zyra.browser-action.continuation-tombstone.v1",
                "locator": locator,
                "reason": str(reason),
                "claim_id": str(claim_id),
                "tombstoned_at": now_iso(),
            }
            temporary = tombstone.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, tombstone)
            target.unlink(missing_ok=True)
            self._tombstones += 1

    def _path(self, locator: str) -> Path:
        if not str(locator).startswith("browser-session:"):
            raise PermissionContinuationIdentityError("browser continuation locator scheme is invalid")
        token = str(locator).split(":", 1)[1]
        if not token or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in token):
            raise PermissionContinuationIdentityError("browser continuation locator token is invalid")
        target = (self.root / f"{token}.json").resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise PermissionContinuationIdentityError("browser continuation path escaped its state root") from exc
        return target

    def _ensure_available(self) -> None:
        if self.disabled:
            raise PermissionContinuationPayloadMissing("browser continuation payload store is disabled")

    def snapshot(self) -> dict[str, Any]:
        files = tuple(self.root.glob("*.json")) if self.root.exists() else ()
        return {
            "runtime_id": "zyra-browser-continuation-payload-store",
            "owner_unit": "M1-S04C-02",
            "permission_state_owner": False,
            "root": str(self.root),
            "disabled": self.disabled,
            "payload_count": sum(not path.name.endswith(".tombstone.json") for path in files),
            "tombstone_count": sum(path.name.endswith(".tombstone.json") for path in files),
            "writes": self._writes,
            "reads": self._reads,
            "tombstones": self._tombstones,
        }


@dataclass(frozen=True, slots=True)
class BrowserContinuationClaim:
    claim: PermissionContinuationClaim
    checkpoint: PendingActionCheckpoint
    application_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "application_payload", copy.deepcopy(dict(self.application_payload)))
        if self.claim.record.request_id != self.checkpoint.permission_request_id:
            raise ValueError("browser continuation claim and checkpoint request ids differ")

    @property
    def record(self) -> PermissionContinuationRecord:
        return self.claim.record

    def public_dict(self) -> dict[str, Any]:
        return {
            "continuation_id": self.record.continuation_id,
            "request_id": self.record.request_id,
            "claim_id": self.record.claim_id,
            "claim_attempt": self.record.claim_attempt,
            "claim_expires_at": self.record.claim_expires_at,
            "checkpoint": self.checkpoint.public_dict(),
        }


class BrowserActionContinuationRuntime:
    def __init__(
        self,
        permission_gate: Any,
        payload_store: BrowserContinuationPayloadStore,
        *,
        disabled: bool = False,
    ) -> None:
        if permission_gate is None:
            raise ValueError("browser continuation runtime requires the 03A permission gate")
        self.permission_gate = permission_gate
        self.payload_store = payload_store
        self.disabled = disabled
        self.runtime = PermissionContinuationRuntime(
            permission_gate.state_store,
            session_id=permission_gate.session_id,
            payload_resolver=payload_store.resolve_replay,
            disabled=disabled,
            external_permission_authority=True,
        )
        self._parked = 0
        self._claimed = 0
        self._completed = 0
        self._failed = 0

    def park(
        self,
        plan: BrowserActionPlan,
        prepared: PreparedBrowserAction,
        authorized: AuthorizedBrowserAction,
    ) -> PendingActionCheckpoint:
        self._ensure_available()
        pending = authorized.permission.decision.guard.pending_request
        if pending is None or not authorized.pending or authorized.allowed:
            raise PermissionContinuationStateError("browser action is not pending ASK approval")
        if not isinstance(pending, PermissionRequestRecord):
            pending = PermissionRequestRecord.from_dict(pending.to_dict())
        if pending.phase is PermissionRequestPhase.CREATED:
            pending = self.permission_gate.runtime.request_queue.mark_delivered(
                pending.request_id,
                expected_request_revision=pending.revision,
                channel="browser-action-application",
            )
        if pending.phase is not PermissionRequestPhase.DELIVERED:
            raise PermissionContinuationStateError(
                "browser ASK request is not deliverable at the continuation boundary"
            )
        permission_arguments = authorized.permission.permission_input.permission_arguments
        continuation_pending = self._continuation_request_projection(
            pending,
            permission_arguments,
        )
        existing: PermissionContinuationRecord | None
        try:
            existing = self.runtime.get(pending.request_id)
        except KeyError:
            existing = None
        locator = "browser-session:" + stable_id(
            "brpayload",
            pending.request_id,
            pending.session_id,
            pending.tool_use_id,
            pending.request_fingerprint,
        ).removeprefix("brpayload_")
        if existing is not None and existing.payload_locator != locator:
            raise PermissionContinuationIdentityError("existing browser continuation locator changed")
        sequence = existing.session_sequence if existing else self._next_sequence()
        replay = {
            "session_id": continuation_pending.session_id,
            "task_id": continuation_pending.task_id,
            "run_id": continuation_pending.run_id,
            "tool_use_id": continuation_pending.tool_use_id,
            "tool_identity": continuation_pending.tool_identity.to_dict(),
            "arguments": to_jsonable(permission_arguments),
            "arguments_digest": continuation_pending.arguments_digest,
            "request_fingerprint": continuation_pending.request_fingerprint,
            "scope": continuation_pending.scope.to_dict(),
            "payload_locator": locator,
            "session_sequence": sequence,
            "source": "BrowserSessionContinuationPayloadStore",
            "metadata": {
                "request_id": pending.request_id,
                "permission_guard_required": True,
                "browser_action_id": prepared.action_id,
            },
        }
        PermissionContinuationReplay.from_value(replay, source="browser_action")
        expectation = prepared.selector_expectation
        binding = prepared.receipt.selector_binding
        checkpoint = PendingActionCheckpoint(
            checkpoint_id=create_checkpoint_id(
                plan,
                action_id=prepared.action_id,
                permission_request_id=pending.request_id,
                permission_tool_use_id=pending.tool_use_id,
            ),
            plan_id=plan.plan_id,
            plan_digest=plan.digest,
            action_id=prepared.action_id,
            step_index=prepared.request.identity.step_index,
            permission_request_id=pending.request_id,
            permission_tool_use_id=pending.tool_use_id,
            permission_session_id=pending.session_id,
            browser_session_id=plan.browser_session_id,
            canonical_session_id=plan.canonical_session_id,
            request_digest=prepared.request.request_digest,
            preflight_receipt_id=prepared.receipt.receipt_id,
            selector_expectation_digest=expectation.digest if expectation else "",
            target_id=binding.target_id if binding else "",
            target_generation=binding.target_generation if binding else 0,
            cdp_session_id=binding.cdp_session_id if binding else "",
            cdp_generation=binding.cdp_generation if binding else 0,
            selector_revision_id=binding.selector_revision_id if binding else "",
            expires_at=pending.expires_at,
        )
        application = {
            "schema": "zyra.browser-action.application-continuation.v1",
            "plan": plan.public_dict(),
            "raw_plan": [to_jsonable(step.source) for step in plan.steps],
            "checkpoint": checkpoint.public_dict(),
            "permission_input_digest": arguments_digest(permission_arguments),
            "registry_digest": prepared.receipt.registry_digest,
            "security_receipts": prepared.security.public_dict(),
            "prepared_request": prepared.request.public_dict(arguments=prepared.receipt.public_arguments),
        }
        payload = BrowserContinuationPayload.create(
            locator=locator,
            request_id=pending.request_id,
            replay=replay,
            application=application,
        )
        self.payload_store.write(payload)
        if existing is None:
            record = self.runtime.park(
                continuation_pending,
                payload_locator=locator,
                session_sequence=sequence,
                metadata={
                    "owner_unit": "M1-S04C-02",
                    "browser_action_id": prepared.action_id,
                    "browser_plan_id": plan.plan_id,
                    "browser_session_id": plan.browser_session_id,
                    "payload_owner": "BrowserSessionContinuationPayloadStore",
                    "canonical_permission_owner": "typescript",
                    "typescript_arguments_digest": pending.arguments_digest,
                    "python_transport_arguments_digest": continuation_pending.arguments_digest,
                    "raw_arguments_persisted_in_permission_state": False,
                },
            )
        else:
            record = existing
        if record.phase is PermissionContinuationPhase.PARKED:
            record = self.runtime.deliver(
                continuation_pending,
                expected_record_revision=record.revision,
            )
        self._parked += 1
        return checkpoint

    def claim_for_authorized(
        self,
        plan: BrowserActionPlan,
        prepared: PreparedBrowserAction,
        authorized: AuthorizedBrowserAction,
        *,
        claimant: str,
    ) -> BrowserContinuationClaim | None:
        self._ensure_available()
        if not authorized.allowed:
            return None
        tool_use_id = authorized.permission.decision.tool_use_id
        matches = [
            record
            for record in self.runtime.records()
            if record.tool_use_id == tool_use_id
            and record.task_id == plan.task_id
            and record.run_id == plan.run_id
            and not record.terminal
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise PermissionContinuationIdentityError("browser action tool-use id maps to multiple continuations")
        record = matches[0]
        request = self.permission_gate.runtime.request_queue.get(record.request_id)
        if request is None:
            raise PermissionContinuationStateError("authoritative permission request is missing")
        continuation_request = self._continuation_request_projection(
            request,
            authorized.permission.permission_input.permission_arguments,
            parked_record=record,
        )
        if request.phase in {
            PermissionRequestPhase.EXPIRED,
            PermissionRequestPhase.CANCELLED,
            PermissionRequestPhase.ABORTED,
        }:
            terminal = self.runtime.cancel(
                request.request_id,
                reason=f"authoritative request is {request.phase}",
                expected_record_revision=record.revision,
            )
            self.payload_store.tombstone(terminal.payload_locator, reason=str(request.phase))
            raise PermissionContinuationStateError("browser permission request is no longer resumable")
        if request.resolution_effect not in {PermissionEffect.ALLOW, PermissionEffect.DENY}:
            raise PermissionContinuationStateError("browser permission request is not resolved")
        if record.phase is not PermissionContinuationPhase.RESOLUTION_READY:
            record = self.runtime.resolution_ready(
                continuation_request,
                expected_record_revision=record.revision,
            )
        if request.resolution_effect is PermissionEffect.DENY:
            terminal = self.runtime.cancel(
                request.request_id,
                reason="browser permission request was denied",
                expected_record_revision=record.revision,
            )
            self.payload_store.tombstone(terminal.payload_locator, reason="permission_denied")
            raise BrowserActionIntegrationError(
                "browser_action_permission_denied",
                "browser action permission was denied",
                phase=PlanPhase.PERMISSION,
                action_id=prepared.action_id,
                step_index=prepared.request.identity.step_index,
            )
        payload = self.payload_store.read(record.payload_locator)
        checkpoint = checkpoint_from_mapping(payload.application.get("checkpoint") or {})
        self._assert_resume_binding(plan, prepared, checkpoint, record)
        current_replay = self._replay_for(prepared, authorized, record)
        claim = self.runtime.prepare_resume(
            request.request_id,
            current_replay,
            claimant=claimant,
            idempotency_key=stable_id("brclaim", request.request_id, prepared.action_id, plan.digest),
            expected_record_revision=record.revision,
            authoritative_request=continuation_request,
        )
        self._claimed += 1
        return BrowserContinuationClaim(claim, checkpoint, payload.application)

    @staticmethod
    def _continuation_request_projection(
        request: PermissionRequestRecord,
        permission_arguments: Mapping[str, Any],
        *,
        parked_record: PermissionContinuationRecord | None = None,
    ) -> PermissionRequestRecord:
        """Project a TS request into the retained Python payload-custody codec.

        TypeScript's digest remains the canonical permission identity.  The
        older Python continuation codec recomputes its own JSON digest while
        validating the session-owned replay payload, so that transport digest
        is kept in a projection-only record and never used to decide or grant
        permission.
        """

        typescript_digest = str(
            request.metadata.get("typescript_arguments_digest")
            or request.arguments_digest
        )
        if request.arguments_digest != typescript_digest:
            raise PermissionContinuationIdentityError(
                "browser continuation lost the TypeScript permission digest"
            )
        transport_digest = arguments_digest(permission_arguments)
        expected_transport_digest = str(
            request.metadata.get("python_transport_arguments_digest") or ""
        )
        if expected_transport_digest and expected_transport_digest != transport_digest:
            raise PermissionContinuationIdentityError(
                "browser continuation arguments changed after TypeScript evaluation"
            )
        if parked_record is not None:
            parked_typescript_digest = str(
                parked_record.metadata.get("typescript_arguments_digest")
                or parked_record.scope.metadata.get("typescript_arguments_digest")
                or ""
            )
            mismatches: list[str] = []
            for expected, actual, name in (
                (parked_record.request_id, request.request_id, "request_id"),
                (parked_record.session_id, request.session_id, "session_id"),
                (parked_record.task_id, request.task_id, "task_id"),
                (parked_record.run_id, request.run_id, "run_id"),
                (parked_record.worker_request_id, request.worker_request_id, "worker_request_id"),
                (parked_record.tool_use_id, request.tool_use_id, "tool_use_id"),
                (parked_record.request_fingerprint, request.request_fingerprint, "request_fingerprint"),
                (parked_record.expires_at, request.expires_at, "expires_at"),
                (parked_record.tool_identity.namespace, request.tool_identity.namespace, "tool_namespace"),
                (parked_record.tool_identity.name, request.tool_identity.name, "tool_name"),
                (parked_record.tool_identity.server_id, request.tool_identity.server_id, "server_id"),
                (parked_typescript_digest, typescript_digest, "typescript_arguments_digest"),
            ):
                if expected != actual:
                    mismatches.append(name)
            if (
                request.scope.workspace_root
                and parked_record.scope.workspace_root != request.scope.workspace_root
            ):
                mismatches.append("workspace_root")
            if parked_record.arguments_digest != transport_digest:
                mismatches.append("python_transport_arguments_digest")
            if mismatches:
                raise PermissionContinuationIdentityError(
                    "TypeScript approval no longer matches parked browser continuation: "
                    + ", ".join(mismatches)
                )
            return replace(
                request,
                tool_identity=parked_record.tool_identity,
                arguments_digest=parked_record.arguments_digest,
                request_fingerprint=parked_record.request_fingerprint,
                scope=parked_record.scope,
                metadata={
                    **request.metadata,
                    "canonical_owner": "typescript",
                    "typescript_arguments_digest": typescript_digest,
                    "python_transport_arguments_digest": transport_digest,
                    "projection_role": "browser_continuation_payload_custody",
                    "python_decision_fallback": False,
                },
            )
        scope = replace(
            request.scope,
            argument_digest=transport_digest,
            metadata={
                **request.scope.metadata,
                "canonical_permission_owner": "typescript",
                "typescript_arguments_digest": typescript_digest,
                "python_continuation_projection": True,
            },
        )
        return replace(
            request,
            arguments_digest=transport_digest,
            scope=scope,
            metadata={
                **request.metadata,
                "canonical_owner": "typescript",
                "typescript_arguments_digest": typescript_digest,
                "python_transport_arguments_digest": transport_digest,
                "projection_role": "browser_continuation_payload_custody",
                "python_decision_fallback": False,
            },
        )

    def complete(self, claim: BrowserContinuationClaim) -> PermissionContinuationRecord:
        record = self.runtime.complete(
            claim.record.request_id,
            claim_id=claim.record.claim_id,
            expected_record_revision=claim.record.revision,
        )
        self.payload_store.tombstone(record.payload_locator, reason="completed", claim_id=record.claim_id)
        self._completed += 1
        return record

    def fail(
        self,
        claim: BrowserContinuationClaim,
        *,
        code: str,
        outcome_unknown: bool,
    ) -> PermissionContinuationRecord:
        if outcome_unknown:
            record = self.runtime.fail(
                claim.record.request_id,
                claim_id=claim.record.claim_id,
                failure_code=code,
                expected_record_revision=claim.record.revision,
            )
            self.payload_store.tombstone(record.payload_locator, reason=code, claim_id=record.claim_id)
        else:
            record = self.runtime.release_claim(
                claim.record.request_id,
                claim_id=claim.record.claim_id,
                reason=code,
                expected_record_revision=claim.record.revision,
            )
        self._failed += 1
        return record

    def checkpoint_for_continuation(self, continuation_id: str) -> PendingActionCheckpoint:
        records = [record for record in self.runtime.records() if record.continuation_id == continuation_id]
        if len(records) != 1:
            raise KeyError(continuation_id)
        payload = self.payload_store.read(records[0].payload_locator)
        return checkpoint_from_mapping(payload.application.get("checkpoint") or {})

    def _replay_for(
        self,
        prepared: PreparedBrowserAction,
        authorized: AuthorizedBrowserAction,
        record: PermissionContinuationRecord,
    ) -> Mapping[str, Any]:
        permission_arguments = authorized.permission.permission_input.permission_arguments
        return {
            "session_id": record.session_id,
            "task_id": record.task_id,
            "run_id": record.run_id,
            "tool_use_id": authorized.permission.decision.tool_use_id,
            "tool_identity": record.tool_identity.to_dict(),
            "arguments": to_jsonable(permission_arguments),
            "arguments_digest": arguments_digest(permission_arguments),
            "request_fingerprint": record.request_fingerprint,
            "scope": record.scope.to_dict(),
            "payload_locator": record.payload_locator,
            "session_sequence": record.session_sequence,
            "source": "BrowserActionContinuationRuntime.resume",
            "metadata": {
                "request_id": record.request_id,
                "browser_action_id": prepared.action_id,
                "permission_guard_required": True,
            },
        }

    @staticmethod
    def _assert_resume_binding(
        plan: BrowserActionPlan,
        prepared: PreparedBrowserAction,
        checkpoint: PendingActionCheckpoint,
        record: PermissionContinuationRecord,
    ) -> None:
        mismatches: dict[str, Any] = {}
        expected = {
            "plan_id": checkpoint.plan_id,
            "action_id": checkpoint.action_id,
            "step_index": checkpoint.step_index,
            "permission_request_id": checkpoint.permission_request_id,
            "permission_tool_use_id": checkpoint.permission_tool_use_id,
            "permission_session_id": checkpoint.permission_session_id,
            "browser_session_id": checkpoint.browser_session_id,
            "canonical_session_id": checkpoint.canonical_session_id,
            "request_digest": checkpoint.request_digest,
            "selector_expectation_digest": checkpoint.selector_expectation_digest,
        }
        actual = {
            "plan_id": plan.plan_id,
            "action_id": prepared.action_id,
            "step_index": prepared.request.identity.step_index,
            "permission_request_id": record.request_id,
            "permission_tool_use_id": record.tool_use_id,
            "permission_session_id": record.session_id,
            "browser_session_id": plan.browser_session_id,
            "canonical_session_id": plan.canonical_session_id,
            "request_digest": prepared.request.request_digest,
            "selector_expectation_digest": (
                prepared.selector_expectation.digest if prepared.selector_expectation else ""
            ),
        }
        for key, expected_value in expected.items():
            if actual[key] != expected_value:
                mismatches[key] = {"expected": expected_value, "actual": actual[key]}
        binding = prepared.receipt.selector_binding
        if binding is not None:
            for key, current in (
                ("target_id", binding.target_id),
                ("target_generation", binding.target_generation),
                ("cdp_session_id", binding.cdp_session_id),
                ("cdp_generation", binding.cdp_generation),
                ("selector_revision_id", binding.selector_revision_id),
            ):
                approved = getattr(checkpoint, key)
                if approved != current:
                    mismatches[key] = {"expected": approved, "actual": current}
        if mismatches:
            raise BrowserActionIntegrationError(
                "browser_action_resume_binding_changed",
                "browser action binding changed while permission was pending",
                phase=PlanPhase.RESUME_VALIDATION,
                action_id=prepared.action_id,
                step_index=prepared.request.identity.step_index,
                details={"mismatches": mismatches, "side_effect_count": 0},
            )

    def _next_sequence(self) -> int:
        records = self.runtime.records()
        return max((record.session_sequence for record in records), default=0) + 1

    def _ensure_available(self) -> None:
        if self.disabled:
            raise PermissionContinuationStateError("browser action continuation runtime is disabled")

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-action-continuation-runtime",
            "owner_unit": "M1-S04C-02",
            "canonical_permission_owner": "PermissionContinuationRuntime/M1-03A",
            "disabled": self.disabled,
            "parked": self._parked,
            "claimed": self._claimed,
            "completed": self._completed,
            "failed": self._failed,
            "permission": self.runtime.snapshot(),
            "payload_store": self.payload_store.snapshot(),
        }


def checkpoint_from_mapping(raw: Mapping[str, Any]) -> PendingActionCheckpoint:
    allowed = {
        "checkpoint_id",
        "plan_id",
        "plan_digest",
        "action_id",
        "step_index",
        "permission_request_id",
        "permission_tool_use_id",
        "permission_session_id",
        "browser_session_id",
        "canonical_session_id",
        "request_digest",
        "preflight_receipt_id",
        "selector_expectation_digest",
        "target_id",
        "target_generation",
        "cdp_session_id",
        "cdp_generation",
        "selector_revision_id",
        "state_revision",
        "expires_at",
        "created_at",
    }
    return PendingActionCheckpoint(**{key: value for key, value in raw.items() if key in allowed})
