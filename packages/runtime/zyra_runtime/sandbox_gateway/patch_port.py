from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from zyra_workspace import WorkspaceKind

from .canonical import content_digest, stable_id
from .constants import DEFAULT_MAX_PATCH_BYTES, DEFAULT_MAX_PATCH_FILES
from .errors import GatewayErrorCode, SandboxGatewayError
from .file_policy import GatewayFilePolicy
from .models import (
    ArtifactProvenance,
    FileArtifactRequest,
    GatewayPatchSet,
    OperationKind,
    PatchMutation,
    PatchOperation,
    PatchReceipt,
    ProvenanceKind,
    TrustLevel,
)
from .provenance import ProvenanceRegistry


class WorkspacePatchPortLike(Protocol):
    @property
    def workspace_id(self) -> str:
        ...

    def current_access(self) -> Any:
        ...

    def read_bytes(self, path: str, *, mount_kind: WorkspaceKind = WorkspaceKind.TASK) -> Any:
        ...

    def write_bytes(
        self,
        path: str,
        content: bytes,
        *,
        mount_kind: WorkspaceKind = WorkspaceKind.TASK,
        idempotency_key: str,
        causation_id: str = "",
    ) -> Any:
        ...

    def delete_file(
        self,
        path: str,
        *,
        mount_kind: WorkspaceKind = WorkspaceKind.TASK,
        idempotency_key: str,
        causation_id: str = "",
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class PreparedPatch:
    patch_set: GatewayPatchSet
    previous_digests: Mapping[str, str]
    policy_digests: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_set": self.patch_set.to_dict(),
            "previous_digests": dict(self.previous_digests),
            "policy_digests": dict(self.policy_digests),
        }


class GatewayPatchPort:
    """Preflights every mutation, then delegates each write to WorkspaceEditPort."""

    def __init__(
        self,
        workspace_edit_port: WorkspacePatchPortLike,
        *,
        file_policy: GatewayFilePolicy,
        provenance_registry: ProvenanceRegistry,
        maximum_files: int = DEFAULT_MAX_PATCH_FILES,
        maximum_bytes: int = DEFAULT_MAX_PATCH_BYTES,
        enabled: bool = True,
    ) -> None:
        self.workspace_edit_port = workspace_edit_port
        self.file_policy = file_policy
        self.provenance_registry = provenance_registry
        self.maximum_files = int(maximum_files)
        self.maximum_bytes = int(maximum_bytes)
        self.enabled = bool(enabled)

    def prepare(self, patch_set: GatewayPatchSet) -> PreparedPatch:
        self._require_enabled()
        current = self.workspace_edit_port.current_access()
        if self.workspace_edit_port.workspace_id != patch_set.base_workspace_id:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_STALE,
                "patch set is bound to another workspace",
                operation="patch_prepare",
            )
        if int(getattr(current, "owner_epoch", 0)) != patch_set.base_owner_epoch:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_STALE,
                "workspace owner epoch changed before patch preflight",
                operation="patch_prepare",
            )
        if len(patch_set.mutations) > self.maximum_files:
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_REJECTED,
                "patch exceeds the file count budget",
                operation="patch_prepare",
            )
        if patch_set.content_bytes > self.maximum_bytes:
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_REJECTED,
                "patch exceeds the content byte budget",
                operation="patch_prepare",
            )
        previous: dict[str, str] = {}
        policies: dict[str, str] = {}
        for mutation in patch_set.mutations:
            prior = self._read_optional(mutation.logical_path)
            prior_digest = content_digest(prior) if prior is not None else ""
            previous[mutation.logical_path] = prior_digest
            self._validate_operation(mutation, prior)
            if (
                mutation.expected_previous_digest
                and mutation.expected_previous_digest != prior_digest
            ):
                raise SandboxGatewayError(
                    GatewayErrorCode.PATCH_CONFLICT,
                    f"patch base digest mismatch: {mutation.logical_path}",
                    operation="patch_prepare",
                )
            if mutation.operation is not PatchOperation.DELETE:
                provenance = self._provenance(mutation)
                request = FileArtifactRequest.build(
                    session_id=patch_set.session_id,
                    logical_path=mutation.logical_path,
                    content=mutation.content,
                    content_type=mutation.content_type,
                    provenance=provenance,
                    operation=OperationKind.PATCH_APPLY,
                    expected_previous_digest=prior_digest,
                    idempotency_key=stable_id(
                        "patch-preflight",
                        patch_set.idempotency_key,
                        mutation.logical_path,
                        mutation.content_digest,
                    ),
                    causation_id=patch_set.command_id,
                    metadata=mutation.metadata,
                )
                decision = self.file_policy.inspect(request)
                if not decision.allowed or decision.quarantine:
                    raise SandboxGatewayError(
                        GatewayErrorCode.PATCH_REJECTED,
                        f"patch mutation failed file policy: {mutation.logical_path}",
                        operation="patch_prepare",
                        metadata=decision.to_dict(),
                    )
                policies[mutation.logical_path] = decision.policy_digest
        return PreparedPatch(
            patch_set=patch_set,
            previous_digests=previous,
            policy_digests=policies,
        )

    def commit(self, prepared: PreparedPatch) -> PatchReceipt:
        self._require_enabled()
        patch_set = prepared.patch_set
        current = self.workspace_edit_port.current_access()
        if int(getattr(current, "owner_epoch", 0)) != patch_set.base_owner_epoch:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_STALE,
                "workspace owner epoch changed after patch preflight",
                operation="patch_commit",
            )
        for mutation in patch_set.mutations:
            actual = self._read_optional(mutation.logical_path)
            actual_digest = content_digest(actual) if actual is not None else ""
            if actual_digest != prepared.previous_digests[mutation.logical_path]:
                raise SandboxGatewayError(
                    GatewayErrorCode.PATCH_CONFLICT,
                    f"workspace changed after patch preflight: {mutation.logical_path}",
                    operation="patch_commit",
                )
        transaction_ids: list[str] = []
        owner_epoch = patch_set.base_owner_epoch
        for index, mutation in enumerate(patch_set.mutations):
            key = stable_id(
                "gateway-patch-mutation",
                patch_set.idempotency_key,
                index,
                mutation.operation.value,
                mutation.logical_path,
                mutation.content_digest,
            )
            if mutation.operation is PatchOperation.DELETE:
                result = self.workspace_edit_port.delete_file(
                    mutation.logical_path,
                    mount_kind=WorkspaceKind.TASK,
                    idempotency_key=key,
                    causation_id=patch_set.command_id or patch_set.patch_set_id,
                )
            else:
                result = self.workspace_edit_port.write_bytes(
                    mutation.logical_path,
                    mutation.content,
                    mount_kind=WorkspaceKind.TASK,
                    idempotency_key=key,
                    causation_id=patch_set.command_id or patch_set.patch_set_id,
                )
            transaction = getattr(result, "transaction", None)
            transaction_ids.append(str(getattr(transaction, "transaction_id", "")))
            owner_epoch = int(getattr(result.access, "owner_epoch", owner_epoch))
            if hasattr(self.workspace_edit_port, "adopt_access"):
                self.workspace_edit_port.adopt_access(result.access)
        receipt = PatchReceipt(
            receipt_id=stable_id(
                "gateway-patch-receipt",
                patch_set.patch_set_id,
                transaction_ids,
                owner_epoch,
            ),
            patch_set_id=patch_set.patch_set_id,
            committed=True,
            workspace_id=self.workspace_edit_port.workspace_id,
            owner_epoch_before=patch_set.base_owner_epoch,
            owner_epoch_after=owner_epoch,
            transaction_ids=tuple(transaction_ids),
            reason="all patch mutations committed through WorkspaceEditPort",
            metadata={
                "preflight_policy_digests": dict(prepared.policy_digests),
                "write_owner": "WorkspaceEditPort",
                "batch_atomicity": "preflight_all_then_journal_each",
            },
        )
        return receipt

    def apply(self, patch_set: GatewayPatchSet) -> PatchReceipt:
        return self.commit(self.prepare(patch_set))

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "port_id": "GatewayPatchPort",
            "enabled": self.enabled,
            "maximum_files": self.maximum_files,
            "maximum_bytes": self.maximum_bytes,
            "workspace_owner": "WorkspaceManagerRuntime",
            "write_owner": "WorkspaceEditPort",
            "raw_write_fallback": False,
            "preflight_all": True,
        }

    def _read_optional(self, logical_path: str) -> bytes | None:
        try:
            result = self.workspace_edit_port.read_bytes(
                logical_path,
                mount_kind=WorkspaceKind.TASK,
            )
        except Exception as error:
            code = str(getattr(error, "code", "")).casefold()
            if "not_found" in code or isinstance(error, FileNotFoundError):
                return None
            raise
        if not bool(getattr(result, "exists", True)):
            return None
        return bytes(result.content)

    @staticmethod
    def _validate_operation(
        mutation: PatchMutation,
        previous: bytes | None,
    ) -> None:
        if mutation.operation is PatchOperation.CREATE and previous is not None:
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_CONFLICT,
                f"create target already exists: {mutation.logical_path}",
                operation="patch_prepare",
            )
        if mutation.operation in {PatchOperation.EDIT, PatchOperation.DELETE} and previous is None:
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_CONFLICT,
                f"patch target does not exist: {mutation.logical_path}",
                operation="patch_prepare",
            )
        if mutation.operation is PatchOperation.DELETE and mutation.content:
            raise SandboxGatewayError(
                GatewayErrorCode.PATCH_REJECTED,
                "delete mutation cannot carry replacement content",
                operation="patch_prepare",
            )

    def _provenance(self, mutation: PatchMutation) -> ArtifactProvenance:
        if mutation.provenance_id:
            return self.provenance_registry.require(mutation.provenance_id)
        record = ArtifactProvenance.build(
            kind=ProvenanceKind.GENERATED,
            trust=TrustLevel.CONSTRAINED,
            source_id="SandboxGatewayRuntime.patch",
            content_digest_value=mutation.content_digest,
            metadata={"logical_path": mutation.logical_path},
        )
        return self.provenance_registry.register(record)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_GATEWAY_UNAVAILABLE,
                "GatewayPatchPort is disabled; direct workspace mutation is forbidden",
                operation="patch_port",
            )
