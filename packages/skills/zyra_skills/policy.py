from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .digests import digest_object
from .errors import SkillAllowedToolsPolicyDisabled, SkillPolicyDenied
from .models import (
    SkillPolicySnapshot,
    SkillRevision,
    SkillVersionRef,
    ToolSelector,
    new_id,
)


@dataclass(frozen=True, slots=True)
class ToolUseIdentity:
    namespace: str
    name: str
    server_id: str = ""
    operation: str = "execute"
    path: str = ""
    domain: str = ""

    @property
    def canonical_name(self) -> str:
        server = f"/{self.server_id}" if self.server_id else ""
        return f"{self.namespace}{server}/{self.name}"


@dataclass(frozen=True, slots=True)
class SkillToolPolicyDecision:
    allowed_by_ceiling: bool
    reason: str
    invocation_ids: tuple[str, ...]
    matching_selectors: tuple[ToolSelector, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_by_ceiling": self.allowed_by_ceiling,
            "reason": self.reason,
            "invocation_ids": list(self.invocation_ids),
            "matching_selectors": [item.to_dict() for item in self.matching_selectors],
            "metadata": dict(self.metadata),
        }


class PermissionHookAdapterPort(Protocol):
    def register(self, hook: Any) -> Any: ...

    def unregister(self, hook_id: str) -> bool: ...


class SkillAllowedToolsPolicy:
    """Session/invocation capability ceiling; never an authorization source.

    A missing ``allowed-tools`` field means the skill adds no extra ceiling.
    An explicit empty list denies every tool within that invocation. Nested
    scopes intersect. A matching selector returns passthrough, so 03A still
    performs risk/rule/mode evaluation and issues an exact one-use grant.
    """

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._lock = RLock()
        self._snapshots: dict[str, SkillPolicySnapshot] = {}
        self._active_by_session: dict[str, list[str]] = {}
        self._active_by_invocation: dict[str, str] = {}
        self._registered_hook_ids: dict[int, str] = {}

    def bind(
        self,
        *,
        invocation_id: str,
        session_id: str,
        revision: SkillRevision,
        parent_snapshot_ids: Sequence[str] = (),
    ) -> SkillPolicySnapshot:
        self._ensure_enabled()
        with self._lock:
            parents = [self._require_snapshot(value) for value in parent_snapshot_ids]
            effective = _intersect_policy_sets(
                [revision.metadata.allowed_tools, *(parent.effective_tools for parent in parents)]
            )
            payload = {
                "invocation_id": invocation_id,
                "session_id": session_id,
                "version_ref": revision.version_ref.to_dict(),
                "effective_tools": None if effective is None else [item.to_dict() for item in effective],
                "parent_snapshot_ids": list(parent_snapshot_ids),
            }
            snapshot = SkillPolicySnapshot(
                snapshot_id=new_id("skillpolicy"),
                invocation_id=invocation_id,
                session_id=session_id,
                version_ref=revision.version_ref,
                effective_tools=effective,
                parent_snapshot_ids=tuple(parent_snapshot_ids),
                policy_digest=digest_object(payload),
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
            self._active_by_invocation[invocation_id] = snapshot.snapshot_id
            self._active_by_session.setdefault(session_id, []).append(snapshot.snapshot_id)
            return snapshot

    def unbind(self, invocation_id: str) -> SkillPolicySnapshot | None:
        self._ensure_enabled()
        with self._lock:
            snapshot_id = self._active_by_invocation.pop(invocation_id, "")
            if not snapshot_id:
                return None
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            session_items = self._active_by_session.get(snapshot.session_id, [])
            self._active_by_session[snapshot.session_id] = [value for value in session_items if value != snapshot_id]
            if not self._active_by_session[snapshot.session_id]:
                self._active_by_session.pop(snapshot.session_id, None)
            return snapshot

    def clear_session(self, session_id: str) -> tuple[SkillPolicySnapshot, ...]:
        self._ensure_enabled()
        with self._lock:
            ids = tuple(self._active_by_session.pop(session_id, ()))
            snapshots = tuple(self._snapshots[item] for item in ids if item in self._snapshots)
            for snapshot in snapshots:
                self._active_by_invocation.pop(snapshot.invocation_id, None)
            return snapshots

    def clear_version(self, version_ref: SkillVersionRef) -> tuple[SkillPolicySnapshot, ...]:
        self._ensure_enabled()
        with self._lock:
            invocation_ids = [
                snapshot.invocation_id
                for snapshot in self._snapshots.values()
                if snapshot.version_ref.content_digest == version_ref.content_digest
                and snapshot.invocation_id in self._active_by_invocation
            ]
        return tuple(filter(None, (self.unbind(invocation_id) for invocation_id in invocation_ids)))

    def decide(
        self,
        *,
        session_id: str,
        tool: ToolUseIdentity,
        invocation_id: str = "",
    ) -> SkillToolPolicyDecision:
        self._ensure_enabled()
        with self._lock:
            if invocation_id:
                snapshot_id = self._active_by_invocation.get(invocation_id, "")
                snapshots = [self._snapshots[snapshot_id]] if snapshot_id else []
            else:
                snapshots = [
                    self._snapshots[snapshot_id]
                    for snapshot_id in self._active_by_session.get(session_id, ())
                    if snapshot_id in self._snapshots
                ]
        if not snapshots:
            return SkillToolPolicyDecision(
                allowed_by_ceiling=True,
                reason="no active skill capability ceiling for session",
                invocation_ids=(),
                metadata={"permission_owner": "M1-03A", "grant_issued": False},
            )
        matches: list[ToolSelector] = []
        constrained = False
        for snapshot in snapshots:
            selectors = snapshot.effective_tools
            if selectors is None:
                continue
            constrained = True
            selected = tuple(
                selector
                for selector in selectors
                if selector.matches(
                    namespace=tool.namespace,
                    name=tool.name,
                    server_id=tool.server_id,
                    operation=tool.operation,
                    path=tool.path,
                    domain=tool.domain,
                )
            )
            if not selected:
                return SkillToolPolicyDecision(
                    allowed_by_ceiling=False,
                    reason=f"tool {tool.canonical_name} is outside active skill allowed-tools ceiling",
                    invocation_ids=tuple(snapshot.invocation_id for snapshot in snapshots),
                    metadata={
                        "permission_owner": "M1-03A",
                        "grant_issued": False,
                        "denied_snapshot_id": snapshot.snapshot_id,
                        "bypass_immune": True,
                    },
                )
            matches.extend(selected)
        return SkillToolPolicyDecision(
            allowed_by_ceiling=True,
            reason=(
                "tool is inside every active skill ceiling; continue through 03A permission"
                if constrained
                else "active skills declare no additional tool ceiling; continue through 03A permission"
            ),
            invocation_ids=tuple(snapshot.invocation_id for snapshot in snapshots),
            matching_selectors=tuple(matches),
            metadata={
                "permission_owner": "M1-03A",
                "grant_issued": False,
                "advisory_allow_only": True,
            },
        )

    def require_allowed(self, **kwargs: Any) -> SkillToolPolicyDecision:
        decision = self.decide(**kwargs)
        if not decision.allowed_by_ceiling:
            raise SkillPolicyDenied(decision.reason, detail=decision.to_dict())
        return decision

    def register_permission_hook(self, adapter: PermissionHookAdapterPort) -> str:
        self._ensure_enabled()
        adapter_id = id(adapter)
        with self._lock:
            existing = self._registered_hook_ids.get(adapter_id)
            if existing:
                return existing
        try:
            from zyra_runtime.permission.hooks import (
                PermissionHookEffect,
                PermissionHookFailureMode,
                PermissionHookProposal,
                PermissionHookStage,
                RegisteredPermissionHook,
            )
        except ImportError as error:
            raise SkillAllowedToolsPolicyDisabled("03A permission hook contract is unavailable") from error

        def callback(hook_input: Any) -> Any:
            namespace = str(hook_input.metadata.get("tool_namespace") or "builtin")
            operation = str(hook_input.metadata.get("operation") or "execute")
            path = str(hook_input.arguments.get("path") or "")
            domain = str(hook_input.arguments.get("domain") or hook_input.arguments.get("url_domain") or "")
            invocation_id = str(hook_input.metadata.get("skill_invocation_id") or "")
            decision = self.decide(
                session_id=hook_input.session_id,
                invocation_id=invocation_id,
                tool=ToolUseIdentity(
                    namespace=namespace,
                    name=hook_input.tool_name,
                    server_id=hook_input.server_name,
                    operation=operation,
                    path=path,
                    domain=domain,
                ),
            )
            metadata = {
                "skill_policy": decision.to_dict(),
                "owner_unit": "M1-03C",
                "permission_owner": "M1-03A",
            }
            if not decision.allowed_by_ceiling:
                return PermissionHookProposal.deny(decision.reason, metadata=metadata)
            return PermissionHookProposal(
                effect=PermissionHookEffect.PASSTHROUGH,
                reason=decision.reason,
                metadata=metadata,
            )

        hook = RegisteredPermissionHook(
            name="zyra-skill-allowed-tools-ceiling",
            callback=callback,
            stage=PermissionHookStage.PRE_TOOL_USE,
            source="zyra_skills.policy",
            priority=5,
            timeout_seconds=2.0,
            failure_mode=PermissionHookFailureMode.FAIL_CLOSED,
            metadata={
                "owner_unit": "M1-03C",
                "permission_owner": "M1-03A",
                "semantics": "deny-only capability ceiling; allow is passthrough",
            },
        )
        registered = adapter.register(hook)
        hook_id = str(registered.hook_id)
        with self._lock:
            self._registered_hook_ids[adapter_id] = hook_id
        return hook_id

    def unregister_permission_hook(self, adapter: PermissionHookAdapterPort) -> bool:
        with self._lock:
            hook_id = self._registered_hook_ids.pop(id(adapter), "")
        return bool(hook_id and adapter.unregister(hook_id))

    def snapshot(self, snapshot_id: str) -> SkillPolicySnapshot:
        self._ensure_enabled()
        with self._lock:
            return self._require_snapshot(snapshot_id)

    def active_snapshots(self, session_id: str) -> tuple[SkillPolicySnapshot, ...]:
        self._ensure_enabled()
        with self._lock:
            return tuple(
                self._snapshots[value]
                for value in self._active_by_session.get(session_id, ())
                if value in self._snapshots
            )

    def restore_snapshot(
        self,
        snapshot: SkillPolicySnapshot,
        *,
        current_revision: SkillRevision,
        parent_snapshot_ids: Sequence[str] = (),
        activate: bool = True,
    ) -> SkillPolicySnapshot:
        self._ensure_enabled()
        if snapshot.version_ref.skill_id != current_revision.version_ref.skill_id:
            raise SkillPolicyDenied("skill policy snapshot belongs to a different skill")
        # Historical scope is evidence and an upper bound, never authority.
        historical = snapshot.effective_tools
        parents = [self._require_snapshot(value) for value in parent_snapshot_ids]
        effective = _intersect_policy_sets(
            [historical, current_revision.metadata.allowed_tools, *(parent.effective_tools for parent in parents)]
        )
        payload = {
            "restored_from": snapshot.snapshot_id,
            "current_version": current_revision.version_ref.to_dict(),
            "effective_tools": None if effective is None else [item.to_dict() for item in effective],
            "parents": list(parent_snapshot_ids),
        }
        restored = SkillPolicySnapshot(
            snapshot_id=new_id("skillpolicy"),
            invocation_id=snapshot.invocation_id,
            session_id=snapshot.session_id,
            version_ref=current_revision.version_ref,
            effective_tools=effective,
            parent_snapshot_ids=tuple(parent_snapshot_ids),
            policy_digest=digest_object(payload),
        )
        with self._lock:
            self._snapshots[restored.snapshot_id] = restored
            if activate:
                previous_id = self._active_by_invocation.get(restored.invocation_id, "")
                if previous_id:
                    previous = self._snapshots.get(previous_id)
                    if previous is not None:
                        self._active_by_session[previous.session_id] = [
                            value
                            for value in self._active_by_session.get(previous.session_id, ())
                            if value != previous_id
                        ]
                self._active_by_invocation[restored.invocation_id] = restored.snapshot_id
                self._active_by_session.setdefault(restored.session_id, []).append(restored.snapshot_id)
        return restored

    def _require_snapshot(self, snapshot_id: str) -> SkillPolicySnapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise SkillPolicyDenied("parent skill policy snapshot was not found", detail={"snapshot_id": snapshot_id})
        return snapshot

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise SkillAllowedToolsPolicyDisabled("SkillAllowedToolsPolicy is disabled")


def _intersect_policy_sets(
    policies: Iterable[tuple[ToolSelector, ...] | None],
) -> tuple[ToolSelector, ...] | None:
    constrained = [tuple(policy) for policy in policies if policy is not None]
    if not constrained:
        return None
    if any(not policy for policy in constrained):
        return ()
    result = list(constrained[0])
    for policy in constrained[1:]:
        intersections: list[ToolSelector] = []
        for left in result:
            for right in policy:
                merged = _intersect_selector(left, right)
                if merged is not None:
                    intersections.append(merged)
        result = _dedupe_selectors(intersections)
        if not result:
            return ()
    return tuple(_dedupe_selectors(result))


def _intersect_selector(left: ToolSelector, right: ToolSelector) -> ToolSelector | None:
    namespace = _intersect_component(left.namespace, right.namespace)
    name = _intersect_component(left.name, right.name)
    server_id = _intersect_optional_component(left.server_id, right.server_id)
    if namespace is None or name is None or server_id is None:
        return None
    operations = _intersect_values(left.operations, right.operations)
    if left.operations and right.operations and not operations:
        return None
    paths = _intersect_path_prefixes(left.path_prefixes, right.path_prefixes)
    if left.path_prefixes and right.path_prefixes and not paths:
        return None
    domains = _intersect_domains(left.domains, right.domains)
    if left.domains and right.domains and not domains:
        return None
    return ToolSelector(
        namespace=namespace,
        name=name,
        server_id=server_id,
        operations=operations,
        path_prefixes=paths,
        domains=domains,
        read_only=left.read_only or right.read_only,
    )


def _intersect_component(left: str, right: str) -> str | None:
    if left == "*":
        return right
    if right == "*":
        return left
    return left if left == right else None


def _intersect_optional_component(left: str, right: str) -> str | None:
    if not left:
        return right
    if not right:
        return left
    return left if left == right else None


def _intersect_values(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    if not left:
        return tuple(right)
    if not right:
        return tuple(left)
    return tuple(sorted(set(left) & set(right)))


def _intersect_path_prefixes(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    if not left:
        return tuple(right)
    if not right:
        return tuple(left)
    result: set[str] = set()
    from pathlib import Path

    for first in left:
        for second in right:
            first_path = Path(first).resolve()
            second_path = Path(second).resolve()
            try:
                first_path.relative_to(second_path)
                result.add(str(first_path))
                continue
            except ValueError:
                pass
            try:
                second_path.relative_to(first_path)
                result.add(str(second_path))
            except ValueError:
                pass
    return tuple(sorted(result))


def _intersect_domains(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    if not left:
        return tuple(right)
    if not right:
        return tuple(left)
    result: set[str] = set()
    for first in left:
        for second in right:
            if first == second or first.endswith(f".{second}"):
                result.add(first)
            elif second.endswith(f".{first}"):
                result.add(second)
    return tuple(sorted(result))


def _dedupe_selectors(selectors: Iterable[ToolSelector]) -> list[ToolSelector]:
    result: list[ToolSelector] = []
    seen: set[str] = set()
    for selector in selectors:
        key = digest_object(selector.to_dict())
        if key not in seen:
            seen.add(key)
            result.append(selector)
    return result
