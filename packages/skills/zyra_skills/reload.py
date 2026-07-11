from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Iterable

from .events import SkillRuntimeEvent, SkillRuntimeEventKind
from .hooks import SkillHookRuntime
from .invocation import SkillInvocationRuntime
from .models import SkillRevision, utc_now
from .policy import SkillAllowedToolsPolicy
from .registry import SkillRegistry, SkillRegistryReloadResult


@dataclass(frozen=True, slots=True)
class SkillReloadLifecycleResult:
    registry: SkillRegistryReloadResult
    revoked_invocations: tuple[str, ...]
    removed_hook_leases: tuple[str, ...]
    removed_policy_snapshots: tuple[str, ...]
    events: tuple[SkillRuntimeEvent, ...]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry": self.registry.to_dict(),
            "revoked_invocations": list(self.revoked_invocations),
            "removed_hook_leases": list(self.removed_hook_leases),
            "removed_policy_snapshots": list(self.removed_policy_snapshots),
            "events": [event.to_dict() for event in self.events],
            "created_at": self.created_at,
        }


class SkillReloadCoordinator:
    """Atomic registry reload plus active invocation lifecycle cleanup."""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        invocation_runtime: SkillInvocationRuntime,
        hook_runtime: SkillHookRuntime,
        allowed_tools_policy: SkillAllowedToolsPolicy,
    ) -> None:
        self.registry = registry
        self.invocation_runtime = invocation_runtime
        self.hook_runtime = hook_runtime
        self.allowed_tools_policy = allowed_tools_policy
        self._lock = RLock()
        self._listeners: list[Callable[[SkillReloadLifecycleResult], None]] = []

    def subscribe(self, listener: Callable[[SkillReloadLifecycleResult], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners = [value for value in self._listeners if value is not listener]

        return unsubscribe

    def reload(self) -> SkillReloadLifecycleResult:
        with self._lock:
            previous = self.registry.snapshot()
            registry_result = self.registry.reload(expected_generation=previous.generation)
            if not registry_result.applied:
                result = SkillReloadLifecycleResult(
                    registry=registry_result,
                    revoked_invocations=(),
                    removed_hook_leases=(),
                    removed_policy_snapshots=(),
                    events=(
                        SkillRuntimeEvent(
                            kind=SkillRuntimeEventKind.RELOAD_REJECTED,
                            run_id="registry",
                            task_id="registry",
                            session_id="registry",
                            payload={"reload": registry_result.to_dict()},
                        ),
                    ),
                )
                self._notify(result)
                return result
            current = self.registry.snapshot()
            removed = set(registry_result.removed_refs)
            changed = set(registry_result.changed_refs)
            affected_refs = removed | {
                old_ref
                for qualified, old_ref in previous.active_by_qualified_name.items()
                if current.active_by_qualified_name.get(qualified) != old_ref
            }
            revoked_invocations: list[str] = []
            removed_hooks: list[str] = []
            removed_policies: list[str] = []
            for ref in sorted(affected_refs):
                revision = previous.revisions_by_ref.get(ref)
                if revision is None:
                    continue
                states = self.invocation_runtime.revoke_active_version(
                    revision,
                    reason="skill revision changed or was removed during atomic reload",
                )
                revoked_invocations.extend(state.invocation_id for state in states)
                hooks = self.hook_runtime.cleanup_version(
                    revision.version_ref,
                    reason="skill atomic reload",
                )
                removed_hooks.extend(item.lease_id for item in hooks)
                policies = self.allowed_tools_policy.clear_version(revision.version_ref)
                removed_policies.extend(item.snapshot_id for item in policies)
            result = SkillReloadLifecycleResult(
                registry=registry_result,
                revoked_invocations=tuple(sorted(set(revoked_invocations))),
                removed_hook_leases=tuple(sorted(set(removed_hooks))),
                removed_policy_snapshots=tuple(sorted(set(removed_policies))),
                events=(
                    SkillRuntimeEvent(
                        kind=SkillRuntimeEventKind.REGISTRY_SWAPPED,
                        run_id="registry",
                        task_id="registry",
                        session_id="registry",
                        payload={
                            "previous_generation": previous.generation,
                            "generation": current.generation,
                            "changed_refs": sorted(changed),
                            "removed_refs": sorted(removed),
                            "revoked_invocations": sorted(set(revoked_invocations)),
                        },
                    ),
                ),
            )
            self._notify(result)
            return result

    def _notify(self, result: SkillReloadLifecycleResult) -> None:
        listeners = tuple(self._listeners)
        for listener in listeners:
            listener(result)
