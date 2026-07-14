from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .integration_models import (
    GatewayDispatchReceipt,
    canonical_json,
    canonical_value,
    content_digest,
    stable_identifier,
)
from .integration_policy import GatewayPolicyConfig, GatewayPolicyRuntime


@dataclass(frozen=True, slots=True)
class GatewayDispatchAttestation:
    receipt: GatewayDispatchReceipt
    envelope_digest: str
    canonical_envelope: Mapping[str, Any]
    accepted: bool
    reason: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.safe_dict(),
            "envelope_digest": self.envelope_digest,
            "canonical_envelope": canonical_value(self.canonical_envelope),
            "accepted": self.accepted,
            "reason": self.reason,
        }


class GatewayDispatchReceiptStore:
    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.path = self.state_root / "dispatch-receipts.jsonl"
        self._lock = threading.RLock()
        self._receipts: dict[str, Mapping[str, Any]] = {}
        self._envelopes: dict[str, str] = {}
        self._load()

    def save(self, attestation: GatewayDispatchAttestation) -> GatewayDispatchAttestation:
        value = attestation.safe_dict()
        receipt_id = attestation.receipt.receipt_id
        envelope_id = attestation.receipt.envelope_id
        with self._lock:
            existing = self._receipts.get(receipt_id)
            if existing is not None:
                if canonical_json(existing) != canonical_json(value):
                    raise ValueError("gateway dispatch receipt collision")
                return attestation
            existing_receipt = self._envelopes.get(envelope_id)
            if existing_receipt and existing_receipt != receipt_id:
                raise ValueError("dispatch envelope already has another receipt")
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
                handle.write("\n")
                handle.flush()
            self._receipts[receipt_id] = value
            self._envelopes[envelope_id] = receipt_id
        return attestation

    def get(self, receipt_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            return self._receipts.get(receipt_id)

    def for_envelope(self, envelope_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            receipt_id = self._envelopes.get(envelope_id)
            return self._receipts.get(receipt_id) if receipt_id else None

    def list(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(self._receipts.values())

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            count = len(self._receipts)
        return {
            "runtime": "GatewayDispatchReceiptStore",
            "receipt_count": count,
            "state_path_digest": content_digest(str(self.path)),
            "raw_workspace_path_exposed": False,
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line)
                receipt = value.get("receipt") or {}
                receipt_id = str(receipt.get("receipt_id") or "")
                envelope_id = str(receipt.get("envelope_id") or "")
                if not receipt_id or not envelope_id:
                    raise ValueError("invalid gateway dispatch receipt record")
                existing = self._receipts.get(receipt_id)
                if existing is not None and canonical_json(existing) != canonical_json(value):
                    raise ValueError("persisted gateway dispatch receipt collision")
                self._receipts[receipt_id] = value
                self._envelopes[envelope_id] = receipt_id


class GatewayDispatchAttestor:
    """Binds scheduler dispatch to the same gateway policy without owning scheduling."""

    def __init__(
        self,
        policy_runtime: GatewayPolicyRuntime,
        store: GatewayDispatchReceiptStore,
        *,
        receipt_ttl_seconds: float = 300.0,
    ) -> None:
        if receipt_ttl_seconds <= 0:
            raise ValueError("receipt_ttl_seconds must be positive")
        self.policy_runtime = policy_runtime
        self.store = store
        self.receipt_ttl_seconds = receipt_ttl_seconds

    @classmethod
    def for_workspace(
        cls,
        *,
        workspace_root: str | Path,
        artifact_root: str | Path,
        receipt_ttl_seconds: float = 300.0,
    ) -> "GatewayDispatchAttestor":
        from .command_policy import StructuredCommandPolicy
        from .file_policy import GatewayFilePolicy

        workspace = Path(workspace_root).resolve()
        artifacts = Path(artifact_root).resolve()
        policy = GatewayPolicyRuntime(
            GatewayPolicyConfig(workspace_root=workspace),
            command_policy=StructuredCommandPolicy(),
            file_policy=GatewayFilePolicy(),
        )
        return cls(
            policy,
            GatewayDispatchReceiptStore(artifacts / ".sandbox-gateway" / "dispatch"),
            receipt_ttl_seconds=receipt_ttl_seconds,
        )

    def attest(self, envelope: Mapping[str, Any]) -> GatewayDispatchAttestation:
        canonical = self._canonical_envelope(envelope)
        decision = self.policy_runtime.evaluate_dispatch(canonical)
        if not decision.allowed:
            raise ValueError(f"gateway dispatch denied: {decision.reason}")
        issued_at = time.time()
        receipt = GatewayDispatchReceipt(
            receipt_id=stable_identifier(
                "gateway-dispatch",
                canonical["envelope_id"],
                decision.subject_digest,
                decision.policy_digest,
            ),
            envelope_id=canonical["envelope_id"],
            run_id=canonical["run_id"],
            task_id=canonical["task_id"],
            worker_id=canonical["runtime_worker"],
            backend=canonical["backend"],
            location=canonical["location"],
            sandbox=canonical["sandbox"],
            gateway=canonical["gateway"],
            workspace_digest=content_digest(canonical["workspace_root"]),
            artifact_root_digest=content_digest(canonical["artifact_root"]),
            policy_digest=decision.policy_digest,
            owner_epoch=int(canonical.get("owner_epoch") or 0),
            backend_generation=int(canonical.get("backend_generation") or 0),
            issued_at=issued_at,
            expires_at=issued_at + self.receipt_ttl_seconds,
            metadata={
                "manifest_id": canonical.get("manifest_id", ""),
                "node_id": canonical.get("node_id", ""),
                "decision_id": canonical.get("decision_id", ""),
                "dispatch_owner": "scheduler",
                "gateway_owner": "SandboxGatewayRuntime",
            },
        )
        attestation = GatewayDispatchAttestation(
            receipt=receipt,
            envelope_digest=content_digest(canonical),
            canonical_envelope={
                key: value
                for key, value in canonical.items()
                if key not in {"workspace_root", "artifact_root"}
            },
            accepted=True,
            reason=decision.reason,
        )
        return self.store.save(attestation)

    def validate(
        self,
        envelope: Mapping[str, Any],
        receipt: GatewayDispatchReceipt,
        *,
        expected_owner_epoch: int | None = None,
        expected_backend_generation: int | None = None,
    ) -> bool:
        receipt.assert_fresh()
        canonical = self._canonical_envelope(envelope)
        decision = self.policy_runtime.evaluate_dispatch(canonical)
        if not decision.allowed:
            return False
        if receipt.envelope_id != canonical["envelope_id"]:
            return False
        if receipt.run_id != canonical["run_id"] or receipt.task_id != canonical["task_id"]:
            return False
        if receipt.worker_id != canonical["runtime_worker"]:
            return False
        if receipt.backend != canonical["backend"] or receipt.location != canonical["location"]:
            return False
        if receipt.sandbox != canonical["sandbox"] or receipt.gateway != canonical["gateway"]:
            return False
        if receipt.workspace_digest != content_digest(canonical["workspace_root"]):
            return False
        if receipt.artifact_root_digest != content_digest(canonical["artifact_root"]):
            return False
        if receipt.policy_digest != decision.policy_digest:
            return False
        if receipt.owner_epoch != int(canonical.get("owner_epoch") or 0):
            return False
        if receipt.backend_generation != int(canonical.get("backend_generation") or 0):
            return False
        if expected_owner_epoch is not None and receipt.owner_epoch != expected_owner_epoch:
            return False
        if expected_backend_generation is not None and receipt.backend_generation != expected_backend_generation:
            return False
        persisted = self.store.get(receipt.receipt_id)
        return persisted is not None

    def bind_metadata(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        attestation = self.attest(envelope)
        return {
            "gateway_receipt_id": attestation.receipt.receipt_id,
            "gateway_receipt_digest": attestation.receipt.receipt_digest,
            "gateway_policy_digest": attestation.receipt.policy_digest,
            "gateway_envelope_digest": attestation.envelope_digest,
            "gateway_receipt_expires_at": attestation.receipt.expires_at,
            "gateway_dispatch_attested": True,
        }

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime": "GatewayDispatchAttestor",
            "receipt_ttl_seconds": self.receipt_ttl_seconds,
            "policy_digest": self.policy_runtime.policy_digest,
            "store": self.store.descriptor(),
            "scheduler_owner_preserved": True,
        }

    @staticmethod
    def _canonical_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
        required = (
            "envelope_id",
            "run_id",
            "task_id",
            "runtime_worker",
            "backend",
            "location",
            "sandbox",
            "gateway",
            "workspace_root",
            "artifact_root",
        )
        canonical = {str(key): canonical_value(value) for key, value in envelope.items()}
        missing = [key for key in required if not str(canonical.get(key) or "")]
        if missing:
            raise ValueError(f"dispatch envelope is missing: {', '.join(missing)}")
        return canonical


__all__ = [
    "GatewayDispatchAttestation",
    "GatewayDispatchAttestor",
    "GatewayDispatchReceiptStore",
]
