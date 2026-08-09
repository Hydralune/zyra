from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


DIRECT_RESPONSE_SCHEMA = "zyra.direct-response-contract/v1"
DELIVERY_CONTRACT_SCHEMA = "zyra.goal-delivery-contract/v1"

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

_WORKSPACE_CHANGE = re.compile(
    r"(?:创建|新建|建立|建一个|建一份|制作|添加|编辑|写入|生成|保存|修改|更新|删除|移除|修复|实现|开发|重构|替换|"
    r"安装|配置|构建|搭建|补充|create|write|generate|save|modify|update|delete|"
    r"remove|fix|implement|develop|refactor|replace|install|configure|build|make|patch|add)",
    re.IGNORECASE,
)
_FILE_PATH_PATTERNS = (
    re.compile(
        r"[`\"'“‘]((?![A-Za-z]+://)(?![A-Za-z]:[\\/])"
        r"[^`\"'”’\r\n]{1,240}\.[A-Za-z0-9]{1,12})[`\"'”’]"
    ),
    re.compile(
        r"(?<![\w./\\@])((?:[A-Za-z0-9_.-]+[\\/])*"
        r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})(?![\w.])"
    ),
)
_FILE_CONTENT_PATTERNS = (
    re.compile(
        r"(?:文件)?\s*内容\s*(?:是|为|：|:)\s*(?:一行\s*)?"
        r"[`\"'“‘]([^`\"'”’\r\n]{1,4000})[`\"'”’]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:file\s+)?content\s*(?:(?:is|should\s+be|:|=)\s*)?"
        r"[`\"']([^`\"'\r\n]{1,4000})[`\"']",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:写入|write(?:\s+exactly)?)\s*[`\"'“‘]"
        r"([^`\"'”’\r\n]{1,4000})[`\"'”’]",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:文件)?\s*内容\s*(?:是|为|：|:)\s*(?:一行\s*)?"
        r"([^，,。.!！?？\r\n]{1,500})",
        re.IGNORECASE,
    ),
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


@dataclass(frozen=True, slots=True)
class GoalDeliveryContract:
    goal_digest: str
    interaction_kind: str
    workspace_mutation_required: bool
    required_paths: tuple[str, ...]
    expected_file_contents: tuple[tuple[str, str], ...]
    provider_reasoning_required: bool = True
    final_response_required: bool = True
    schema: str = DELIVERY_CONTRACT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "interaction_kind": self.interaction_kind,
            "workspace_mutation_required": self.workspace_mutation_required,
            "required_paths": list(self.required_paths),
            "expected_file_contents": {
                path: {
                    "expected_text": content,
                    "expected_text_digest": _digest(content),
                    "match_mode": "exact_text_with_optional_single_trailing_newline",
                }
                for path, content in self.expected_file_contents
            },
            "provider_reasoning_required": self.provider_reasoning_required,
            "final_response_required": self.final_response_required,
            "goal_digest": self.goal_digest,
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


def goal_delivery_contract(user_goal: str) -> GoalDeliveryContract:
    """Compile explicit delivery obligations without pretending to understand prose.

    Broad goals retain provider/final-response requirements.  Deterministic
    path/content checks are added only when the goal contains an explicit
    workspace-change verb and a safe relative file path.
    """

    goal = _normalized(user_goal)
    response = direct_response_contract(goal)
    workspace_mutation_required = bool(_WORKSPACE_CHANGE.search(goal))
    paths: list[str] = []
    if workspace_mutation_required:
        for pattern in _FILE_PATH_PATTERNS:
            for match in pattern.finditer(goal):
                normalized_path = _safe_relative_path(match.group(1))
                if normalized_path and normalized_path not in paths:
                    paths.append(normalized_path)
    expected_content = ""
    for pattern in _FILE_CONTENT_PATTERNS:
        match = pattern.search(goal)
        if match is not None:
            expected_content = _normalized(match.group(1))
            if expected_content:
                break
    expected_file_contents = (
        ((paths[0], expected_content),)
        if paths and expected_content
        else ()
    )
    return GoalDeliveryContract(
        goal_digest=_digest(goal),
        interaction_kind=(
            "direct_response"
            if response is not None
            else "workspace_change"
            if workspace_mutation_required
            else "answer"
        ),
        workspace_mutation_required=workspace_mutation_required,
        required_paths=tuple(paths),
        expected_file_contents=expected_file_contents,
    )


def validate_goal_delivery(
    user_goal: str,
    *,
    projection: Mapping[str, Any] | None,
    workspace_root: str | Path | None,
    workspace_delta: Mapping[str, Any] | None,
    final_response: str | None,
    provider_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contract = goal_delivery_contract(user_goal)
    expected_projection = contract.to_dict()
    observed_projection = dict(projection or {})
    provider = dict(provider_evidence or {})
    delta = dict(workspace_delta or {})
    root = Path(workspace_root).resolve() if workspace_root is not None else None
    response = _normalized(final_response or "")
    direct = direct_response_contract(user_goal)
    checks: dict[str, bool] = {
        "delivery_contract_projection_exact": (
            observed_projection == expected_projection
        ),
        "provider_reasoning_executed": bool(
            not contract.provider_reasoning_required
            or (
                provider.get("provider_called") is True
                and provider.get("task_execution_verified") is True
                and provider.get("prompt_goal_bound") is True
                and provider.get("synthetic_usage") is False
                and provider.get("calls")
            )
        ),
        "final_response_present": bool(
            not contract.final_response_required or response
        ),
        "workspace_mutation_observed": bool(
            not contract.workspace_mutation_required
            or any(delta.get(name) for name in ("created", "modified", "deleted"))
        ),
        "required_paths_present": True,
        "expected_file_contents_match": True,
        "direct_response_exact": bool(
            direct is None or response == direct.expected_response
        ),
    }
    path_evidence: list[dict[str, Any]] = []
    for relative in contract.required_paths:
        path = _resolve_contract_path(root, relative)
        exists = bool(path is not None and path.is_file())
        checks["required_paths_present"] = (
            checks["required_paths_present"] and exists
        )
        evidence: dict[str, Any] = {
            "path": relative,
            "exists": exists,
            "physical_location_redacted": True,
        }
        if exists and path is not None:
            size = path.stat().st_size
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            evidence.update(
                {
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        path_evidence.append(evidence)
    for relative, expected in contract.expected_file_contents:
        path = _resolve_contract_path(root, relative)
        matches = False
        if path is not None and path.is_file():
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    observed = ""
                else:
                    observed = path.read_text(encoding="utf-8")
                matches = observed in (
                    expected,
                    expected + "\n",
                    expected + "\r\n",
                )
            except UnicodeDecodeError:
                matches = False
        checks["expected_file_contents_match"] = (
            checks["expected_file_contents_match"] and matches
        )
    return {
        "schema": "zyra.goal-delivery-verification/v1",
        "passed": all(checks.values()),
        "checks": checks,
        "contract": expected_projection,
        "path_evidence": path_evidence,
        "workspace_root_redacted": True,
    }


def _safe_relative_path(value: str) -> str:
    rendered = str(value or "").strip().replace("\\", "/")
    if (
        not rendered
        or rendered.startswith("/")
        or rendered.startswith("//")
        or "@" in rendered
        or re.match(r"^[A-Za-z]:/", rendered)
        or re.fullmatch(r"\d+(?:\.\d+)+", rendered)
    ):
        return ""
    candidate = PurePosixPath(rendered)
    if candidate.is_absolute() or ".." in candidate.parts:
        return ""
    return candidate.as_posix()


def _resolve_contract_path(root: Path | None, relative: str) -> Path | None:
    if root is None:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


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
    "DELIVERY_CONTRACT_SCHEMA",
    "DIRECT_RESPONSE_SCHEMA",
    "DirectResponseContract",
    "GoalDeliveryContract",
    "direct_response_contract",
    "goal_delivery_contract",
    "goal_contract_matches_projection",
    "validate_direct_response",
    "validate_goal_delivery",
]
