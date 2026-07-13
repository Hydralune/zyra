from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from .models import utc_now


class BlankState(StrEnum):
    NEW = "new"
    EXPECTED = "expected"
    NAVIGATING = "navigating"
    READY = "ready"
    STALLED = "stalled"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class BlankPolicy:
    startup_grace_ms: int = 5_000
    navigation_grace_ms: int = 15_000
    allow_new_tab_blank_ms: int = 3_000
    blank_urls: frozenset[str] = frozenset(
        {
            "",
            "about:blank",
            "about:newtab",
            "chrome://newtab/",
            "edge://newtab/",
        }
    )

    def __post_init__(self) -> None:
        if min(
            self.startup_grace_ms,
            self.navigation_grace_ms,
            self.allow_new_tab_blank_ms,
        ) < 0:
            raise ValueError("about-blank timeouts must be non-negative")


@dataclass(frozen=True, slots=True)
class BlankTarget:
    target_id: str
    state: BlankState
    first_seen_ms: int
    updated_ms: int
    url: str = ""
    expected_until_ms: int = 0
    navigation_started_ms: int | None = None
    ready_ms: int | None = None
    reason: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "state": str(self.state),
            "first_seen_ms": self.first_seen_ms,
            "updated_ms": self.updated_ms,
            "url": self.url,
            "expected_until_ms": self.expected_until_ms,
            "navigation_started_ms": self.navigation_started_ms,
            "ready_ms": self.ready_ms,
            "reason": self.reason,
            "created_at": self.created_at,
        }


class AboutBlankRuntime:
    def __init__(
        self,
        *,
        policy: BlankPolicy | None = None,
    ) -> None:
        self.policy = policy or BlankPolicy()
        self._targets: dict[str, BlankTarget] = {}

    def target_created(
        self,
        target_id: str,
        *,
        url: str,
        at_ms: int,
        opener_target_id: str = "",
    ) -> BlankTarget:
        blank = self.is_blank(url)
        grace = (
            self.policy.allow_new_tab_blank_ms
            if opener_target_id
            else self.policy.startup_grace_ms
        )
        item = BlankTarget(
            target_id=target_id,
            state=BlankState.EXPECTED if blank else BlankState.READY,
            first_seen_ms=at_ms,
            updated_ms=at_ms,
            url=url,
            expected_until_ms=at_ms + grace if blank else at_ms,
            ready_ms=None if blank else at_ms,
            reason=(
                "new_target_blank_grace"
                if blank
                else "target_created_with_non_blank_url"
            ),
        )
        self._targets[target_id] = item
        return item

    def navigation_started(
        self,
        target_id: str,
        *,
        url: str,
        at_ms: int,
    ) -> BlankTarget:
        item = self.require(target_id)
        updated = replace(
            item,
            state=BlankState.NAVIGATING,
            updated_ms=at_ms,
            url=url,
            expected_until_ms=at_ms + self.policy.navigation_grace_ms,
            navigation_started_ms=at_ms,
            reason="navigation_started",
        )
        self._targets[target_id] = updated
        return updated

    def url_changed(
        self,
        target_id: str,
        *,
        url: str,
        at_ms: int,
    ) -> BlankTarget:
        item = self.require(target_id)
        if self.is_blank(url):
            state = (
                BlankState.EXPECTED
                if at_ms <= item.expected_until_ms
                else BlankState.STALLED
            )
            ready_ms = item.ready_ms
            reason = (
                "blank_within_grace"
                if state == BlankState.EXPECTED
                else "blank_after_grace"
            )
        else:
            state = BlankState.READY
            ready_ms = at_ms
            reason = "non_blank_url_observed"
        updated = replace(
            item,
            state=state,
            updated_ms=at_ms,
            url=url,
            ready_ms=ready_ms,
            reason=reason,
        )
        self._targets[target_id] = updated
        return updated

    def poll(
        self,
        target_id: str,
        *,
        at_ms: int,
    ) -> BlankTarget:
        item = self.require(target_id)
        if (
            item.state in {BlankState.EXPECTED, BlankState.NAVIGATING}
            and self.is_blank(item.url)
            and at_ms > item.expected_until_ms
        ):
            item = replace(
                item,
                state=BlankState.STALLED,
                updated_ms=at_ms,
                reason="blank_grace_expired",
            )
            self._targets[target_id] = item
        return item

    def closed(
        self,
        target_id: str,
        *,
        at_ms: int,
    ) -> BlankTarget:
        item = self.require(target_id)
        updated = replace(
            item,
            state=BlankState.CLOSED,
            updated_ms=at_ms,
            reason="target_closed",
        )
        self._targets[target_id] = updated
        return updated

    def require(
        self,
        target_id: str,
    ) -> BlankTarget:
        item = self._targets.get(target_id)
        if item is None:
            raise KeyError(target_id)
        return item

    def projection(self) -> dict[str, Any]:
        values = tuple(self._targets.values())
        return {
            "schema": "zyra.browser-observability.about-blank.v1",
            "target_count": len(values),
            "expected": sum(
                1
                for item in values
                if item.state == BlankState.EXPECTED
            ),
            "ready": sum(
                1
                for item in values
                if item.state == BlankState.READY
            ),
            "stalled": sum(
                1
                for item in values
                if item.state == BlankState.STALLED
            ),
            "targets": [item.to_dict() for item in values],
        }

    def is_blank(
        self,
        url: str,
    ) -> bool:
        normalized = str(url or "").strip().casefold()
        if normalized in self.policy.blank_urls:
            return True
        try:
            parsed = urlsplit(normalized)
        except ValueError:
            return False
        return parsed.scheme == "about" and parsed.path in {"blank", "newtab"}
