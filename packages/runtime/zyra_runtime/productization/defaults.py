from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .contracts import (
    REQUIRED_RUNTIME_DOMAINS,
    CodeReference,
    OwnerBinding,
    OwnerRole,
    ProductizationContractError,
    RuntimeDomain,
    RuntimeSurface,
    canonicalize,
    digest_payload,
    stable_id,
)
from .owner_registry import RuntimeOwnerRegistry


class EntryProbe(Protocol):
    def __call__(self, entry: DefaultEntryBinding) -> Mapping[str, Any] | bool:
        ...


@dataclass(frozen=True, slots=True)
class DefaultEntryBinding:
    entry_id: str
    domain: RuntimeDomain
    surface: RuntimeSurface
    reference: CodeReference
    command_or_route: str
    write_symbols: tuple[str, ...]
    fallback_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.entry_id or any(char.isspace() for char in self.entry_id):
            raise ProductizationContractError(
                "default_entry_id_invalid",
                "default entry id must be a non-empty bounded token",
            )
        if self.reference.role not in {OwnerRole.WRITER, OwnerRole.PROJECTION}:
            raise ProductizationContractError(
                "default_entry_role_invalid",
                "default entry reference must be a writer or projection",
            )
        if not str(self.command_or_route).strip():
            raise ProductizationContractError(
                "default_entry_surface_missing",
                f"{self.entry_id} has no route or command",
            )
        if not self.write_symbols:
            raise ProductizationContractError(
                "default_entry_write_path_missing",
                f"{self.entry_id} has no canonical write symbol",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "domain": self.domain.value,
            "surface": self.surface.value,
            "reference": self.reference.to_dict(),
            "command_or_route": self.command_or_route,
            "write_symbols": list(self.write_symbols),
            "fallback_symbols": list(self.fallback_symbols),
        }


@dataclass(frozen=True, slots=True)
class DefaultEntryProbeResult:
    entry_id: str
    domain: RuntimeDomain
    surface: RuntimeSurface
    reachable: bool
    owner_ready: bool
    write_path_ready: bool
    fallback_bypass: bool
    checked_at_ns: int
    latency_ns: int
    evidence: Mapping[str, Any]
    reason: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.reachable
            and self.owner_ready
            and self.write_path_ready
            and not self.fallback_bypass
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "domain": self.domain.value,
            "surface": self.surface.value,
            "reachable": self.reachable,
            "owner_ready": self.owner_ready,
            "write_path_ready": self.write_path_ready,
            "fallback_bypass": self.fallback_bypass,
            "checked_at_ns": self.checked_at_ns,
            "latency_ns": self.latency_ns,
            "evidence": canonicalize(self.evidence),
            "reason": self.reason,
            "ready": self.ready,
        }


@dataclass(slots=True)
class _EntrySlot:
    binding: DefaultEntryBinding
    probe: EntryProbe
    enabled: bool = True
    last_result: DefaultEntryProbeResult | None = None


class DefaultEntryRegistry:
    """Verifies product default entries without becoming a state owner."""

    def __init__(
        self,
        owner_registry: RuntimeOwnerRegistry,
        *,
        clock: Callable[[], int] = time.time_ns,
        enabled: bool = True,
    ) -> None:
        self.owner_registry = owner_registry
        self.clock = clock
        self.enabled = bool(enabled)
        self._entries: dict[str, _EntrySlot] = {}
        self._domain_entries: dict[RuntimeDomain, set[str]] = {}
        self._sealed = False
        self._lock = threading.RLock()

    def register(self, binding: DefaultEntryBinding, probe: EntryProbe) -> None:
        if not callable(probe):
            raise ProductizationContractError(
                "default_entry_probe_invalid",
                f"{binding.entry_id} probe is not callable",
            )
        with self._lock:
            if self._sealed:
                raise ProductizationContractError(
                    "default_entry_registry_sealed",
                    "default entry registry cannot be changed after seal",
                )
            if binding.entry_id in self._entries:
                raise ProductizationContractError(
                    "default_entry_duplicate",
                    f"default entry {binding.entry_id} is already registered",
                )
            owner_binding = self.owner_registry.binding(binding.domain)
            expected_entries = set(owner_binding.default_entries)
            if binding.entry_id not in expected_entries:
                raise ProductizationContractError(
                    "default_entry_owner_contract_mismatch",
                    f"{binding.entry_id} is not declared by {binding.domain.value}",
                    details={"declared": sorted(expected_entries)},
                )
            forbidden = {
                item.symbol
                for item in (*owner_binding.projections, *owner_binding.caches)
            }
            promoted = forbidden & set(binding.write_symbols)
            if promoted:
                raise ProductizationContractError(
                    "projection_promoted_by_default_entry",
                    f"{binding.entry_id} promotes a projection/cache to writer",
                    details={"symbols": sorted(promoted)},
                )
            self._entries[binding.entry_id] = _EntrySlot(binding=binding, probe=probe)
            self._domain_entries.setdefault(binding.domain, set()).add(binding.entry_id)

    def seal(self) -> str:
        with self._lock:
            missing = [
                domain.value
                for domain in REQUIRED_RUNTIME_DOMAINS
                if not self._domain_entries.get(domain)
            ]
            if missing:
                raise ProductizationContractError(
                    "default_domain_entry_missing",
                    "one or more canonical domains have no product default entry",
                    details={"domains": sorted(missing)},
                )
            for owner in self.owner_registry.bindings():
                expected = set(owner.default_entries)
                actual = self._domain_entries.get(owner.domain, set())
                missing_entries = expected - actual
                if missing_entries:
                    raise ProductizationContractError(
                        "declared_default_entry_unregistered",
                        f"{owner.domain.value} default entries are incomplete",
                        details={"missing": sorted(missing_entries)},
                    )
            self._sealed = True
            return self.configuration_digest()

    def configuration_digest(self) -> str:
        with self._lock:
            return digest_payload(
                {
                    "enabled": self.enabled,
                    "sealed": self._sealed,
                    "entries": [
                        self._entries[key].binding.to_dict()
                        for key in sorted(self._entries)
                    ],
                }
            )

    def disable(self) -> None:
        with self._lock:
            self.enabled = False

    def enable(self) -> None:
        with self._lock:
            self.enabled = True

    def disable_entry(self, entry_id: str) -> None:
        with self._lock:
            self._require_slot(entry_id).enabled = False

    def enable_entry(self, entry_id: str) -> None:
        with self._lock:
            self._require_slot(entry_id).enabled = True

    def probe(self, entry_id: str) -> DefaultEntryProbeResult:
        with self._lock:
            slot = self._require_slot(entry_id)
            if not self.enabled:
                return self._unavailable(slot.binding, "default entry registry is disabled")
            if not slot.enabled:
                return self._unavailable(slot.binding, "default entry is disabled")
            binding = slot.binding
            probe = slot.probe
        owner_result = self.owner_registry.probe(binding.domain)
        started = self.clock()
        try:
            raw = probe(binding)
            finished = self.clock()
            if isinstance(raw, bool):
                payload: Mapping[str, Any] = {
                    "reachable": raw,
                    "write_path_ready": raw,
                    "fallback_bypass": False,
                }
            elif isinstance(raw, Mapping):
                payload = raw
            else:
                to_dict = getattr(raw, "to_dict", None)
                if not callable(to_dict):
                    raise ProductizationContractError(
                        "default_entry_probe_result_invalid",
                        f"{entry_id} probe returned {type(raw).__name__}",
                    )
                converted = to_dict()
                if not isinstance(converted, Mapping):
                    raise ProductizationContractError(
                        "default_entry_probe_result_invalid",
                        f"{entry_id} to_dict did not return a mapping",
                    )
                payload = converted
            fallback_bypass = bool(
                payload.get("fallback_bypass")
                or payload.get("fallbackBypass")
                or payload.get("fallback_active")
            )
            observed_symbols = {
                str(item)
                for item in payload.get("write_symbols", binding.write_symbols)
            }
            required_symbols = set(binding.write_symbols)
            write_ready = bool(
                payload.get("write_path_ready", payload.get("writePathReady", False))
            ) and required_symbols.issubset(observed_symbols)
            observed_fallbacks = {
                str(item)
                for item in payload.get("observed_fallback_symbols", ())
            }
            if observed_fallbacks & set(binding.fallback_symbols):
                fallback_bypass = True
            result = DefaultEntryProbeResult(
                entry_id=binding.entry_id,
                domain=binding.domain,
                surface=binding.surface,
                reachable=bool(payload.get("reachable", payload.get("ready", False))),
                owner_ready=owner_result.ready,
                write_path_ready=write_ready,
                fallback_bypass=fallback_bypass,
                checked_at_ns=finished,
                latency_ns=max(0, finished - started),
                evidence={
                    **{
                        str(key): canonicalize(value)
                        for key, value in payload.items()
                        if key
                        not in {
                            "reachable",
                            "ready",
                            "write_path_ready",
                            "writePathReady",
                            "fallback_bypass",
                            "fallbackBypass",
                            "fallback_active",
                        }
                    },
                    "owner": owner_result.to_dict(),
                    "required_write_symbols": sorted(required_symbols),
                },
                reason=str(payload.get("reason") or ""),
            )
        except Exception as error:  # noqa: BLE001 - entry readiness must fail closed.
            finished = self.clock()
            result = DefaultEntryProbeResult(
                entry_id=binding.entry_id,
                domain=binding.domain,
                surface=binding.surface,
                reachable=False,
                owner_ready=owner_result.ready,
                write_path_ready=False,
                fallback_bypass=False,
                checked_at_ns=finished,
                latency_ns=max(0, finished - started),
                evidence={"error_type": type(error).__name__},
                reason=f"{type(error).__name__}: {error}",
            )
        with self._lock:
            self._require_slot(entry_id).last_result = result
        return result

    def probe_domain(self, domain: RuntimeDomain) -> tuple[DefaultEntryProbeResult, ...]:
        with self._lock:
            entry_ids = tuple(sorted(self._domain_entries.get(domain, ())))
        return tuple(self.probe(entry_id) for entry_id in entry_ids)

    def probe_all(self) -> tuple[DefaultEntryProbeResult, ...]:
        with self._lock:
            entry_ids = tuple(sorted(self._entries))
        return tuple(self.probe(entry_id) for entry_id in entry_ids)

    def require_domain(self, domain: RuntimeDomain) -> tuple[DefaultEntryProbeResult, ...]:
        results = self.probe_domain(domain)
        if not results or not any(item.ready for item in results):
            raise ProductizationContractError(
                "default_domain_unreachable",
                f"{domain.value} has no ready default entry",
                details={"entries": [item.to_dict() for item in results]},
            )
        return results

    def snapshot(self) -> dict[str, Any]:
        results = self.probe_all()
        payload = {
            "enabled": self.enabled,
            "sealed": self._sealed,
            "configuration_digest": self.configuration_digest(),
            "entries": [item.to_dict() for item in results],
            "domain_ready": {
                domain.value: any(
                    item.ready for item in results if item.domain is domain
                )
                for domain in REQUIRED_RUNTIME_DOMAINS
            },
        }
        payload["ready"] = (
            self.enabled
            and self._sealed
            and all(payload["domain_ready"].values())
        )
        payload["digest"] = digest_payload(payload)
        return payload

    def _require_slot(self, entry_id: str) -> _EntrySlot:
        try:
            return self._entries[entry_id]
        except KeyError as error:
            raise ProductizationContractError(
                "default_entry_unregistered",
                f"default entry {entry_id} is not registered",
            ) from error

    def _unavailable(
        self,
        binding: DefaultEntryBinding,
        reason: str,
    ) -> DefaultEntryProbeResult:
        return DefaultEntryProbeResult(
            entry_id=binding.entry_id,
            domain=binding.domain,
            surface=binding.surface,
            reachable=False,
            owner_ready=False,
            write_path_ready=False,
            fallback_bypass=False,
            checked_at_ns=self.clock(),
            latency_ns=0,
            evidence={},
            reason=reason,
        )


class CallableEntryProbe:
    def __init__(self, callable_value: Callable[..., Any]) -> None:
        if not callable(callable_value):
            raise TypeError("entry probe requires a callable")
        self.callable_value = callable_value

    def __call__(self, entry: DefaultEntryBinding) -> Mapping[str, Any]:
        signature = inspect.signature(self.callable_value)
        requires_entry = any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in signature.parameters.values()
        )
        raw = (
            self.callable_value(entry)
            if requires_entry
            else self.callable_value()
        )
        if isinstance(raw, bool):
            return {
                "reachable": raw,
                "write_path_ready": raw,
                "write_symbols": list(entry.write_symbols),
                "fallback_bypass": False,
                "probe_kind": "callable_boolean",
            }
        if not isinstance(raw, Mapping):
            to_dict = getattr(raw, "to_dict", None)
            if callable(to_dict):
                raw = to_dict()
        if not isinstance(raw, Mapping):
            raise ProductizationContractError(
                "entry_callable_result_invalid",
                f"entry callable returned {type(raw).__name__}",
            )
        result = dict(raw)
        result.setdefault("reachable", bool(result.get("ready", result.get("ok", False))))
        result.setdefault(
            "write_path_ready",
            bool(result.get("ready", result.get("ok", False))),
        )
        result.setdefault("write_symbols", list(entry.write_symbols))
        result.setdefault("fallback_bypass", False)
        result["probe_kind"] = "callable_default_entry"
        return result


class SourceSymbolEntryProbe:
    """Supporting source/symbol check used with a real callable owner probe.

    This probe never claims behavior from file existence alone. It additionally
    requires a runtime activation callback that proves the entry was composed.
    """

    def __init__(
        self,
        project_root: Path,
        activation: Callable[[DefaultEntryBinding], Mapping[str, Any] | bool],
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.activation = CallableEntryProbe(activation)

    def __call__(self, entry: DefaultEntryBinding) -> Mapping[str, Any]:
        path = (self.project_root / entry.reference.path).resolve()
        if not path.is_relative_to(self.project_root) or not path.is_file():
            return {
                "reachable": False,
                "write_path_ready": False,
                "write_symbols": [],
                "fallback_bypass": False,
                "reason": "entry source path is missing",
                "probe_kind": "source_symbol_plus_activation",
            }
        source = path.read_text(encoding="utf-8")
        if entry.reference.symbol not in source:
            return {
                "reachable": False,
                "write_path_ready": False,
                "write_symbols": [],
                "fallback_bypass": False,
                "reason": "entry source symbol is missing",
                "probe_kind": "source_symbol_plus_activation",
            }
        activation = dict(self.activation(entry))
        activation["source_path"] = entry.reference.path
        activation["source_symbol"] = entry.reference.symbol
        activation["source_present"] = True
        activation["probe_kind"] = "source_symbol_plus_activation"
        return activation


def default_owner_bindings() -> tuple[OwnerBinding, ...]:
    py = "python"
    ts = "typescript"
    return (
        OwnerBinding(
            domain=RuntimeDomain.SESSION_EVENT_PROJECTION,
            owner=CodeReference(
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "ClaudeRuntimeCore",
                ts,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/runtime/runtime-event-spine/src/sqlite-store.ts",
                "RuntimeEventSqliteStore",
                ts,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/claude-runtime/src/query-engine.ts",
                    "ClaudeRuntimeCore",
                    ts,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/runtime/claude-runtime/src/session/durable-runtime.ts",
                "DurableSessionRuntime",
                ts,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/runtime/claude-runtime/src/session/durable-runtime.ts",
                "DurableSessionRuntime",
                ts,
                OwnerRole.RECOVERY,
            ),
            projections=(
                CodeReference(
                    "apps/web/src/state/store.ts",
                    "CanonicalProjectionStore",
                    ts,
                    OwnerRole.PROJECTION,
                ),
            ),
            default_entries=("cli.session.runtime",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.PERMISSION,
            owner=CodeReference(
                "packages/runtime/claude-runtime/src/permission/approval-runtime.ts",
                "PermissionApprovalRuntime",
                ts,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/runtime/claude-runtime/src/permission/audit-runtime.ts",
                "PermissionAuditRuntime",
                ts,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/claude-runtime/src/permission/approval-runtime.ts",
                    "PermissionApprovalRuntime",
                    ts,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/runtime/claude-runtime/src/permission/audit-runtime.ts",
                "PermissionAuditRuntime",
                ts,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/runtime/claude-runtime/src/permission/approval-runtime.ts",
                "PermissionApprovalRuntime",
                ts,
                OwnerRole.RECOVERY,
            ),
            projections=(
                CodeReference(
                    "apps/web/src/features/permissions/runtime.ts",
                    "PermissionConsoleRuntime",
                    ts,
                    OwnerRole.PROJECTION,
                ),
            ),
            default_entries=("web.permission.runtime",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.MEMORY_COMPACT,
            owner=CodeReference(
                "packages/memory/zyra_memory/integration_runtime.py",
                "RetrievalIntegrationRuntime",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/memory/zyra_memory/retrieval_store.py",
                "SQLiteRetrievalIndex",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/memory/zyra_memory/curator_commit.py",
                    "MemoryCommitRuntime",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/memory/skill-memory-runtime/src/archive-runtime.ts",
                "CompactArchiveRuntime",
                ts,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/memory/skill-memory-runtime/src/resume-continuity-runtime.ts",
                "SkillMemoryResumeContinuityRuntime",
                ts,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.memory.retrieval",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.SCHEDULER_RECOVERY,
            owner=CodeReference(
                "packages/scheduler/zyra_scheduler/recovery_runtime/application.py",
                "RecoveryApplication",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/scheduler/zyra_scheduler/recovery_runtime/store.py",
                "RecoveryPlanStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/scheduler/zyra_scheduler/recovery_runtime/route_runtime.py",
                    "LayeredRouteRuntime",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/scheduler/zyra_scheduler/recovery_runtime/checkpoint_runtime.py",
                "CheckpointCommitRuntime",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/scheduler/zyra_scheduler/recovery_runtime/checkpoint_runtime.py",
                "CheckpointResumeBridge",
                py,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.scheduler.recovery",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.ARTIFACT,
            owner=CodeReference(
                "packages/runtime/zyra_runtime/artifacts.py",
                "LocalArtifactStore",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/runtime/zyra_runtime/artifacts.py",
                "LocalArtifactStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/zyra_runtime/sandbox_gateway/artifact_port.py",
                    "GatewayFileArtifactPort",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/runtime/zyra_runtime/artifacts.py",
                "LocalArtifactStore",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/artifact_port.py",
                "GatewayFileArtifactPort",
                py,
                OwnerRole.RECOVERY,
            ),
            projections=(
                CodeReference(
                    "apps/api/zyra_api/artifact_api.py",
                    "ArtifactCatalogService",
                    py,
                    OwnerRole.PROJECTION,
                ),
                CodeReference(
                    "apps/web/src/features/artifacts/runtime.ts",
                    "ArtifactWorkbenchRuntime",
                    ts,
                    OwnerRole.PROJECTION,
                ),
            ),
            caches=(
                CodeReference(
                    "apps/web/src/features/artifacts/cache.ts",
                    "ArtifactContentCache",
                    ts,
                    OwnerRole.CACHE,
                ),
            ),
            default_entries=("api.artifact.catalog",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.WORKER_ROUTE,
            owner=CodeReference(
                "packages/scheduler/zyra_scheduler/worker_pool/application.py",
                "WorkerPoolFoundationRuntime",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/scheduler/zyra_scheduler/worker_pool/store.py",
                "WorkerPoolStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/scheduler/zyra_scheduler/worker_pool/integration.py",
                    "WorkerPoolIntegrationRuntime",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/scheduler/zyra_scheduler/worker_pool/store.py",
                "WorkerPoolStore",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/scheduler/zyra_scheduler/worker_pool/integration.py",
                "WorkerPoolIntegrationRuntime",
                py,
                OwnerRole.RECOVERY,
            ),
            projections=(
                CodeReference(
                    "packages/scheduler/zyra_scheduler/worker_pool/application.py",
                    "WorkerPoolEventProjector",
                    py,
                    OwnerRole.PROJECTION,
                ),
            ),
            default_entries=("worker.pool.dispatch",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.GRAPH_CHECKPOINT,
            owner=CodeReference(
                "packages/orchestration/zyra_orchestration/graph_custody/runtime.py",
                "GraphStateCustody",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/orchestration/zyra_orchestration/graph_custody/store.py",
                "GraphStateStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/orchestration/zyra_orchestration/graph_custody/runtime.py",
                    "DynamicTopologyRuntime",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/orchestration/zyra_orchestration/graph_custody/store.py",
                "GraphStateStore",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/orchestration/zyra_orchestration/graph_custody/runtime.py",
                "GraphStateCustody",
                py,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.graph.topology",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER,
            owner=CodeReference(
                "packages/runtime/provider-control-plane/src/control-plane.ts",
                "ProviderControlPlane",
                ts,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/runtime/provider-control-plane/src/store.ts",
                "ProviderControlPlaneStore",
                ts,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/provider-control-plane/src/catalog-reconciler.ts",
                    "ProviderCatalogReconciler",
                    ts,
                    OwnerRole.WRITER,
                ),
                CodeReference(
                    "packages/runtime/provider-control-plane/src/credential-pool.ts",
                    "ProviderCredentialPoolRuntime",
                    ts,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/runtime/provider-control-plane/src/store.ts",
                "ProviderControlPlaneStore",
                ts,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/runtime/provider-control-plane/src/control-plane.ts",
                "ProviderControlPlane",
                ts,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.provider.dispatch",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.MCP_PLUGIN_REGISTRY,
            owner=CodeReference(
                "packages/integrations/claude-mcp/src/runtime/coordinator.ts",
                "McpRuntimeCoordinator",
                ts,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/integrations/claude-mcp/src/runtime/request-journal.ts",
                "McpRequestJournal",
                ts,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/claude-runtime/src/skills/coordinator.ts",
                    "SkillCoordinator",
                    ts,
                    OwnerRole.WRITER,
                ),
                CodeReference(
                    "packages/runtime/claude-runtime/src/skills/registry-runtime.ts",
                    "SkillRegistryRuntime",
                    ts,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/integrations/claude-mcp/src/runtime/request-journal.ts",
                "McpRequestJournal",
                ts,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/integrations/claude-mcp/src/runtime/coordinator.ts",
                "McpRuntimeCoordinator",
                ts,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.mcp.coordinator",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.GATEWAY_LEASE_BUSY,
            owner=CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/runtime.py",
                "SandboxGatewayRuntime",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/state_store.py",
                "GatewayStateStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/runtime/zyra_runtime/sandbox_gateway/lifecycle.py",
                    "SandboxLifecycle",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/state_store.py",
                "GatewayStateStore",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/runtime.py",
                "SandboxGatewayRuntime",
                py,
                OwnerRole.RECOVERY,
            ),
            default_entries=("worker.gateway.command",),
        ),
        OwnerBinding(
            domain=RuntimeDomain.TERMINAL_BROWSER_SESSION,
            owner=CodeReference(
                "packages/workers/zyra_workers/terminal/runtime.py",
                "TerminalSessionRegistry",
                py,
                OwnerRole.OWNER,
            ),
            store=CodeReference(
                "packages/workers/zyra_workers/terminal/runtime.py",
                "TerminalStateStore",
                py,
                OwnerRole.STORE,
            ),
            writers=(
                CodeReference(
                    "packages/workers/zyra_workers/browser_session/session_runtime.py",
                    "BrowserSessionRuntime",
                    py,
                    OwnerRole.WRITER,
                ),
            ),
            checkpoint=CodeReference(
                "packages/workers/zyra_workers/browser_session/store.py",
                "JsonBrowserStateStore",
                py,
                OwnerRole.CHECKPOINT,
            ),
            recovery=CodeReference(
                "packages/workers/zyra_workers/browser_session/resume_runtime.py",
                "BrowserSessionResumeRuntime",
                py,
                OwnerRole.RECOVERY,
            ),
            projections=(
                CodeReference(
                    "apps/web/src/features/terminal/runtime.ts",
                    "TerminalRuntime",
                    ts,
                    OwnerRole.PROJECTION,
                ),
            ),
            default_entries=("web.interactive.sessions",),
        ),
    )


def default_entry_bindings() -> tuple[DefaultEntryBinding, ...]:
    py = "python"
    ts = "typescript"
    return (
        DefaultEntryBinding(
            "cli.session.runtime",
            RuntimeDomain.SESSION_EVENT_PROJECTION,
            RuntimeSurface.CLI,
            CodeReference(
                "apps/code-worker/src/main.ts",
                "main",
                ts,
                OwnerRole.WRITER,
            ),
            "bun apps/code-worker/src/main.ts --stdio",
            ("ClaudeRuntimeCore", "DurableSessionRuntime", "RuntimeEventSqliteStore"),
        ),
        DefaultEntryBinding(
            "web.permission.runtime",
            RuntimeDomain.PERMISSION,
            RuntimeSurface.WEB,
            CodeReference(
                "apps/web/src/features/permissions/runtime.ts",
                "PermissionConsoleRuntime",
                ts,
                OwnerRole.PROJECTION,
            ),
            "POST /permissions/{request_id}/resolve",
            ("PermissionApprovalRuntime", "PermissionAuditRuntime"),
            ("PermissionWorkbenchRuntime",),
        ),
        DefaultEntryBinding(
            "worker.memory.retrieval",
            RuntimeDomain.MEMORY_COMPACT,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/memory/zyra_memory/integration_runtime.py",
                "RetrievalIntegrationRuntime",
                py,
                OwnerRole.WRITER,
            ),
            "worker:memory-retrieval",
            ("RetrievalIntegrationRuntime", "MemoryCommitRuntime", "SQLiteRetrievalIndex"),
        ),
        DefaultEntryBinding(
            "worker.scheduler.recovery",
            RuntimeDomain.SCHEDULER_RECOVERY,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/scheduler/zyra_scheduler/recovery_runtime/application.py",
                "RecoveryApplication",
                py,
                OwnerRole.WRITER,
            ),
            "worker:recovery",
            ("RecoveryApplication", "RecoveryPlanStore", "LayeredRouteRuntime"),
        ),
        DefaultEntryBinding(
            "api.artifact.catalog",
            RuntimeDomain.ARTIFACT,
            RuntimeSurface.API,
            CodeReference(
                "apps/api/zyra_api/main.py",
                "artifact_catalog_service",
                py,
                OwnerRole.WRITER,
            ),
            "GET /tasks/{task_id}/artifacts",
            ("LocalArtifactStore",),
            ("ArtifactContentCache",),
        ),
        DefaultEntryBinding(
            "worker.pool.dispatch",
            RuntimeDomain.WORKER_ROUTE,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/scheduler/zyra_scheduler/worker_pool/integration.py",
                "WorkerPoolIntegrationRuntime",
                py,
                OwnerRole.WRITER,
            ),
            "worker:dispatch",
            ("WorkerPoolIntegrationRuntime", "WorkerPoolFoundationRuntime", "WorkerPoolStore"),
            ("WorkerPoolEventProjector",),
        ),
        DefaultEntryBinding(
            "worker.graph.topology",
            RuntimeDomain.GRAPH_CHECKPOINT,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/orchestration/zyra_orchestration/graph_custody/runtime.py",
                "DynamicTopologyRuntime",
                py,
                OwnerRole.WRITER,
            ),
            "worker:dynamic-topology",
            ("DynamicTopologyRuntime", "GraphStateCustody", "GraphStateStore"),
        ),
        DefaultEntryBinding(
            "worker.provider.dispatch",
            RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/runtime/provider-control-plane/src/control-plane.ts",
                "ProviderControlPlane",
                ts,
                OwnerRole.WRITER,
            ),
            "worker:provider-dispatch",
            ("ProviderControlPlane", "ProviderControlPlaneStore"),
        ),
        DefaultEntryBinding(
            "worker.mcp.coordinator",
            RuntimeDomain.MCP_PLUGIN_REGISTRY,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/integrations/claude-mcp/src/runtime/coordinator.ts",
                "McpRuntimeCoordinator",
                ts,
                OwnerRole.WRITER,
            ),
            "worker:mcp",
            ("McpRuntimeCoordinator", "McpRequestJournal"),
        ),
        DefaultEntryBinding(
            "worker.gateway.command",
            RuntimeDomain.GATEWAY_LEASE_BUSY,
            RuntimeSurface.WORKER,
            CodeReference(
                "packages/runtime/zyra_runtime/sandbox_gateway/runtime.py",
                "SandboxGatewayRuntime",
                py,
                OwnerRole.WRITER,
            ),
            "worker:sandbox-command",
            ("SandboxGatewayRuntime", "SandboxLifecycle", "GatewayStateStore"),
        ),
        DefaultEntryBinding(
            "web.interactive.sessions",
            RuntimeDomain.TERMINAL_BROWSER_SESSION,
            RuntimeSurface.WEB,
            CodeReference(
                "apps/web/src/features/terminal/runtime.ts",
                "TerminalRuntime",
                ts,
                OwnerRole.PROJECTION,
            ),
            "GET /tasks/{task_id}/terminals",
            ("TerminalSessionRegistry", "TerminalStateStore", "BrowserSessionRuntime"),
            ("TerminalRuntime",),
        ),
    )
