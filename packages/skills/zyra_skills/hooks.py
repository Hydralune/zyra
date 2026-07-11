from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Callable, Mapping

from .digests import digest_object
from .errors import SkillHookError
from .models import (
    SkillHookEvent,
    SkillHookLifetime,
    SkillHookSpec,
    SkillVersionRef,
    new_id,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class SkillHookLease:
    lease_id: str
    invocation_id: str
    session_id: str
    version_ref: SkillVersionRef
    hook_id: str
    event: SkillHookEvent
    lifetime: SkillHookLifetime
    action: str
    parameters: dict[str, Any]
    digest: str
    active: bool = True
    success_count: int = 0
    failure_count: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "version_ref": self.version_ref.to_dict(),
            "hook_id": self.hook_id,
            "event": str(self.event),
            "lifetime": str(self.lifetime),
            "action": self.action,
            "parameters": dict(self.parameters),
            "digest": self.digest,
            "active": self.active,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class SkillHookResult:
    lease_id: str
    event: SkillHookEvent
    ok: bool
    action: str
    output: dict[str, Any]
    error: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "event": str(self.event),
            "ok": self.ok,
            "action": self.action,
            "output": dict(self.output),
            "error": self.error,
            "created_at": self.created_at,
        }


class SkillHookRuntime:
    """Declarative invocation/session hook lease owner.

    Skill frontmatter cannot register Python callbacks or subprocess commands.
    Only product-owned handlers for a small declarative action set execute.
    Every terminal lifecycle path calls cleanup and removes active leases.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._leases: dict[str, SkillHookLease] = {}
        self._by_invocation: dict[str, list[str]] = {}
        self._by_session: dict[str, list[str]] = {}
        self._handlers: dict[str, Callable[[SkillHookLease, Mapping[str, Any]], dict[str, Any]]] = {
            "emit_event": _emit_event_handler,
            "attach_reference": _attach_reference_handler,
            "require_verification": _require_verification_handler,
        }

    def register(
        self,
        *,
        invocation_id: str,
        session_id: str,
        version_ref: SkillVersionRef,
        specs: tuple[SkillHookSpec, ...],
    ) -> tuple[SkillHookLease, ...]:
        leases: list[SkillHookLease] = []
        with self._lock:
            for spec in specs:
                payload = {
                    "invocation_id": invocation_id,
                    "session_id": session_id,
                    "version_ref": version_ref.to_dict(),
                    "spec": spec.to_dict(),
                }
                lease = SkillHookLease(
                    lease_id=new_id("skillhook"),
                    invocation_id=invocation_id,
                    session_id=session_id,
                    version_ref=version_ref,
                    hook_id=spec.hook_id,
                    event=spec.event,
                    lifetime=spec.lifetime,
                    action=spec.action,
                    parameters=dict(spec.parameters),
                    digest=digest_object(payload),
                )
                self._leases[lease.lease_id] = lease
                self._by_invocation.setdefault(invocation_id, []).append(lease.lease_id)
                self._by_session.setdefault(session_id, []).append(lease.lease_id)
                leases.append(lease)
        return tuple(leases)

    def fire(
        self,
        event: SkillHookEvent,
        *,
        invocation_id: str,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[SkillHookResult, ...]:
        with self._lock:
            leases = [
                self._leases[lease_id]
                for lease_id in self._by_invocation.get(invocation_id, ())
                if lease_id in self._leases
                and self._leases[lease_id].active
                and self._leases[lease_id].event is event
            ]
        results: list[SkillHookResult] = []
        for lease in leases:
            handler = self._handlers.get(lease.action)
            if handler is None:
                result = SkillHookResult(
                    lease_id=lease.lease_id,
                    event=event,
                    ok=False,
                    action=lease.action,
                    output={},
                    error="unsupported_skill_hook_action",
                )
            else:
                try:
                    output = handler(lease, dict(context or {}))
                    result = SkillHookResult(
                        lease_id=lease.lease_id,
                        event=event,
                        ok=True,
                        action=lease.action,
                        output=output,
                    )
                except Exception as error:  # noqa: BLE001 - hook failure becomes fail-closed evidence.
                    result = SkillHookResult(
                        lease_id=lease.lease_id,
                        event=event,
                        ok=False,
                        action=lease.action,
                        output={},
                        error=type(error).__name__,
                    )
            results.append(result)
            self._record_result(lease, result)
        return tuple(results)

    def cleanup_invocation(self, invocation_id: str, *, reason: str) -> tuple[SkillHookLease, ...]:
        with self._lock:
            ids = tuple(self._by_invocation.pop(invocation_id, ()))
            removed = tuple(self._deactivate(lease_id, reason=reason) for lease_id in ids if lease_id in self._leases)
            for lease in removed:
                session_ids = self._by_session.get(lease.session_id, [])
                self._by_session[lease.session_id] = [value for value in session_ids if value != lease.lease_id]
                if not self._by_session[lease.session_id]:
                    self._by_session.pop(lease.session_id, None)
            return removed

    def cleanup_session(self, session_id: str, *, reason: str = "session ended") -> tuple[SkillHookLease, ...]:
        with self._lock:
            invocation_ids = {
                self._leases[lease_id].invocation_id
                for lease_id in self._by_session.get(session_id, ())
                if lease_id in self._leases
            }
        removed: list[SkillHookLease] = []
        for invocation_id in invocation_ids:
            removed.extend(self.cleanup_invocation(invocation_id, reason=reason))
        return tuple(removed)

    def cleanup_version(self, version_ref: SkillVersionRef, *, reason: str) -> tuple[SkillHookLease, ...]:
        with self._lock:
            invocation_ids = {
                lease.invocation_id
                for lease in self._leases.values()
                if lease.active and lease.version_ref.content_digest == version_ref.content_digest
            }
        removed: list[SkillHookLease] = []
        for invocation_id in invocation_ids:
            removed.extend(self.cleanup_invocation(invocation_id, reason=reason))
        return tuple(removed)

    def active_for_session(self, session_id: str) -> tuple[SkillHookLease, ...]:
        with self._lock:
            return tuple(
                self._leases[lease_id]
                for lease_id in self._by_session.get(session_id, ())
                if lease_id in self._leases and self._leases[lease_id].active
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # Callbacks/handlers are intentionally not serialized.
            return {
                "version": 1,
                "leases": {
                    lease_id: lease.to_dict()
                    for lease_id, lease in self._leases.items()
                    if lease.active
                },
            }

    def _record_result(self, lease: SkillHookLease, result: SkillHookResult) -> None:
        with self._lock:
            current = self._leases.get(lease.lease_id)
            if current is None or not current.active:
                return
            updated = replace(
                current,
                success_count=current.success_count + (1 if result.ok else 0),
                failure_count=current.failure_count + (0 if result.ok else 1),
                updated_at=utc_now(),
            )
            if result.ok and current.lifetime is SkillHookLifetime.ONCE:
                updated = replace(updated, active=False)
            self._leases[lease.lease_id] = updated
            if not updated.active:
                self._remove_indexes(updated)

    def _deactivate(self, lease_id: str, *, reason: str) -> SkillHookLease:
        lease = self._leases[lease_id]
        updated = replace(
            lease,
            active=False,
            updated_at=utc_now(),
            parameters={**lease.parameters, "cleanup_reason": reason},
        )
        self._leases[lease_id] = updated
        return updated

    def _remove_indexes(self, lease: SkillHookLease) -> None:
        inv = self._by_invocation.get(lease.invocation_id, [])
        self._by_invocation[lease.invocation_id] = [value for value in inv if value != lease.lease_id]
        session = self._by_session.get(lease.session_id, [])
        self._by_session[lease.session_id] = [value for value in session if value != lease.lease_id]


def _emit_event_handler(lease: SkillHookLease, context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": str(lease.parameters.get("event_type") or "skill_hook"),
        "payload": {
            "hook_id": lease.hook_id,
            "invocation_id": lease.invocation_id,
            "context_digest": digest_object(context),
            **dict(lease.parameters.get("payload") or {}),
        },
    }


def _attach_reference_handler(lease: SkillHookLease, context: Mapping[str, Any]) -> dict[str, Any]:
    reference = str(lease.parameters.get("reference") or "")
    if not reference:
        raise SkillHookError("attach_reference hook is missing a reference")
    return {"attachment_ref": reference, "invocation_id": lease.invocation_id}


def _require_verification_handler(lease: SkillHookLease, context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verification_required": True,
        "reason": str(lease.parameters.get("reason") or "skill lifecycle requires verification"),
        "invocation_id": lease.invocation_id,
        "task_id": str(context.get("task_id") or ""),
    }
