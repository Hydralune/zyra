from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
import time
from typing import Any, Protocol


class ModeName(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"
    AUTO = "auto"
    PLAN = "plan"
    SEALED = "sealed"


class DenialAction(StrEnum):
    CONTINUE = "continue"
    FALLBACK = "fallback"
    ABORT = "abort"


class RuleLike(Protocol):
    """Structural rule boundary used to avoid coupling mode state to storage."""


@dataclass(frozen=True, slots=True)
class DenialState:
    consecutive: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class DenialOutcome:
    action: DenialAction
    state: DenialState
    reason: str


@dataclass(frozen=True, slots=True)
class ModeEvaluation:
    effect: str
    reason: str
    mode: ModeName
    bypass_applied: bool = False
    classifier_consulted: bool = False
    recovery_alternatives: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModeTransition:
    from_mode: ModeName
    to_mode: ModeName
    effective_rules: tuple[Any, ...]
    stripped_rule_ids: tuple[str, ...] = ()
    restored_rule_ids: tuple[str, ...] = ()
    pre_plan_mode: ModeName | None = None
    auto_active: bool = False
    changed: bool = True
    reason: str = ""


def _enum_value(value: Any) -> str:
    if isinstance(value, StrEnum):
        return str(value)
    raw = getattr(value, "value", value)
    return str(raw)


def _mode(value: Any) -> ModeName:
    text = _enum_value(value)
    aliases = {
        "accept_edits": ModeName.ACCEPT_EDITS,
        "bypass": ModeName.BYPASS,
        "dont_ask": ModeName.DONT_ASK,
    }
    if text in aliases:
        return aliases[text]
    try:
        return ModeName(text)
    except ValueError as exc:
        raise ValueError(f"unsupported permission mode: {text!r}") from exc


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _effect(value: Any) -> str:
    candidate = _get(value, "effect", _get(value, "behavior", value))
    text = _enum_value(candidate).lower()
    if text not in {"allow", "ask", "deny"}:
        raise ValueError(f"unsupported permission effect: {text!r}")
    return text


def _reason(value: Any, default: str) -> str:
    reason = _get(value, "reason", None)
    if reason is None:
        reason = _get(value, "message", None)
    return str(reason or default)


def _truth(value: Any, *names: str) -> bool:
    return any(bool(_get(value, name, False)) for name in names)


def _classifier_effect(result: Any) -> tuple[str, str]:
    if isinstance(result, bool):
        return ("allow" if result else "deny", "classifier boolean proposal")
    if result is None:
        return "deny", "classifier unavailable"
    return _effect(result), _reason(result, "classifier proposal")


def _rule_id(rule: Any, index: int) -> str:
    for key in ("rule_id", "id", "source_id"):
        value = _get(rule, key, None)
        if value:
            return str(value)
    return f"rule:{index}"


def _rule_tool(rule: Any) -> str:
    direct = _get(rule, "tool_name", _get(rule, "tool", ""))
    if direct:
        return str(direct)
    value = _get(rule, "rule_value", None)
    return str(_get(value, "tool_name", ""))


def _rule_pattern(rule: Any) -> str | None:
    direct = _get(rule, "pattern", _get(rule, "rule_content", None))
    if direct is not None:
        return str(direct)
    value = _get(rule, "rule_value", None)
    content = _get(value, "rule_content", None)
    return None if content is None else str(content)


def _rule_is_allow(rule: Any) -> bool:
    raw = _get(rule, "effect", _get(rule, "behavior", _get(rule, "rule_behavior", "")))
    return _enum_value(raw).lower() == "allow"


_INTERPRETERS = {
    "bash",
    "sh",
    "zsh",
    "python",
    "python3",
    "node",
    "deno",
    "bun",
    "ruby",
    "perl",
    "php",
    "pwsh",
    "powershell",
    "cmd",
}


def is_dangerous_auto_allow_rule(rule: Any) -> bool:
    """Return whether an allow rule could suppress per-call safety evaluation."""

    if not _rule_is_allow(rule):
        return False
    tool = _rule_tool(rule).strip().lower()
    pattern = (_rule_pattern(rule) or "").strip().lower()
    if tool in {"agent", "task", "subagent", "workflow"}:
        return True
    if tool in {"bash", "powershell", "shell", "terminal", "repl"} and pattern in {"", "*"}:
        return True
    normalized = pattern.removesuffix(":*").removesuffix(" *").removesuffix("*").strip()
    first = normalized.split(maxsplit=1)[0] if normalized else ""
    return first in _INTERPRETERS


class PermissionModeRuntime:
    """Deterministic permission-mode state machine.

    Modes may only transform an already-computed base decision.  A base result
    marked bypass-immune, safety-sensitive, or interaction-required is never
    auto-approved.  Sealed mode is sticky and converts unresolved asks to deny.
    """

    MAX_CONSECUTIVE_DENIALS = 3
    MAX_TOTAL_DENIALS = 20

    def __init__(
        self,
        mode: Any = ModeName.DEFAULT,
        clock: Callable[[], float] = time.time,
        *,
        auto_available: bool = True,
        bypass_available: bool = False,
        use_auto_in_plan: bool = False,
    ) -> None:
        self._mode = _mode(mode)
        self._clock = clock
        self._auto_available = bool(auto_available)
        self._bypass_available = bool(bypass_available)
        self._use_auto_in_plan = bool(use_auto_in_plan)
        self._pre_plan_mode: ModeName | None = None
        self._auto_active = self._mode is ModeName.AUTO
        self._stripped_rules: dict[str, Any] = {}
        self._denials = DenialState()

    @property
    def mode(self) -> ModeName:
        return self._mode

    @property
    def pre_plan_mode(self) -> ModeName | None:
        return self._pre_plan_mode

    @property
    def denial_state(self) -> DenialState:
        return self._denials

    @property
    def auto_active(self) -> bool:
        return self._auto_active

    @property
    def sealed(self) -> bool:
        return self._mode is ModeName.SEALED

    def evaluate_mode(
        self,
        base_decision: Any,
        request_context: Any,
        classifier: Callable[[Any], Any] | None = None,
    ) -> ModeEvaluation:
        effect = _effect(base_decision)
        base_reason = _reason(base_decision, "base permission decision")
        recovery = tuple(_get(base_decision, "recovery_alternatives", ()) or ())
        bypass_immune = _truth(
            base_decision,
            "bypass_immune",
            "safety_critical",
        ) or _truth(request_context, "bypass_immune", "safety_critical", "interactive_required")
        classifier_eligible = bool(
            _get(base_decision, "classifier_eligible", _get(request_context, "classifier_eligible", False))
        )
        explicit_low_risk = _truth(
            request_context,
            "explicit_low_risk_allowlisted",
            "sealed_allowlisted",
            "auto_allowlisted",
        )

        if effect == "deny":
            return self._evaluation(effect, base_reason, recovery=recovery)

        if self.sealed:
            if effect == "allow" and explicit_low_risk and not bypass_immune:
                return self._evaluation("allow", base_reason, recovery=recovery)
            alternatives = recovery or ("use a preauthorized read-only action", "narrow the requested scope")
            return self._evaluation(
                "deny",
                "sealed policy rejects actions outside the explicit low-risk allowlist",
                recovery=alternatives,
                metadata={"base_effect": effect, "human_intervention_required": False},
            )

        if effect == "allow":
            return self._evaluation("allow", base_reason, recovery=recovery)

        if self._mode is ModeName.DONT_ASK:
            return self._evaluation("deny", "dontAsk converts ask to deny", recovery=recovery)

        if self._mode is ModeName.BYPASS:
            if bypass_immune:
                return self._evaluation("ask", base_reason, recovery=recovery)
            if not self._bypass_available:
                return self._evaluation("deny", "bypass mode is disabled by policy", recovery=recovery)
            return self._evaluation(
                "allow",
                "bypass mode allowed a non-safety request",
                recovery=recovery,
                bypass_applied=True,
            )

        auto = self._mode is ModeName.AUTO or (self._mode is ModeName.PLAN and self._auto_active)
        if auto:
            if bypass_immune or not classifier_eligible:
                return self._evaluation("ask", base_reason, recovery=recovery)
            if classifier is None:
                return self._evaluation("deny", "classifier unavailable; failed closed", recovery=recovery)
            proposed_effect, proposed_reason = _classifier_effect(classifier(request_context))
            if proposed_effect == "ask":
                proposed_effect = "deny"
            return self._evaluation(
                proposed_effect,
                proposed_reason,
                recovery=recovery,
                classifier_consulted=True,
                metadata={"base_effect": effect, "proposal_only": True},
            )

        if self._mode is ModeName.ACCEPT_EDITS and _truth(request_context, "safe_edit_ready"):
            return self._evaluation("allow", "acceptEdits fast path prerequisites passed", recovery=recovery)
        return self._evaluation("ask", base_reason, recovery=recovery)

    def transition(
        self,
        target_mode: Any | None,
        rules: Iterable[Any] = (),
    ) -> ModeTransition:
        source = self._mode
        if target_mode is None:
            target = self._pre_plan_mode if source is ModeName.PLAN else ModeName.DEFAULT
            target = target or ModeName.DEFAULT
        else:
            target = _mode(target_mode)
        current_rules = list(rules)

        if source is ModeName.SEALED and target is not ModeName.SEALED:
            raise PermissionError("sealed mode cannot be disabled by a session transition")
        if target is ModeName.BYPASS and not self._bypass_available:
            raise PermissionError("bypass mode is disabled by policy")
        if target is ModeName.AUTO and not self._auto_available:
            raise PermissionError("auto mode is disabled by policy")
        stripped: list[str] = []
        restored: list[str] = []

        if target is ModeName.PLAN:
            next_auto_active = self._use_auto_in_plan and source is not ModeName.BYPASS
        else:
            next_auto_active = target is ModeName.AUTO

        leaving_classifier = self._auto_active and not next_auto_active
        if leaving_classifier:
            for rule_id, rule in self._stripped_rules.items():
                if not any(_rule_id(existing, idx) == rule_id for idx, existing in enumerate(current_rules)):
                    current_rules.append(rule)
                    restored.append(rule_id)
            self._stripped_rules.clear()

        if target is ModeName.PLAN and source is not ModeName.PLAN:
            self._pre_plan_mode = source
        elif source is ModeName.PLAN and target is not ModeName.PLAN:
            self._pre_plan_mode = None

        self._auto_active = next_auto_active

        if self._auto_active:
            kept: list[Any] = []
            for index, rule in enumerate(current_rules):
                rule_id = _rule_id(rule, index)
                if is_dangerous_auto_allow_rule(rule):
                    self._stripped_rules.setdefault(rule_id, rule)
                    stripped.append(rule_id)
                else:
                    kept.append(rule)
            current_rules = kept

        self._mode = target
        return ModeTransition(
            from_mode=source,
            to_mode=target,
            effective_rules=tuple(current_rules),
            stripped_rule_ids=tuple(stripped),
            restored_rule_ids=tuple(restored),
            pre_plan_mode=self._pre_plan_mode,
            auto_active=self._auto_active,
            changed=source is not target,
            reason=(
                f"transitioned {source.value} -> {target.value}"
                if source is not target
                else "mode unchanged; active safety overlays reconciled"
            ),
        )

    def record_denial(self, *, headless: bool = False) -> DenialOutcome:
        self._denials = replace(
            self._denials,
            consecutive=self._denials.consecutive + 1,
            total=self._denials.total + 1,
        )
        hit = (
            self._denials.consecutive >= self.MAX_CONSECUTIVE_DENIALS
            or self._denials.total >= self.MAX_TOTAL_DENIALS
        )
        if not hit:
            return DenialOutcome(DenialAction.CONTINUE, self._denials, "denial recorded")
        if headless or self.sealed:
            return DenialOutcome(
                DenialAction.ABORT,
                self._denials,
                "denial circuit breaker reached without an interactive approval path",
            )
        return DenialOutcome(
            DenialAction.FALLBACK,
            self._denials,
            "denial circuit breaker reached; require explicit review",
        )

    def record_success(self) -> DenialState:
        if self._denials.consecutive:
            self._denials = replace(self._denials, consecutive=0)
        return self._denials

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "pre_plan_mode": self._pre_plan_mode.value if self._pre_plan_mode else None,
            "auto_active": self._auto_active,
            "auto_available": self._auto_available,
            "bypass_available": self._bypass_available,
            "use_auto_in_plan": self._use_auto_in_plan,
            "denials": {
                "consecutive": self._denials.consecutive,
                "total": self._denials.total,
            },
            "stripped_rule_ids": tuple(self._stripped_rules),
            "captured_at": self._clock(),
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> "PermissionModeRuntime":
        runtime = cls(
            snapshot.get("mode", ModeName.DEFAULT),
            clock=clock,
            auto_available=bool(snapshot.get("auto_available", True)),
            bypass_available=bool(snapshot.get("bypass_available", False)),
            use_auto_in_plan=bool(snapshot.get("use_auto_in_plan", False)),
        )
        raw_pre_plan = snapshot.get("pre_plan_mode")
        runtime._pre_plan_mode = _mode(raw_pre_plan) if raw_pre_plan else None
        runtime._auto_active = bool(snapshot.get("auto_active", runtime.mode is ModeName.AUTO))
        denials = snapshot.get("denials")
        if isinstance(denials, Mapping):
            runtime._denials = DenialState(
                consecutive=max(0, int(denials.get("consecutive") or 0)),
                total=max(0, int(denials.get("total") or 0)),
            )
        # Rule bodies live in PermissionRuleStore snapshots.  Recording only
        # their ids here avoids duplicating mutable policy state.
        runtime._stripped_rules = {}
        return runtime

    def _evaluation(
        self,
        effect: str,
        reason: str,
        *,
        recovery: tuple[str, ...],
        bypass_applied: bool = False,
        classifier_consulted: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ModeEvaluation:
        return ModeEvaluation(
            effect=effect,
            reason=reason,
            mode=self._mode,
            bypass_applied=bypass_applied,
            classifier_consulted=classifier_consulted,
            recovery_alternatives=recovery,
            metadata={"evaluated_at": self._clock(), **dict(metadata or {})},
        )


__all__ = [
    "DenialAction",
    "DenialOutcome",
    "DenialState",
    "ModeEvaluation",
    "ModeName",
    "ModeTransition",
    "PermissionModeRuntime",
    "is_dangerous_auto_allow_rule",
]
