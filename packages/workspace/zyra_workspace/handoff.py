from __future__ import annotations

"""Signed, path-free workspace capability envelopes."""

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic import atomic_write_bytes
from .errors import WorkspaceError, WorkspaceErrorCode
from .integration_models import (
    IntegrationOperation,
    WorkspaceAccessEnvelope,
    ensure_path_free_projection,
)
from .local_backend import WorkspaceAccessHandle
from .models import WorkspaceOperation, new_workspace_id, stable_digest, utc_now


WORKSPACE_ENVELOPE_KEY_BYTES = 32


class WorkspaceHandoffRuntime:
    """Issue and consume a process-private workspace capability safely.

    The serialized envelope contains identifiers, revisions, operation scope,
    audience and expiry.  It never contains the fence token or physical root.
    A verified envelope still does not authorize filesystem access by itself;
    consumption reacquires a fresh manager capability for the audience and
    validates that the binding epoch/revision have not changed.
    """

    def __init__(
        self,
        manager: Any,
        *,
        key_path: str | Path | None = None,
        disabled: bool = False,
        default_ttl_seconds: int = 15 * 60,
    ) -> None:
        self.manager = manager
        self.store = manager.integration_store
        self.disabled = bool(disabled)
        self.default_ttl_seconds = max(30, int(default_ttl_seconds))
        self.key_path = Path(key_path or (manager.config.state_root / "handoff" / "workspace-envelope.key")).resolve()
        self._key = self._load_or_create_key() if not self.disabled else b""
        self.key_id = f"workspace-handoff-{hashlib.sha256(self._key).hexdigest()[:16]}" if self._key else "disabled"

    def issue(
        self,
        access: WorkspaceAccessHandle,
        *,
        audience: str,
        operations: Iterable[IntegrationOperation] | None = None,
        mount_kinds: Iterable[str] | None = None,
        isolation_id: str = "",
        endpoint_id: str = "",
        ttl_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkspaceAccessEnvelope:
        self._require_enabled(access.workspace_id)
        binding = self.manager.store.require_binding(access.workspace_id)
        self._validate_live_access(access, binding)
        selected_operations = tuple(operations or _integration_operations(access.operations))
        if not selected_operations:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace handoff requires at least one operation.",
                workspace_id=access.workspace_id,
                operation="issue_workspace_handoff",
            )
        selected_mounts = tuple(mount_kinds or (item.value for item in access.mount_kinds))
        selected_metadata = {
            **dict(metadata or {}),
            "fence_token_serialized": False,
            "physical_location_serialized": False,
            "state_owner": "WorkspaceManagerRuntime/WorkspaceHandoffRuntime",
        }
        ensure_path_free_projection(selected_metadata)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(30, int(ttl_seconds or self.default_ttl_seconds)))
        unsigned = WorkspaceAccessEnvelope(
            envelope_id=new_workspace_id("workspace-envelope"),
            workspace_id=binding.workspace_id,
            task_id=binding.task_id,
            audience=str(audience),
            owner_epoch=binding.owner_epoch,
            binding_revision=binding.binding_revision,
            capability_revision=binding.capability_revision,
            lease_id=binding.lease_id,
            operations=selected_operations,
            mount_kinds=selected_mounts,
            issued_at=now.isoformat(),
            expires_at=expires.isoformat(),
            nonce=secrets.token_urlsafe(24),
            signature="",
            key_id=self.key_id,
            isolation_id=isolation_id,
            endpoint_id=endpoint_id or str(binding.metadata.get("endpoint_id") or binding.backend_id),
            metadata=selected_metadata,
        )
        envelope = replace(unsigned, signature=self._sign(unsigned.signing_payload()))
        ensure_path_free_projection(envelope.to_dict())
        self.store.audit_envelope(envelope, action="issued", ok=True)
        self.manager.emit_integration_event(
            "workspace.handoff.issued",
            binding.workspace_id,
            metadata={
                "envelope_id": envelope.envelope_id,
                "audience": envelope.audience,
                "operations": [item.value for item in envelope.operations],
                "expires_at": envelope.expires_at,
                "isolation_id": envelope.isolation_id,
            },
        )
        return envelope

    def verify(
        self,
        value: WorkspaceAccessEnvelope | Mapping[str, Any],
        *,
        audience: str,
        required_operation: IntegrationOperation | None = None,
    ) -> WorkspaceAccessEnvelope:
        self._require_enabled("")
        envelope = value if isinstance(value, WorkspaceAccessEnvelope) else WorkspaceAccessEnvelope.from_dict(value)
        ensure_path_free_projection(envelope.to_dict())
        reason_code = ""
        try:
            if envelope.key_id != self.key_id:
                raise WorkspaceError(
                    WorkspaceErrorCode.CAPABILITY_STALE,
                    "Workspace handoff key identifier is stale.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                )
            expected_signature = self._sign(envelope.signing_payload())
            if not hmac.compare_digest(expected_signature, envelope.signature):
                raise WorkspaceError(
                    WorkspaceErrorCode.FENCE_TOKEN_MISMATCH,
                    "Workspace handoff signature is invalid.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                )
            if envelope.audience != str(audience):
                raise WorkspaceError(
                    WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
                    "Workspace handoff audience does not match the consumer.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                    expected=envelope.audience,
                    actual=str(audience),
                )
            expires_at = datetime.fromisoformat(envelope.expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise WorkspaceError(
                    WorkspaceErrorCode.LEASE_EXPIRED,
                    "Workspace handoff envelope expired.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                )
            if required_operation is not None and required_operation not in envelope.operations:
                raise WorkspaceError(
                    WorkspaceErrorCode.LEASE_REVOKED,
                    "Workspace handoff does not allow the requested operation.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                    actual=required_operation.value,
                )
            binding = self.manager.store.require_binding(envelope.workspace_id)
            if (
                binding.task_id != envelope.task_id
                or binding.owner_epoch != envelope.owner_epoch
                or binding.binding_revision != envelope.binding_revision
                or binding.capability_revision != envelope.capability_revision
                or binding.lease_id != envelope.lease_id
            ):
                raise WorkspaceError(
                    WorkspaceErrorCode.OWNER_EPOCH_STALE,
                    "Workspace handoff references a stale binding revision.",
                    workspace_id=envelope.workspace_id,
                    operation="verify_workspace_handoff",
                    expected={
                        "owner_epoch": binding.owner_epoch,
                        "binding_revision": binding.binding_revision,
                        "capability_revision": binding.capability_revision,
                        "lease_id": binding.lease_id,
                    },
                    actual={
                        "owner_epoch": envelope.owner_epoch,
                        "binding_revision": envelope.binding_revision,
                        "capability_revision": envelope.capability_revision,
                        "lease_id": envelope.lease_id,
                    },
                )
            self.store.audit_envelope(envelope, action="verified", ok=True)
            return envelope
        except WorkspaceError as error:
            reason_code = error.code.value
            self.store.audit_envelope(
                envelope,
                action="verification_rejected",
                ok=False,
                reason_code=reason_code,
            )
            raise

    def consume(
        self,
        value: WorkspaceAccessEnvelope | Mapping[str, Any],
        *,
        audience: str,
        required_operation: IntegrationOperation,
    ) -> WorkspaceAccessHandle:
        envelope = self.verify(
            value,
            audience=audience,
            required_operation=required_operation,
        )
        binding = self.manager.store.require_binding(envelope.workspace_id)
        # Transfer is deliberate.  The serialized lease is checked above,
        # then a new audience-owned lease/epoch is issued.  The envelope and
        # every previously cached capability become stale after consumption.
        access = self.manager.acquire_for_worker(
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id=str(audience),
            operations=_workspace_operations(envelope.operations),
        )
        self.store.audit_envelope(envelope, action="consumed", ok=True)
        self.manager.emit_integration_event(
            "workspace.handoff.consumed",
            envelope.workspace_id,
            metadata={
                "envelope_id": envelope.envelope_id,
                "audience": audience,
                "required_operation": required_operation.value,
                "new_owner_epoch": access.owner_epoch,
                "old_owner_epoch": envelope.owner_epoch,
            },
        )
        return access

    def serialize(self, envelope: WorkspaceAccessEnvelope) -> str:
        ensure_path_free_projection(envelope.to_dict())
        return json.dumps(
            envelope.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def deserialize(self, payload: str) -> WorkspaceAccessEnvelope:
        try:
            value = json.loads(str(payload))
        except json.JSONDecodeError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace handoff payload is not valid JSON.",
                operation="deserialize_workspace_handoff",
            ) from error
        if not isinstance(value, Mapping):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace handoff payload must be an object.",
                operation="deserialize_workspace_handoff",
            )
        ensure_path_free_projection(value)
        return WorkspaceAccessEnvelope.from_dict(value)

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = self.key_path.read_bytes()
        except FileNotFoundError:
            content = secrets.token_bytes(WORKSPACE_ENVELOPE_KEY_BYTES)
            atomic_write_bytes(self.key_path, content, mode=0o600)
        if len(content) < WORKSPACE_ENVELOPE_KEY_BYTES:
            raise WorkspaceError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace handoff signing key is invalid.",
                operation="load_workspace_handoff_key",
            )
        return content

    def _sign(self, payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(self._key, encoded, hashlib.sha256).hexdigest()

    def _validate_live_access(self, access: WorkspaceAccessHandle, binding: Any) -> None:
        if (
            access.workspace_id != binding.workspace_id
            or access.owner_epoch != binding.owner_epoch
            or access.capability_revision != binding.capability_revision
            or access.lease_id != binding.lease_id
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Workspace access is stale and cannot be handed off.",
                workspace_id=binding.workspace_id,
                operation="issue_workspace_handoff",
            )
        lease = self.manager.store.get_lease(access.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "Workspace access references a missing lease.",
                workspace_id=binding.workspace_id,
                operation="issue_workspace_handoff",
            )
        self.manager.backend.validate_lease(
            binding,
            lease,
            fence_token=access.fence_token,
            operation=WorkspaceOperation.READ,
        )

    def _require_enabled(self, workspace_id: str) -> None:
        if self.disabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "Workspace handoff runtime is disabled; physical path fallback is forbidden.",
                workspace_id=workspace_id,
                operation="workspace_handoff_runtime",
            )


def _integration_operations(values: Iterable[WorkspaceOperation]) -> tuple[IntegrationOperation, ...]:
    mapping = {
        WorkspaceOperation.READ: IntegrationOperation.READ,
        WorkspaceOperation.WRITE: IntegrationOperation.WRITE,
        WorkspaceOperation.DELETE: IntegrationOperation.DELETE,
        WorkspaceOperation.SNAPSHOT: IntegrationOperation.RESTORE,
        WorkspaceOperation.REBIND: IntegrationOperation.REBIND,
    }
    return tuple(mapping[item] for item in values if item in mapping)


def _workspace_operations(values: Iterable[IntegrationOperation]) -> tuple[WorkspaceOperation, ...]:
    mapping = {
        IntegrationOperation.READ: WorkspaceOperation.READ,
        IntegrationOperation.WRITE: WorkspaceOperation.WRITE,
        IntegrationOperation.EDIT: WorkspaceOperation.WRITE,
        IntegrationOperation.DELETE: WorkspaceOperation.DELETE,
        IntegrationOperation.PATCH: WorkspaceOperation.WRITE,
        IntegrationOperation.SHELL: WorkspaceOperation.WRITE,
        IntegrationOperation.RESTORE: WorkspaceOperation.SNAPSHOT,
        IntegrationOperation.REBIND: WorkspaceOperation.REBIND,
    }
    selected = [mapping[item] for item in values if item in mapping]
    if WorkspaceOperation.READ not in selected:
        selected.insert(0, WorkspaceOperation.READ)
    return tuple(dict.fromkeys(selected))


__all__ = [
    "WORKSPACE_ENVELOPE_KEY_BYTES",
    "WorkspaceHandoffRuntime",
]
