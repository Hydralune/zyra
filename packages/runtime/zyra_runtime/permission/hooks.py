from __future__ import annotations

"""Deterministic permission hook orchestration.

The upstream Claude permission path lets hooks participate at two distinct
boundaries: before the deterministic tool guard and while an ``ask`` decision
is waiting for a resolver.  This module keeps those extension points without
letting a hook become an alternate permission authority.  A hook may tighten a
decision, annotate it, or propose rewritten arguments; every rewrite is
re-canonicalized and must be evaluated again by :class:`ToolPermissionRuntime`.

The implementation is intentionally independent from React, a plugin process,
or an LLM.  It is a Zyra-owned in-process port that records enough provenance
for the session/event layer to explain every proposal.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
from threading import RLock
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from zyra_core import new_id, now_iso, to_jsonable

from .canonical import arguments_digest, canonical_arguments_json


class PermissionHookStage(StrEnum):
    PRE_TOOL_USE = "pre_tool_use"
    PERMISSION_REQUEST = "permission_request"


class PermissionHookEffect(StrEnum):
    PASSTHROUGH = "passthrough"
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    ABORT = "abort"


class PermissionHookFailureMode(StrEnum):
    FAIL_CLOSED = "fail_closed"
    FAIL_ASK = "fail_ask"
    IGNORE = "ignore"


class PermissionHookStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PermissionHookInput:
    session_id: str
    run_id: str
    task_id: str
    worker_id: str
    tool_call_id: str
    tool_name: str
    server_name: str
    arguments: dict[str, Any]
    arguments_digest: str
    mode: str
    workspace_root: str
    interactive: bool
    sealed: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        worker_id: str,
        tool_call_id: str,
        tool_name: str,
        server_name: str,
        arguments: Mapping[str, Any],
        mode: str,
        workspace_root: str,
        interactive: bool,
        sealed: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionHookInput":
        copied = _copy_arguments(arguments)
        return cls(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            worker_id=worker_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            server_name=server_name,
            arguments=copied,
            arguments_digest=arguments_digest(copied),
            mode=mode,
            workspace_root=workspace_root,
            interactive=interactive,
            sealed=sealed,
            metadata=dict(metadata or {}),
        )

    def with_arguments(self, arguments: Mapping[str, Any]) -> "PermissionHookInput":
        copied = _copy_arguments(arguments)
        return replace(
            self,
            arguments=copied,
            arguments_digest=arguments_digest(copied),
        )

    def to_dict(self, *, include_arguments: bool = False) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "server_name": self.server_name,
            "arguments_digest": self.arguments_digest,
            "mode": self.mode,
            "workspace_root": self.workspace_root,
            "interactive": self.interactive,
            "sealed": self.sealed,
            "metadata": _safe_metadata(self.metadata),
        }
        if include_arguments:
            payload["arguments"] = to_jsonable(self.arguments)
        return payload


@dataclass(frozen=True, slots=True)
class PermissionHookProposal:
    effect: PermissionHookEffect = PermissionHookEffect.PASSTHROUGH
    reason: str = ""
    updated_arguments: dict[str, Any] | None = None
    additional_context: dict[str, Any] = field(default_factory=dict)
    permission_updates: tuple[dict[str, Any], ...] = ()
    prevent_continuation: bool = False
    stop_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passthrough(cls, reason: str = "") -> "PermissionHookProposal":
        return cls(effect=PermissionHookEffect.PASSTHROUGH, reason=reason)

    @classmethod
    def allow(
        cls,
        reason: str,
        *,
        updated_arguments: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionHookProposal":
        return cls(
            effect=PermissionHookEffect.ALLOW,
            reason=reason,
            updated_arguments=None if updated_arguments is None else _copy_arguments(updated_arguments),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def ask(cls, reason: str, *, metadata: Mapping[str, Any] | None = None) -> "PermissionHookProposal":
        return cls(effect=PermissionHookEffect.ASK, reason=reason, metadata=dict(metadata or {}))

    @classmethod
    def deny(cls, reason: str, *, metadata: Mapping[str, Any] | None = None) -> "PermissionHookProposal":
        return cls(effect=PermissionHookEffect.DENY, reason=reason, metadata=dict(metadata or {}))

    @classmethod
    def abort(cls, reason: str, *, metadata: Mapping[str, Any] | None = None) -> "PermissionHookProposal":
        return cls(
            effect=PermissionHookEffect.ABORT,
            reason=reason,
            prevent_continuation=True,
            stop_reason=reason,
            metadata=dict(metadata or {}),
        )

    def to_dict(self, *, include_arguments: bool = False) -> dict[str, Any]:
        payload = {
            "effect": str(self.effect),
            "reason": self.reason,
            "updated_arguments_digest": (
                arguments_digest(self.updated_arguments) if self.updated_arguments is not None else ""
            ),
            "additional_context": to_jsonable(self.additional_context),
            "permission_updates": [to_jsonable(item) for item in self.permission_updates],
            "prevent_continuation": self.prevent_continuation,
            "stop_reason": self.stop_reason,
            "metadata": _safe_metadata(self.metadata),
        }
        if include_arguments and self.updated_arguments is not None:
            payload["updated_arguments"] = to_jsonable(self.updated_arguments)
        return payload


class PermissionHook(Protocol):
    def __call__(self, hook_input: PermissionHookInput) -> PermissionHookProposal | Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class RegisteredPermissionHook:
    name: str
    callback: PermissionHook
    stage: PermissionHookStage = PermissionHookStage.PRE_TOOL_USE
    source: str = "runtime"
    priority: int = 100
    timeout_seconds: float = 5.0
    failure_mode: PermissionHookFailureMode = PermissionHookFailureMode.FAIL_ASK
    enabled: bool = True
    hook_id: str = field(default_factory=lambda: new_id("permhook"))
    metadata: dict[str, str] = field(default_factory=dict)

    def descriptor(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "name": self.name,
            "stage": str(self.stage),
            "source": self.source,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "failure_mode": str(self.failure_mode),
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionHookInvocation:
    invocation_id: str
    hook_id: str
    hook_name: str
    source: str
    stage: PermissionHookStage
    status: PermissionHookStatus
    input_digest: str
    output_digest: str
    proposal: PermissionHookProposal
    started_at: str
    completed_at: str
    duration_ms: int
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tightened(self) -> bool:
        return self.proposal.effect in {
            PermissionHookEffect.ASK,
            PermissionHookEffect.DENY,
            PermissionHookEffect.ABORT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "hook_id": self.hook_id,
            "hook_name": self.hook_name,
            "source": self.source,
            "stage": str(self.stage),
            "status": str(self.status),
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "proposal": self.proposal.to_dict(include_arguments=False),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "tightened": self.tightened,
            "metadata": _safe_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionHookAggregate:
    stage: PermissionHookStage
    original_input: PermissionHookInput
    effective_input: PermissionHookInput
    effect: PermissionHookEffect
    reason: str
    invocations: tuple[PermissionHookInvocation, ...]
    additional_context: tuple[dict[str, Any], ...] = ()
    permission_updates: tuple[dict[str, Any], ...] = ()
    prevent_continuation: bool = False
    stop_reason: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def input_changed(self) -> bool:
        return self.original_input.arguments_digest != self.effective_input.arguments_digest

    @property
    def has_failures(self) -> bool:
        return any(item.status in {PermissionHookStatus.FAILED, PermissionHookStatus.TIMED_OUT} for item in self.invocations)

    @property
    def allow_is_advisory(self) -> bool:
        return self.effect == PermissionHookEffect.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "effect": str(self.effect),
            "reason": self.reason,
            "original_input": self.original_input.to_dict(include_arguments=False),
            "effective_input": self.effective_input.to_dict(include_arguments=False),
            "input_changed": self.input_changed,
            "has_failures": self.has_failures,
            "allow_is_advisory": self.allow_is_advisory,
            "invocations": [item.to_dict() for item in self.invocations],
            "additional_context": [to_jsonable(item) for item in self.additional_context],
            "permission_updates": [to_jsonable(item) for item in self.permission_updates],
            "prevent_continuation": self.prevent_continuation,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
        }


class PermissionHookAdapter:
    """Runs hook proposals in stable order and aggregates with deny precedence.

    ``allow`` is deliberately only a proposal.  The evaluator still executes
    all rule, tool-specific, safety, and mode checks after this adapter returns.
    ``deny``/``ask``/``abort`` may only tighten the downstream decision.
    """

    def __init__(self, hooks: Iterable[RegisteredPermissionHook] = ()) -> None:
        self._lock = RLock()
        self._hooks: dict[str, RegisteredPermissionHook] = {}
        for hook in hooks:
            self.register(hook)

    def register(self, hook: RegisteredPermissionHook) -> RegisteredPermissionHook:
        if not hook.name.strip():
            raise ValueError("permission hook name must not be empty")
        if hook.timeout_seconds <= 0:
            raise ValueError("permission hook timeout_seconds must be positive")
        with self._lock:
            if hook.hook_id in self._hooks:
                raise ValueError(f"permission hook id already registered: {hook.hook_id}")
            self._hooks[hook.hook_id] = hook
        return hook

    def unregister(self, hook_id: str) -> bool:
        with self._lock:
            return self._hooks.pop(hook_id, None) is not None

    def clear(self, *, stage: PermissionHookStage | None = None) -> int:
        with self._lock:
            if stage is None:
                count = len(self._hooks)
                self._hooks.clear()
                return count
            ids = [hook_id for hook_id, hook in self._hooks.items() if hook.stage == stage]
            for hook_id in ids:
                self._hooks.pop(hook_id, None)
            return len(ids)

    def list_hooks(self, *, stage: PermissionHookStage | None = None) -> tuple[RegisteredPermissionHook, ...]:
        with self._lock:
            hooks = [hook for hook in self._hooks.values() if stage is None or hook.stage == stage]
        return tuple(sorted(hooks, key=lambda item: (item.priority, item.source, item.name, item.hook_id)))

    def descriptors(self) -> list[dict[str, Any]]:
        return [hook.descriptor() for hook in self.list_hooks()]

    def run_pre_tool_use(self, hook_input: PermissionHookInput) -> PermissionHookAggregate:
        return self.run(PermissionHookStage.PRE_TOOL_USE, hook_input)

    def run_permission_request(self, hook_input: PermissionHookInput) -> PermissionHookAggregate:
        return self.run(PermissionHookStage.PERMISSION_REQUEST, hook_input)

    def run(self, stage: PermissionHookStage, hook_input: PermissionHookInput) -> PermissionHookAggregate:
        effective = hook_input
        invocations: list[PermissionHookInvocation] = []
        contexts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        proposals: list[tuple[RegisteredPermissionHook, PermissionHookProposal]] = []
        prevent_continuation = False
        stop_reason = ""

        for hook in self.list_hooks(stage=stage):
            if not hook.enabled:
                invocations.append(self._skipped_invocation(hook, effective))
                continue
            invocation = self._invoke(hook, effective)
            invocations.append(invocation)
            proposal = invocation.proposal
            proposals.append((hook, proposal))
            if proposal.updated_arguments is not None and invocation.status == PermissionHookStatus.COMPLETED:
                effective = effective.with_arguments(proposal.updated_arguments)
            if proposal.additional_context:
                contexts.append(dict(proposal.additional_context))
            if proposal.permission_updates:
                updates.extend(dict(item) for item in proposal.permission_updates)
            if proposal.prevent_continuation:
                prevent_continuation = True
                if proposal.stop_reason and not stop_reason:
                    stop_reason = proposal.stop_reason

        effect, reason = _aggregate_effect(proposals)
        if prevent_continuation and effect != PermissionHookEffect.ABORT:
            effect = PermissionHookEffect.ABORT
            reason = stop_reason or reason or "permission hook stopped continuation"
        return PermissionHookAggregate(
            stage=stage,
            original_input=hook_input,
            effective_input=effective,
            effect=effect,
            reason=reason,
            invocations=tuple(invocations),
            additional_context=tuple(contexts),
            permission_updates=tuple(updates),
            prevent_continuation=prevent_continuation,
            stop_reason=stop_reason,
        )

    def _invoke(self, hook: RegisteredPermissionHook, hook_input: PermissionHookInput) -> PermissionHookInvocation:
        started_at = now_iso()
        started = monotonic()
        status = PermissionHookStatus.COMPLETED
        error = ""
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"permission-hook-{hook.name[:24]}")
        try:
            future = pool.submit(hook.callback, hook_input)
            raw = future.result(timeout=hook.timeout_seconds)
            proposal = _coerce_proposal(raw)
        except FutureTimeout:
            status = PermissionHookStatus.TIMED_OUT
            error = "permission_hook_timeout"
            proposal = _failure_proposal(hook.failure_mode, f"hook {hook.name} timed out")
        except Exception as exc:  # noqa: BLE001 - extension failures must become auditable proposals.
            status = PermissionHookStatus.FAILED
            error = type(exc).__name__
            proposal = _failure_proposal(hook.failure_mode, f"hook {hook.name} failed: {type(exc).__name__}")
        finally:
            # Context-manager shutdown waits for a timed-out callback and would
            # silently defeat the policy deadline.  A running Python thread
            # cannot be force-killed, so detach it and fail closed immediately.
            pool.shutdown(wait=False, cancel_futures=True)
        duration_ms = max(0, int((monotonic() - started) * 1000))
        output_digest = _proposal_digest(proposal)
        return PermissionHookInvocation(
            invocation_id=new_id("permhookrun"),
            hook_id=hook.hook_id,
            hook_name=hook.name,
            source=hook.source,
            stage=hook.stage,
            status=status,
            input_digest=hook_input.arguments_digest,
            output_digest=output_digest,
            proposal=proposal,
            started_at=started_at,
            completed_at=now_iso(),
            duration_ms=duration_ms,
            error=error,
            metadata={
                "priority": hook.priority,
                "failure_mode": str(hook.failure_mode),
                **dict(hook.metadata),
            },
        )

    def _skipped_invocation(
        self,
        hook: RegisteredPermissionHook,
        hook_input: PermissionHookInput,
    ) -> PermissionHookInvocation:
        proposal = PermissionHookProposal.passthrough("hook disabled")
        timestamp = now_iso()
        return PermissionHookInvocation(
            invocation_id=new_id("permhookrun"),
            hook_id=hook.hook_id,
            hook_name=hook.name,
            source=hook.source,
            stage=hook.stage,
            status=PermissionHookStatus.SKIPPED,
            input_digest=hook_input.arguments_digest,
            output_digest=_proposal_digest(proposal),
            proposal=proposal,
            started_at=timestamp,
            completed_at=timestamp,
            duration_ms=0,
            metadata={"reason": "disabled"},
        )


def _aggregate_effect(
    proposals: Sequence[tuple[RegisteredPermissionHook, PermissionHookProposal]],
) -> tuple[PermissionHookEffect, str]:
    precedence = {
        PermissionHookEffect.PASSTHROUGH: 0,
        PermissionHookEffect.ALLOW: 1,
        PermissionHookEffect.ASK: 2,
        PermissionHookEffect.DENY: 3,
        PermissionHookEffect.ABORT: 4,
    }
    selected: tuple[RegisteredPermissionHook, PermissionHookProposal] | None = None
    for pair in proposals:
        if selected is None or precedence[pair[1].effect] > precedence[selected[1].effect]:
            selected = pair
    if selected is None:
        return PermissionHookEffect.PASSTHROUGH, "no permission hook proposal"
    hook, proposal = selected
    reason = proposal.reason or f"permission hook {hook.name} proposed {proposal.effect}"
    return proposal.effect, reason


def _coerce_proposal(raw: PermissionHookProposal | Mapping[str, Any] | None) -> PermissionHookProposal:
    if raw is None:
        return PermissionHookProposal.passthrough()
    if isinstance(raw, PermissionHookProposal):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("permission hook result must be PermissionHookProposal, mapping, or None")
    effect_value = str(raw.get("effect") or raw.get("behavior") or PermissionHookEffect.PASSTHROUGH)
    try:
        effect = PermissionHookEffect(effect_value)
    except ValueError as exc:
        raise ValueError(f"invalid permission hook effect: {effect_value}") from exc
    updated = raw.get("updated_arguments")
    if updated is None:
        updated = raw.get("updated_input")
    if updated is not None and not isinstance(updated, Mapping):
        raise TypeError("permission hook updated_arguments must be an object")
    contexts = raw.get("additional_context")
    context = dict(contexts) if isinstance(contexts, Mapping) else {}
    raw_updates = raw.get("permission_updates")
    updates = tuple(dict(item) for item in raw_updates if isinstance(item, Mapping)) if isinstance(raw_updates, list | tuple) else ()
    metadata = raw.get("metadata")
    return PermissionHookProposal(
        effect=effect,
        reason=str(raw.get("reason") or raw.get("message") or ""),
        updated_arguments=None if updated is None else _copy_arguments(updated),
        additional_context=context,
        permission_updates=updates,
        prevent_continuation=bool(raw.get("prevent_continuation", False)),
        stop_reason=str(raw.get("stop_reason") or ""),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _failure_proposal(mode: PermissionHookFailureMode, reason: str) -> PermissionHookProposal:
    if mode == PermissionHookFailureMode.FAIL_CLOSED:
        return PermissionHookProposal.deny(reason, metadata={"hook_failure": "true"})
    if mode == PermissionHookFailureMode.FAIL_ASK:
        return PermissionHookProposal.ask(reason, metadata={"hook_failure": "true"})
    return PermissionHookProposal.passthrough(reason)


def _copy_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    # Canonical JSON round-tripping both deep-copies and rejects values that
    # would not have a stable cross-process identity.
    return json.loads(canonical_arguments_json(arguments))


def _proposal_digest(proposal: PermissionHookProposal) -> str:
    payload = proposal.to_dict(include_arguments=False)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


_SENSITIVE_METADATA_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "private_key",
)


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized = str(key).lower()
        if any(fragment in normalized for fragment in _SENSITIVE_METADATA_FRAGMENTS):
            safe[str(key)] = "[REDACTED]"
        else:
            safe[str(key)] = to_jsonable(value)
    return safe


__all__ = [
    "PermissionHook",
    "PermissionHookAdapter",
    "PermissionHookAggregate",
    "PermissionHookEffect",
    "PermissionHookFailureMode",
    "PermissionHookInput",
    "PermissionHookInvocation",
    "PermissionHookProposal",
    "PermissionHookStage",
    "PermissionHookStatus",
    "RegisteredPermissionHook",
]
