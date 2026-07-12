from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from .errors import BrowserPermissionDenied, BrowserPermissionPending
from .models import (
    BrowserPermissionDecision,
    BrowserPermissionEffect,
    BrowserPermissionRequest,
    browser_id,
)


class BrowserPermissionPort(Protocol):
    def evaluate(self, request: BrowserPermissionRequest) -> BrowserPermissionDecision: ...


@dataclass(frozen=True, slots=True)
class BrowserPermissionRule:
    rule_id: str
    action_pattern: str = "*"
    host_pattern: str = "*"
    effect: BrowserPermissionEffect = BrowserPermissionEffect.DENY
    reason: str = ""
    priority: int = 0
    max_uses: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, request: BrowserPermissionRequest) -> bool:
        action_match = self.action_pattern == "*" or request.action == self.action_pattern
        host = (urlparse(request.url).hostname or "").casefold()
        pattern = self.host_pattern.casefold()
        host_match = pattern == "*" or host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))
        return action_match and host_match


class BrowserPermissionControlBridge:
    SENSITIVE_ACTIONS = frozenset({
        "navigate",
        "download",
        "upload",
        "form_submit",
        "evaluate_js",
        "grant_browser_permission",
        "read_cookie",
        "write_cookie",
        "storage_restore",
    })

    def __init__(
        self,
        *,
        evaluator: Callable[[BrowserPermissionRequest], BrowserPermissionDecision] | None = None,
        sealed: bool = True,
        disabled: bool = False,
    ) -> None:
        self.evaluator = evaluator
        self.sealed = sealed
        self.disabled = disabled
        self._rules: dict[str, BrowserPermissionRule] = {}
        self._uses: dict[str, int] = {}
        self._decisions: list[BrowserPermissionDecision] = []
        self._lock = threading.RLock()

    def add_rule(self, rule: BrowserPermissionRule) -> None:
        with self._lock:
            self._rules[rule.rule_id] = rule
            self._uses.setdefault(rule.rule_id, 0)

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            self._uses.pop(rule_id, None)
            return self._rules.pop(rule_id, None) is not None

    def evaluate(self, request: BrowserPermissionRequest) -> BrowserPermissionDecision:
        if self.disabled:
            raise BrowserPermissionDenied("browser permission bridge is disabled", session_id=request.session_id)
        if self.evaluator is not None:
            decision = self.evaluator(request)
            return self._commit(request, decision)
        with self._lock:
            candidates = [rule for rule in self._rules.values() if rule.matches(request)]
            candidates.sort(key=lambda item: (item.priority, item.rule_id), reverse=True)
            for rule in candidates:
                uses = self._uses.get(rule.rule_id, 0)
                if rule.max_uses is not None and uses >= rule.max_uses:
                    continue
                self._uses[rule.rule_id] = uses + 1
                decision = BrowserPermissionDecision(
                    request_id=request.request_id,
                    effect=rule.effect,
                    reason=rule.reason or f"matched browser rule {rule.rule_id}",
                    metadata={"rule_id": rule.rule_id, "use": uses + 1},
                )
                return self._commit(request, decision)
        effect = self._default_effect(request)
        decision = BrowserPermissionDecision(
            request_id=request.request_id,
            effect=effect,
            reason="sealed browser policy default" if self.sealed else "interactive browser policy default",
        )
        return self._commit(request, decision)

    def _default_effect(self, request: BrowserPermissionRequest) -> BrowserPermissionEffect:
        if request.action not in self.SENSITIVE_ACTIONS:
            return BrowserPermissionEffect.ALLOW
        return BrowserPermissionEffect.DENY if self.sealed else BrowserPermissionEffect.ASK

    def _commit(
        self,
        request: BrowserPermissionRequest,
        decision: BrowserPermissionDecision,
    ) -> BrowserPermissionDecision:
        if decision.request_id != request.request_id:
            raise BrowserPermissionDenied("permission decision identity mismatch", session_id=request.session_id)
        with self._lock:
            self._decisions.append(decision)
            del self._decisions[:-2048]
        return decision

    def authorize(self, request: BrowserPermissionRequest) -> BrowserPermissionDecision:
        decision = self.evaluate(request)
        if decision.effect == BrowserPermissionEffect.DENY:
            raise BrowserPermissionDenied(decision.reason, session_id=request.session_id, details=decision.to_dict())
        if decision.effect == BrowserPermissionEffect.ASK:
            raise BrowserPermissionPending(decision.reason, session_id=request.session_id, details=decision.to_dict())
        return decision

    def decisions(self, *, request_id: str = "") -> tuple[BrowserPermissionDecision, ...]:
        with self._lock:
            values = tuple(self._decisions)
        if request_id:
            values = tuple(item for item in values if item.request_id == request_id)
        return values

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_id": "zyra-browser-permission-bridge",
                "sealed": self.sealed,
                "disabled": self.disabled,
                "rules": len(self._rules),
                "decisions": len(self._decisions),
                "uses": dict(self._uses),
            }


class AllowLifecyclePermissionPort:
    SAFE_ACTIONS = frozenset({"session_start", "session_stop", "session_diagnose", "target_list", "target_focus"})

    def evaluate(self, request: BrowserPermissionRequest) -> BrowserPermissionDecision:
        effect = BrowserPermissionEffect.ALLOW if request.action in self.SAFE_ACTIONS else BrowserPermissionEffect.DENY
        return BrowserPermissionDecision(
            request_id=request.request_id,
            effect=effect,
            reason="04A lifecycle allowlist" if effect == BrowserPermissionEffect.ALLOW else "04A action deferred",
            decision_id=browser_id("brdecision"),
        )
