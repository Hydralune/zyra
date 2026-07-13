from __future__ import annotations

import concurrent.futures
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from .models import ActionRequest, digest_value, stable_id


class BrowserHookError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class HookPhase(StrEnum):
    BEFORE_SCHEMA = "before_schema"
    AFTER_SCHEMA = "after_schema"
    AFTER_SECURITY = "after_security"
    BEFORE_PERMISSION = "before_permission"
    BEFORE_DISPATCH = "before_dispatch"
    AFTER_RESULT = "after_result"


class HookDisposition(StrEnum):
    CONTINUE = "continue"
    TRANSFORM = "transform"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class HookContext:
    phase: HookPhase
    action_id: str
    action: str
    public_arguments: Mapping[str, Any]
    arguments_digest: str
    request_digest: str
    registry_digest: str
    selector_binding_digest: str = ""
    network_binding_digest: str = ""
    file_binding_digest: str = ""
    secret_binding_digest: str = ""
    policy_tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_arguments", MappingProxyType(dict(self.public_arguments)))
        object.__setattr__(self, "policy_tags", tuple(self.policy_tags))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def binding_digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "action_id": self.action_id,
            "action": self.action,
            "public_arguments": dict(self.public_arguments),
            "arguments_digest": self.arguments_digest,
            "request_digest": self.request_digest,
            "registry_digest": self.registry_digest,
            "selector_binding_digest": self.selector_binding_digest,
            "network_binding_digest": self.network_binding_digest,
            "file_binding_digest": self.file_binding_digest,
            "secret_binding_digest": self.secret_binding_digest,
            "policy_tags": list(self.policy_tags),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HookDecision:
    disposition: HookDisposition
    reason: str = ""
    transformed_arguments: Mapping[str, Any] | None = None
    tags: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.disposition == HookDisposition.BLOCK and not self.reason:
            raise ValueError("blocking browser hook decision requires a reason")
        if self.disposition == HookDisposition.TRANSFORM and self.transformed_arguments is None:
            raise ValueError("transforming browser hook decision requires arguments")
        if self.disposition != HookDisposition.TRANSFORM and self.transformed_arguments is not None:
            raise ValueError("only transform browser hook decisions may return arguments")
        if self.transformed_arguments is not None:
            object.__setattr__(self, "transformed_arguments", MappingProxyType(dict(self.transformed_arguments)))
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @classmethod
    def continue_(cls, *tags: str) -> "HookDecision":
        return cls(HookDisposition.CONTINUE, tags=tuple(tags))

    @classmethod
    def block(cls, reason: str, **details: Any) -> "HookDecision":
        return cls(HookDisposition.BLOCK, reason=reason, details=details)

    @classmethod
    def transform(cls, arguments: Mapping[str, Any], reason: str, *tags: str) -> "HookDecision":
        return cls(HookDisposition.TRANSFORM, reason=reason, transformed_arguments=arguments, tags=tuple(tags))


@dataclass(frozen=True, slots=True)
class HookDefinition:
    hook_id: str
    phase: HookPhase
    handler: Callable[[HookContext], HookDecision] = field(repr=False, compare=False)
    priority: int = 100
    timeout_ms: int = 250
    source: str = "zyra_builtin"
    enabled: bool = True
    allow_transform: bool = False

    def __post_init__(self) -> None:
        if not self.hook_id or not self.source:
            raise ValueError("browser hook requires stable id and source")
        if self.timeout_ms < 1 or self.timeout_ms > 10_000:
            raise ValueError("browser hook timeout must be between 1 and 10000 milliseconds")
        if not callable(self.handler):
            raise TypeError("browser hook handler must be callable")


@dataclass(frozen=True, slots=True)
class HookReceipt:
    receipt_id: str
    hook_id: str
    phase: HookPhase
    action_id: str
    context_digest: str
    input_arguments_digest: str
    output_arguments_digest: str
    disposition: HookDisposition
    tags: tuple[str, ...]
    duration_ms: float
    reason: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "hook_id": self.hook_id,
            "phase": str(self.phase),
            "action_id": self.action_id,
            "context_digest": self.context_digest,
            "input_arguments_digest": self.input_arguments_digest,
            "output_arguments_digest": self.output_arguments_digest,
            "disposition": str(self.disposition),
            "tags": list(self.tags),
            "duration_ms": self.duration_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class HookChainResult:
    arguments: Mapping[str, Any]
    receipts: tuple[HookReceipt, ...]
    tags: tuple[str, ...]
    transformed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        object.__setattr__(self, "receipts", tuple(self.receipts))
        object.__setattr__(self, "tags", tuple(self.tags))


class BrowserPreflightHookRegistry:
    """Static deny-first hook chain inspired by OMP's central wrapper.

    Hooks are registered programmatically by trusted Zyra construction code.
    There is no dynamic import, marketplace lookup or yolo bypass.  A hook can
    only block, annotate or transform public arguments; transformed arguments
    must pass schema, risk and permission again in the outer gateway.
    """

    def __init__(self, hooks: Sequence[HookDefinition] = (), *, disabled: bool = False) -> None:
        self.disabled = disabled
        by_id: dict[str, HookDefinition] = {}
        for hook in hooks:
            if hook.hook_id in by_id:
                raise ValueError(f"duplicate browser hook id: {hook.hook_id}")
            by_id[hook.hook_id] = hook
        self._hooks = MappingProxyType(by_id)
        self._consumed_before_dispatch: set[str] = set()
        self._lock = threading.Lock()

    @property
    def digest(self) -> str:
        return digest_value(
            [
                {
                    "hook_id": hook.hook_id,
                    "phase": str(hook.phase),
                    "priority": hook.priority,
                    "timeout_ms": hook.timeout_ms,
                    "source": hook.source,
                    "enabled": hook.enabled,
                    "allow_transform": hook.allow_transform,
                }
                for hook in sorted(self._hooks.values(), key=lambda item: item.hook_id)
            ]
        )

    def run(self, context: HookContext) -> HookChainResult:
        if self.disabled:
            raise BrowserHookError("hook_registry_disabled", "browser preflight hook registry is disabled")
        arguments = dict(context.public_arguments)
        receipts: list[HookReceipt] = []
        tags: set[str] = set(context.policy_tags)
        transformed = False
        hooks = sorted(
            (hook for hook in self._hooks.values() if hook.enabled and hook.phase == context.phase),
            key=lambda item: (item.priority, item.hook_id),
        )
        for hook in hooks:
            current_context = HookContext(
                phase=context.phase,
                action_id=context.action_id,
                action=context.action,
                public_arguments=arguments,
                arguments_digest=digest_value(arguments),
                request_digest=context.request_digest,
                registry_digest=context.registry_digest,
                selector_binding_digest=context.selector_binding_digest,
                network_binding_digest=context.network_binding_digest,
                file_binding_digest=context.file_binding_digest,
                secret_binding_digest=context.secret_binding_digest,
                policy_tags=tuple(sorted(tags)),
                metadata=context.metadata,
            )
            decision, duration_ms = self._invoke(hook, current_context)
            input_digest = digest_value(arguments)
            output_digest = input_digest
            if decision.disposition == HookDisposition.BLOCK:
                raise BrowserHookError(
                    "hook_blocked",
                    f"browser preflight hook {hook.hook_id} blocked the action: {decision.reason}",
                    details={"hook_id": hook.hook_id, **dict(decision.details)},
                )
            if decision.disposition == HookDisposition.TRANSFORM:
                if not hook.allow_transform:
                    raise BrowserHookError(
                        "hook_transform_denied",
                        f"browser hook {hook.hook_id} is not allowed to transform arguments",
                    )
                arguments = dict(decision.transformed_arguments or {})
                output_digest = digest_value(arguments)
                transformed = transformed or output_digest != input_digest
            tags.update(decision.tags)
            receipt_id = stable_id(
                "brhook",
                hook.hook_id,
                context.action_id,
                context.phase,
                current_context.binding_digest,
                input_digest,
                output_digest,
            )
            receipts.append(
                HookReceipt(
                    receipt_id=receipt_id,
                    hook_id=hook.hook_id,
                    phase=context.phase,
                    action_id=context.action_id,
                    context_digest=current_context.binding_digest,
                    input_arguments_digest=input_digest,
                    output_arguments_digest=output_digest,
                    disposition=decision.disposition,
                    tags=decision.tags,
                    duration_ms=duration_ms,
                    reason=decision.reason,
                )
            )
        if context.phase == HookPhase.BEFORE_DISPATCH:
            chain_digest = digest_value([receipt.receipt_id for receipt in receipts])
            key = f"{context.action_id}:{chain_digest}:{digest_value(arguments)}"
            with self._lock:
                if key in self._consumed_before_dispatch:
                    raise BrowserHookError("hook_receipt_replayed", "before-dispatch hook chain can run only once")
                self._consumed_before_dispatch.add(key)
        return HookChainResult(arguments, tuple(receipts), tuple(sorted(tags)), transformed)

    @staticmethod
    def _invoke(hook: HookDefinition, context: HookContext) -> tuple[HookDecision, float]:
        started = time.monotonic()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"zyra-hook-{hook.hook_id}")
        future = executor.submit(hook.handler, context)
        try:
            decision = future.result(timeout=hook.timeout_ms / 1000)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise BrowserHookError("hook_timeout", f"browser hook {hook.hook_id} exceeded its deadline") from exc
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise BrowserHookError("hook_failed", f"browser hook {hook.hook_id} failed closed: {exc}") from exc
        else:
            executor.shutdown(wait=True)
        if not isinstance(decision, HookDecision):
            raise BrowserHookError("hook_invalid_result", f"browser hook {hook.hook_id} returned an invalid decision")
        return decision, (time.monotonic() - started) * 1000


def request_hook_context(
    request: ActionRequest,
    *,
    phase: HookPhase,
    public_arguments: Mapping[str, Any],
    registry_digest: str,
    selector_binding_digest: str = "",
    network_binding_digest: str = "",
    file_binding_digest: str = "",
    secret_binding_digest: str = "",
    policy_tags: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> HookContext:
    return HookContext(
        phase=phase,
        action_id=request.identity.action_id,
        action=request.action,
        public_arguments=public_arguments,
        arguments_digest=digest_value(public_arguments),
        request_digest=request.request_digest,
        registry_digest=registry_digest,
        selector_binding_digest=selector_binding_digest,
        network_binding_digest=network_binding_digest,
        file_binding_digest=file_binding_digest,
        secret_binding_digest=secret_binding_digest,
        policy_tags=tuple(policy_tags),
        metadata=dict(metadata or {}),
    )

def deny_coordinate_mutation(context: HookContext) -> HookDecision:
    if context.action in {"click_element", "drag_element"}:
        arguments = context.public_arguments
        if any(key in arguments for key in ("x", "y", "coordinate_x", "coordinate_y")):
            return HookDecision.block("sealed browser mutations require selector identity, not raw coordinates")
    return HookDecision.continue_("selector_only_mutation")


def deny_unbounded_script(context: HookContext) -> HookDecision:
    if context.action == "evaluate_js":
        return HookDecision.continue_("arbitrary_script_requires_explicit_permission")
    return HookDecision.continue_()


def default_preflight_hooks() -> BrowserPreflightHookRegistry:
    return BrowserPreflightHookRegistry(
        (
            HookDefinition(
                "selector-only-mutation",
                HookPhase.AFTER_SCHEMA,
                deny_coordinate_mutation,
                priority=10,
                timeout_ms=100,
                source="zyra_workers.browser_action",
            ),
            HookDefinition(
                "script-risk-tag",
                HookPhase.BEFORE_PERMISSION,
                deny_unbounded_script,
                priority=20,
                timeout_ms=100,
                source="zyra_workers.browser_action",
            ),
        )
    )
