from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import (
    CAUSAL_RECEIPT_SCHEMA,
    CanonicalEventEnvelope,
    CausalEffectKind,
    CausalEventContract,
    CausalVerification,
    EventSemanticClass,
    MutationIdentity,
    MutationReceipt,
    OwnerLease,
    ProductizationContractError,
    RuntimeDomain,
    canonicalize,
    digest_payload,
    stable_id,
)
from .owner_registry import RuntimeOwnerRegistry


MAX_MUTATION_RECEIPTS = 8_192
MAX_EVENT_ENVELOPES = 16_384
MAX_VERIFICATIONS = 16_384


def default_causal_contracts() -> tuple[CausalEventContract, ...]:
    return (
        CausalEventContract(
            link_id="session.turn.completed",
            domain=RuntimeDomain.SESSION_EVENT_PROJECTION,
            event_name="runtime.turn.completed",
            effect_kind=CausalEffectKind.STATE_MUTATION,
            required_attributes=(
                "run_id",
                "task_id",
                "session_id",
                "sequence",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "session_id": ("sessionId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="permission.decision.recorded",
            domain=RuntimeDomain.PERMISSION,
            event_name="permission.decision.recorded",
            effect_kind=CausalEffectKind.PERMISSION,
            required_attributes=(
                "run_id",
                "task_id",
                "decision_id",
                "disposition",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "decision_id": ("decisionId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="memory.candidate.committed",
            domain=RuntimeDomain.MEMORY_COMPACT,
            event_name="memory.committed",
            effect_kind=CausalEffectKind.COMPACT,
            required_attributes=(
                "run_id",
                "task_id",
                "candidate_id",
                "revision",
                "mutation_id",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "candidate_id": ("candidateId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="recovery.plan.applied",
            domain=RuntimeDomain.SCHEDULER_RECOVERY,
            event_name="recovery_applied",
            effect_kind=CausalEffectKind.RECOVERY,
            required_attributes=(
                "run_id",
                "task_id",
                "plan_id",
                "attempt_id",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "plan_id": ("planId",),
                "attempt_id": ("attemptId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="artifact.content.committed",
            domain=RuntimeDomain.ARTIFACT,
            event_name="artifact.created",
            effect_kind=CausalEffectKind.ARTIFACT,
            required_attributes=(
                "run_id",
                "task_id",
                "artifact_id",
                "digest",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "artifact_id": ("artifactId", "artifact_ref"),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="worker.dispatch.bound",
            domain=RuntimeDomain.WORKER_ROUTE,
            event_name="worker_dispatch_started",
            effect_kind=CausalEffectKind.PLACEMENT,
            required_attributes=(
                "run_id",
                "task_id",
                "backend_id",
                "lease_id",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "backend_id": ("backendId", "backend"),
                "lease_id": ("leaseId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="graph.snapshot.committed",
            domain=RuntimeDomain.GRAPH_CHECKPOINT,
            event_name="graph_commit",
            effect_kind=CausalEffectKind.STATE_MUTATION,
            required_attributes=(
                "run_id",
                "task_id",
                "graph_id",
                "revision",
                "mutation_id",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "graph_id": ("graphId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="provider.dispatch.completed",
            domain=RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER,
            event_name="provider.dispatch.completed",
            effect_kind=CausalEffectKind.ROUTE,
            required_attributes=(
                "run_id",
                "task_id",
                "provider_id",
                "model_id",
                "dispatch_id",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "provider_id": ("providerId",),
                "model_id": ("modelId",),
                "dispatch_id": ("dispatchId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="mcp.request.journaled",
            domain=RuntimeDomain.MCP_PLUGIN_REGISTRY,
            event_name="mcp.request.completed",
            effect_kind=CausalEffectKind.TOOL,
            required_attributes=(
                "run_id",
                "task_id",
                "request_id",
                "session_id",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "request_id": ("requestId",),
                "session_id": ("sessionId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="gateway.session.recovered",
            domain=RuntimeDomain.GATEWAY_LEASE_BUSY,
            event_name="session_recovered",
            effect_kind=CausalEffectKind.RECOVERY,
            required_attributes=(
                "run_id",
                "task_id",
                "session_id",
                "lease_id",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "session_id": ("sessionId",),
                "lease_id": ("leaseId",),
                "mutation_id": ("mutationId",),
            },
        ),
        CausalEventContract(
            link_id="browser.session.started",
            domain=RuntimeDomain.TERMINAL_BROWSER_SESSION,
            event_name="browser.session.started",
            effect_kind=CausalEffectKind.STATE_MUTATION,
            required_attributes=(
                "run_id",
                "task_id",
                "session_id",
                "generation",
                "mutation_id",
                "revision",
            ),
            aliases={
                "run_id": ("runId",),
                "task_id": ("taskId",),
                "session_id": ("sessionId",),
                "mutation_id": ("mutationId",),
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class CausalLedgerSnapshot:
    mutation_receipts: tuple[MutationReceipt, ...]
    events: tuple[CanonicalEventEnvelope, ...]
    verifications: tuple[CausalVerification, ...]
    duplicate_receipts: int
    rejected_events: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAUSAL_RECEIPT_SCHEMA,
            "mutation_receipts": [item.to_dict() for item in self.mutation_receipts],
            "events": [item.to_dict() for item in self.events],
            "verifications": [item.to_dict() for item in self.verifications],
            "duplicate_receipts": self.duplicate_receipts,
            "rejected_events": self.rejected_events,
            "digest": self.digest,
        }


class CausalReceiptLedger:
    """Derived receipt ledger that verifies effects without owning them."""

    def __init__(
        self,
        owner_registry: RuntimeOwnerRegistry,
        *,
        clock: Callable[[], int] = time.time_ns,
        max_mutations: int = MAX_MUTATION_RECEIPTS,
        max_events: int = MAX_EVENT_ENVELOPES,
        max_verifications: int = MAX_VERIFICATIONS,
        enabled: bool = True,
    ) -> None:
        if min(max_mutations, max_events, max_verifications) < 1:
            raise ProductizationContractError(
                "causal_ledger_capacity_invalid",
                "causal ledger capacities must be positive",
            )
        self.owner_registry = owner_registry
        self.clock = clock
        self.enabled = bool(enabled)
        self._mutations: dict[str, MutationReceipt] = {}
        self._mutation_order: deque[str] = deque(maxlen=max_mutations)
        self._events: dict[str, CanonicalEventEnvelope] = {}
        self._event_order: deque[str] = deque(maxlen=max_events)
        self._verifications: dict[str, CausalVerification] = {}
        self._verification_order: deque[str] = deque(maxlen=max_verifications)
        self._effect_refs: dict[tuple[RuntimeDomain, str], str] = {}
        self._revision_heads: dict[tuple[RuntimeDomain, str, str], int] = {}
        self._duplicate_receipts = 0
        self._rejected_events = 0
        self._lock = threading.RLock()

    def disable(self) -> None:
        with self._lock:
            self.enabled = False

    def enable(self) -> None:
        with self._lock:
            self.enabled = True

    def commit(
        self,
        lease: OwnerLease,
        *,
        identity: MutationIdentity,
        effect_kind: CausalEffectKind,
        effect_ref: str,
        effect_payload: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> MutationReceipt:
        self._require_enabled()
        binding = self.owner_registry.validate_lease(lease)
        self.owner_registry.assert_lease_owner(
            lease,
            owner_token=binding.owner_token,
            generation=identity.owner_generation,
        )
        if identity.run_id != str(metadata.get("run_id", identity.run_id) if metadata else identity.run_id):
            raise ProductizationContractError(
                "mutation_run_identity_mismatch",
                "mutation metadata attempted to rebind run identity",
            )
        effect_ref_value = str(effect_ref).strip()
        if not effect_ref_value:
            raise ProductizationContractError(
                "mutation_effect_ref_missing",
                "canonical mutation receipt requires an effect reference",
            )
        effect_digest = digest_payload(effect_payload)
        receipt_id = stable_id(
            "mutation",
            lease.domain.value,
            binding.owner_token,
            identity.to_dict(),
            effect_kind.value,
            effect_ref_value,
            effect_digest,
        )
        receipt = MutationReceipt(
            receipt_id=receipt_id,
            domain=lease.domain,
            owner_token=binding.owner_token,
            identity=identity,
            effect_kind=effect_kind,
            effect_ref=effect_ref_value,
            effect_digest=effect_digest,
            committed_at_ns=self.clock(),
            metadata=dict(metadata or {}),
        )
        revision_key = (lease.domain, identity.run_id, identity.task_id)
        effect_key = (lease.domain, effect_ref_value)
        with self._lock:
            existing = self._mutations.get(receipt_id)
            if existing is not None:
                if existing != receipt:
                    raise ProductizationContractError(
                        "mutation_receipt_rebound",
                        f"mutation receipt {receipt_id} was rebound",
                    )
                self._duplicate_receipts += 1
                return existing
            existing_effect = self._effect_refs.get(effect_key)
            if existing_effect and existing_effect != receipt_id:
                prior = self._mutations.get(existing_effect)
                if prior is not None and prior.effect_digest != effect_digest:
                    raise ProductizationContractError(
                        "mutation_effect_ref_reused",
                        f"effect reference {effect_ref_value} was reused with new bytes",
                    )
            head = self._revision_heads.get(revision_key, 0)
            if identity.revision <= head:
                raise ProductizationContractError(
                    "mutation_revision_not_monotonic",
                    "canonical mutation revision must advance monotonically",
                    details={
                        "domain": lease.domain.value,
                        "head": head,
                        "proposed": identity.revision,
                    },
                )
            self._evict_mutation_if_full()
            self._mutations[receipt_id] = receipt
            self._mutation_order.append(receipt_id)
            self._effect_refs[effect_key] = receipt_id
            self._revision_heads[revision_key] = identity.revision
            return receipt

    def record_external_receipt(self, receipt: MutationReceipt) -> MutationReceipt:
        """Admit an owner-produced receipt after registry/token validation."""

        self._require_enabled()
        binding = self.owner_registry.binding(receipt.domain)
        if receipt.owner_token != binding.owner_token:
            raise ProductizationContractError(
                "external_receipt_owner_mismatch",
                "external mutation receipt does not belong to the selected owner",
            )
        result = self.owner_registry.require_ready(receipt.domain)
        if result.generation != receipt.identity.owner_generation:
            raise ProductizationContractError(
                "external_receipt_generation_mismatch",
                "external mutation receipt belongs to another owner generation",
            )
        revision_key = (receipt.domain, receipt.identity.run_id, receipt.identity.task_id)
        effect_key = (receipt.domain, receipt.effect_ref)
        with self._lock:
            existing = self._mutations.get(receipt.receipt_id)
            if existing is not None:
                if existing != receipt:
                    raise ProductizationContractError(
                        "external_receipt_rebound",
                        f"external receipt {receipt.receipt_id} changed",
                    )
                self._duplicate_receipts += 1
                return existing
            head = self._revision_heads.get(revision_key, 0)
            if receipt.identity.revision <= head:
                raise ProductizationContractError(
                    "external_receipt_revision_stale",
                    "external mutation receipt is not newer than the domain head",
                )
            self._evict_mutation_if_full()
            self._mutations[receipt.receipt_id] = receipt
            self._mutation_order.append(receipt.receipt_id)
            self._effect_refs[effect_key] = receipt.receipt_id
            self._revision_heads[revision_key] = receipt.identity.revision
            return receipt

    def record_event(self, event: CanonicalEventEnvelope) -> CanonicalEventEnvelope:
        self._require_enabled()
        if event.semantic_class.counts_as_effect and not event.mutation_receipt_id:
            self._rejected_events += 1
            raise ProductizationContractError(
                "canonical_event_receipt_missing",
                f"{event.event_name} claims a canonical effect without a mutation receipt",
            )
        if event.mutation_receipt_id:
            with self._lock:
                receipt = self._mutations.get(event.mutation_receipt_id)
            if receipt is None:
                self._rejected_events += 1
                raise ProductizationContractError(
                    "canonical_event_receipt_unknown",
                    f"{event.event_name} references an unknown mutation receipt",
                )
            if receipt.domain is not event.domain:
                self._rejected_events += 1
                raise ProductizationContractError(
                    "canonical_event_domain_mismatch",
                    "event and mutation receipt belong to different state domains",
                )
            if event.emitted_at_ns < receipt.committed_at_ns:
                self._rejected_events += 1
                raise ProductizationContractError(
                    "canonical_event_precedes_effect",
                    "canonical event was emitted before the state effect committed",
                )
        with self._lock:
            existing = self._events.get(event.event_id)
            if existing is not None:
                if existing != event:
                    raise ProductizationContractError(
                        "canonical_event_rebound",
                        f"event {event.event_id} changed during replay",
                    )
                return existing
            self._evict_event_if_full()
            self._events[event.event_id] = event
            self._event_order.append(event.event_id)
            return event

    def mutation(self, receipt_id: str) -> MutationReceipt | None:
        with self._lock:
            return self._mutations.get(receipt_id)

    def event(self, event_id: str) -> CanonicalEventEnvelope | None:
        with self._lock:
            return self._events.get(event_id)

    def snapshot(self) -> CausalLedgerSnapshot:
        with self._lock:
            mutations = tuple(
                self._mutations[item]
                for item in self._mutation_order
                if item in self._mutations
            )
            events = tuple(
                self._events[item]
                for item in self._event_order
                if item in self._events
            )
            verifications = tuple(
                self._verifications[item]
                for item in self._verification_order
                if item in self._verifications
            )
            payload = {
                "schema": CAUSAL_RECEIPT_SCHEMA,
                "mutation_receipts": [item.to_dict() for item in mutations],
                "events": [item.to_dict() for item in events],
                "verifications": [item.to_dict() for item in verifications],
                "duplicate_receipts": self._duplicate_receipts,
                "rejected_events": self._rejected_events,
            }
            return CausalLedgerSnapshot(
                mutation_receipts=mutations,
                events=events,
                verifications=verifications,
                duplicate_receipts=self._duplicate_receipts,
                rejected_events=self._rejected_events,
                digest=digest_payload(payload),
            )

    def store_verification(self, verification: CausalVerification) -> None:
        with self._lock:
            existing = self._verifications.get(verification.digest)
            if existing is not None and existing != verification:
                raise ProductizationContractError(
                    "causal_verification_digest_collision",
                    "causal verification digest collision",
                )
            if existing is not None:
                return
            self._evict_verification_if_full()
            self._verifications[verification.digest] = verification
            self._verification_order.append(verification.digest)

    def _require_enabled(self) -> None:
        with self._lock:
            if not self.enabled:
                raise ProductizationContractError(
                    "causal_receipt_ledger_disabled",
                    "causal receipt validation is disabled; fallback event success is forbidden",
                )

    def _evict_mutation_if_full(self) -> None:
        if len(self._mutation_order) < self._mutation_order.maxlen:
            return
        oldest = self._mutation_order.popleft()
        removed = self._mutations.pop(oldest, None)
        if removed is not None:
            self._effect_refs.pop((removed.domain, removed.effect_ref), None)

    def _evict_event_if_full(self) -> None:
        if len(self._event_order) < self._event_order.maxlen:
            return
        oldest = self._event_order.popleft()
        self._events.pop(oldest, None)

    def _evict_verification_if_full(self) -> None:
        if len(self._verification_order) < self._verification_order.maxlen:
            return
        oldest = self._verification_order.popleft()
        self._verifications.pop(oldest, None)


class CausalEffectVerifier:
    def __init__(
        self,
        ledger: CausalReceiptLedger,
        contracts: Iterable[CausalEventContract] = (),
    ) -> None:
        self.ledger = ledger
        self._contracts_by_link: dict[str, CausalEventContract] = {}
        self._contracts_by_event: dict[str, list[CausalEventContract]] = defaultdict(list)
        for contract in contracts or default_causal_contracts():
            self.register(contract)

    def register(self, contract: CausalEventContract) -> None:
        if contract.link_id in self._contracts_by_link:
            raise ProductizationContractError(
                "causal_contract_duplicate",
                f"duplicate causal link id: {contract.link_id}",
            )
        self._contracts_by_link[contract.link_id] = contract
        self._contracts_by_event[contract.event_name].append(contract)

    def contract(self, link_id: str) -> CausalEventContract:
        try:
            return self._contracts_by_link[link_id]
        except KeyError as error:
            raise ProductizationContractError(
                "causal_contract_unknown",
                f"unknown causal link: {link_id}",
            ) from error

    def verify(
        self,
        link_id: str,
        event: CanonicalEventEnvelope,
    ) -> CausalVerification:
        contract = self.contract(link_id)
        missing: list[str] = []
        mismatches: list[str] = []
        if event.event_name != contract.event_name:
            mismatches.append(
                f"event_name expected={contract.event_name} actual={event.event_name}"
            )
        if event.domain is not contract.domain:
            mismatches.append(
                f"domain expected={contract.domain.value} actual={event.domain.value}"
            )
        if not event.semantic_class.counts_as_effect:
            mismatches.append(
                f"semantic_class={event.semantic_class.value} cannot prove an effect"
            )
        receipt = (
            self.ledger.mutation(event.mutation_receipt_id)
            if event.mutation_receipt_id
            else None
        )
        if receipt is None:
            mismatches.append("mutation receipt is missing")
        else:
            if receipt.domain is not contract.domain:
                mismatches.append("mutation receipt domain differs from contract")
            if receipt.effect_kind is not contract.effect_kind:
                mismatches.append(
                    f"effect_kind expected={contract.effect_kind.value} "
                    f"actual={receipt.effect_kind.value}"
                )
            self._verify_identity(event.attributes, receipt, mismatches)
        normalized_attributes: dict[str, Any] = {}
        for attribute in contract.required_attributes:
            found = False
            for accepted in contract.accepted_names(attribute):
                if accepted not in event.attributes:
                    continue
                value = event.attributes[accepted]
                if value is None or value == "":
                    continue
                normalized_attributes[attribute] = value
                found = True
                break
            if not found:
                missing.append(attribute)
        if receipt is not None:
            self._verify_required_against_receipt(
                normalized_attributes,
                receipt,
                mismatches,
            )
        valid = not missing and not mismatches
        payload = {
            "link_id": link_id,
            "valid": valid,
            "event_id": event.event_id,
            "mutation_receipt_id": event.mutation_receipt_id,
            "missing_attributes": missing,
            "mismatches": mismatches,
            "reason": "causal effect verified" if valid else "causal effect rejected",
        }
        verification = CausalVerification(
            link_id=link_id,
            valid=valid,
            event_id=event.event_id,
            mutation_receipt_id=event.mutation_receipt_id,
            missing_attributes=tuple(missing),
            mismatches=tuple(mismatches),
            reason=str(payload["reason"]),
            digest=digest_payload(payload),
        )
        self.ledger.store_verification(verification)
        return verification

    def verify_all(self) -> tuple[CausalVerification, ...]:
        snapshot = self.ledger.snapshot()
        by_name: dict[str, list[CanonicalEventEnvelope]] = defaultdict(list)
        for event in snapshot.events:
            by_name[event.event_name].append(event)
        results: list[CausalVerification] = []
        for link_id in sorted(self._contracts_by_link):
            contract = self._contracts_by_link[link_id]
            candidates = by_name.get(contract.event_name, ())
            if not candidates:
                payload = {
                    "link_id": link_id,
                    "valid": False,
                    "event_id": "",
                    "mutation_receipt_id": "",
                    "missing_attributes": list(contract.required_attributes),
                    "mismatches": ["canonical event was not observed"],
                    "reason": "causal event missing",
                }
                verification = CausalVerification(
                    link_id=link_id,
                    valid=False,
                    event_id="",
                    mutation_receipt_id="",
                    missing_attributes=contract.required_attributes,
                    mismatches=("canonical event was not observed",),
                    reason="causal event missing",
                    digest=digest_payload(payload),
                )
                self.ledger.store_verification(verification)
                results.append(verification)
                continue
            verified = [self.verify(link_id, event) for event in candidates]
            preferred = next((item for item in reversed(verified) if item.valid), verified[-1])
            results.append(preferred)
        return tuple(results)

    def classify_unregistered(
        self,
        event_name: str,
        attributes: Mapping[str, Any],
    ) -> EventSemanticClass:
        name = str(event_name).strip().casefold()
        path = str(attributes.get("source_path") or "").replace("\\", "/").casefold()
        if not name:
            return EventSemanticClass.UNKNOWN
        if name in self._contracts_by_event:
            return EventSemanticClass.CANONICAL_EFFECT
        if (
            path.startswith(("apps/web/", "packages/core/typed-api-client/"))
            or path.startswith("packages/evaluation/")
            or any(
                marker in path
                for marker in (
                    "/stdio",
                    "/source_audit",
                    "/integration-contracts",
                    "/omp-rpc-mapper",
                    "/subagents/typescript_port",
                    "/permission/command-risk-runtime",
                    "/tasks/registry",
                    "/e01/coordinator",
                )
            )
        ):
            return EventSemanticClass.DERIVED_PROJECTION
        if any(token in name for token in ("heartbeat", "keepalive", "ping", "pong")):
            return EventSemanticClass.HEARTBEAT
        if any(token in name for token in ("replay", "snapshot.loaded", "cursor")):
            return EventSemanticClass.REPLAY
        if any(token in name for token in ("render", "panel", "selected", "tab.", "ui.")):
            return EventSemanticClass.UI_ONLY
        if any(
            token in name
            for token in (
                "diagnostic",
                "observed",
                "log",
                "trace",
                "metric.sample",
                "progress",
                "started",
                "proposed",
                "failed",
                "fenced",
                "ready",
                "cache_hit",
                "request",
            )
        ):
            return EventSemanticClass.DIAGNOSTIC
        if path.endswith(
            (
                "code_index/worker.py",
                "memory/zyra_memory/index_worker.py",
                "provider-control-plane/src/control-plane.ts",
                "workspace/zyra_workspace/manager.py",
            )
        ):
            return EventSemanticClass.DERIVED_PROJECTION
        if bool(attributes.get("canonical_mutation")) and bool(
            attributes.get("mutation_receipt_id")
        ):
            return EventSemanticClass.CANONICAL_EFFECT
        if bool(attributes.get("projection_only")) or bool(attributes.get("derived")):
            return EventSemanticClass.DERIVED_PROJECTION
        return EventSemanticClass.UNKNOWN

    @staticmethod
    def _verify_identity(
        attributes: Mapping[str, Any],
        receipt: MutationReceipt,
        mismatches: list[str],
    ) -> None:
        identity = receipt.identity
        comparisons = (
            (("run_id", "runId"), identity.run_id, "run_id"),
            (("task_id", "taskId"), identity.task_id, "task_id"),
            (("mutation_id", "mutationId"), identity.mutation_id, "mutation_id"),
            (("revision",), identity.revision, "revision"),
            (("correlation_id", "correlationId"), identity.correlation_id, "correlation_id"),
            (("causation_id", "causationId"), identity.causation_id, "causation_id"),
        )
        for aliases, expected, label in comparisons:
            actual = next(
                (
                    attributes[name]
                    for name in aliases
                    if name in attributes and attributes[name] not in {None, ""}
                ),
                None,
            )
            if actual is None:
                continue
            if str(actual) != str(expected):
                mismatches.append(
                    f"{label} expected={expected!s} actual={actual!s}"
                )

    @staticmethod
    def _verify_required_against_receipt(
        attributes: Mapping[str, Any],
        receipt: MutationReceipt,
        mismatches: list[str],
    ) -> None:
        identity = receipt.identity
        expected = {
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "mutation_id": identity.mutation_id,
            "revision": identity.revision,
        }
        for key, value in expected.items():
            actual = attributes.get(key)
            if actual is None:
                continue
            if str(actual) != str(value):
                mismatches.append(
                    f"{key} does not match mutation receipt: {actual!s} != {value!s}"
                )
        digest_value = attributes.get("digest")
        if digest_value is not None and str(digest_value) != receipt.effect_digest:
            metadata_digest = str(receipt.metadata.get("content_digest") or "")
            if str(digest_value) != metadata_digest:
                mismatches.append("event digest does not match committed effect")


def envelope_for_receipt(
    receipt: MutationReceipt,
    *,
    event_name: str,
    attributes: Mapping[str, Any],
    emitted_at_ns: int | None = None,
) -> CanonicalEventEnvelope:
    merged = {
        **dict(attributes),
        "run_id": receipt.identity.run_id,
        "task_id": receipt.identity.task_id,
        "mutation_id": receipt.identity.mutation_id,
        "revision": receipt.identity.revision,
        "correlation_id": receipt.identity.correlation_id,
        "causation_id": receipt.identity.causation_id,
        "effect_ref": receipt.effect_ref,
        "effect_digest": receipt.effect_digest,
    }
    clock_value = time.time_ns() if emitted_at_ns is None else int(emitted_at_ns)
    if clock_value < receipt.committed_at_ns:
        clock_value = receipt.committed_at_ns
    return CanonicalEventEnvelope(
        event_id=stable_id(
            "canonical_event",
            event_name,
            receipt.receipt_id,
            merged,
        ),
        event_name=event_name,
        domain=receipt.domain,
        attributes=canonicalize(merged),
        emitted_at_ns=clock_value,
        semantic_class=EventSemanticClass.CANONICAL_EFFECT,
        mutation_receipt_id=receipt.receipt_id,
    )
