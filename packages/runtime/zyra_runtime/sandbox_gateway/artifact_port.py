from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Protocol

from zyra_workspace import WorkspaceKind

from .archive_policy import ArchiveInspection, ArchivePolicy
from .canonical import content_digest, stable_id
from .errors import GatewayErrorCode, SandboxGatewayError
from .file_policy import FilePolicyDecision, GatewayFilePolicy
from .models import (
    ArtifactProvenance,
    FileArtifactReceipt,
    FileArtifactRequest,
    OperationKind,
    ProvenanceKind,
    TrustLevel,
)
from .provenance import ProvenanceRegistry
from .quarantine import QuarantineStore
from .redaction import SecretRedactor


class WorkspaceEditPortLike(Protocol):
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


class GatewayFileArtifactPort:
    """Only gateway path allowed to transfer file payloads into WorkspaceEditPort."""

    def __init__(
        self,
        workspace_edit_port: WorkspaceEditPortLike,
        *,
        file_policy: GatewayFilePolicy,
        provenance_registry: ProvenanceRegistry,
        quarantine_store: QuarantineStore,
        archive_policy: ArchivePolicy | None = None,
        redactor: SecretRedactor | None = None,
        enabled: bool = True,
    ) -> None:
        self.workspace_edit_port = workspace_edit_port
        self.file_policy = file_policy
        self.provenance_registry = provenance_registry
        self.quarantine_store = quarantine_store
        self.archive_policy = archive_policy or ArchivePolicy()
        self.redactor = redactor or SecretRedactor()
        self.enabled = bool(enabled)

    def inspect(
        self,
        request: FileArtifactRequest,
    ) -> FilePolicyDecision:
        self._require_enabled()
        self.provenance_registry.register(request.provenance)
        return self.file_policy.inspect(request)

    def commit(self, request: FileArtifactRequest) -> FileArtifactReceipt:
        decision = self.inspect(request)
        if decision.quarantine or not decision.allowed:
            reason_codes = tuple(item.code for item in decision.findings) or (
                "file_policy_rejected",
            )
            quarantine = self.quarantine_store.quarantine(
                request,
                reason_codes=reason_codes,
                metadata={
                    "file_policy_digest": decision.policy_digest,
                    "detected_content_type": decision.detected_content_type,
                },
            )
            return FileArtifactReceipt(
                receipt_id=stable_id(
                    "artifact-receipt",
                    request.request_id,
                    quarantine.quarantine_id,
                    "quarantined",
                ),
                request_id=request.request_id,
                session_id=request.session_id,
                logical_path=request.logical_path,
                content_digest=request.content_digest,
                content_bytes=len(request.content),
                provenance_id=request.provenance.provenance_id,
                committed=False,
                quarantined=True,
                quarantine_id=quarantine.quarantine_id,
                reason=decision.provenance.reason,
                metadata={"policy": decision.to_dict()},
            )
        if request.expected_previous_digest:
            previous = self.workspace_edit_port.read_bytes(
                request.logical_path,
                mount_kind=self._mount(request.mount_kind),
            )
            actual = content_digest(bytes(previous.content))
            if actual != request.expected_previous_digest:
                raise SandboxGatewayError(
                    GatewayErrorCode.CONTENT_MISMATCH,
                    "workspace file changed after artifact request was prepared",
                    operation="artifact_commit",
                    metadata={
                        "expected_previous_digest": request.expected_previous_digest,
                        "actual_previous_digest": actual,
                    },
                )
        before = self.workspace_edit_port.current_access()
        result = self.workspace_edit_port.write_bytes(
            request.logical_path,
            request.content,
            mount_kind=self._mount(request.mount_kind),
            idempotency_key=(
                request.idempotency_key
                or stable_id("artifact-write", request.request_id, request.content_digest)
            ),
            causation_id=request.causation_id or request.request_id,
        )
        after = result.access
        transaction = getattr(result, "transaction", None)
        transaction_id = str(getattr(transaction, "transaction_id", ""))
        return FileArtifactReceipt(
            receipt_id=stable_id(
                "artifact-receipt",
                request.request_id,
                request.content_digest,
                transaction_id,
            ),
            request_id=request.request_id,
            session_id=request.session_id,
            logical_path=request.logical_path,
            content_digest=request.content_digest,
            content_bytes=len(request.content),
            provenance_id=request.provenance.provenance_id,
            committed=True,
            quarantined=False,
            workspace_id=self.workspace_edit_port.workspace_id,
            owner_epoch_before=int(getattr(before, "owner_epoch", 0)),
            owner_epoch_after=int(getattr(after, "owner_epoch", 0)),
            transaction_id=transaction_id,
            artifact_ref=str(getattr(result, "artifact_ref", "")),
            reason="artifact committed through WorkspaceEditPort",
            metadata={
                "policy_digest": decision.policy_digest,
                "detected_content_type": decision.detected_content_type,
                "workspace_owner": "WorkspaceManagerRuntime",
                "write_owner": "WorkspaceEditPort",
            },
        )

    def import_archive(
        self,
        *,
        session_id: str,
        archive_content: bytes,
        archive_name: str,
        destination: str,
        provenance: ArtifactProvenance,
        idempotency_key: str,
        mount_kind: str = "task",
        causation_id: str = "",
    ) -> tuple[ArchiveInspection, tuple[FileArtifactReceipt, ...]]:
        self._require_enabled()
        inspection = self.archive_policy.inspect(
            archive_content,
            filename=archive_name,
        )
        prefix = destination.strip("/\\")
        requests: list[FileArtifactRequest] = []
        decisions: list[FilePolicyDecision] = []
        self.provenance_registry.register(provenance)
        for entry in inspection.entries:
            logical_path = (
                f"{prefix}/{entry.logical_path}" if prefix else entry.logical_path
            )
            child = self.provenance_registry.derive(
                kind=ProvenanceKind.GENERATED,
                source_id=f"archive:{archive_name}:{entry.source_index}",
                parent_refs=(provenance.provenance_id,),
                content=entry.content,
                trust=(
                    TrustLevel.UNTRUSTED
                    if provenance.untrusted
                    else TrustLevel.CONSTRAINED
                ),
                metadata={
                    "archive_digest": inspection.archive_digest,
                    "archive_format": inspection.format,
                    "archive_entry": entry.logical_path,
                },
            )
            request = FileArtifactRequest.build(
                session_id=session_id,
                logical_path=logical_path,
                content=entry.content,
                content_type=self.file_policy.detect_content_type(
                    entry.content,
                    entry.logical_path,
                ),
                provenance=child,
                operation=OperationKind.ARCHIVE_IMPORT,
                mount_kind=mount_kind,
                executable_allowed=False,
                archive_expansion_allowed=False,
                idempotency_key=stable_id(
                    "archive-entry",
                    idempotency_key,
                    entry.source_index,
                    entry.content_digest,
                ),
                causation_id=causation_id,
                metadata={
                    "archive_name": PurePosixPath(archive_name).name,
                    "archive_digest": inspection.archive_digest,
                },
            )
            decision = self.inspect(request)
            requests.append(request)
            decisions.append(decision)
        rejected = [
            request
            for request, decision in zip(requests, decisions)
            if not decision.allowed or decision.quarantine
        ]
        if rejected:
            receipts = tuple(self.commit(request) for request in rejected)
            raise SandboxGatewayError(
                GatewayErrorCode.QUARANTINED,
                "one or more archive entries failed preflight; no entries were committed",
                operation="archive_import",
                metadata={
                    "archive": inspection.to_dict(),
                    "quarantine_receipts": [item.to_dict() for item in receipts],
                },
            )
        receipts = tuple(self.commit(request) for request in requests)
        return inspection, receipts

    def read(
        self,
        logical_path: str,
        *,
        mount_kind: str = "task",
    ) -> bytes:
        self._require_enabled()
        result = self.workspace_edit_port.read_bytes(
            logical_path,
            mount_kind=self._mount(mount_kind),
        )
        return bytes(result.content)

    def export(
        self,
        *,
        session_id: str,
        logical_path: str,
        source_id: str,
        mount_kind: str = "task",
    ) -> tuple[bytes, ArtifactProvenance]:
        content = self.read(logical_path, mount_kind=mount_kind)
        provenance = ArtifactProvenance.build(
            kind=ProvenanceKind.WORKSPACE,
            trust=TrustLevel.TRUSTED,
            source_id=source_id,
            content_digest_value=content_digest(content),
            metadata={
                "logical_path": logical_path,
                "workspace_id": self.workspace_edit_port.workspace_id,
                "session_id": session_id,
            },
        )
        self.provenance_registry.register(provenance)
        return content, provenance

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "port_id": "GatewayFileArtifactPort",
            "enabled": self.enabled,
            "workspace_owner": "WorkspaceManagerRuntime",
            "write_owner": "WorkspaceEditPort",
            "direct_filesystem_write": False,
            "file_policy": self.file_policy.descriptor(),
            "archive_policy": self.archive_policy.descriptor(),
        }

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_GATEWAY_UNAVAILABLE,
                "GatewayFileArtifactPort is disabled; raw write fallback is forbidden",
                operation="artifact_port",
            )

    @staticmethod
    def _mount(value: str) -> WorkspaceKind:
        try:
            return WorkspaceKind(str(value))
        except ValueError as error:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                f"unsupported workspace mount: {value}",
                operation="artifact_port",
            ) from error
