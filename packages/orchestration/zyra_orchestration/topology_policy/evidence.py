from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType

from .contracts import (
    PolicyContract,
    PolicyDecisionReceipt,
    PolicyOutcome,
    StableArtifactRef,
    canonical_json,
    parse_policy_contract,
)


class ArtifactStorePort(Protocol):
    def write_text(
        self,
        *,
        run_id: str,
        task_id: str,
        content: str,
        title: str,
        kind: ArtifactKind,
        extension: str,
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        encoding: str = "utf-8",
    ) -> ArtifactRef: ...

    def verify(
        self,
        artifact: ArtifactRef,
        *,
        expected_revision: str | None = None,
    ) -> Any: ...

    def iter_bytes(
        self,
        artifact: ArtifactRef,
        *,
        expected_revision: str | None = None,
        chunk_bytes: int = 256 * 1024,
    ) -> Iterator[bytes]: ...


@dataclass(frozen=True, slots=True)
class PublishedPolicyEvidence:
    artifact: ArtifactRef
    artifact_ref: StableArtifactRef
    event: EventRecord


class PolicyEvidencePublisher:
    """Persists through the existing artifact/event owners and verifies replay."""

    def __init__(
        self,
        artifact_store: ArtifactStorePort,
        *,
        admit_event: Callable[[EventRecord], None],
    ) -> None:
        self.artifact_store = artifact_store
        self.admit_event = admit_event

    def publish(
        self,
        contract: PolicyContract,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None = None,
    ) -> PublishedPolicyEvidence:
        artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=canonical_json(contract.to_dict()),
            title=f"{contract.CONTRACT_KIND} {contract.header.contract_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
            metadata={
                "policy_contract_kind": contract.CONTRACT_KIND,
                "policy_schema_version": contract.SCHEMA_VERSION,
                "policy_contract_id": contract.header.contract_id,
                "policy_contract_digest": contract.digest,
                "source_event_id": contract.header.source_event_id,
                "correlation_id": contract.header.correlation_id,
                "causation_id": contract.header.causation_id,
                "idempotency_key": contract.header.idempotency_key,
            },
        )
        artifact_digest = str(artifact.metadata.get("sha256") or "")
        stable = StableArtifactRef(
            ref_id=artifact.artifact_id,
            uri=artifact.uri,
            digest=artifact_digest,
            media_type=str(artifact.metadata.get("media_type") or "application/json"),
        )
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=producer_node_id,
            event_type=self._event_type(contract),
            payload={
                "schema": "zyra.policy-contract-event/v1",
                "policy_contract_kind": contract.CONTRACT_KIND,
                "policy_schema_version": contract.SCHEMA_VERSION,
                "policy_contract_id": contract.header.contract_id,
                "policy_contract_digest": contract.digest,
                "policy_artifact": stable.to_dict(),
                "source_event_id": contract.header.source_event_id,
                "correlation_id": contract.header.correlation_id,
                "causation_id": contract.header.causation_id,
                "idempotency_key": contract.header.idempotency_key,
            },
        )
        self.admit_event(event)
        return PublishedPolicyEvidence(artifact=artifact, artifact_ref=stable, event=event)

    def replay(self, artifact: ArtifactRef) -> PolicyContract:
        observed = self.artifact_store.verify(
            artifact,
            expected_revision=str(artifact.metadata.get("revision") or "") or None,
        )
        expected_digest = str(artifact.metadata.get("sha256") or "")
        if expected_digest and getattr(observed, "sha256", "") != expected_digest:
            raise ValueError("policy artifact observed digest differs from committed metadata")
        raw = b"".join(
            self.artifact_store.iter_bytes(
                artifact,
                expected_revision=str(artifact.metadata.get("revision") or "") or None,
            )
        )
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("policy artifact is not a JSON object")
        contract = parse_policy_contract(value)
        metadata_contract_digest = str(
            artifact.metadata.get("policy_contract_digest") or ""
        )
        if metadata_contract_digest and contract.digest != metadata_contract_digest:
            raise ValueError("policy contract digest differs from artifact metadata")
        return contract

    @staticmethod
    def _event_type(contract: PolicyContract) -> EventType:
        if isinstance(contract, PolicyDecisionReceipt):
            return EventType.CONSTRAINT_CHECK
        if isinstance(contract, PolicyOutcome):
            return EventType.EVALUATION
        return EventType.ARTIFACT_WRITTEN
