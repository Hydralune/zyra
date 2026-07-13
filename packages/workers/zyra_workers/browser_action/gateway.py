from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, now_iso

from .clipboard_guard import BrowserClipboardGuard, ClipboardAccess, ClipboardReceipt
from .event_port import BrowserActionEventPort, BrowserActionResultProjector, ResultProjection
from .executor import BrowserExecutionError, BrowserSideEffectFence, ExecutionBindings, ExecutionContext
from .file_policy import BrowserFilePolicy, BrowserFilePolicyError, FileIntent, FileReceipt
from .form_policy import BrowserFormPolicy, FormPolicyError, FormReceipt
from .geometry_guard import BrowserGeometryGuard, GeometryGuardError, GeometryReceipt
from .hook_preflight import (
    BrowserHookError,
    BrowserPreflightHookRegistry,
    HookPhase,
    HookReceipt,
    request_hook_context,
)
from .models import (
    ActionExecutionResult,
    ActionFailureKind,
    ActionIdentity,
    ActionPhase,
    ActionPreflightReceipt,
    ActionRequest,
    PermissionDisposition,
    digest_value,
    stable_id,
)
from .network_policy import BrowserNetworkPolicy, NetworkPolicyError, NetworkReceipt
from .permission_bridge import (
    BrowserActionPermissionBridge,
    PermissionBridgeConsumption,
    PermissionBridgeDecision,
    PermissionBridgeError,
)
from .registry import BrowserActionRegistry, BrowserActionRegistryError, ResolvedAction
from .schema import ActionSchemaError
from .secret_policy import BrowserSecretPolicy, SecretPolicyError, SecretReceipt, SecretRedactor
from .selector_guard import (
    BrowserSelectorGuard,
    SelectorExpectation,
    SelectorGuardError,
    SelectorReceipt,
)
from .sensitive_policy import (
    BrowserSensitiveActionClassifier,
    ExecutionMode,
    RiskContext,
    RiskPolicyError,
)


class BrowserActionGatewayError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_kind: ActionFailureKind = ActionFailureKind.INTERNAL,
        details: Mapping[str, Any] | None = None,
        events: Sequence[EventRecord] = (),
    ) -> None:
        self.code = code
        self.failure_kind = failure_kind
        self.details = dict(details or {})
        self.events = tuple(events)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BrowserActionGatewayConfig:
    execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE
    receipt_ttl_seconds: float = 30.0
    require_event_port: bool = True
    require_selector_guard: bool = True
    require_network_policy: bool = True
    require_file_policy: bool = True
    require_secret_policy_for_placeholders: bool = True
    max_actions_per_batch: int = 64
    disabled: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.receipt_ttl_seconds <= 300:
            raise ValueError("browser action receipt TTL must be between 1 and 300 seconds")
        if not 1 <= self.max_actions_per_batch <= 256:
            raise ValueError("browser action batch limit must be between 1 and 256")


@dataclass(frozen=True, slots=True)
class SecurityReceipts:
    selector: SelectorReceipt | None = None
    network: NetworkReceipt | None = None
    file: FileReceipt | None = None
    secret: SecretReceipt | None = None
    clipboard: ClipboardReceipt | None = None
    form: FormReceipt | None = None
    hooks: tuple[HookReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hooks", tuple(self.hooks))

    def public_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector.public_dict() if self.selector else None,
            "network": self.network.public_dict() if self.network else None,
            "file": self.file.public_dict() if self.file else None,
            "secret": self.secret.public_dict() if self.secret else None,
            "clipboard": self.clipboard.public_dict() if self.clipboard else None,
            "form": self.form.public_dict() if self.form else None,
            "hooks": [receipt.public_dict() for receipt in self.hooks],
        }


@dataclass(frozen=True, slots=True)
class PreparedBrowserAction:
    request: ActionRequest
    resolved: ResolvedAction
    receipt: ActionPreflightReceipt
    security: SecurityReceipts
    selector_expectation: SelectorExpectation | None
    target_url: str
    events: tuple[EventRecord, ...]

    @property
    def action_id(self) -> str:
        return self.request.identity.action_id

    def public_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.public_dict(arguments=self.receipt.public_arguments),
            "resolved": self.resolved.to_dict(),
            "receipt": self.receipt.public_dict(),
            "security": self.security.public_dict(),
            "selector_expectation": self.selector_expectation.to_dict() if self.selector_expectation else None,
            "target_url": self.target_url,
            "event_ids": [event.event_id for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class AuthorizedBrowserAction:
    prepared: PreparedBrowserAction
    permission: PermissionBridgeDecision
    events: tuple[EventRecord, ...]

    @property
    def pending(self) -> bool:
        return self.permission.ask_pending

    @property
    def allowed(self) -> bool:
        return self.permission.allowed

    def public_dict(self) -> dict[str, Any]:
        return {
            "prepared": self.prepared.public_dict(),
            "permission": self.permission.public_dict(),
            "pending": self.pending,
            "allowed": self.allowed,
            "event_ids": [event.event_id for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class CompletedBrowserAction:
    prepared: PreparedBrowserAction
    permission: PermissionBridgeConsumption
    geometry: GeometryReceipt | None
    result: ActionExecutionResult
    projection: ResultProjection
    events: tuple[EventRecord, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.prepared.action_id,
            "receipt_id": self.permission.preflight.receipt_id,
            "permission": self.permission.public_dict(),
            "geometry": self.geometry.public_dict() if self.geometry else None,
            "result": self.projection.result.public_dict(),
            "artifacts": [artifact.artifact_id for artifact in self.projection.artifacts],
            "event_ids": [event.event_id for event in self.events],
        }


class BrowserActionGateway:
    """Zyra-owned browser action main path for registry→security→03A→CDP.

    Construction explicitly receives every existing owner.  Missing or
    disabled registry, event, permission, selector, network, file, secret or
    executor components fail closed; there is no static/browser-use fallback.
    """

    def __init__(
        self,
        *,
        registry: BrowserActionRegistry,
        classifier: BrowserSensitiveActionClassifier,
        hooks: BrowserPreflightHookRegistry,
        permission: BrowserActionPermissionBridge,
        executor: BrowserSideEffectFence,
        event_port: BrowserActionEventPort,
        result_projector: BrowserActionResultProjector,
        selector_guard: BrowserSelectorGuard | None = None,
        network_policy: BrowserNetworkPolicy | None = None,
        file_policy: BrowserFilePolicy | None = None,
        secret_policy: BrowserSecretPolicy | None = None,
        geometry_guard: BrowserGeometryGuard | None = None,
        clipboard_guard: BrowserClipboardGuard | None = None,
        form_policy: BrowserFormPolicy | None = None,
        config: BrowserActionGatewayConfig | None = None,
    ) -> None:
        self.registry = registry
        self.classifier = classifier
        self.hooks = hooks
        self.permission = permission
        self.executor = executor
        self.event_port = event_port
        self.result_projector = result_projector
        self.selector_guard = selector_guard
        self.network_policy = network_policy
        self.file_policy = file_policy
        self.secret_policy = secret_policy
        self.geometry_guard = geometry_guard
        self.clipboard_guard = clipboard_guard
        self.form_policy = form_policy
        self.config = config or BrowserActionGatewayConfig()

    def prepare(
        self,
        request: ActionRequest,
        *,
        selector_expectation: SelectorExpectation | None = None,
    ) -> PreparedBrowserAction:
        self._ensure_available()
        safe_initial = scrub_placeholders(request.arguments)
        events: list[EventRecord] = []
        try:
            events.append(self.event_port.start(request, public_arguments=safe_initial))
            resolved = self.registry.resolve(request.action, request.arguments)
            resolved.arguments.require()
            canonical_request = replace(
                request,
                action=resolved.canonical_name,
                arguments=dict(resolved.arguments.values),
            )
            events.append(
                self.event_port.transition(
                    canonical_request.identity,
                    ActionPhase.SCHEMA_VALIDATED,
                    details={
                        "action": resolved.canonical_name,
                        "registry_digest": self.registry.digest,
                        "schema_identity": resolved.definition.identity,
                        "arguments_digest": resolved.arguments.digest,
                        "alias_used": resolved.alias_used,
                    },
                )
            )
            public_arguments = dict(resolved.arguments.values)
            hook_receipts: list[HookReceipt] = []
            after_schema = self.hooks.run(
                request_hook_context(
                    canonical_request,
                    phase=HookPhase.AFTER_SCHEMA,
                    public_arguments=public_arguments,
                    registry_digest=self.registry.digest,
                )
            )
            hook_receipts.extend(after_schema.receipts)
            if after_schema.transformed:
                transformed = self.registry.resolve(resolved.canonical_name, after_schema.arguments)
                transformed.arguments.require()
                resolved = transformed
                public_arguments = dict(transformed.arguments.values)
                canonical_request = replace(canonical_request, arguments=public_arguments)
            target_url = action_target_url(canonical_request, public_arguments)
            selector = self._selector_preflight(
                canonical_request,
                resolved,
                selector_expectation=selector_expectation,
                target_url=target_url,
                public_arguments=public_arguments,
            )
            if selector:
                events.append(
                    self.event_port.transition(
                        canonical_request.identity,
                        ActionPhase.SELECTOR_CHECKED,
                        details={
                            "selector_receipt_id": selector.receipt_id,
                            "selector_binding_digest": selector.binding.identity_digest,
                            "semantics_digest": selector.semantics.digest,
                        },
                    )
                )
            form = self._form_preflight(canonical_request, resolved, selector, public_arguments)
            if form:
                target_url = form.action_url
            network = self._network_preflight(canonical_request, resolved, target_url)
            if network:
                events.append(
                    self.event_port.transition(
                        canonical_request.identity,
                        ActionPhase.NETWORK_CHECKED,
                        details={
                            "network_receipt_id": network.receipt_id,
                            "network_binding_digest": network.binding_digest,
                            "origin": network.canonical_url.origin,
                            "address_classes": sorted({str(item.classification) for item in network.resolution.addresses}),
                        },
                    )
                )
            file_receipt = self._file_preflight(canonical_request, resolved, public_arguments)
            if file_receipt:
                events.append(
                    self.event_port.transition(
                        canonical_request.identity,
                        ActionPhase.FILE_CHECKED,
                        details={
                            "file_receipt_id": file_receipt.receipt_id,
                            "file_binding_digest": file_receipt.binding_digest,
                            "intent": str(file_receipt.intent),
                        },
                    )
                )
            secret = self._secret_preflight(canonical_request, resolved, public_arguments, target_url)
            if secret:
                public_arguments = dict(secret.public_arguments)
                canonical_request = replace(canonical_request, arguments=public_arguments)
                events.append(
                    self.event_port.transition(
                        canonical_request.identity,
                        ActionPhase.SECRET_BOUND,
                        details={
                            "secret_receipt_id": secret.receipt_id,
                            "secret_binding_digest": secret.binding_digest,
                            "secret_reference_count": len(secret.references),
                        },
                    )
                )
            after_security = self.hooks.run(
                request_hook_context(
                    canonical_request,
                    phase=HookPhase.AFTER_SECURITY,
                    public_arguments=public_arguments,
                    registry_digest=self.registry.digest,
                    selector_binding_digest=selector.binding.identity_digest if selector else "",
                    network_binding_digest=network.binding_digest if network else "",
                    file_binding_digest=file_receipt.binding_digest if file_receipt else "",
                    secret_binding_digest=secret.binding_digest if secret else "",
                    policy_tags=after_schema.tags,
                )
            )
            hook_receipts.extend(after_security.receipts)
            if after_security.transformed:
                raise BrowserActionGatewayError(
                    "post_security_transform_requires_replan",
                    "hook transformed security-bound arguments; action must restart preflight",
                    failure_kind=ActionFailureKind.HOOK,
                )
            clipboard = self._clipboard_preflight(canonical_request, resolved, public_arguments, target_url)
            security = SecurityReceipts(
                selector=selector,
                network=network,
                file=file_receipt,
                secret=secret,
                clipboard=clipboard,
                form=form,
                hooks=tuple(hook_receipts),
            )
            assessment = self.classifier.classify(
                RiskContext(
                    request=canonical_request,
                    definition=resolved.definition,
                    selector=selector,
                    network=network,
                    file=file_receipt,
                    secret=secret,
                    form=form,
                    hook_tags=after_security.tags,
                    mode=self.config.execution_mode,
                )
            )
            before_permission = self.hooks.run(
                request_hook_context(
                    canonical_request,
                    phase=HookPhase.BEFORE_PERMISSION,
                    public_arguments=public_arguments,
                    registry_digest=self.registry.digest,
                    selector_binding_digest=selector.binding.identity_digest if selector else "",
                    network_binding_digest=network.binding_digest if network else "",
                    file_binding_digest=file_receipt.binding_digest if file_receipt else "",
                    secret_binding_digest=secret.binding_digest if secret else "",
                    policy_tags=assessment.risk_tags,
                )
            )
            hook_receipts.extend(before_permission.receipts)
            if before_permission.transformed:
                raise BrowserActionGatewayError(
                    "pre_permission_transform_requires_replan",
                    "hook transformed classified arguments; action must restart preflight",
                    failure_kind=ActionFailureKind.HOOK,
                )
            expires_at = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.config.receipt_ttl_seconds)).isoformat().replace("+00:00", "Z")
            receipt = ActionPreflightReceipt(
                receipt_id=stable_id(
                    "brpreflight",
                    canonical_request.identity.action_id,
                    canonical_request.request_digest,
                    self.registry.digest,
                    digest_value(public_arguments),
                    security.public_dict(),
                    assessment.to_dict(),
                ),
                action_id=canonical_request.identity.action_id,
                request_digest=canonical_request.request_digest,
                registry_digest=self.registry.digest,
                arguments_digest=digest_value(public_arguments),
                public_arguments=public_arguments,
                execution_arguments=public_arguments,
                definition=resolved.definition,
                assessment=assessment,
                selector_binding=selector.binding if selector else None,
                network_receipt_id=network.receipt_id if network else "",
                file_receipt_id=file_receipt.receipt_id if file_receipt else "",
                secret_receipt_id=secret.receipt_id if secret else "",
                clipboard_receipt_id=clipboard.receipt_id if clipboard else "",
                form_receipt_id=form.receipt_id if form else "",
                hook_receipt_ids=tuple(item.receipt_id for item in hook_receipts),
                expires_at=expires_at,
            )
            events.append(
                self.event_port.transition(
                    canonical_request.identity,
                    ActionPhase.HOOKS_CHECKED,
                    details={
                        "preflight_receipt_id": receipt.receipt_id,
                        "hook_receipt_ids": list(receipt.hook_receipt_ids),
                        "risk": assessment.to_dict(),
                    },
                )
            )
            if assessment.permission == PermissionDisposition.DENY:
                raise BrowserActionGatewayError(
                    "browser_action_policy_denied",
                    "browser action risk policy denied execution",
                    failure_kind=ActionFailureKind.PERMISSION,
                    details=assessment.to_dict(),
                )
            return PreparedBrowserAction(
                request=canonical_request,
                resolved=resolved,
                receipt=receipt,
                security=replace(security, hooks=tuple(hook_receipts)),
                selector_expectation=selector_expectation,
                target_url=target_url,
                events=tuple(events),
            )
        except Exception as exc:
            raise self._fail(request.identity, exc, events) from exc

    def authorize(self, prepared: PreparedBrowserAction) -> AuthorizedBrowserAction:
        try:
            decision = self.permission.guard(request=prepared.request, preflight=prepared.receipt)
            permission_events = self.event_port.permission_events(prepared.request.identity, decision.events)
            transition = self.event_port.transition(
                prepared.request.identity,
                ActionPhase.PERMISSION_CHECKED,
                details={
                    "decision": decision.public_dict(),
                    "risk": prepared.receipt.assessment.to_dict(),
                },
            )
            return AuthorizedBrowserAction(prepared, decision, (*permission_events, transition))
        except Exception as exc:
            if isinstance(exc, BrowserActionGatewayError):
                raise
            raise self._fail(prepared.request.identity, exc, prepared.events) from exc

    def execute(self, authorized: AuthorizedBrowserAction) -> CompletedBrowserAction:
        prepared = authorized.prepared
        if not authorized.allowed:
            raise BrowserActionGatewayError(
                "permission_not_allowed",
                "browser action permission is not allowed",
                failure_kind=ActionFailureKind.PERMISSION,
            )
        try:
            fresh = self._refresh(prepared)
            before_dispatch = self.hooks.run(
                request_hook_context(
                    fresh.request,
                    phase=HookPhase.BEFORE_DISPATCH,
                    public_arguments=fresh.receipt.public_arguments,
                    registry_digest=fresh.receipt.registry_digest,
                    selector_binding_digest=fresh.security.selector.binding.identity_digest if fresh.security.selector else "",
                    network_binding_digest=fresh.security.network.binding_digest if fresh.security.network else "",
                    file_binding_digest=fresh.security.file.binding_digest if fresh.security.file else "",
                    secret_binding_digest=fresh.security.secret.binding_digest if fresh.security.secret else "",
                    policy_tags=fresh.receipt.assessment.risk_tags,
                )
            )
            if before_dispatch.transformed:
                raise BrowserActionGatewayError(
                    "dispatch_transform_denied",
                    "arguments cannot change at the browser dispatch boundary",
                    failure_kind=ActionFailureKind.HOOK,
                )
            consumption = self.permission.consume(
                authorized.permission,
                request=fresh.request,
                fresh_preflight=fresh.receipt,
            )
            permission_events = self.event_port.permission_events(fresh.request.identity, consumption.events)
            if not consumption.accepted:
                raise BrowserActionGatewayError(
                    "permission_grant_rejected",
                    "browser action exact grant was rejected or replayed",
                    failure_kind=ActionFailureKind.PERMISSION,
                    details=consumption.public_dict(),
                    events=permission_events,
                )
            grant_event = self.event_port.transition(
                fresh.request.identity,
                ActionPhase.GRANT_CONSUMED,
                details=consumption.public_dict(),
            )
            geometry: GeometryReceipt | None = None
            if fresh.security.selector and fresh.security.selector.probe_required:
                if self.geometry_guard is None:
                    raise BrowserActionGatewayError(
                        "geometry_guard_unavailable",
                        "browser element action requires a geometry guard",
                        failure_kind=ActionFailureKind.NOT_INTERACTIVE,
                    )
                geometry = self.geometry_guard.preflight(
                    selector=fresh.security.selector,
                    action=fresh.resolved.canonical_name,
                )
                self.event_port.transition(
                    fresh.request.identity,
                    ActionPhase.GEOMETRY_CHECKED,
                    details={"geometry_receipt": geometry.public_dict()},
                )
            executing = self.event_port.transition(
                fresh.request.identity,
                ActionPhase.EXECUTING,
                details={"receipt_id": consumption.preflight.receipt_id},
            )
            result = self.executor.dispatch(
                ExecutionContext(
                    request=fresh.request,
                    permission=consumption,
                    bindings=ExecutionBindings(
                        selector=fresh.security.selector,
                        geometry=geometry,
                        network_receipt_id=fresh.security.network.receipt_id if fresh.security.network else "",
                        network_receipt=fresh.security.network,
                        file_receipt=fresh.security.file,
                        secret_receipt=fresh.security.secret,
                        clipboard_receipt=fresh.security.clipboard,
                        form_receipt=fresh.security.form,
                    ),
                )
            )
            projection = self.result_projector.project(
                request=fresh.request,
                receipt=consumption.preflight,
                result=result,
                redactor=SecretRedactor(),
            )
            result_events = self.event_port.success(fresh.request.identity, consumption.preflight, projection)
            return CompletedBrowserAction(
                fresh,
                consumption,
                geometry,
                result,
                projection,
                (*authorized.events, *permission_events, grant_event, executing, *result_events),
            )
        except Exception as exc:
            raise self._fail(prepared.request.identity, exc, authorized.events) from exc

    def run(
        self,
        request: ActionRequest,
        *,
        selector_expectation: SelectorExpectation | None = None,
    ) -> CompletedBrowserAction:
        prepared = self.prepare(request, selector_expectation=selector_expectation)
        authorized = self.authorize(prepared)
        if not authorized.allowed:
            code = "permission_required" if authorized.pending else "permission_denied"
            raise self._fail(
                request.identity,
                BrowserActionGatewayError(
                    code,
                    "browser action stopped at the permission boundary",
                    failure_kind=ActionFailureKind.PERMISSION,
                    details=authorized.permission.public_dict(),
                    events=authorized.events,
                ),
                prepared.events,
            )
        return self.execute(authorized)

    def prepare_batch(
        self,
        requests: Sequence[ActionRequest],
        *,
        selector_expectations: Mapping[str, SelectorExpectation] | None = None,
    ) -> tuple[PreparedBrowserAction, ...]:
        if not requests or len(requests) > self.config.max_actions_per_batch:
            raise BrowserActionGatewayError("batch_size_invalid", "browser action batch size is invalid")
        expectations = dict(selector_expectations or {})
        prepared: list[PreparedBrowserAction] = []
        try:
            for request in requests:
                prepared.append(self.prepare(request, selector_expectation=expectations.get(request.identity.action_id)))
        except Exception as exc:
            raise BrowserActionGatewayError(
                "batch_preflight_failed",
                "browser action batch failed atomic preflight; no action was authorized",
                failure_kind=getattr(exc, "failure_kind", ActionFailureKind.INTERNAL),
                details={"prepared_action_ids": [item.action_id for item in prepared], "failed": str(exc)},
            ) from exc
        return tuple(prepared)

    def _refresh(self, prepared: PreparedBrowserAction) -> PreparedBrowserAction:
        if receipt_expired(prepared.receipt):
            raise BrowserActionGatewayError(
                "preflight_receipt_expired",
                "browser action preflight receipt expired before dispatch",
                failure_kind=ActionFailureKind.DEADLINE,
            )
        selector = prepared.security.selector
        if selector:
            if self.selector_guard is None or prepared.selector_expectation is None:
                raise BrowserActionGatewayError(
                    "selector_guard_unavailable",
                    "browser selector cannot be refreshed",
                    failure_kind=ActionFailureKind.SELECTOR_STALE,
                )
            selector = self.selector_guard.revalidate(
                selector,
                expectation=prepared.selector_expectation,
                action=prepared.resolved.canonical_name,
                current_url=prepared.request.current_url,
                secret_input=bool(prepared.security.secret and prepared.security.secret.references),
            )
        network = prepared.security.network
        if network:
            if self.network_policy is None:
                raise BrowserActionGatewayError("network_policy_unavailable", "network receipt cannot be refreshed", failure_kind=ActionFailureKind.NETWORK)
            network = self.network_policy.revalidate(network)
        file_receipt = prepared.security.file
        if file_receipt:
            if self.file_policy is None:
                raise BrowserActionGatewayError("file_policy_unavailable", "file receipt cannot be refreshed", failure_kind=ActionFailureKind.FILE_CONTAINMENT)
            self.file_policy.revalidate(file_receipt)
        secret = prepared.security.secret
        if secret:
            if self.secret_policy is None:
                raise BrowserActionGatewayError("secret_policy_unavailable", "secret receipt cannot be refreshed", failure_kind=ActionFailureKind.SECRET_SCOPE)
            self.secret_policy.revalidate(secret, target_url=prepared.target_url)
        clipboard = prepared.security.clipboard
        if clipboard and self.clipboard_guard is None:
            raise BrowserActionGatewayError("clipboard_guard_unavailable", "clipboard receipt cannot be refreshed", failure_kind=ActionFailureKind.PERMISSION)
        form = prepared.security.form
        if form:
            if self.form_policy is None or selector is None:
                raise BrowserActionGatewayError("form_policy_unavailable", "form receipt cannot be refreshed", failure_kind=ActionFailureKind.NETWORK)
            form = self.form_policy.revalidate(
                form,
                selector=selector,
                current_url=prepared.request.current_url,
                arguments=prepared.receipt.public_arguments,
            )
        security = SecurityReceipts(
            selector=selector,
            network=network,
            file=file_receipt,
            secret=secret,
            clipboard=clipboard,
            form=form,
            hooks=prepared.security.hooks,
        )
        receipt = replace(
            prepared.receipt,
            selector_binding=selector.binding if selector else None,
            network_receipt_id=network.receipt_id if network else "",
            file_receipt_id=file_receipt.receipt_id if file_receipt else "",
            secret_receipt_id=secret.receipt_id if secret else "",
            clipboard_receipt_id=clipboard.receipt_id if clipboard else "",
            form_receipt_id=form.receipt_id if form else "",
        )
        return replace(prepared, security=security, receipt=receipt)

    def _selector_preflight(
        self,
        request: ActionRequest,
        resolved: ResolvedAction,
        *,
        selector_expectation: SelectorExpectation | None,
        target_url: str,
        public_arguments: Mapping[str, Any],
    ) -> SelectorReceipt | None:
        if not resolved.definition.selector_required:
            return None
        if self.selector_guard is None or selector_expectation is None:
            raise BrowserActionGatewayError(
                "selector_identity_required",
                "browser element action requires a complete 04B selector expectation",
                failure_kind=ActionFailureKind.SELECTOR_IDENTITY,
            )
        return self.selector_guard.resolve(
            action_id=request.identity.action_id,
            expectation=selector_expectation,
            action=resolved.canonical_name,
            current_url=request.current_url or target_url,
            secret_input=contains_secret_placeholder(public_arguments),
        )

    def _network_preflight(self, request: ActionRequest, resolved: ResolvedAction, target_url: str) -> NetworkReceipt | None:
        if not target_url or "network" not in {str(item) for item in resolved.definition.access}:
            return None
        if self.network_policy is None:
            if self.config.require_network_policy:
                raise BrowserActionGatewayError("network_policy_required", "network browser action has no policy", failure_kind=ActionFailureKind.NETWORK)
            return None
        return self.network_policy.preflight(action_id=request.identity.action_id, raw_url=target_url)

    def _form_preflight(
        self,
        request: ActionRequest,
        resolved: ResolvedAction,
        selector: SelectorReceipt | None,
        arguments: Mapping[str, Any],
    ) -> FormReceipt | None:
        if resolved.canonical_name != "submit_form":
            return None
        if self.form_policy is None or selector is None:
            raise BrowserActionGatewayError(
                "form_policy_required",
                "form submission requires selector-bound destination and effect policy",
                failure_kind=ActionFailureKind.NETWORK,
            )
        return self.form_policy.preflight(
            action_id=request.identity.action_id,
            selector=selector,
            current_url=request.current_url,
            arguments=arguments,
        )

    def _file_preflight(self, request: ActionRequest, resolved: ResolvedAction, arguments: Mapping[str, Any]) -> FileReceipt | None:
        action = resolved.canonical_name
        if action not in {"upload_file", "download_file", "save_as_pdf", "take_screenshot"}:
            return None
        if self.file_policy is None:
            if self.config.require_file_policy:
                raise BrowserActionGatewayError("file_policy_required", "file browser action has no owned policy", failure_kind=ActionFailureKind.FILE_CONTAINMENT)
            return None
        if action == "upload_file":
            paths = arguments.get("paths") or ([arguments.get("path")] if arguments.get("path") else [])
            receipt = self.file_policy.preflight_upload(action_id=request.identity.action_id, paths=paths)
            requested_max = int(arguments.get("max_bytes") or 0)
            if requested_max and sum(item.identity.size for item in receipt.uploads) > requested_max:
                raise BrowserActionGatewayError(
                    "upload_requested_quota_exceeded",
                    "upload files exceed the action-specific byte limit",
                    failure_kind=ActionFailureKind.FILE_CONTAINMENT,
                    details={
                        "maximum": requested_max,
                        "actual": sum(item.identity.size for item in receipt.uploads),
                    },
                )
            return receipt
        filename = str(
            arguments.get("filename")
            or arguments.get("suggested_filename")
            or arguments.get("file_name")
            or arguments.get("path")
            or default_filename(action, arguments)
        )
        intent = FileIntent.PDF if action == "save_as_pdf" else FileIntent.SCREENSHOT if action == "take_screenshot" else FileIntent.DOWNLOAD
        return self.file_policy.preflight_destination(action_id=request.identity.action_id, filename=filename, intent=intent)

    def _secret_preflight(
        self,
        request: ActionRequest,
        resolved: ResolvedAction,
        arguments: Mapping[str, Any],
        target_url: str,
    ) -> SecretReceipt | None:
        if not contains_secret_placeholder(arguments):
            return None
        if self.secret_policy is None or not target_url:
            raise BrowserActionGatewayError(
                "secret_policy_required",
                "browser secret placeholders require a scoped secret policy and destination",
                failure_kind=ActionFailureKind.SECRET_SCOPE,
            )
        return self.secret_policy.preflight(
            action_id=request.identity.action_id,
            arguments=arguments,
            target_url=target_url,
        )

    def _clipboard_preflight(
        self,
        request: ActionRequest,
        resolved: ResolvedAction,
        arguments: Mapping[str, Any],
        target_url: str,
    ) -> ClipboardReceipt | None:
        if resolved.canonical_name not in {"clipboard_read", "clipboard_write"}:
            return None
        if self.clipboard_guard is None or not target_url:
            raise BrowserActionGatewayError(
                "clipboard_guard_required",
                "clipboard actions require an exact-origin one-use guard",
                failure_kind=ActionFailureKind.PERMISSION,
            )
        access = ClipboardAccess.READ if resolved.canonical_name == "clipboard_read" else ClipboardAccess.WRITE
        return self.clipboard_guard.preflight(
            action_id=request.identity.action_id,
            target_url=target_url,
            browser_context_id=str(request.metadata.get("browser_context_id") or request.identity.browser_session_id),
            access=access,
            value=str(arguments.get("text", "")),
        )

    def _ensure_available(self) -> None:
        if self.config.disabled:
            raise BrowserActionGatewayError("gateway_disabled", "browser action gateway is disabled")
        if self.registry is None or self.classifier is None or self.hooks is None or self.permission is None or self.executor is None:
            raise BrowserActionGatewayError("gateway_dependency_missing", "browser action gateway dependency is missing")
        if self.config.require_event_port:
            self.event_port.ensure_available()
        self.registry.assert_self_contained()

    def _fail(self, identity: ActionIdentity, exc: Exception, prior_events: Sequence[EventRecord]) -> BrowserActionGatewayError:
        if isinstance(exc, BrowserActionGatewayError):
            code = exc.code
            kind = exc.failure_kind
            details = exc.details
            message = str(exc)
            embedded_events = exc.events
        else:
            code, kind = classify_exception(exc)
            details = getattr(exc, "details", {})
            message = str(exc)
            embedded_events = ()
        latest_phase = self.event_port.latest_phase(identity.action_id)
        after_grant = latest_phase in {
            ActionPhase.GRANT_CONSUMED,
            ActionPhase.GEOMETRY_CHECKED,
            ActionPhase.EXECUTING,
            ActionPhase.FAILED,
        }
        side_effect_count = max(
            0,
            int(
                getattr(exc, "side_effect_count", 0)
                or getattr(exc, "details", {}).get("side_effect_count", 0)
                if isinstance(getattr(exc, "details", {}), Mapping)
                else 0
            ),
        )
        blocked = not after_grant
        outcome_unknown = after_grant and latest_phase == ActionPhase.EXECUTING
        details = {
            **dict(details),
            "latest_phase": str(latest_phase) if latest_phase else "",
            "blocked_before_grant": blocked,
            "outcome_unknown": outcome_unknown,
            "side_effect_count": side_effect_count,
        }
        events: tuple[EventRecord, ...] = tuple(embedded_events)
        try:
            terminal = self.event_port.failure(
                identity,
                failure_kind=kind,
                code=code,
                message=message,
                details=details,
                blocked=blocked,
                side_effect_count=side_effect_count,
                outcome_unknown=outcome_unknown,
            )
            events = (*events, *terminal)
        except Exception as event_error:
            details["event_projection_error"] = f"{type(event_error).__name__}: {event_error}"
            details["outcome_unknown"] = after_grant
        return BrowserActionGatewayError(
            code,
            message,
            failure_kind=kind,
            details=details,
            events=(*tuple(prior_events), *events),
        )


def classify_exception(exc: Exception) -> tuple[str, ActionFailureKind]:
    if isinstance(exc, (BrowserActionRegistryError, ActionSchemaError)):
        return "schema_validation_failed", ActionFailureKind.SCHEMA
    if isinstance(exc, SelectorGuardError):
        return exc.code, ActionFailureKind.SELECTOR_STALE if "stale" in exc.code or "changed" in exc.code else ActionFailureKind.SELECTOR_IDENTITY
    if isinstance(exc, NetworkPolicyError):
        if exc.code == "dns_rebinding":
            return exc.code, ActionFailureKind.DNS_REBINDING
        return exc.code, ActionFailureKind.REDIRECT if "redirect" in exc.code else ActionFailureKind.NETWORK
    if isinstance(exc, BrowserFilePolicyError):
        return exc.code, ActionFailureKind.FILE_CONTAINMENT
    if isinstance(exc, FormPolicyError):
        return exc.code, ActionFailureKind.NETWORK
    if isinstance(exc, SecretPolicyError):
        return exc.code, ActionFailureKind.SECRET_SCOPE
    if isinstance(exc, GeometryGuardError):
        return exc.code, ActionFailureKind.OCCLUDED if "occlud" in exc.code else ActionFailureKind.NOT_INTERACTIVE
    if isinstance(exc, BrowserHookError):
        return exc.code, ActionFailureKind.DEADLINE if "timeout" in exc.code else ActionFailureKind.HOOK
    if isinstance(exc, PermissionBridgeError):
        return exc.code, ActionFailureKind.PERMISSION
    if isinstance(exc, RiskPolicyError):
        return exc.code, ActionFailureKind.PERMISSION
    if isinstance(exc, BrowserExecutionError):
        return exc.code, ActionFailureKind.EXECUTION
    return "browser_action_internal_error", ActionFailureKind.INTERNAL


def action_target_url(request: ActionRequest, arguments: Mapping[str, Any]) -> str:
    for key in ("url", "target_url", "download_url"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return request.target_url or request.current_url


def contains_secret_placeholder(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(contains_secret_placeholder(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return any(contains_secret_placeholder(item) for item in value)
    if isinstance(value, str):
        return "<secret>" in value or "${secret:" in value
    return False


def scrub_placeholders(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): scrub_placeholders(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [scrub_placeholders(item) for item in value]
    if isinstance(value, str) and contains_secret_placeholder(value):
        return {"$zyra_secret_placeholder": digest_value(value)}
    return value


def receipt_expired(receipt: ActionPreflightReceipt) -> bool:
    if not receipt.expires_at:
        return True
    try:
        expires = dt.datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return expires <= dt.datetime.now(dt.UTC)


def default_filename(action: str, arguments: Mapping[str, Any]) -> str:
    suffix = {
        "save_as_pdf": ".pdf",
        "take_screenshot": ".png",
        "download_file": ".bin",
    }.get(action, ".bin")
    return stable_id("browser-output", action, digest_value(arguments)) + suffix
