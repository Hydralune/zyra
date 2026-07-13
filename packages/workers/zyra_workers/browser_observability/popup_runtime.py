from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .models import utc_now


class PopupState(StrEnum):
    OBSERVED = "observed"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"
    FOCUSED = "focused"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PopupPolicy:
    max_open_popups: int = 8
    focus_user_gesture_popups: bool = True
    close_untrusted_popups: bool = True
    allow_openerless: bool = False
    trusted_origins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_open_popups < 0:
            raise ValueError("popup limit must be non-negative")
        object.__setattr__(
            self,
            "trusted_origins",
            frozenset(str(item).casefold() for item in self.trusted_origins),
        )


@dataclass(frozen=True, slots=True)
class PopupTarget:
    target_id: str
    opener_target_id: str
    url: str
    origin: str
    state: PopupState
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    user_gesture: bool = False
    close_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "opener_target_id": self.opener_target_id,
            "url": self.url,
            "origin": self.origin,
            "state": str(self.state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_gesture": self.user_gesture,
            "close_reason": self.close_reason,
            "metadata": dict(self.metadata),
        }


class BrowserPopupRuntime:
    def __init__(
        self,
        *,
        policy: PopupPolicy | None = None,
    ) -> None:
        self.policy = policy or PopupPolicy()
        self._targets: dict[str, PopupTarget] = {}

    def observed(
        self,
        *,
        target_id: str,
        opener_target_id: str,
        url: str,
        origin: str,
        user_gesture: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> PopupTarget:
        active = tuple(
            item
            for item in self._targets.values()
            if item.state != PopupState.CLOSED
        )
        trusted = (
            origin.casefold() in self.policy.trusted_origins
            or (
                user_gesture
                and self.policy.focus_user_gesture_popups
            )
        )
        quarantine = (
            len(active) >= self.policy.max_open_popups
            or (not opener_target_id and not self.policy.allow_openerless)
            or (self.policy.close_untrusted_popups and not trusted)
        )
        state = (
            PopupState.QUARANTINED
            if quarantine
            else PopupState.TRUSTED
            if trusted
            else PopupState.OBSERVED
        )
        item = PopupTarget(
            target_id=target_id,
            opener_target_id=opener_target_id,
            url=url,
            origin=origin,
            state=state,
            user_gesture=user_gesture,
            metadata=dict(metadata or {}),
        )
        self._targets[target_id] = item
        return item

    def focused(
        self,
        target_id: str,
    ) -> PopupTarget:
        item = self.require(target_id)
        if item.state in {PopupState.QUARANTINED, PopupState.CLOSED}:
            raise RuntimeError("quarantined or closed popup cannot receive focus")
        updated = replace(
            item,
            state=PopupState.FOCUSED,
            updated_at=utc_now(),
        )
        self._targets[target_id] = updated
        return updated

    def closed(
        self,
        target_id: str,
        *,
        reason: str,
    ) -> PopupTarget:
        item = self.require(target_id)
        updated = replace(
            item,
            state=PopupState.CLOSED,
            updated_at=utc_now(),
            close_reason=reason,
        )
        self._targets[target_id] = updated
        return updated

    def require(
        self,
        target_id: str,
    ) -> PopupTarget:
        item = self._targets.get(target_id)
        if item is None:
            raise KeyError(target_id)
        return item

    def opener_chain(
        self,
        target_id: str,
    ) -> tuple[str, ...]:
        chain: list[str] = []
        seen: set[str] = set()
        current = self.require(target_id)
        while current.opener_target_id:
            if current.opener_target_id in seen:
                raise RuntimeError("popup opener graph contains a cycle")
            seen.add(current.opener_target_id)
            chain.append(current.opener_target_id)
            opener = self._targets.get(current.opener_target_id)
            if opener is None:
                break
            current = opener
        return tuple(chain)

    def projection(self) -> dict[str, Any]:
        values = tuple(self._targets.values())
        return {
            "schema": "zyra.browser-observability.popups.v1",
            "total": len(values),
            "open": sum(1 for item in values if item.state != PopupState.CLOSED),
            "quarantined": sum(
                1
                for item in values
                if item.state == PopupState.QUARANTINED
            ),
            "focused": sum(
                1
                for item in values
                if item.state == PopupState.FOCUSED
            ),
            "targets": [item.to_dict() for item in values],
        }
