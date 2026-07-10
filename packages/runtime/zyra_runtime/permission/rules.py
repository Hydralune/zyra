from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_arguments_json
from .models import (
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)


_TOOL_ALIASES = {"Task": "Agent", "KillShell": "TaskStop"}
_EFFECT_ORDER = {PermissionEffect.DENY: 3, PermissionEffect.ASK: 2, PermissionEffect.ALLOW: 1}
_SOURCE_ORDER = {
    PermissionRuleSource.BUILTIN_SAFETY: 80,
    PermissionRuleSource.POLICY: 70,
    PermissionRuleSource.CLI: 60,
    PermissionRuleSource.SESSION: 50,
    PermissionRuleSource.COMMAND: 45,
    PermissionRuleSource.PROJECT: 40,
    PermissionRuleSource.LOCAL: 30,
    PermissionRuleSource.USER: 20,
}
_SCOPE_ORDER = {
    PermissionScopeKind.ACTION: 80,
    PermissionScopeKind.SESSION: 70,
    PermissionScopeKind.TASK: 60,
    PermissionScopeKind.RUN: 50,
    PermissionScopeKind.WORKSPACE: 40,
    PermissionScopeKind.PROJECT: 30,
    PermissionScopeKind.USER: 20,
    PermissionScopeKind.GLOBAL: 10,
}


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule: PermissionRuleRecord
    matched: bool
    specificity: int = 0
    matched_argument: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def effect(self) -> PermissionEffect:
        return self.rule.effect

    @property
    def precedence(self) -> tuple[int, int, int, int, str]:
        return (
            _EFFECT_ORDER[self.rule.effect],
            self.rule.priority,
            self.specificity,
            _SOURCE_ORDER[self.rule.source],
            self.rule.rule_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_dict(),
            "matched": self.matched,
            "specificity": self.specificity,
            "matched_argument": self.matched_argument,
            "reasons": list(self.reasons),
            "precedence": list(self.precedence),
        }


@dataclass(frozen=True, slots=True)
class RuleShadow:
    shadowed_rule_id: str
    dominant_rule_id: str
    reason: str
    complete: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadowed_rule_id": self.shadowed_rule_id,
            "dominant_rule_id": self.dominant_rule_id,
            "reason": self.reason,
            "complete": self.complete,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    effect: PermissionEffect | None
    matches: tuple[RuleMatch, ...]
    winning_match: RuleMatch | None
    shadowed: tuple[RuleShadow, ...] = ()
    reason: str = ""

    @property
    def matched_rule_ids(self) -> tuple[str, ...]:
        return tuple(item.rule.rule_id for item in self.matches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": str(self.effect) if self.effect else None,
            "matches": [item.to_dict() for item in self.matches],
            "winning_match": self.winning_match.to_dict() if self.winning_match else None,
            "shadowed": [item.to_dict() for item in self.shadowed],
            "reason": self.reason,
            "matched_rule_ids": list(self.matched_rule_ids),
        }


class RuleMatcher:
    """Matches structured rules without allowing source order to override effect order."""

    def match(
        self,
        rule: PermissionRuleRecord,
        request: PermissionEvaluationRequest,
        *,
        at: datetime | None = None,
    ) -> RuleMatch:
        moment = at or datetime.now(timezone.utc)
        if not rule.is_active(moment):
            return RuleMatch(rule, False, reasons=("inactive_or_expired",))
        if not rule.scope.contains(request, at=moment):
            return RuleMatch(rule, False, reasons=("scope_mismatch",))

        checks = (
            (rule.namespace_pattern, request.tool_identity.namespace, "namespace"),
            (rule.server_pattern, request.tool_identity.server_id, "server"),
            (rule.tool_pattern, request.tool_identity.name, "tool"),
            (rule.operation_pattern, request.operation, "operation"),
        )
        reasons: list[str] = ["scope_match"]
        specificity = _SCOPE_ORDER[rule.scope.kind]
        for pattern, value, label in checks:
            if not _glob_match(pattern, value, case_sensitive=False):
                return RuleMatch(rule, False, reasons=(*reasons, f"{label}_mismatch"))
            specificity += _pattern_specificity(pattern)
            reasons.append(f"{label}_match")

        matched_argument = ""
        if rule.argument_pattern and rule.argument_pattern != "*":
            candidates = _argument_candidates(request)
            case_sensitive = bool(rule.metadata.get("argument_case_sensitive", True))
            matched_argument = next(
                (candidate for candidate in candidates if _glob_match(rule.argument_pattern, candidate, case_sensitive=case_sensitive)),
                "",
            )
            if not matched_argument:
                return RuleMatch(rule, False, reasons=(*reasons, "argument_mismatch"))
            specificity += _pattern_specificity(rule.argument_pattern) * 2
            reasons.append("argument_match")
        elif rule.scope.argument_digest:
            matched_argument = request.arguments_digest
            specificity += 50
            reasons.append("argument_digest_match")

        if rule.scope.request_fingerprint:
            specificity += 100
            reasons.append("request_fingerprint_match")
        return RuleMatch(rule, True, specificity=specificity, matched_argument=matched_argument, reasons=tuple(reasons))

    def matches(
        self,
        rules: Iterable[PermissionRuleRecord],
        request: PermissionEvaluationRequest,
        *,
        at: datetime | None = None,
    ) -> tuple[RuleMatch, ...]:
        selected = [match for rule in rules if (match := self.match(rule, request, at=at)).matched]
        return tuple(sorted(selected, key=lambda item: item.precedence, reverse=True))


def evaluate_rules(
    rules: Iterable[PermissionRuleRecord],
    request: PermissionEvaluationRequest,
    *,
    matcher: RuleMatcher | None = None,
    at: datetime | None = None,
) -> RuleEvaluation:
    """Evaluate matching rules with the invariant deny > ask > allow.

    Priority, specificity and source only select the audit winner *within* an
    effect. A session allow can therefore never override a policy or safety
    deny, and an allow cannot silently consume an explicit ask.
    """
    active_matcher = matcher or RuleMatcher()
    matches = active_matcher.matches(rules, request, at=at)
    if not matches:
        return RuleEvaluation(None, (), None, reason="no_matching_rule")
    for effect in (PermissionEffect.DENY, PermissionEffect.ASK, PermissionEffect.ALLOW):
        candidates = tuple(item for item in matches if item.effect == effect)
        if candidates:
            winner = max(candidates, key=lambda item: item.precedence)
            return RuleEvaluation(
                effect,
                matches,
                winner,
                shadowed=analyze_rule_shadows([item.rule for item in matches]),
                reason=f"{effect}_rule:{winner.rule.rule_id}",
            )
    return RuleEvaluation(None, matches, None, reason="matching_rules_without_effect")


def analyze_rule_shadows(rules: Sequence[PermissionRuleRecord]) -> tuple[RuleShadow, ...]:
    """Return conservative, explainable shadow relationships.

    Complete shadowing is only asserted when selectors are provably equal or
    the dominant selector is a wildcard. Complex glob subset analysis remains
    deliberately conservative and is reported as partial.
    """
    output: list[RuleShadow] = []
    for candidate in rules:
        dominators: list[PermissionRuleRecord] = []
        for dominant in rules:
            if dominant.rule_id == candidate.rule_id:
                continue
            if _EFFECT_ORDER[dominant.effect] < _EFFECT_ORDER[candidate.effect]:
                continue
            if not _scope_covers(dominant.scope, candidate.scope):
                continue
            if not all(
                _pattern_covers(outer, inner)
                for outer, inner in (
                    (dominant.namespace_pattern, candidate.namespace_pattern),
                    (dominant.server_pattern, candidate.server_pattern),
                    (dominant.tool_pattern, candidate.tool_pattern),
                    (dominant.operation_pattern, candidate.operation_pattern),
                    (dominant.argument_pattern or "*", candidate.argument_pattern or "*"),
                )
            ):
                continue
            dominators.append(dominant)
        if not dominators:
            continue
        dominant = max(
            dominators,
            key=lambda rule: (_EFFECT_ORDER[rule.effect], rule.priority, _SOURCE_ORDER[rule.source], rule.rule_id),
        )
        complete = all(
            outer in {"", "*"} or outer == inner
            for outer, inner in (
                (dominant.namespace_pattern, candidate.namespace_pattern),
                (dominant.server_pattern, candidate.server_pattern),
                (dominant.tool_pattern, candidate.tool_pattern),
                (dominant.operation_pattern, candidate.operation_pattern),
                (dominant.argument_pattern, candidate.argument_pattern),
            )
        )
        output.append(
            RuleShadow(
                shadowed_rule_id=candidate.rule_id,
                dominant_rule_id=dominant.rule_id,
                reason=f"{dominant.effect} rule has equal-or-higher effect precedence and covers the same selector",
                complete=complete,
                metadata={
                    "dominant_source": str(dominant.source),
                    "shadowed_source": str(candidate.source),
                },
            )
        )
    return tuple(output)


def parse_permission_rule(
    expression: str,
    *,
    effect: PermissionEffect | str = PermissionEffect.ASK,
    source: PermissionRuleSource | str = PermissionRuleSource.SESSION,
    scope: PermissionScope | None = None,
    reason: str = "",
    priority: int = 0,
    rule_id: str | None = None,
    expires_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PermissionRuleRecord:
    """Parse ``[namespace::][server/]Tool[#operation][(argument glob)]``.

    Backslash escapes ``\\``, ``(``, ``)``, ``#`` and ``/``. ``Tool()``,
    ``Tool(*)`` and ``Tool`` all normalize to a tool-wide rule, matching the
    source runtime's reversible rule behavior.
    """
    raw = str(expression).strip()
    if not raw:
        raise ValueError("permission rule expression is empty")
    head, argument_pattern = _split_argument(raw)
    namespace_pattern = "*"
    if "::" in head:
        namespace_text, head = head.split("::", 1)
        namespace_pattern = _unescape(namespace_text) or "*"
    operation_pattern = "*"
    operation_index = _find_unescaped(head, "#")
    if operation_index >= 0:
        operation_pattern = _unescape(head[operation_index + 1 :]) or "*"
        head = head[:operation_index]
    server_pattern = "*"
    slash_index = _find_unescaped(head, "/")
    if slash_index >= 0:
        server_pattern = _unescape(head[:slash_index]) or "*"
        head = head[slash_index + 1 :]
    tool_pattern = _unescape(head)
    tool_pattern = _TOOL_ALIASES.get(tool_pattern, tool_pattern)
    if not tool_pattern:
        raise ValueError("permission rule tool pattern is empty")
    normalized_argument = _unescape(argument_pattern)
    if normalized_argument in {"", "*"}:
        normalized_argument = ""
    resolved_effect = effect if isinstance(effect, PermissionEffect) else PermissionEffect(str(effect))
    resolved_source = source if isinstance(source, PermissionRuleSource) else PermissionRuleSource(str(source))
    return PermissionRuleRecord(
        rule_id=rule_id or PermissionRuleRecord.__dataclass_fields__["rule_id"].default_factory(),  # type: ignore[misc]
        effect=resolved_effect,
        source=resolved_source,
        scope=scope or PermissionScope(PermissionScopeKind.GLOBAL),
        tool_pattern=tool_pattern,
        namespace_pattern=namespace_pattern,
        server_pattern=server_pattern,
        operation_pattern=operation_pattern,
        argument_pattern=normalized_argument,
        reason=reason,
        priority=priority,
        expires_at=expires_at,
        metadata=dict(metadata or {}),
    )


def serialize_permission_rule(rule: PermissionRuleRecord) -> str:
    namespace = "" if rule.namespace_pattern == "*" else f"{_escape(rule.namespace_pattern)}::"
    server = "" if rule.server_pattern == "*" else f"{_escape(rule.server_pattern)}/"
    operation = "" if rule.operation_pattern == "*" else f"#{_escape(rule.operation_pattern)}"
    argument = "" if not rule.argument_pattern else f"({_escape(rule.argument_pattern)})"
    return f"{namespace}{server}{_escape(rule.tool_pattern)}{operation}{argument}"


parse_rule = parse_permission_rule
serialize_rule = serialize_permission_rule


def _argument_candidates(request: PermissionEvaluationRequest) -> tuple[str, ...]:
    output = [canonical_arguments_json(request.arguments)]
    for key in ("command", "path", "url", "domain", "resource", "subject"):
        value = request.arguments.get(key, request.attributes.get(key))
        if value is not None and not isinstance(value, (dict, list, tuple)):
            output.append(str(value))
    return tuple(dict.fromkeys(output))


def _glob_match(pattern: str, value: str, *, case_sensitive: bool) -> bool:
    expected = pattern or "*"
    actual = value or ""
    if not case_sensitive:
        expected = expected.casefold()
        actual = actual.casefold()
    return fnmatchcase(actual, expected)


def _pattern_specificity(pattern: str) -> int:
    if pattern in {"", "*"}:
        return 0
    wildcard_count = sum(pattern.count(token) for token in ("*", "?", "["))
    return max(1, len(pattern) - wildcard_count * 3) + (20 if wildcard_count == 0 else 0)


def _pattern_covers(outer: str, inner: str) -> bool:
    outer = outer or "*"
    inner = inner or "*"
    if outer == "*" or outer == inner:
        return True
    if not any(token in inner for token in "*?["):
        return fnmatchcase(inner, outer)
    return False


def _scope_covers(outer: PermissionScope, inner: PermissionScope) -> bool:
    if _SCOPE_ORDER[outer.kind] > _SCOPE_ORDER[inner.kind]:
        return False
    fields = (
        "session_id", "task_id", "run_id", "workspace_root", "project_id", "principal_id",
        "tool_namespace", "tool_name", "server_id", "argument_digest", "request_fingerprint",
    )
    if any(getattr(outer, field) and getattr(outer, field) != getattr(inner, field) for field in fields):
        return False
    if outer.path_prefixes and not set(outer.path_prefixes).issubset(set(inner.path_prefixes)):
        return False
    if outer.domains and not set(outer.domains).issubset(set(inner.domains)):
        return False
    return True


def _split_argument(expression: str) -> tuple[str, str]:
    if not expression.endswith(")") or _is_escaped(expression, len(expression) - 1):
        return expression, ""
    start = -1
    escaped = False
    depth = 0
    for index, char in enumerate(expression):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced closing parenthesis in permission rule")
            if depth == 0 and index != len(expression) - 1:
                start = -1
    if depth != 0:
        raise ValueError("unbalanced parenthesis in permission rule")
    if start < 0:
        return expression, ""
    return expression[:start], expression[start + 1 : -1]


def _find_unescaped(value: str, needle: str) -> int:
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == needle:
            return index
    return -1


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _escape(value: str) -> str:
    output = value.replace("\\", "\\\\")
    for token in ("(", ")", "#", "/"):
        output = output.replace(token, f"\\{token}")
    return output


def _unescape(value: str) -> str:
    output: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            output.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            output.append(char)
    if escaped:
        raise ValueError("permission rule ends with an incomplete escape")
    return "".join(output)
