from __future__ import annotations

import contextlib
import inspect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, MutableMapping, Sequence

from .contracts import (
    REQUIRED_RUNTIME_DOMAINS,
    MutationIdentity,
    MutationReceipt,
    OwnerBinding,
    OwnerLease,
    ProductizationContractError,
    RuntimeDomain,
    RuntimeReadiness,
    canonicalize,
    digest_payload,
)
from .defaults import (
    DefaultEntryBinding,
    DefaultEntryRegistry,
    SourceSymbolEntryProbe,
    default_entry_bindings,
    default_owner_bindings,
)
from .owner_registry import (
    CallableOwnerProbe,
    CompositeOwnerProbe,
    RuntimeOwnerRegistry,
)


Activation = Callable[..., Mapping[str, Any] | bool]


@dataclass(frozen=True, slots=True)
class RuntimeActivationSet:
    """Behavior probes supplied by the product composition root.

    The productization package deliberately does not import API, scheduler,
    memory, or TypeScript process adapters.  Doing so would make this
    coordinator a second composition root.  Instead the actual product root
    supplies one behavior probe per canonical domain.  Missing probes are a
    startup error, never an implicit source-file or in-memory fallback.
    """

    owner: Mapping[RuntimeDomain, Activation]
    entry: Mapping[str, Activation]

    def __post_init__(self) -> None:
        owner_domains = set(self.owner)
        required = set(REQUIRED_RUNTIME_DOMAINS)
        missing = sorted(item.value for item in required - owner_domains)
        unknown = sorted(
            str(getattr(item, "value", item)) for item in owner_domains - required
        )
        if missing or unknown:
            raise ProductizationContractError(
                "runtime_activation_owner_population_invalid",
                "runtime owner activation probes must exactly cover canonical domains",
                details={"missing": missing, "unknown": unknown},
            )
        expected_entries = {
            entry.entry_id for entry in default_entry_bindings()
        }
        supplied_entries = set(self.entry)
        missing_entries = sorted(expected_entries - supplied_entries)
        unknown_entries = sorted(supplied_entries - expected_entries)
        if missing_entries or unknown_entries:
            raise ProductizationContractError(
                "runtime_activation_entry_population_invalid",
                "default entry activation probes must exactly cover product entries",
                details={
                    "missing": missing_entries,
                    "unknown": unknown_entries,
                },
            )
        for label, values in (("owner", self.owner), ("entry", self.entry)):
            invalid = sorted(
                str(getattr(key, "value", key))
                for key, value in values.items()
                if not callable(value)
            )
            if invalid:
                raise ProductizationContractError(
                    "runtime_activation_not_callable",
                    f"{label} activation set contains non-callable probes",
                    details={"keys": invalid},
                )


class OwnerSourceContractProbe:
    """Validate every owner-bound implementation reference in the clean tree.

    Source presence is intentionally only one child of a composite probe.  It
    cannot produce readiness without the behavior activation supplied by the
    product composition root.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.expanduser().resolve()

    def __call__(self, binding: OwnerBinding) -> Mapping[str, Any]:
        references = (
            binding.owner,
            binding.store,
            *binding.writers,
            binding.checkpoint,
            binding.recovery,
            *binding.projections,
            *binding.caches,
        )
        observations: list[dict[str, Any]] = []
        ready = True
        for reference in references:
            candidate = (self.project_root / reference.path).resolve()
            inside = candidate.is_relative_to(self.project_root)
            exists = inside and candidate.is_file()
            symbol_present = False
            error = ""
            if exists:
                try:
                    source = candidate.read_text(encoding="utf-8")
                    symbol_present = reference.symbol in source
                except (OSError, UnicodeError) as failure:
                    error = f"{type(failure).__name__}: {failure}"
            accepted = inside and exists and symbol_present
            ready = ready and accepted
            observations.append(
                {
                    "path": reference.path,
                    "symbol": reference.symbol,
                    "role": reference.role.value,
                    "inside_project": inside,
                    "exists": exists,
                    "symbol_present": symbol_present,
                    "accepted": accepted,
                    "error": error,
                }
            )
        return {
            "ready": ready,
            "write_path_ready": ready,
            "checkpoint_ready": ready,
            "recovery_ready": ready,
            "fallback_active": False,
            "probe_kind": "owner_source_contract",
            "references": observations,
        }


@dataclass(frozen=True, slots=True)
class DomainGateReceipt:
    domain: RuntimeDomain
    lease: OwnerLease
    owner_ready: bool
    default_ready: bool
    acquired_at_ns: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain.value,
            "lease": self.lease.to_dict(),
            "owner_ready": self.owner_ready,
            "default_ready": self.default_ready,
            "acquired_at_ns": self.acquired_at_ns,
            "digest": self.digest,
        }


class RuntimeOwnerComposition:
    """Production gate around the already-selected runtime state owners.

    This class coordinates readiness and owner-generation leases.  It never
    writes a domain store itself and therefore cannot become a shadow owner.
    The caller performs the mutation while holding ``mutation_gate`` and
    returns the domain owner's durable receipt.
    """

    def __init__(
        self,
        project_root: Path,
        activations: RuntimeActivationSet,
        *,
        enabled: bool = True,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        if not self.project_root.is_dir():
            raise ProductizationContractError(
                "runtime_project_root_missing",
                f"runtime project root does not exist: {self.project_root}",
            )
        self.activations = activations
        self.clock = clock
        self.owner_registry = RuntimeOwnerRegistry(clock=clock, enabled=enabled)
        self.default_registry = DefaultEntryRegistry(
            self.owner_registry,
            clock=clock,
            enabled=enabled,
        )
        self._enabled = bool(enabled)
        self._lock = threading.RLock()
        self._configure()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def _configure(self) -> None:
        source_probe = OwnerSourceContractProbe(self.project_root)
        owner_bindings = default_owner_bindings()
        if {item.domain for item in owner_bindings} != set(REQUIRED_RUNTIME_DOMAINS):
            raise ProductizationContractError(
                "runtime_owner_binding_population_invalid",
                "default owner bindings do not exactly cover canonical domains",
            )
        for binding in owner_bindings:
            self.owner_registry.register(
                binding,
                CompositeOwnerProbe(
                    CallableOwnerProbe(self.activations.owner[binding.domain]),
                    source_probe,
                ),
            )
        self.owner_registry.seal()

        for entry in default_entry_bindings():
            self.default_registry.register(
                entry,
                SourceSymbolEntryProbe(
                    self.project_root,
                    self.activations.entry[entry.entry_id],
                ),
            )
        self.default_registry.seal()

    def disable(self, reason: str) -> None:
        normalized = str(reason).strip()
        if not normalized:
            raise ProductizationContractError(
                "runtime_composition_disable_reason_missing",
                "disabling runtime owner composition requires a reason",
            )
        with self._lock:
            self._enabled = False
            self.default_registry.disable()
            self.owner_registry.disable(normalized)

    def enable(self) -> None:
        with self._lock:
            self.owner_registry.enable()
            self.default_registry.enable()
            self._enabled = True

    def probe_domain(self, domain: RuntimeDomain) -> dict[str, Any]:
        owner = self.owner_registry.probe(domain)
        entries = self.default_registry.probe_domain(domain)
        default_ready = bool(entries) and any(item.ready for item in entries)
        payload = {
            "domain": domain.value,
            "owner": owner.to_dict(),
            "default_entries": [item.to_dict() for item in entries],
            "owner_ready": owner.ready,
            "default_ready": default_ready,
            "ready": self.enabled and owner.ready and default_ready,
        }
        payload["digest"] = digest_payload(payload)
        return payload

    def probe_all(self) -> dict[str, Any]:
        domains = tuple(
            self.probe_domain(domain)
            for domain in sorted(REQUIRED_RUNTIME_DOMAINS, key=lambda item: item.value)
        )
        blockers = tuple(
            item["domain"] for item in domains if not item["ready"]
        )
        payload = {
            "enabled": self.enabled,
            "owner_registry_digest": self.owner_registry.configuration_digest(),
            "default_registry_digest": self.default_registry.configuration_digest(),
            "domains": domains,
            "blockers": blockers,
            "ready": self.enabled and not blockers,
        }
        payload["digest"] = digest_payload(payload)
        return canonicalize(payload)

    def runtime_readiness(
        self,
        *,
        revision: str,
        unresolved_work_ids: Sequence[str] = (),
        source_boundary_ready: bool,
        causal_boundary_ready: bool,
    ) -> RuntimeReadiness:
        owner_results = self.owner_registry.probe_all()
        defaults = self.default_registry.snapshot()
        unresolved = tuple(sorted(set(str(item) for item in unresolved_work_ids)))
        payload = {
            "revision": revision,
            "generated_at_ns": self.clock(),
            "owners": [item.to_dict() for item in owner_results],
            "unresolved_work_ids": list(unresolved),
            "source_boundary_ready": bool(source_boundary_ready),
            "causal_boundary_ready": bool(causal_boundary_ready),
            "default_boundary_ready": bool(defaults["ready"]),
            "module_enabled": self.enabled,
        }
        return RuntimeReadiness(
            revision=revision,
            generated_at_ns=int(payload["generated_at_ns"]),
            owner_results=owner_results,
            unresolved_work_ids=unresolved,
            source_boundary_ready=(
                bool(source_boundary_ready) and bool(defaults["ready"])
            ),
            causal_boundary_ready=bool(causal_boundary_ready),
            module_enabled=self.enabled,
            digest=digest_payload(payload),
        )

    def acquire_gate(
        self,
        domain: RuntimeDomain,
        *,
        operation: str,
        correlation_id: str,
    ) -> DomainGateReceipt:
        owner_result = self.owner_registry.require_ready(domain)
        default_results = self.default_registry.require_domain(domain)
        lease = self.owner_registry.acquire(
            domain,
            operation=operation,
            correlation_id=correlation_id,
        )
        payload = {
            "domain": domain.value,
            "lease": lease.to_dict(),
            "owner_ready": owner_result.ready,
            "default_ready": any(item.ready for item in default_results),
            "acquired_at_ns": self.clock(),
        }
        return DomainGateReceipt(
            domain=domain,
            lease=lease,
            owner_ready=True,
            default_ready=True,
            acquired_at_ns=int(payload["acquired_at_ns"]),
            digest=digest_payload(payload),
        )

    @contextlib.contextmanager
    def mutation_gate(
        self,
        domain: RuntimeDomain,
        *,
        operation: str,
        correlation_id: str,
    ) -> Iterator[DomainGateReceipt]:
        gate = self.acquire_gate(
            domain,
            operation=operation,
            correlation_id=correlation_id,
        )
        outcome = "aborted"
        mutation_receipt_id = ""
        try:
            yield gate
            outcome = "completed"
        except BaseException:
            outcome = "failed"
            raise
        finally:
            self.owner_registry.release(
                gate.lease,
                outcome=outcome,
                mutation_receipt_id=mutation_receipt_id,
            )

    def release_gate(
        self,
        gate: DomainGateReceipt,
        *,
        mutation_receipt: MutationReceipt,
    ) -> None:
        if mutation_receipt.domain is not gate.domain:
            raise ProductizationContractError(
                "runtime_gate_receipt_domain_mismatch",
                "mutation receipt belongs to a different canonical domain",
                details={
                    "gate_domain": gate.domain.value,
                    "receipt_domain": mutation_receipt.domain.value,
                },
            )
        self.owner_registry.validate_lease(gate.lease)
        self.owner_registry.release(
            gate.lease,
            outcome="completed",
            mutation_receipt_id=mutation_receipt.receipt_id,
        )

    def prove_owner_loss(self, domain: RuntimeDomain) -> dict[str, Any]:
        """Run a reversible disable probe without invoking a domain mutation."""

        before = self.probe_domain(domain)
        generation_before = self.owner_registry.generation(domain)
        self.owner_registry.disable_domain(domain, "m3 reversible owner-loss proof")
        rejected = False
        error_code = ""
        fallback_success = False
        try:
            self.acquire_gate(
                domain,
                operation="owner-loss-proof",
                correlation_id=f"proof-{domain.value}-{self.clock()}",
            )
            fallback_success = True
        except ProductizationContractError as error:
            rejected = error.code == "canonical_owner_unavailable"
            error_code = error.code
        finally:
            self.owner_registry.enable_domain(domain)
        after = self.probe_domain(domain)
        payload = {
            "domain": domain.value,
            "ready_before": bool(before["ready"]),
            "rejected_while_disabled": rejected,
            "fallback_success": fallback_success,
            "ready_after": bool(after["ready"]),
            "generation_before": generation_before,
            "generation_after": self.owner_registry.generation(domain),
            "error_code": error_code,
        }
        payload["valid"] = (
            payload["ready_before"]
            and payload["rejected_while_disabled"]
            and not payload["fallback_success"]
            and payload["ready_after"]
            and payload["generation_after"] > payload["generation_before"]
        )
        payload["digest"] = digest_payload(payload)
        return payload


def activation_result(
    *,
    ready: bool,
    owner: str,
    store: str,
    state_root: str = "",
    revision: int | str | None = None,
    fallback_active: bool = False,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a strict, serializable health observation for composition roots."""

    payload = {
        "ready": bool(ready),
        "write_path_ready": bool(ready),
        "checkpoint_ready": bool(ready),
        "recovery_ready": bool(ready),
        "fallback_active": bool(fallback_active),
        "canonical_owner": str(owner),
        "canonical_store": str(store),
        "state_root": str(state_root),
        "revision": revision,
        "details": canonicalize(details or {}),
    }
    if not str(owner).strip() or not str(store).strip():
        raise ProductizationContractError(
            "runtime_activation_identity_missing",
            "activation result requires canonical owner and store identities",
        )
    if fallback_active:
        payload["ready"] = False
    return payload


def invoke_activation(
    activation: Activation,
    value: OwnerBinding | DefaultEntryBinding,
) -> Mapping[str, Any] | bool:
    """Invoke callbacks with zero or one positional parameter deterministically."""

    signature = inspect.signature(activation)
    positional = tuple(
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    )
    if not positional:
        return activation()
    return activation(value)
