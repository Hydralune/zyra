from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import RecoveryContext, RecoverySignal, stable_digest, utc_now


class RecoveryStateFusionError(RuntimeError):
    pass


class RecoveryStateQuorumError(RecoveryStateFusionError):
    pass


class RecoveryStateScopeError(RecoveryStateFusionError):
    pass


class RecoveryStateFamily(StrEnum):
    SESSION = "session"
    PERMISSION = "permission"
    WORKER = "worker"
    BACKEND = "backend"
    PROVIDER = "provider"
    CHECKPOINT = "checkpoint"
    MEMORY = "memory"
    PROCEDURE = "procedure"
    FAILURE_HISTORY = "failure_history"
    ARTIFACT = "artifact"
    GRAPH = "graph"
    TOOL = "tool"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class StateEvidence:
    family: RecoveryStateFamily
    owner: str
    run_id: str
    task_id: str
    revision: str
    observed_at: str
    state: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    canonical: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return stable_digest({
            "family": self.family.value,
            "owner": self.owner,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "state": dict(self.state),
            "evidence_refs": list(self.evidence_refs),
            "canonical": self.canonical,
        })

    def age(self, now: datetime | None = None) -> timedelta:
        selected = now or datetime.now(UTC)
        try:
            observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
        except ValueError:
            return timedelta.max
        return max(selected - observed, timedelta())

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "owner": self.owner,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "observed_at": self.observed_at,
            "state": copy.deepcopy(dict(self.state)),
            "evidence_refs": list(self.evidence_refs),
            "canonical": self.canonical,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "digest": self.digest,
        }


class StateEvidencePort(Protocol):
    family: RecoveryStateFamily
    owner: str

    def snapshot(self, signal: RecoverySignal) -> StateEvidence | Mapping[str, Any]: ...


class CallbackStateEvidencePort:
    def __init__(
        self,
        family: RecoveryStateFamily,
        owner: str,
        callback: Callable[[RecoverySignal], StateEvidence | Mapping[str, Any]],
    ) -> None:
        self.family = family
        self.owner = str(owner).strip()
        if not self.owner:
            raise ValueError("state evidence owner is required")
        self._callback = callback

    def snapshot(self, signal: RecoverySignal) -> StateEvidence:
        value = self._callback(signal)
        if isinstance(value, StateEvidence):
            if value.owner != self.owner or value.family is not self.family:
                raise RecoveryStateScopeError("state evidence port returned another owner or family")
            return value
        result = copy.deepcopy(dict(value))
        state = copy.deepcopy(dict(result.pop("state", result)))
        return StateEvidence(
            family=self.family,
            owner=self.owner,
            run_id=str(result.get("run_id") or signal.refs.run_id),
            task_id=str(result.get("task_id") or signal.refs.task_id),
            revision=str(result.get("revision") or result.get("version") or state.get("revision") or ""),
            observed_at=str(result.get("observed_at") or result.get("updated_at") or utc_now()),
            state=state,
            evidence_refs=tuple(result.get("evidence_refs") or ()),
            canonical=bool(result.get("canonical", True)),
            metadata=dict(result.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class StateFusionPolicy:
    minimum_families: int = 3
    maximum_age_seconds: int = 300
    required_families: tuple[RecoveryStateFamily, ...] = ()
    fail_on_noncanonical: bool = True
    fail_on_owner_error: bool = True
    allow_stale_families: tuple[RecoveryStateFamily, ...] = (RecoveryStateFamily.MEMORY, RecoveryStateFamily.PROCEDURE)
    maximum_evidence_per_family: int = 8
    maximum_state_keys: int = 256

    def __post_init__(self) -> None:
        if self.minimum_families < 3:
            raise ValueError("recovery state fusion requires at least three state families")
        if self.maximum_age_seconds < 0:
            raise ValueError("maximum evidence age cannot be negative")
        if self.maximum_evidence_per_family < 1 or self.maximum_state_keys < 1:
            raise ValueError("state fusion bounds must be positive")


@dataclass(frozen=True, slots=True)
class StateFusionFailure:
    family: RecoveryStateFamily
    owner: str
    code: str
    message: str
    blocking: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "owner": self.owner,
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class FusedRecoveryState:
    signal_id: str
    run_id: str
    task_id: str
    context: RecoveryContext
    evidence: tuple[StateEvidence, ...]
    failures: tuple[StateFusionFailure, ...]
    families: tuple[RecoveryStateFamily, ...]
    fused_at: str = field(default_factory=utc_now)

    @property
    def quorum_satisfied(self) -> bool:
        return len(self.families) >= 3 and not any(item.blocking for item in self.failures)

    @property
    def digest(self) -> str:
        return stable_digest({
            "signal_id": self.signal_id,
            "context": self.context.to_dict(),
            "evidence": [item.digest for item in self.evidence],
            "failures": [item.to_dict() for item in self.failures],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fused-recovery-state/v1",
            "signal_id": self.signal_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "context": self.context.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "failures": [item.to_dict() for item in self.failures],
            "families": [item.value for item in self.families],
            "quorum_satisfied": self.quorum_satisfied,
            "fused_at": self.fused_at,
            "digest": self.digest,
        }


class RecoveryStateFusionRuntime:
    SECRET_MARKERS = (
        "authorization",
        "bearer_token",
        "access_token",
        "refresh_token",
        "api_key",
        "credential_material",
        "secret_value",
        "password",
        "private_key",
    )

    def __init__(
        self,
        base_resolver: Any,
        ports: Sequence[StateEvidencePort] = (),
        *,
        policy: StateFusionPolicy | None = None,
        components: RecoveryComponentControl | None = None,
    ) -> None:
        self.base_resolver = base_resolver
        self.policy = policy or StateFusionPolicy()
        self.components = components or RecoveryComponentControl()
        self._ports: dict[tuple[RecoveryStateFamily, str], StateEvidencePort] = {}
        for port in ports:
            self.register(port)

    def register(self, port: StateEvidencePort) -> None:
        key = (port.family, port.owner)
        if key in self._ports:
            raise ValueError(f"state evidence port already registered: {port.family.value}:{port.owner}")
        self._ports[key] = port

    def replace(self, port: StateEvidencePort) -> None:
        self._ports[(port.family, port.owner)] = port

    def resolve(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> RecoveryContext:
        return self.fuse(signal, overrides).context

    def fuse(self, signal: RecoverySignal, overrides: Mapping[str, Any] | None = None) -> FusedRecoveryState:
        self.components.require(RecoveryComponent.PLAN_STORE, operation="fuse recovery owner state")
        base = self.base_resolver.resolve(signal, dict(overrides or {}))
        evidence, failures = self._collect(signal)
        base_evidence = self._base_evidence(signal, base)
        combined = self._dedupe((*base_evidence, *evidence))
        families = tuple(sorted({item.family for item in combined}, key=lambda item: item.value))
        required_missing = [family for family in self.policy.required_families if family not in families]
        failures.extend(StateFusionFailure(
            family=family,
            owner="unavailable",
            code="required_family_missing",
            message=f"required state family is unavailable: {family.value}",
            blocking=True,
        ) for family in required_missing)
        if len(families) < self.policy.minimum_families:
            raise RecoveryStateQuorumError(
                f"recovery state fusion requires {self.policy.minimum_families} real families; got "
                + ", ".join(item.value for item in families)
            )
        blocking = [item for item in failures if item.blocking]
        if blocking:
            raise RecoveryStateFusionError("; ".join(item.message for item in blocking))
        context = self._enrich(base, signal, combined, failures)
        return FusedRecoveryState(
            signal_id=signal.signal_id,
            run_id=signal.refs.run_id,
            task_id=signal.refs.task_id,
            context=context,
            evidence=combined,
            failures=tuple(failures),
            families=families,
        )

    def explain(self, signal: RecoverySignal, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        fused = self.fuse(signal, overrides)
        revisions = defaultdict(list)
        for item in fused.evidence:
            revisions[item.family.value].append({"owner": item.owner, "revision": item.revision, "digest": item.digest})
        return {
            "fusion": fused.to_dict(),
            "owner_revisions": dict(revisions),
            "policy": {
                "minimum_families": self.policy.minimum_families,
                "required_families": [item.value for item in self.policy.required_families],
                "maximum_age_seconds": self.policy.maximum_age_seconds,
            },
            "decision_owner": "RecoveryDecisionRuntime",
            "copied_canonical_state": False,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-state-fusion-contract/v1",
            "minimum_real_state_families": self.policy.minimum_families,
            "required_families": [item.value for item in self.policy.required_families],
            "ports": [{"family": family.value, "owner": owner} for family, owner in sorted(self._ports, key=lambda item: (item[0].value, item[1]))],
            "state_copy_custody": False,
            "secret_redaction": list(self.SECRET_MARKERS),
            "context_consumer": "RecoveryDecisionRuntime",
        }

    def _collect(self, signal: RecoverySignal) -> tuple[tuple[StateEvidence, ...], list[StateFusionFailure]]:
        collected: list[StateEvidence] = []
        failures: list[StateFusionFailure] = []
        for (family, owner), port in sorted(self._ports.items(), key=lambda item: (item[0][0].value, item[0][1])):
            try:
                item = port.snapshot(signal)
                self._validate(signal, item)
                state = self._sanitize(item.state)
                if len(state) > self.policy.maximum_state_keys:
                    raise RecoveryStateFusionError(f"{family.value} owner snapshot exceeds key bound")
                normalized = replace(item, state=state)
                age = normalized.age()
                if age.total_seconds() > self.policy.maximum_age_seconds and family not in self.policy.allow_stale_families:
                    failures.append(StateFusionFailure(
                        family=family,
                        owner=owner,
                        code="state_evidence_stale",
                        message=f"{family.value} state from {owner} is stale",
                        blocking=self.policy.fail_on_owner_error,
                    ))
                    continue
                collected.append(normalized)
            except Exception as error:
                failures.append(StateFusionFailure(
                    family=family,
                    owner=owner,
                    code=type(error).__name__,
                    message=str(error)[:1000],
                    blocking=self.policy.fail_on_owner_error or family in self.policy.required_families,
                ))
        return tuple(collected), failures

    def _base_evidence(self, signal: RecoverySignal, context: RecoveryContext) -> tuple[StateEvidence, ...]:
        definitions = (
            (RecoveryStateFamily.SESSION, "SessionLifecycleRuntime", context.session_state),
            (RecoveryStateFamily.PERMISSION, "PermissionControlPlane", context.permission_state),
            (RecoveryStateFamily.WORKER, "WorkerPoolFoundationRuntime", context.worker_state),
            (RecoveryStateFamily.BACKEND, "BackendRegistry", context.backend_state),
            (RecoveryStateFamily.PROVIDER, "ProviderControlPlane", context.provider_state),
            (RecoveryStateFamily.CHECKPOINT, "RecoveryPlanStore", context.checkpoint_state),
        )
        receipts = list(context.metadata.get("context_owner_receipts") or ())
        by_owner = {str(item.get("owner") or ""): dict(item) for item in receipts if isinstance(item, Mapping)}
        result: list[StateEvidence] = []
        for family, owner, state in definitions:
            if not state:
                continue
            receipt = by_owner.get(owner, {})
            result.append(StateEvidence(
                family=family,
                owner=owner,
                run_id=signal.refs.run_id,
                task_id=signal.refs.task_id,
                revision=str(receipt.get("state_revision") or state.get("revision") or state.get("version") or ""),
                observed_at=str(state.get("updated_at") or utc_now()),
                state=self._sanitize(state),
                evidence_refs=tuple(item for item in (str(receipt.get("digest") or ""),) if item),
                canonical=True,
                metadata={"source": "RecoveryContextRuntime"},
            ))
        for index, memory in enumerate(context.memory_evidence[: self.policy.maximum_evidence_per_family]):
            result.append(StateEvidence(
                family=RecoveryStateFamily.MEMORY,
                owner="MemoryFabric",
                run_id=signal.refs.run_id,
                task_id=signal.refs.task_id,
                revision=str(memory.get("revision") or index),
                observed_at=str(memory.get("created_at") or utc_now()),
                state=self._sanitize(memory),
                evidence_refs=tuple(str(item) for item in memory.get("evidence_refs") or ()),
                canonical=True,
            ))
        return tuple(result)

    def _validate(self, signal: RecoverySignal, evidence: StateEvidence) -> None:
        if evidence.run_id != signal.refs.run_id or evidence.task_id != signal.refs.task_id:
            raise RecoveryStateScopeError(f"{evidence.owner} state evidence crosses run/task custody")
        if not evidence.owner.strip():
            raise RecoveryStateScopeError("state evidence owner is required")
        if self.policy.fail_on_noncanonical and not evidence.canonical:
            raise RecoveryStateFusionError(f"noncanonical {evidence.family.value} evidence cannot drive recovery")
        if not isinstance(evidence.state, Mapping):
            raise RecoveryStateFusionError("state evidence payload must be an object")
        if self._contains_secret(evidence.state):
            raise RecoveryStateFusionError(f"{evidence.family.value} evidence contains secret material")

    def _enrich(
        self,
        base: RecoveryContext,
        signal: RecoverySignal,
        evidence: Sequence[StateEvidence],
        failures: Sequence[StateFusionFailure],
    ) -> RecoveryContext:
        memory = list(base.memory_evidence)
        known = {stable_digest(dict(item)) for item in memory}
        for item in evidence:
            record = {
                "kind": "recovery_owner_state",
                "family": item.family.value,
                "owner": item.owner,
                "revision": item.revision,
                "state_digest": item.digest,
                "evidence_refs": list(item.evidence_refs),
                "canonical": item.canonical,
                "state": copy.deepcopy(dict(item.state)),
            }
            digest = stable_digest(record)
            if digest not in known:
                memory.append(record)
                known.add(digest)
        family_names = sorted({item.family.value for item in evidence})
        metadata = {
            **copy.deepcopy(dict(base.metadata)),
            "state_fusion": {
                "signal_id": signal.signal_id,
                "family_count": len(family_names),
                "families": family_names,
                "owner_count": len({item.owner for item in evidence}),
                "evidence_digests": [item.digest for item in evidence],
                "failures": [item.to_dict() for item in failures],
                "minimum_family_gate": self.policy.minimum_families,
                "canonical_owner_state_copied": False,
            },
        }
        checkpoint = copy.deepcopy(dict(base.checkpoint_state))
        checkpoint["exact_resume_ready"] = bool(
            checkpoint.get("checkpoint_id")
            and not checkpoint.get("signature_mismatch")
            and not checkpoint.get("corrupt")
        )
        worker = copy.deepcopy(dict(base.worker_state))
        backend = copy.deepcopy(dict(base.backend_state))
        provider = copy.deepcopy(dict(base.provider_state))
        worker["owner_available"] = self._family_available(evidence, RecoveryStateFamily.WORKER, default=bool(worker))
        backend["owner_available"] = self._family_available(evidence, RecoveryStateFamily.BACKEND, default=bool(backend))
        provider["owner_available"] = self._family_available(evidence, RecoveryStateFamily.PROVIDER, default=bool(provider))
        worker["healthy_alternative_count"] = self._candidate_count(evidence, RecoveryStateFamily.WORKER, worker)
        backend["eligible_alternative_count"] = self._candidate_count(evidence, RecoveryStateFamily.BACKEND, backend)
        provider["eligible_alternative_count"] = self._candidate_count(evidence, RecoveryStateFamily.PROVIDER, provider)
        return replace(
            base,
            worker_state=worker,
            backend_state=backend,
            provider_state=provider,
            checkpoint_state=checkpoint,
            memory_evidence=tuple(memory),
            metadata=metadata,
        )

    def _dedupe(self, evidence: Sequence[StateEvidence]) -> tuple[StateEvidence, ...]:
        by_digest: dict[str, StateEvidence] = {}
        by_family: defaultdict[RecoveryStateFamily, list[StateEvidence]] = defaultdict(list)
        for item in evidence:
            by_digest[item.digest] = item
        for item in by_digest.values():
            by_family[item.family].append(item)
        selected: list[StateEvidence] = []
        for family in sorted(by_family, key=lambda item: item.value):
            values = sorted(by_family[family], key=lambda item: (item.observed_at, item.owner, item.digest), reverse=True)
            selected.extend(values[: self.policy.maximum_evidence_per_family])
        return tuple(sorted(selected, key=lambda item: (item.family.value, item.owner, item.revision, item.digest)))

    @classmethod
    def _sanitize(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        def walk(item: Any, path: tuple[str, ...]) -> Any:
            if isinstance(item, Mapping):
                result: dict[str, Any] = {}
                for raw_key, child in item.items():
                    key = str(raw_key)
                    if any(marker in key.casefold() for marker in cls.SECRET_MARKERS):
                        result[key] = "[REDACTED]"
                    else:
                        result[key] = walk(child, (*path, key))
                return result
            if isinstance(item, (list, tuple)):
                return [walk(child, (*path, str(index))) for index, child in enumerate(item[:512])]
            if isinstance(item, (str, int, float, bool)) or item is None:
                return copy.deepcopy(item)
            return str(item)[:1000]
        return dict(walk(dict(value), ()))

    @classmethod
    def _contains_secret(cls, value: Mapping[str, Any]) -> bool:
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(marker in normalized for marker in cls.SECRET_MARKERS) and item not in {None, "", "[REDACTED]"}:
                return True
            if isinstance(item, Mapping) and cls._contains_secret(item):
                return True
        return False

    @staticmethod
    def _family_available(
        evidence: Sequence[StateEvidence],
        family: RecoveryStateFamily,
        *,
        default: bool,
    ) -> bool:
        values = [item for item in evidence if item.family is family]
        if not values:
            return default
        return any(bool(item.state.get("available", item.state.get("ready", True))) for item in values)

    @staticmethod
    def _candidate_count(
        evidence: Sequence[StateEvidence],
        family: RecoveryStateFamily,
        fallback: Mapping[str, Any],
    ) -> int:
        count = int(fallback.get("eligible_alternative_count") or fallback.get("healthy_alternative_count") or 0)
        for item in evidence:
            if item.family is not family:
                continue
            candidates = item.state.get("candidates") or item.state.get("alternatives") or ()
            if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes, bytearray)):
                count = max(count, sum(
                    isinstance(candidate, Mapping) and bool(candidate.get("available", True))
                    for candidate in candidates
                ))
        return count


def state_fusion_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-state-fusion-surface/v1",
        "minimum_real_state_families": 3,
        "families": [item.value for item in RecoveryStateFamily],
        "canonical_mutable_state_copied": False,
        "structured_owner_revision_required": True,
        "output": "RecoveryContext consumed by RecoveryDecisionRuntime",
    }


__all__ = [
    "CallbackStateEvidencePort",
    "FusedRecoveryState",
    "RecoveryStateFamily",
    "RecoveryStateFusionError",
    "RecoveryStateFusionRuntime",
    "RecoveryStateQuorumError",
    "RecoveryStateScopeError",
    "StateEvidence",
    "StateEvidencePort",
    "StateFusionFailure",
    "StateFusionPolicy",
    "state_fusion_runtime_contract",
]
