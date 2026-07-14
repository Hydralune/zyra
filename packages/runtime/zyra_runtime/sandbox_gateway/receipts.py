from __future__ import annotations

from typing import Any, Iterable, Mapping

from .canonical import digest, stable_id
from .models import (
    CommandPolicyDecision,
    CommandReceipt,
    GatewayCommandEnvelope,
    PatchReceipt,
    PermissionBinding,
    ProcessResult,
)
from .redaction import SecretRedactor
from .state_store import GatewayStateStore


class ReceiptLedger:
    """Persists redacted evidence receipts; it never stores execution grants."""

    def __init__(
        self,
        store: GatewayStateStore,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.store = store
        self.redactor = redactor or SecretRedactor()

    def command(
        self,
        envelope: GatewayCommandEnvelope,
        policy: CommandPolicyDecision,
        binding: PermissionBinding,
        result: ProcessResult,
        *,
        patch_receipt: PatchReceipt | None = None,
        artifact_refs: Iterable[str] = (),
        event_refs: Iterable[str] = (),
        owner_epoch_after: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CommandReceipt:
        receipt_id = stable_id(
            "gateway-command-receipt",
            envelope.command_id,
            envelope.identity_digest,
            policy.policy_digest,
            binding.binding_id,
            binding.consumption_id,
            result.to_dict(),
            patch_receipt.receipt_id if patch_receipt else "",
        )
        projected_metadata = self.redactor.redact_value(
            dict(metadata or {}),
            source="command_receipt",
        ).value
        receipt = CommandReceipt(
            receipt_id=receipt_id,
            command_id=envelope.command_id,
            session_id=envelope.session_id,
            command_digest=envelope.identity_digest,
            policy_digest=policy.policy_digest,
            permission_binding_id=binding.binding_id,
            permission_consumption_id=binding.consumption_id,
            result=result,
            workspace_id=envelope.workspace_id,
            owner_epoch_before=envelope.owner_epoch,
            owner_epoch_after=(
                envelope.owner_epoch
                if owner_epoch_after is None
                else int(owner_epoch_after)
            ),
            patch_receipt_id=patch_receipt.receipt_id if patch_receipt else "",
            artifact_refs=tuple(str(item) for item in artifact_refs),
            event_refs=tuple(str(item) for item in event_refs),
            metadata=dict(projected_metadata),
        )
        self.store.save_receipt(
            receipt.receipt_id,
            receipt.to_dict(),
            idempotency_key=envelope.idempotency_key,
        )
        return receipt

    def patch(self, receipt: PatchReceipt, *, idempotency_key: str) -> PatchReceipt:
        self.store.save_receipt(
            receipt.receipt_id,
            receipt.to_dict(),
            idempotency_key=idempotency_key,
        )
        return receipt

    def find_idempotent(self, key: str) -> Mapping[str, Any] | None:
        return self.store.receipt_for_idempotency(key)

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "ledger": "GatewayStateStore.receipts",
            "stores_execution_grants": False,
            "redaction": type(self.redactor).__name__,
            "content_addressed": True,
            "descriptor_digest": digest(
                {
                    "ledger": "GatewayStateStore.receipts",
                    "stores_execution_grants": False,
                    "redaction": type(self.redactor).__name__,
                }
            ),
        }
