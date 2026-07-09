from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT


class ProviderCredentialStatus(StrEnum):
    READY = "ready"
    OPTIONAL = "optional"
    MISSING = "missing"
    BLOCKED = "blocked"


class ProviderRouteStatus(StrEnum):
    READY = "ready"
    FALLBACK = "fallback"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ProviderCapability(StrEnum):
    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    PROMPT_CACHE = "prompt_cache"
    JSON_PATCH = "json_patch"
    FILES = "files"
    RETRY_AFTER = "retry_after"
    FALLBACK = "fallback"
    LOCAL_DETERMINISTIC = "local_deterministic"


class ProviderQuirk(StrEnum):
    RATE_LIMIT_RETRY_AFTER = "rate_limit_retry_after"
    MODEL_UNAVAILABLE_FALLBACK = "model_unavailable_fallback"
    PROMPT_TOO_LONG_REDUCE = "prompt_too_long_reduce"
    TOOL_RESULT_PAIRING_STRICT = "tool_result_pairing_strict"
    STREAM_STALL_WATCHDOG = "stream_stall_watchdog"
    USAGE_PATCH_LATE = "usage_patch_late"


class ProviderCatalogSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ProviderCatalogSurface(StrEnum):
    CATALOG = "catalog"
    MODEL = "model"
    CREDENTIAL = "credential"
    ROUTE = "route"
    FALLBACK = "fallback"
    QUIRK = "quirk"


@dataclass(frozen=True, slots=True)
class ProviderCatalogFinding:
    code: str
    severity: ProviderCatalogSeverity
    surface: ProviderCatalogSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ProviderCatalogSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderCredentialBinding:
    provider_id: str
    credential_id: str
    status: ProviderCredentialStatus
    required: bool
    source: str = "zyra-runtime"
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if not self.required:
            return self.status in {ProviderCredentialStatus.READY, ProviderCredentialStatus.OPTIONAL, ProviderCredentialStatus.MISSING}
        return self.status == ProviderCredentialStatus.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "credential_id": self.credential_id,
            "status": str(self.status),
            "required": self.required,
            "ok": self.ok,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderModelDescriptor:
    model_id: str
    provider_id: str
    display_name: str
    capabilities: tuple[ProviderCapability, ...]
    quirks: tuple[ProviderQuirk, ...] = ()
    context_window_tokens: int = 200000
    output_tokens: int = 8192
    input_cost_per_token: float = 0.0000003
    output_cost_per_token: float = 0.0000012
    priority: int = 100
    enabled: bool = True
    local: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def streaming(self) -> bool:
        return ProviderCapability.STREAMING in self.capabilities

    @property
    def tool_use(self) -> bool:
        return ProviderCapability.TOOL_USE in self.capabilities

    @property
    def prompt_cache(self) -> bool:
        return ProviderCapability.PROMPT_CACHE in self.capabilities

    @property
    def fallback_eligible(self) -> bool:
        return self.enabled and ProviderCapability.FALLBACK in self.capabilities

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    def has_quirk(self, quirk: ProviderQuirk) -> bool:
        return quirk in self.quirks

    def estimate_cost(self, *, input_tokens: int, output_tokens: int) -> float:
        return max(0, input_tokens) * self.input_cost_per_token + max(0, output_tokens) * self.output_cost_per_token

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "capabilities": [str(item) for item in self.capabilities],
            "quirks": [str(item) for item in self.quirks],
            "context_window_tokens": self.context_window_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_per_token": self.input_cost_per_token,
            "output_cost_per_token": self.output_cost_per_token,
            "priority": self.priority,
            "enabled": self.enabled,
            "local": self.local,
            "streaming": self.streaming,
            "tool_use": self.tool_use,
            "prompt_cache": self.prompt_cache,
            "fallback_eligible": self.fallback_eligible,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    route_id: str
    requested_model: str
    selected_model: ProviderModelDescriptor
    credential: ProviderCredentialBinding
    status: ProviderRouteStatus
    fallback_models: tuple[ProviderModelDescriptor, ...]
    required_capabilities: tuple[ProviderCapability, ...]
    missing_capabilities: tuple[ProviderCapability, ...]
    selected_fallback: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status not in {ProviderRouteStatus.BLOCKED} and self.credential.ok and not self.missing_capabilities

    @property
    def fallback_model_ids(self) -> tuple[str, ...]:
        return tuple(model.model_id for model in self.fallback_models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "requested_model": self.requested_model,
            "selected_model": self.selected_model.to_dict(),
            "credential": self.credential.to_dict(),
            "status": str(self.status),
            "ok": self.ok,
            "fallback_models": [model.to_dict() for model in self.fallback_models],
            "fallback_model_ids": list(self.fallback_model_ids),
            "required_capabilities": [str(item) for item in self.required_capabilities],
            "missing_capabilities": [str(item) for item in self.missing_capabilities],
            "selected_fallback": self.selected_fallback,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ProviderCatalogReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    route: ProviderRoute
    models: tuple[ProviderModelDescriptor, ...]
    credentials: tuple[ProviderCredentialBinding, ...]
    findings: tuple[ProviderCatalogFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.route.ok and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ProviderRouteStatus:
        if any(finding.blocking for finding in self.findings) or not self.route.ok:
            return ProviderRouteStatus.BLOCKED
        if self.findings:
            return ProviderRouteStatus.DEGRADED
        return self.route.status

    @property
    def enabled_model_count(self) -> int:
        return sum(1 for model in self.models if model.enabled)

    @property
    def fallback_model_count(self) -> int:
        return len(self.route.fallback_models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.model_provider_catalog.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "ok": self.ok,
            "status": str(self.status),
            "route": self.route.to_dict(),
            "models": [model.to_dict() for model in self.models],
            "credentials": [credential.to_dict() for credential in self.credentials],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "enabled_model_count": self.enabled_model_count,
            "fallback_model_count": self.fallback_model_count,
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "model_provider_report_id": self.report_id,
            "model_provider_owner_unit": self.owner_unit,
            "model_provider_runtime_id": self.runtime_id,
            "model_provider_ok": str(self.ok).lower(),
            "model_provider_status": str(self.status),
            "model_provider_requested_model": self.route.requested_model,
            "model_provider_selected_model": self.route.selected_model.model_id,
            "model_provider_selected_provider": self.route.selected_model.provider_id,
            "model_provider_selected_fallback": str(self.route.selected_fallback).lower(),
            "model_provider_fallback_models": ",".join(self.route.fallback_model_ids),
            "model_provider_enabled_models": str(self.enabled_model_count),
            "model_provider_findings": str(len(self.findings)),
            "model_provider_credential_status": str(self.route.credential.status),
        }


class ModelProviderCatalogRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        models: Sequence[ProviderModelDescriptor] | None = None,
        credentials: Sequence[ProviderCredentialBinding] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.models = tuple(models or default_provider_models())
        self.credentials = tuple(credentials or default_provider_credentials())

    def build_report(
        self,
        *,
        requested_model: str,
        fallback_models: Sequence[str] = (),
        required_capabilities: Sequence[ProviderCapability] = (
            ProviderCapability.STREAMING,
            ProviderCapability.TOOL_USE,
        ),
        constraints: Mapping[str, Any] | None = None,
    ) -> ProviderCatalogReport:
        constraints = dict(constraints or {})
        required = tuple(required_capabilities)
        models = self._models_from_constraints(constraints)
        credentials = self._credentials_from_constraints(constraints)
        requested = requested_model or "zyra-local-code-model"
        selected = self._find_model(models, requested)
        selected_fallback = False
        findings: list[ProviderCatalogFinding] = []
        fallback_descriptors = tuple(
            model for model_id in fallback_models for model in [_find_model_in(models, model_id)] if model is not None
        )
        if selected is None:
            selected = self._first_ready_fallback(models, fallback_descriptors, required)
            selected_fallback = selected is not None
            findings.append(
                ProviderCatalogFinding(
                    code="REQUESTED_MODEL_NOT_FOUND",
                    severity=ProviderCatalogSeverity.WARNING if selected else ProviderCatalogSeverity.BLOCKER,
                    surface=ProviderCatalogSurface.MODEL,
                    message=f"Requested model {requested} was not found in Zyra provider catalog.",
                    metadata={"requested_model": requested},
                )
            )
        if selected is None:
            selected = disabled_model_descriptor(requested)
        credential = self._credential_for(credentials, selected.provider_id)
        missing_capabilities = tuple(capability for capability in required if not selected.supports(capability))
        if missing_capabilities:
            findings.append(
                ProviderCatalogFinding(
                    code="MODEL_MISSING_REQUIRED_CAPABILITIES",
                    severity=ProviderCatalogSeverity.BLOCKER,
                    surface=ProviderCatalogSurface.MODEL,
                    message=f"Selected model {selected.model_id} lacks required capabilities.",
                    metadata={"missing": ",".join(str(item) for item in missing_capabilities)},
                )
            )
        if not credential.ok:
            findings.append(
                ProviderCatalogFinding(
                    code="PROVIDER_CREDENTIAL_NOT_READY",
                    severity=ProviderCatalogSeverity.BLOCKER,
                    surface=ProviderCatalogSurface.CREDENTIAL,
                    message=f"Provider credential for {selected.provider_id} is not ready.",
                    metadata={"credential_status": str(credential.status)},
                )
            )
        status = ProviderRouteStatus.FALLBACK if selected_fallback else ProviderRouteStatus.READY
        if findings and any(finding.blocking for finding in findings):
            status = ProviderRouteStatus.BLOCKED
        elif findings:
            status = ProviderRouteStatus.DEGRADED
        route = ProviderRoute(
            route_id=new_id("provider_route"),
            requested_model=requested,
            selected_model=selected,
            credential=credential,
            status=status,
            fallback_models=fallback_descriptors,
            required_capabilities=required,
            missing_capabilities=missing_capabilities,
            selected_fallback=selected_fallback,
            metadata={
                "source_path": "packages/runtime/zyra_runtime/model_provider_runtime.py",
                "opencode_source_path": "opencode/packages/opencode/src/provider",
            },
        )
        return ProviderCatalogReport(
            report_id=new_id("provider_catalog"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            route=route,
            models=tuple(models),
            credentials=tuple(credentials),
            findings=tuple(findings),
            source_decisions=default_provider_source_decisions(),
        )

    def event_for_report(
        self,
        report: ProviderCatalogReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "phase": "model_provider_catalog",
                    "model_provider_catalog": report.to_dict(),
                }
            },
        )

    def _models_from_constraints(self, constraints: Mapping[str, Any]) -> tuple[ProviderModelDescriptor, ...]:
        disabled = set(_string_list(constraints.get("disable_provider_models")))
        extra = []
        for model_id in _string_list(constraints.get("api_retry_fallback_models")):
            if self._find_model(self.models, model_id) is None:
                extra.append(
                    ProviderModelDescriptor(
                        model_id=model_id,
                        provider_id="zyra-local",
                        display_name=model_id,
                        capabilities=(
                            ProviderCapability.STREAMING,
                            ProviderCapability.TOOL_USE,
                            ProviderCapability.FALLBACK,
                            ProviderCapability.LOCAL_DETERMINISTIC,
                        ),
                        quirks=(ProviderQuirk.MODEL_UNAVAILABLE_FALLBACK,),
                        local=True,
                        priority=80,
                        metadata={"source": "constraints.api_retry_fallback_models"},
                    )
                )
        models = [model for model in self.models if model.model_id not in disabled]
        models.extend(extra)
        return tuple(models)

    def _credentials_from_constraints(self, constraints: Mapping[str, Any]) -> tuple[ProviderCredentialBinding, ...]:
        status_override = str(constraints.get("provider_credential_status") or "").strip().lower()
        if not status_override:
            return self.credentials
        status = _enum_or_default(ProviderCredentialStatus, status_override, ProviderCredentialStatus.READY)
        return tuple(
            ProviderCredentialBinding(
                provider_id=item.provider_id,
                credential_id=item.credential_id,
                status=status,
                required=item.required,
                source=item.source,
                metadata={**item.metadata, "override": "provider_credential_status"},
            )
            for item in self.credentials
        )

    def _find_model(self, models: Sequence[ProviderModelDescriptor], model_id: str) -> ProviderModelDescriptor | None:
        return _find_model_in(models, model_id)

    def _credential_for(
        self,
        credentials: Sequence[ProviderCredentialBinding],
        provider_id: str,
    ) -> ProviderCredentialBinding:
        for credential in credentials:
            if credential.provider_id == provider_id:
                return credential
        return ProviderCredentialBinding(
            provider_id=provider_id,
            credential_id=f"{provider_id}:implicit",
            status=ProviderCredentialStatus.OPTIONAL,
            required=False,
            source="implicit-local",
        )

    def _first_ready_fallback(
        self,
        models: Sequence[ProviderModelDescriptor],
        fallback_models: Sequence[ProviderModelDescriptor],
        required: Sequence[ProviderCapability],
    ) -> ProviderModelDescriptor | None:
        candidates = list(fallback_models) or [model for model in models if model.fallback_eligible]
        candidates.sort(key=lambda model: (model.priority, model.model_id))
        for model in candidates:
            if all(model.supports(capability) for capability in required):
                return model
        return None


def default_provider_models() -> tuple[ProviderModelDescriptor, ...]:
    return (
        ProviderModelDescriptor(
            model_id="zyra-local-code-model",
            provider_id="zyra-local",
            display_name="Zyra Local Code Model",
            capabilities=(
                ProviderCapability.STREAMING,
                ProviderCapability.TOOL_USE,
                ProviderCapability.PROMPT_CACHE,
                ProviderCapability.JSON_PATCH,
                ProviderCapability.FALLBACK,
                ProviderCapability.LOCAL_DETERMINISTIC,
            ),
            quirks=(
                ProviderQuirk.TOOL_RESULT_PAIRING_STRICT,
                ProviderQuirk.STREAM_STALL_WATCHDOG,
                ProviderQuirk.USAGE_PATCH_LATE,
            ),
            local=True,
            priority=10,
            metadata={"source": "zyra-owned-default"},
        ),
        ProviderModelDescriptor(
            model_id="zyra-local-fallback",
            provider_id="zyra-local",
            display_name="Zyra Local Fallback",
            capabilities=(
                ProviderCapability.STREAMING,
                ProviderCapability.TOOL_USE,
                ProviderCapability.FALLBACK,
                ProviderCapability.LOCAL_DETERMINISTIC,
            ),
            quirks=(ProviderQuirk.MODEL_UNAVAILABLE_FALLBACK,),
            local=True,
            priority=20,
            metadata={"source": "zyra-owned-default"},
        ),
        ProviderModelDescriptor(
            model_id="opencode-aid-sdk-compatible",
            provider_id="opencode-adapter",
            display_name="opencode AISDK compatible route",
            capabilities=(
                ProviderCapability.STREAMING,
                ProviderCapability.TOOL_USE,
                ProviderCapability.RETRY_AFTER,
                ProviderCapability.FALLBACK,
            ),
            quirks=(ProviderQuirk.RATE_LIMIT_RETRY_AFTER, ProviderQuirk.MODEL_UNAVAILABLE_FALLBACK),
            priority=50,
            enabled=True,
            metadata={"source": "opencode-provider-pattern"},
        ),
    )


def default_provider_credentials() -> tuple[ProviderCredentialBinding, ...]:
    return (
        ProviderCredentialBinding(
            provider_id="zyra-local",
            credential_id="zyra-local:none",
            status=ProviderCredentialStatus.READY,
            required=False,
            source="local-runtime",
        ),
        ProviderCredentialBinding(
            provider_id="opencode-adapter",
            credential_id="opencode:optional",
            status=ProviderCredentialStatus.OPTIONAL,
            required=False,
            source="opencode-provider-pattern",
        ),
    )


def disabled_model_descriptor(model_id: str) -> ProviderModelDescriptor:
    return ProviderModelDescriptor(
        model_id=model_id or "missing-model",
        provider_id="missing-provider",
        display_name=model_id or "missing-model",
        capabilities=(),
        enabled=False,
        priority=9999,
        metadata={"disabled": "true"},
    )


def default_provider_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/provider/*",
            "target_path": "packages/runtime/zyra_runtime/model_provider_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "provider catalog, model capability and fallback route mapping",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/core/src/catalog.ts",
            "target_path": "packages/runtime/zyra_runtime/model_provider_runtime.py",
            "decision": "adapter_encapsulated",
            "capability": "typed provider/model catalog shape adapted to Zyra dataclasses",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/model_api_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "selected model route feeds ModelStreamRuntime and ApiRetryRuntime",
        },
    )


def model_provider_metadata(report: ProviderCatalogReport | None) -> dict[str, str]:
    if report is None:
        return {"model_provider_ok": "false", "model_provider_status": "missing", "model_provider_report_id": ""}
    return report.metadata()


def render_model_provider_catalog_markdown(report: ProviderCatalogReport) -> str:
    lines = [
        "# Model Provider Catalog",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- requested_model: {report.route.requested_model}",
        f"- selected_model: {report.route.selected_model.model_id}",
        f"- selected_provider: {report.route.selected_model.provider_id}",
        f"- credential_status: {report.route.credential.status}",
        "",
        "## Fallback Models",
    ]
    for model in report.route.fallback_models:
        lines.append(f"- {model.model_id} provider={model.provider_id} priority={model.priority}")
    lines.extend(["", "## Capabilities"])
    for capability in report.route.required_capabilities:
        lines.append(f"- required {capability}")
    for capability in report.route.missing_capabilities:
        lines.append(f"- missing {capability}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _find_model_in(
    models: Sequence[ProviderModelDescriptor],
    model_id: str,
) -> ProviderModelDescriptor | None:
    for model in models:
        if model.model_id == model_id:
            return model
    return None


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _enum_or_default(enum_cls: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_cls(str(value))
    except ValueError:
        return default
