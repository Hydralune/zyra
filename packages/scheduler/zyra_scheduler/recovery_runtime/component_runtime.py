from __future__ import annotations

import copy
import os
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .contracts import stable_digest, utc_now


class RecoveryComponentError(RuntimeError):
    pass


class RecoveryComponentDisabled(RecoveryComponentError):
    def __init__(self, component: "RecoveryComponent", reason: str) -> None:
        self.component = component
        self.reason = reason
        super().__init__(f"recovery component {component.value} is disabled: {reason}")


class RecoveryDependencyViolation(RecoveryComponentError):
    pass


class RecoveryComponent(StrEnum):
    CLASSIFIER = "classifier"
    DECISION_RUNTIME = "decision_runtime"
    PLAN_STORE = "plan_store"
    CHECKPOINT_RESTORER = "checkpoint_restorer"
    SIDE_EFFECT_FENCE = "side_effect_fence"
    GRAPH_ROUTER = "graph_router"
    WORKER_ROUTER = "worker_router"
    BACKEND_ROUTER = "backend_router"
    PROVIDER_ROUTER = "provider_router"
    MEMORY_FEEDBACK = "memory_feedback"
    CONTINUATION_DISPATCH = "continuation_dispatch"
    OMP_NORMALIZER = "omp_normalizer"
    EVENT_SINK = "event_sink"


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    component: RecoveryComponent
    enabled: bool
    ready: bool
    reason: str
    checked_at: str
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.enabled and self.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component.value,
            "enabled": self.enabled,
            "ready": self.ready,
            "usable": self.usable,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "source": self.source,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class ComponentRequirement:
    component: RecoveryComponent
    operation: str
    fallback_forbidden: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DependencyFinding:
    code: str
    path: str
    message: str
    blocking: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "blocking": self.blocking,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class DependencyAuditReport:
    root: str
    findings: tuple[DependencyFinding, ...]
    loaded_forbidden_modules: tuple[str, ...]
    checked_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.findings) and not self.loaded_forbidden_modules

    def require_ok(self) -> "DependencyAuditReport":
        if not self.ok:
            messages = [item.message for item in self.findings if item.blocking]
            messages.extend(f"forbidden module loaded: {item}" for item in self.loaded_forbidden_modules)
            raise RecoveryDependencyViolation("; ".join(messages))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-dependency-audit/v1",
            "root": self.root,
            "ok": self.ok,
            "findings": [item.to_dict() for item in self.findings],
            "loaded_forbidden_modules": list(self.loaded_forbidden_modules),
            "checked_at": self.checked_at,
        }


class RecoveryComponentControl:
    ENVIRONMENT_KEYS: Mapping[RecoveryComponent, str] = {
        RecoveryComponent.CLASSIFIER: "ZYRA_DISABLE_RECOVERY_CLASSIFIER",
        RecoveryComponent.DECISION_RUNTIME: "ZYRA_DISABLE_RECOVERY_DECISION_RUNTIME",
        RecoveryComponent.PLAN_STORE: "ZYRA_DISABLE_RECOVERY_PLAN_STORE",
        RecoveryComponent.CHECKPOINT_RESTORER: "ZYRA_DISABLE_RECOVERY_CHECKPOINT_RESTORER",
        RecoveryComponent.SIDE_EFFECT_FENCE: "ZYRA_DISABLE_RECOVERY_SIDE_EFFECT_FENCE",
        RecoveryComponent.GRAPH_ROUTER: "ZYRA_DISABLE_RECOVERY_GRAPH_ROUTER",
        RecoveryComponent.WORKER_ROUTER: "ZYRA_DISABLE_RECOVERY_WORKER_ROUTER",
        RecoveryComponent.BACKEND_ROUTER: "ZYRA_DISABLE_RECOVERY_BACKEND_ROUTER",
        RecoveryComponent.PROVIDER_ROUTER: "ZYRA_DISABLE_RECOVERY_PROVIDER_ROUTER",
        RecoveryComponent.MEMORY_FEEDBACK: "ZYRA_DISABLE_RECOVERY_MEMORY_FEEDBACK",
        RecoveryComponent.CONTINUATION_DISPATCH: "ZYRA_DISABLE_RECOVERY_CONTINUATION_DISPATCH",
        RecoveryComponent.OMP_NORMALIZER: "ZYRA_DISABLE_RECOVERY_OMP_NORMALIZER",
        RecoveryComponent.EVENT_SINK: "ZYRA_DISABLE_RECOVERY_EVENT_SINK",
    }

    def __init__(
        self,
        *,
        readiness: Mapping[RecoveryComponent, Callable[[], bool | Mapping[str, Any]]] | None = None,
        overrides: Mapping[RecoveryComponent | str, bool] | None = None,
    ) -> None:
        self._readiness = dict(readiness or {})
        self._overrides = {
            RecoveryComponent(str(component)): bool(enabled)
            for component, enabled in dict(overrides or {}).items()
        }
        self._runtime_disabled: dict[RecoveryComponent, str] = {}
        self._lock = threading.RLock()

    def disable(self, component: RecoveryComponent | str, reason: str) -> ComponentStatus:
        selected = RecoveryComponent(str(component))
        value = str(reason).strip()
        if not value:
            raise ValueError("disable reason is required")
        with self._lock:
            self._runtime_disabled[selected] = value
        return self.status(selected)

    def enable(self, component: RecoveryComponent | str) -> ComponentStatus:
        selected = RecoveryComponent(str(component))
        with self._lock:
            self._runtime_disabled.pop(selected, None)
            self._overrides[selected] = True
        return self.status(selected)

    def status(self, component: RecoveryComponent | str) -> ComponentStatus:
        selected = RecoveryComponent(str(component))
        with self._lock:
            runtime_reason = self._runtime_disabled.get(selected, "")
            override = self._overrides.get(selected)
        environment_key = self.ENVIRONMENT_KEYS[selected]
        environment_disabled = self._truthy(os.environ.get(environment_key, ""))
        if runtime_reason:
            return ComponentStatus(selected, False, False, runtime_reason, utc_now(), "runtime")
        if override is False:
            return ComponentStatus(selected, False, False, "disabled by assembly override", utc_now(), "assembly")
        if environment_disabled:
            return ComponentStatus(selected, False, False, f"disabled by {environment_key}", utc_now(), "environment")
        ready, metadata, reason = self._probe(selected)
        enabled = override is not False
        source = "assembly" if override is not None else "default"
        return ComponentStatus(selected, enabled, ready, reason, utc_now(), source, metadata)

    def require(
        self,
        component: RecoveryComponent | str,
        *,
        operation: str,
        fallback_forbidden: bool = True,
    ) -> ComponentStatus:
        selected = RecoveryComponent(str(component))
        status = self.status(selected)
        if not status.usable:
            suffix = "legacy fallback is forbidden" if fallback_forbidden else "component is unavailable"
            raise RecoveryComponentDisabled(selected, f"{operation}: {status.reason}; {suffix}")
        return status

    def require_all(self, requirements: Sequence[ComponentRequirement]) -> tuple[ComponentStatus, ...]:
        statuses: list[ComponentStatus] = []
        failures: list[str] = []
        for requirement in requirements:
            try:
                statuses.append(self.require(
                    requirement.component,
                    operation=requirement.operation,
                    fallback_forbidden=requirement.fallback_forbidden,
                ))
            except RecoveryComponentDisabled as error:
                detail = requirement.reason or str(error)
                failures.append(f"{requirement.component.value}: {detail}")
        if failures:
            raise RecoveryComponentError("component requirements failed: " + "; ".join(failures))
        return tuple(statuses)

    def matrix(self) -> dict[str, Any]:
        statuses = [self.status(component) for component in RecoveryComponent]
        return {
            "schema": "zyra.recovery-component-matrix/v1",
            "ready": all(item.usable for item in statuses),
            "components": {item.component.value: item.to_dict() for item in statuses},
            "fallback_policy": "fail_closed_per_component",
            "legacy_fallback": False,
            "digest": stable_digest([item.to_dict() for item in statuses]),
        }

    def disabled(self) -> tuple[RecoveryComponent, ...]:
        return tuple(component for component in RecoveryComponent if not self.status(component).usable)

    def _probe(self, component: RecoveryComponent) -> tuple[bool, dict[str, Any], str]:
        callback = self._readiness.get(component)
        if callback is None:
            return True, {}, "ready"
        try:
            value = callback()
        except Exception as error:
            return False, {"error_type": type(error).__name__}, str(error)[:1000]
        if isinstance(value, Mapping):
            result = copy.deepcopy(dict(value))
            ready = bool(result.pop("ready", result.pop("available", True)))
            reason = str(result.pop("reason", "ready" if ready else "readiness probe rejected"))
            return ready, result, reason
        ready = bool(value)
        return ready, {}, "ready" if ready else "readiness probe returned false"

    @staticmethod
    def _truthy(value: str) -> bool:
        return str(value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}


class RecoveryDependencyAuditor:
    FORBIDDEN_MODULE_PREFIXES = (
        "langgraph.graph",
        "langgraph.pregel",
        "langgraph.channels",
        "langgraph.prebuilt",
        "langgraph.store",
        "langgraph_sdk",
    )
    FORBIDDEN_PATH_MARKERS = (
        "../claude-code-best",
        "../oh-my-pi",
        "../langgraph",
        "../openhands",
        "../opencode",
        "../agentscope",
        "../agent-framework",
        "../hermes-agent",
        "../openclaw",
        "npm link",
        "pip install -e ..",
    )

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def audit_loaded_modules(self) -> tuple[str, ...]:
        return tuple(sorted(
            name
            for name in sys.modules
            if any(name == prefix or name.startswith(prefix + ".") for prefix in self.FORBIDDEN_MODULE_PREFIXES)
        ))

    def audit_text(self, path: str | Path, text: str) -> tuple[DependencyFinding, ...]:
        selected = Path(path)
        normalized = text.replace("\\", "/").casefold()
        findings: list[DependencyFinding] = []
        for marker in self.FORBIDDEN_PATH_MARKERS:
            if marker.casefold() in normalized:
                findings.append(DependencyFinding(
                    code="forbidden_source_dependency",
                    path=str(selected),
                    message=f"runtime source dependency marker is forbidden: {marker}",
                    blocking=True,
                    metadata={"marker": marker},
                ))
        return tuple(findings)

    def audit_paths(self, paths: Iterable[str | Path]) -> DependencyAuditReport:
        findings: list[DependencyFinding] = []
        for raw in paths:
            selected = Path(raw)
            resolved = selected.resolve() if selected.is_absolute() else (self.root / selected).resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError:
                findings.append(DependencyFinding(
                    code="path_outside_product_root",
                    path=str(resolved),
                    message="recovery runtime dependency resolves outside the Zyra product root",
                    blocking=True,
                ))
                continue
            if any(part.casefold() in {"vendor", "vendor-runtimes", "source-pool", "runtime-sources"} for part in resolved.parts):
                findings.append(DependencyFinding(
                    code="vendor_like_runtime_path",
                    path=str(resolved),
                    message="recovery main path cannot execute from a vendor-like source pool",
                    blocking=True,
                ))
            if any(part.casefold() in {".cache", "__pycache__", "node_modules"} for part in resolved.parts):
                findings.append(DependencyFinding(
                    code="cache_runtime_dependency",
                    path=str(resolved),
                    message="recovery main path cannot require cache or build residue",
                    blocking=True,
                ))
        return DependencyAuditReport(
            root=str(self.root),
            findings=tuple(findings),
            loaded_forbidden_modules=self.audit_loaded_modules(),
        )

    def audit_files(self, paths: Iterable[str | Path]) -> DependencyAuditReport:
        base = self.audit_paths(paths)
        findings = list(base.findings)
        for raw in paths:
            selected = Path(raw)
            resolved = selected.resolve() if selected.is_absolute() else (self.root / selected).resolve()
            if not resolved.is_file():
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                findings.append(DependencyFinding(
                    code="dependency_file_unreadable",
                    path=str(resolved),
                    message=str(error),
                    blocking=True,
                ))
                continue
            findings.extend(self.audit_text(resolved, text))
        return DependencyAuditReport(
            root=str(self.root),
            findings=tuple(findings),
            loaded_forbidden_modules=base.loaded_forbidden_modules,
        )


def component_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-component-control-contract/v1",
        "components": [item.value for item in RecoveryComponent],
        "disable_semantics": "the affected recovery path fails closed and no legacy fallback is invoked",
        "forbidden_production_dependencies": list(RecoveryDependencyAuditor.FORBIDDEN_MODULE_PREFIXES),
        "checkpoint_conformance_only": True,
        "source_repository_runtime_paths": False,
    }


__all__ = [
    "ComponentRequirement",
    "ComponentStatus",
    "DependencyAuditReport",
    "DependencyFinding",
    "RecoveryComponent",
    "RecoveryComponentControl",
    "RecoveryComponentDisabled",
    "RecoveryComponentError",
    "RecoveryDependencyAuditor",
    "RecoveryDependencyViolation",
    "component_runtime_contract",
]
