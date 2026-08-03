from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


DIRECT_RESPONSE_SCHEMA = "zyra.direct-response-contract/v1"

_QUOTED_PATTERNS = (
    re.compile(
        r"(?:请|只需|只|务必)?\s*(?:回复|返回)\s*(?:为|：|:)?\s*"
        r"[\"'“‘]([^\"'”’\r\n]{1,500})[\"'”’]\s*[。.!！?？]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reply|respond|return)\s+(?:with\s+)?(?:exactly\s+)?"
        r"[\"']([^\"'\r\n]{1,500})[\"']\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
)

_BARE_PATTERNS = (
    re.compile(
        r"(?:收到(?:后)?[\s，,]*)?(?:请|只需|只|务必)?\s*(?:回复|返回)"
        r"\s*(?:为|：|:)?\s*([A-Za-z0-9_+.-]{1,64}|[\u4e00-\u9fff]{1,12})"
        r"\s*[。.!！?？]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reply|respond|return)\s+(?:with\s+)?(?:exactly\s+)?"
        r"([A-Za-z0-9_+.-]{1,64})\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
)

_NON_LITERAL_BARE_PREFIXES = (
    "一份",
    "一段",
    "一个",
    "以下",
    "详细",
    "完整",
    "这个",
    "上述",
)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DirectResponseContract:
    expected_response: str
    match_mode: str = "exact_trimmed"
    schema: str = DIRECT_RESPONSE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": "direct_response",
            "expected_response": self.expected_response,
            "expected_response_digest": _digest(self.expected_response),
            "match_mode": self.match_mode,
        }


def direct_response_contract(user_goal: str) -> DirectResponseContract | None:
    """Extract only an explicit, bounded reply contract from a user goal.

    This deliberately does not interpret broad requests such as "reply with a
    report" as literal text. Unquoted replies must be one short token; longer
    answers must be explicitly quoted.
    """

    goal = _normalized(user_goal)
    if not goal:
        return None
    for pattern in _QUOTED_PATTERNS:
        match = pattern.search(goal)
        if match is None:
            continue
        expected = _normalized(match.group(1))
        if expected:
            return DirectResponseContract(expected_response=expected)
    for pattern in _BARE_PATTERNS:
        match = pattern.search(goal)
        if match is None:
            continue
        expected = _normalized(match.group(1)).rstrip("。.!！?？")
        if expected and not expected.startswith(_NON_LITERAL_BARE_PREFIXES):
            return DirectResponseContract(expected_response=expected)
    return None


def validate_direct_response(
    user_goal: str,
    response: str | None,
) -> dict[str, Any]:
    contract = direct_response_contract(user_goal)
    observed = _normalized(response or "")
    if contract is None:
        return {
            "schema": "zyra.goal-contract-verification/v1",
            "applicable": False,
            "passed": True,
            "reason": "no explicit direct-response contract",
        }
    expected = contract.expected_response
    passed = bool(observed) and observed == expected
    return {
        "schema": "zyra.goal-contract-verification/v1",
        "applicable": True,
        "passed": passed,
        "kind": "direct_response",
        "match_mode": contract.match_mode,
        "expected_response_digest": _digest(expected),
        "observed_response_digest": _digest(observed) if observed else "",
        "reason": (
            "final answer exactly satisfies the requested response"
            if passed
            else "final answer is missing or differs from the requested response"
        ),
    }


def goal_contract_matches_projection(
    user_goal: str,
    projection: Mapping[str, Any] | None,
) -> bool:
    contract = direct_response_contract(user_goal)
    if contract is None:
        return projection in (None, {})
    return dict(projection or {}) == contract.to_dict()


__all__ = [
    "DIRECT_RESPONSE_SCHEMA",
    "DirectResponseContract",
    "direct_response_contract",
    "goal_contract_matches_projection",
    "validate_direct_response",
]
