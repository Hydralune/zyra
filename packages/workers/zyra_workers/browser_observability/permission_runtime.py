from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .models import digest_value, new_observation_id, utc_now


class BrowserPermissionState(StrEnum):
    PROMPT = "prompt"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    allowed_permissions: frozenset[str] = frozenset(
        {
            "clipboard-read",
            "clipboard-write",
            "geolocation",
            "notifications",
        }
    )
    default_state: BrowserPermissionState = BrowserPermissionState.DENIED
    max_grant_seconds: int = 900
    require_origin_binding: bool = True
    revoke_on_origin_change: bool = True

    def __post_init__(self) -> None:
        if self.max_grant_seconds < 1:
            raise ValueError("permission max_grant_seconds must be positive")
        object.__setattr__(
            self,
            "allowed_permissions",
            frozenset(item.casefold() for item in self.allowed_permissions),
        )


@dataclass(frozen=True, slots=True)
class BrowserPermissionGrant:
    grant_id: str
    browser_session_id: str
    permission: str
    origin: str
    state: BrowserPermissionState
    created_at: str
    expires_at: str
    source_permission_decision_id: str = ""
    source_tool_use_id: str = ""
    revision: int = 1
    revoked_at: str = ""
    revoke_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def binding_digest(self) -> str:
        return digest_value(
            {
                "browser_session_id": self.browser_session_id,
                "permission": self.permission,
                "origin": self.origin,
                "source_permission_decision_id": self.source_permission_decision_id,
                "source_tool_use_id": self.source_tool_use_id,
            }
        )

    @property
    def active(self) -> bool:
        if self.state != BrowserPermissionState.GRANTED:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expiry > datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "browser_session_id": self.browser_session_id,
            "permission": self.permission,
            "origin": self.origin,
            "state": str(self.state),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "source_permission_decision_id": self.source_permission_decision_id,
            "source_tool_use_id": self.source_tool_use_id,
            "revision": self.revision,
            "revoked_at": self.revoked_at,
            "revoke_reason": self.revoke_reason,
            "metadata": dict(self.metadata),
            "binding_digest": self.binding_digest,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class PermissionDrift:
    expected: tuple[str, ...]
    reported: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    expired: tuple[str, ...]
    origin_mismatches: tuple[str, ...]

    @property
    def drifted(self) -> bool:
        return any(
            (
                self.missing,
                self.unexpected,
                self.expired,
                self.origin_mismatches,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": list(self.expected),
            "reported": list(self.reported),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "expired": list(self.expired),
            "origin_mismatches": list(self.origin_mismatches),
            "drifted": self.drifted,
        }


class BrowserPermissionRuntime:
    """Browser-level grant mirror; M1-03A remains the decision owner."""

    def __init__(
        self,
        *,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self.policy = policy or PermissionPolicy()
        self._grants: dict[str, BrowserPermissionGrant] = {}

    def mirror_grant(
        self,
        *,
        browser_session_id: str,
        permission: str,
        origin: str,
        source_permission_decision_id: str,
        source_tool_use_id: str,
        ttl_seconds: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserPermissionGrant:
        normalized = permission.casefold().strip()
        if normalized not in self.policy.allowed_permissions:
            raise ValueError(f"browser permission {normalized!r} is outside policy")
        if not source_permission_decision_id or not source_tool_use_id:
            raise ValueError("browser permission mirror requires 03A decision identities")
        if self.policy.require_origin_binding and not origin:
            raise ValueError("browser permission mirror requires an origin")
        bounded_ttl = max(1, min(int(ttl_seconds), self.policy.max_grant_seconds))
        created = datetime.now(UTC)
        expires = created.timestamp() + bounded_ttl
        expires_at = (
            datetime.fromtimestamp(expires, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        grant = BrowserPermissionGrant(
            grant_id=new_observation_id("browser-permission"),
            browser_session_id=browser_session_id,
            permission=normalized,
            origin=origin,
            state=BrowserPermissionState.GRANTED,
            created_at=created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            expires_at=expires_at,
            source_permission_decision_id=source_permission_decision_id,
            source_tool_use_id=source_tool_use_id,
            metadata=dict(metadata or {}),
        )
        self._grants[grant.grant_id] = grant
        return grant

    def deny(
        self,
        *,
        browser_session_id: str,
        permission: str,
        origin: str,
        source_permission_decision_id: str,
        source_tool_use_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserPermissionGrant:
        now = utc_now()
        grant = BrowserPermissionGrant(
            grant_id=new_observation_id("browser-permission"),
            browser_session_id=browser_session_id,
            permission=permission.casefold().strip(),
            origin=origin,
            state=BrowserPermissionState.DENIED,
            created_at=now,
            expires_at=now,
            source_permission_decision_id=source_permission_decision_id,
            source_tool_use_id=source_tool_use_id,
            metadata=dict(metadata or {}),
        )
        self._grants[grant.grant_id] = grant
        return grant

    def revoke(
        self,
        grant_id: str,
        *,
        reason: str,
    ) -> BrowserPermissionGrant:
        grant = self.require(grant_id)
        updated = replace(
            grant,
            state=BrowserPermissionState.REVOKED,
            revision=grant.revision + 1,
            revoked_at=utc_now(),
            revoke_reason=reason,
        )
        self._grants[grant_id] = updated
        return updated

    def revoke_for_origin_change(
        self,
        *,
        browser_session_id: str,
        new_origin: str,
    ) -> tuple[BrowserPermissionGrant, ...]:
        if not self.policy.revoke_on_origin_change:
            return ()
        revoked: list[BrowserPermissionGrant] = []
        for grant in self.for_session(browser_session_id):
            if grant.active and grant.origin and grant.origin != new_origin:
                revoked.append(
                    self.revoke(
                        grant.grant_id,
                        reason="browser_origin_changed",
                    )
                )
        return tuple(revoked)

    def audit(
        self,
        *,
        browser_session_id: str,
        origin: str,
        reported_permissions: Iterable[str],
    ) -> PermissionDrift:
        reported = tuple(sorted({item.casefold() for item in reported_permissions}))
        grants = self.for_session(browser_session_id)
        expected = tuple(
            sorted(
                {
                    item.permission
                    for item in grants
                    if item.active and (not item.origin or item.origin == origin)
                }
            )
        )
        expired = tuple(
            sorted(
                {
                    item.permission
                    for item in grants
                    if item.state == BrowserPermissionState.GRANTED and not item.active
                }
            )
        )
        origin_mismatches = tuple(
            sorted(
                {
                    item.permission
                    for item in grants
                    if item.active and item.origin and item.origin != origin
                }
            )
        )
        return PermissionDrift(
            expected=expected,
            reported=reported,
            missing=tuple(sorted(set(expected) - set(reported))),
            unexpected=tuple(sorted(set(reported) - set(expected))),
            expired=expired,
            origin_mismatches=origin_mismatches,
        )

    def require(
        self,
        grant_id: str,
    ) -> BrowserPermissionGrant:
        grant = self._grants.get(grant_id)
        if grant is None:
            raise KeyError(grant_id)
        return grant

    def for_session(
        self,
        browser_session_id: str,
    ) -> tuple[BrowserPermissionGrant, ...]:
        return tuple(
            grant
            for grant in self._grants.values()
            if grant.browser_session_id == browser_session_id
        )

    def projection(
        self,
        *,
        browser_session_id: str = "",
    ) -> dict[str, Any]:
        values = (
            self.for_session(browser_session_id)
            if browser_session_id
            else tuple(self._grants.values())
        )
        return {
            "schema": "zyra.browser-observability.permissions.v1",
            "grant_count": len(values),
            "active_count": sum(1 for item in values if item.active),
            "denied_count": sum(
                1
                for item in values
                if item.state == BrowserPermissionState.DENIED
            ),
            "revoked_count": sum(
                1
                for item in values
                if item.state == BrowserPermissionState.REVOKED
            ),
            "grants": [item.to_dict() for item in values],
            "decision_owner": "M1-03A",
        }
