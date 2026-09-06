from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


DIRECT_RESPONSE_SCHEMA = "zyra.direct-response-contract/v1"
DELIVERY_CONTRACT_SCHEMA = "zyra.goal-delivery-contract/v2"

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
    # Deictic phrases refer to material that must be read or computed.  They
    # are not literal response values.  Treating "只回复其中的内容" as an
    # exact response contract causes the verifier to replace a correct file
    # read with the words "其中的内容" and then approve that wrong answer.
    "其中",
    "其内容",
    "它的",
    "文件内容",
)

_WORKSPACE_CHANGE = re.compile(
    r"(?:创建|新建|建立|建一个|建一份|制作|添加|编辑|写入|生成|形成|同步|保存|修改|更新|删除|移除|修复|实现|开发|重构|替换|"
    r"安装|配置|构建|搭建|补充|交付|提交|提供|放置|create|write|generate|save|deliver|place|modify|update|delete|"
    r"remove|fix|implement|develop|refactor|replace|install|configure|build|make|patch|add)",
    re.IGNORECASE,
)
_FILE_PATH_PATTERNS = (
    re.compile(
        r"(?<![\w./\\@-])(/app/(?:[A-Za-z0-9_.-]+/)*"
        r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})(?!\w)(?!\.[A-Za-z0-9])"
    ),
    re.compile(
        r"[`\"'“‘]((?![A-Za-z]+://)(?![A-Za-z]:[\\/])"
        r"[^`\"'”’\r\n]{1,240}\.[A-Za-z0-9]{1,12})[`\"'”’]"
    ),
    re.compile(
        r"(?<![\w./\\@-])((?:[A-Za-z0-9_.-]+[\\/])*"
        r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})(?![\w.])"
    ),
)
_DIRECTORY_SCOPE_PATTERNS = (
    re.compile(
        r"(?:在|于|至|到)\s*[`\"'“‘]?"
        r"((?:[A-Za-z0-9_.-]+[\\/])+)"
        r"[`\"'”’]?\s*(?:目录|文件夹)?\s*(?:中|内|下)\s*"
        r"(?:交付|提交|提供|保存|生成|放置|写入)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:deliver|place|write|save|create)\b[^\r\n]{0,80}?"
        r"\b(?:in|into|under)\s+[`\"']?"
        r"((?:[A-Za-z0-9_.-]+[\\/])+)[`\"']?",
        re.IGNORECASE,
    ),
)
_FILE_CONTENT_PATTERNS = (
    re.compile(
        r"(?:文件)?\s*内容\s*(?:必须|应当|需要|需)?\s*(?:严格|精确|准确)?\s*"
        r"(?:是|为|等于|：|:)\s*"
        r"([A-Za-z0-9_+./-]{1,500})(?=\s*(?:[（(，,。.!！?？]|$))",
        re.IGNORECASE,
    ),
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
        r"(?:文件)?\s*内容\s*(?:是|为|：|:)\s*(?:一行\s*)?"
        r"([^，,。.!！?？\r\n]{1,500})",
        re.IGNORECASE,
    ),
)

_PATH_CONTEXT_BOUNDARY = re.compile(r"[。.!！?？;；\r\n]")
_KNOWN_SKILL_NAMES = (
    "code-change",
    "codebase-analysis",
    "competition-demo",
    "failure-recovery",
    "pdf-analysis",
    "report-writing",
    "requirement-change",
    "trace-summary",
    "verification",
    "web-research",
)
_SCRIPT_EXTENSIONS = {".bat", ".cmd", ".js", ".mjs", ".ps1", ".py", ".sh", ".ts"}
_SCRIPT_RUN_REQUIREMENT = re.compile(
    r"(?:编写|创建|生成|提供|write|create|generate|provide)"
    r"[^。.!！?？\r\n]{0,80}"
    r"(?:并(?:实际)?运行|并执行|and\s+(?:actually\s+)?(?:run|execute))"
    r"[^。.!！?？\r\n]{0,80}(?:脚本|script)",
    re.IGNORECASE,
)
_LOOPX_REQUIREMENT = re.compile(
    r"(?:先|必须|请|use|required?)?[^。.!！?？\r\n]{0,24}\bLoopX\b"
    r"[^。.!！?？\r\n]{0,80}(?:建立|创建|规划|跟踪|编排|build|create|plan|track|orchestrat)",
    re.IGNORECASE,
)
_ROLE_SEPARATION_REQUIREMENT = re.compile(
    r"(?:不同角色|独立角色|独立复核|独立审查|角色分离|different\s+roles?|"
    r"independent\s+(?:role|review|verification)|role\s+separation)",
    re.IGNORECASE,
)
_ISOLATED_COPY_REQUIREMENT = re.compile(
    r"(?:隔离(?:的)?副本|独立副本|副本中|isolated\s+(?:copy|workspace|worktree))",
    re.IGNORECASE,
)
_QUOTED_DIRECTORY = re.compile(
    r"[`\"'“‘]((?![A-Za-z]+://)(?![A-Za-z]:[\\/])"
    r"[^`\"'”’\r\n]{1,240}[\\/])[`\"'”’]"
)
_BASELINE_TEST_REQUIREMENT = re.compile(
    r"(?:"
    r"(?:运行|执行|run|execute)[^。.!！?？\r\n]{0,48}"
    r"(?:现有|已有|existing|current)[^。.!！?？\r\n]{0,24}"
    r"(?:测试|tests?)[^。.!！?？\r\n]{0,48}(?:基线|baseline)"
    r"|"
    r"(?:基线|baseline)[^。.!！?？\r\n]{0,48}"
    r"(?:现有|已有|existing|current)?[^。.!！?？\r\n]{0,24}(?:测试|tests?)"
    r")",
    re.IGNORECASE,
)
_PROTECT_EXISTING_TESTS = re.compile(
    r"(?:不得|不要|禁止|不可|do\s+not|must\s+not)"
    r"[^。.!！?？\r\n]{0,32}(?:修改|改写|编辑|覆盖|modify|edit|overwrite)"
    r"[^。.!！?？\r\n]{0,24}(?:既有|现有|已有|existing|current)"
    r"[^。.!！?？\r\n]{0,12}(?:测试|tests?)",
    re.IGNORECASE,
)
_TRACE_DELIVERY_REQUIREMENT = re.compile(
    r"(?:"
    r"(?:保留|保存|记录|生成|汇总|总结|retain|preserve|save|record|generate|summari[sz]e)"
    r"[^。.!！?？\r\n]{0,64}(?:trace|追踪|轨迹)"
    r"|"
    r"(?:trace|追踪|轨迹)[^。.!！?？\r\n]{0,64}"
    r"(?:保留|保存|记录|生成|汇总|总结|retain|preserve|save|record|generate|summari[sz]e)"
    r")",
    re.IGNORECASE,
)
_NEGATED_TRACE_DELIVERY_REQUIREMENT = re.compile(
    r"(?:不要|不得|无需|不必|do\s+not|don't|must\s+not)"
    r"[^。.!！?？\r\n]{0,96}(?:trace|追踪|轨迹)",
    re.IGNORECASE,
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
    verification_required: bool
    required_paths: tuple[str, ...]
    expected_file_contents: tuple[tuple[str, str], ...]
    required_skills: tuple[str, ...] = ()
    required_executed_paths: tuple[str, ...] = ()
    provenance_index_paths: tuple[str, ...] = ()
    loopx_required: bool = False
    role_separation_required: bool = False
    mutation_policy: Mapping[str, Any] | None = None
    provider_reasoning_required: bool = True
    final_response_required: bool = True
    schema: str = DELIVERY_CONTRACT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "interaction_kind": self.interaction_kind,
            "workspace_mutation_required": self.workspace_mutation_required,
            "verification_required": self.verification_required,
            "required_paths": list(self.required_paths),
            "expected_file_contents": {
                path: {
                    "expected_text": content,
                    "expected_text_digest": _digest(content),
                    "match_mode": "exact_text_with_optional_single_trailing_newline",
                }
                for path, content in self.expected_file_contents
            },
            "required_skills": list(self.required_skills),
            "required_executed_paths": list(self.required_executed_paths),
            "provenance_index_paths": list(self.provenance_index_paths),
            "loopx_required": self.loopx_required,
            "role_separation_required": self.role_separation_required,
            "mutation_policy": dict(self.mutation_policy or {}),
            "provider_reasoning_required": self.provider_reasoning_required,
            "final_response_required": self.final_response_required,
            "goal_digest": self.goal_digest,
        }


def independent_role_evidence_satisfied(
    required_skills: Iterable[object],
    skill_invocations: Iterable[Mapping[str, Any]],
) -> bool:
    """Validate distinct forked-role evidence without demanding inline skills fork.

    ``codebase-analysis`` is an inline bundled skill, so treating every material
    analysis skill as a required fork makes the role contract impossible to
    satisfy.  Role separation instead requires two different successful forked
    child tasks performing two different skill roles.  Tasks that use the
    fork-capable PDF or web material lanes keep the stronger material-reviewer
    pairing requirement.
    """

    required = {str(item) for item in required_skills if str(item)}
    forked = [
        item
        for item in skill_invocations
        if str(item.get("execution_mode") or "") == "fork"
        and str(item.get("child_task_id") or "")
        and str(item.get("name") or "")
    ]
    child_ids = {str(item.get("child_task_id") or "") for item in forked}
    role_names = {str(item.get("name") or "") for item in forked}
    if len(child_ids) < 2 or len(role_names) < 2:
        return False

    reviewer_children = {
        str(item.get("child_task_id") or "")
        for item in forked
        if str(item.get("name") or "") == "verification"
    }
    if "verification" in required:
        non_reviewer_children = child_ids - reviewer_children
        if not reviewer_children or not non_reviewer_children:
            return False

    fork_material_skills = required.intersection(
        {"pdf-analysis", "web-research"}
    )
    if fork_material_skills and "verification" in required:
        material_children = {
            str(item.get("child_task_id") or "")
            for item in forked
            if str(item.get("name") or "") in fork_material_skills
        }
        return bool(
            material_children
            and reviewer_children
            and len(material_children | reviewer_children) >= 2
        )
    return True


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


def _workspace_change_requested(text: str) -> bool:
    for match in _WORKSPACE_CHANGE.finditer(text):
        # A prohibition or a reference to supplied evidence is not a request
        # for a physical mutation. Keep positive clauses independent.
        prefix = re.split(r"[。.!！?？;；，,\r\n]", text[:match.start()])[-1]
        if re.search(
            r"(?:不要|请勿|不得|禁止|无需|不需要|不必|不能|未|不)(?:[^。;；，,\r\n]{0,24})$"
            r"|\b(?:do\s+not|don't|without|never|no\s+need\s+to)\b[^.;,\r\n]{0,60}$",
            prefix,
            re.IGNORECASE,
        ):
            continue
        if match.group().casefold() == "提供" and re.search(r"(?:用户|我|已|已经|此前|之前)\s*$", prefix):
            continue
        # English verbs must not match substrings such as 'updated' or 'address'.
        if match.group().isascii():
            before = text[match.start() - 1:match.start()] if match.start() else ""
            after = text[match.end():match.end() + 1]
            if (before and before.isalpha()) or (after and after.isalpha()):
                continue
        return True
    return False


def goal_delivery_contract(user_goal: str) -> GoalDeliveryContract:
    """Compile explicit delivery obligations without pretending to understand prose.

    Broad goals retain provider/final-response requirements.  Deterministic
    path/content checks are added only when the goal contains an explicit
    workspace-change verb and a safe relative file path.
    """

    goal = _normalized(user_goal)
    response = direct_response_contract(goal)
    workspace_mutation_required = _workspace_change_requested(goal)
    paths: list[str] = []
    if workspace_mutation_required:
        directory_scopes: list[tuple[int, str]] = []
        for pattern in _DIRECTORY_SCOPE_PATTERNS:
            for match in pattern.finditer(goal):
                normalized_directory = _safe_relative_path(match.group(1))
                if normalized_directory:
                    directory_scopes.append((match.start(), normalized_directory))
        directory_scopes.sort(key=lambda item: item[0])
        path_candidates: list[tuple[int, str]] = []
        for pattern in _FILE_PATH_PATTERNS:
            for match in pattern.finditer(goal):
                normalized_path = _safe_relative_path(match.group(1))
                if normalized_path:
                    path_candidates.append((match.start(), normalized_path))
        for position, normalized_path in sorted(
            path_candidates,
            key=lambda item: item[0],
        ):
            active_scope = next(
                (
                    directory
                    for scope_position, directory in reversed(directory_scopes)
                    if scope_position < position
                ),
                "",
            )
            if not active_scope and not _path_has_delivery_context(
                goal,
                position,
                normalized_path,
            ):
                continue
            if "/" not in normalized_path:
                if active_scope:
                    normalized_path = str(
                        PurePosixPath(active_scope) / normalized_path
                    )
            if normalized_path not in paths:
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
    deterministic_content_only = bool(expected_file_contents) and all(
        PurePosixPath(path).suffix.casefold()
        in {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv"}
        for path, _content in expected_file_contents
    )
    required_skills = _required_skills(goal, paths)
    executable_paths = tuple(
        path
        for path in paths
        if PurePosixPath(path).suffix.casefold() in _SCRIPT_EXTENSIONS
    )
    required_executed_paths: tuple[str, ...] = ()
    if _SCRIPT_RUN_REQUIREMENT.search(goal):
        required_executed_paths = executable_paths
    provenance_index_paths = tuple(
        path
        for path in paths
        if PurePosixPath(path).suffix.casefold() == ".json"
        and any(
            token in PurePosixPath(path).stem.casefold()
            for token in ("source_index", "evidence_index", "provenance", "source_manifest")
        )
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
        # Exact, bounded text deliverables are fully validated by an in-place
        # content read at the Python completion boundary.  Requiring a shell
        # test for those tasks makes a correct one-file delivery loop forever.
        # Source code, scripts, structured data, and open-ended changes retain
        # the fail-closed behavioral-verification requirement.
        verification_required=(
            workspace_mutation_required and not deterministic_content_only
        ),
        required_paths=tuple(paths),
        expected_file_contents=expected_file_contents,
        required_skills=required_skills,
        required_executed_paths=required_executed_paths,
        provenance_index_paths=provenance_index_paths,
        loopx_required=bool(_LOOPX_REQUIREMENT.search(goal)),
        role_separation_required=bool(_ROLE_SEPARATION_REQUIREMENT.search(goal)),
        mutation_policy=_task_mutation_policy(goal),
    )


def _task_mutation_policy(goal: str) -> dict[str, Any]:
    """Compile only explicit, high-confidence workspace mutation constraints.

    This projection is deliberately separate from final delivery obligations:
    forked workers inherit the mutation boundary without becoming responsible
    for the parent's final artifacts.
    """

    protected_roots: list[str] = []
    for match in _QUOTED_DIRECTORY.finditer(goal):
        start = max(0, match.start() - 160)
        end = min(len(goal), match.end() + 160)
        if _ISOLATED_COPY_REQUIREMENT.search(goal[start:end]) is None:
            continue
        path = _safe_relative_path(match.group(1))
        if path and path not in protected_roots:
            protected_roots.append(path.rstrip("/"))
    baseline_required = bool(_BASELINE_TEST_REQUIREMENT.search(goal))
    protect_existing_tests = bool(_PROTECT_EXISTING_TESTS.search(goal))
    enabled = bool(protected_roots or baseline_required or protect_existing_tests)
    return {
        "schema": "zyra.task-mutation-policy/v1",
        "enabled": enabled,
        "protected_source_roots": protected_roots,
        "required_pre_mutation_evidence": (
            ["existing_test_baseline"] if baseline_required else []
        ),
        "protect_existing_test_files": protect_existing_tests,
        "inherit_across_execution_lineage": enabled,
    }


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
        "provenance_indexes_valid": True,
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
    provenance_index_evidence: list[dict[str, Any]] = []
    for relative in contract.provenance_index_paths:
        path = _resolve_contract_path(root, relative)
        index_result = validate_provenance_index(path, relative)
        checks["provenance_indexes_valid"] = (
            checks["provenance_indexes_valid"] and index_result["passed"]
        )
        provenance_index_evidence.append(index_result)
    evidence = [
        _evidence(
            tier=1,
            source="task_contract",
            name="delivery_contract_projection_exact",
            passed=checks["delivery_contract_projection_exact"],
            decisive=True,
        ),
        _evidence(
            tier=1,
            source="workspace",
            name="required_paths_present",
            passed=checks["required_paths_present"],
            decisive=bool(contract.required_paths),
        ),
        _evidence(
            tier=1,
            source="workspace",
            name="expected_file_contents_match",
            passed=checks["expected_file_contents_match"],
            decisive=bool(contract.expected_file_contents),
        ),
        _evidence(
            tier=1,
            source="workspace",
            name="provenance_indexes_valid",
            passed=checks["provenance_indexes_valid"],
            decisive=bool(contract.provenance_index_paths),
        ),
        _evidence(
            tier=1,
            source="task_contract",
            name="direct_response_exact",
            passed=checks["direct_response_exact"],
            decisive=direct is not None,
        ),
        _evidence(
            tier=2,
            source="deterministic_verifier",
            name="provider_reasoning_executed",
            passed=checks["provider_reasoning_executed"],
            decisive=contract.provider_reasoning_required,
        ),
        _evidence(
            tier=3,
            source="workspace_delta",
            name="workspace_mutation_observed",
            passed=checks["workspace_mutation_observed"],
            decisive=contract.workspace_mutation_required,
        ),
        _evidence(
            tier=4,
            source="model_final_response",
            name="final_response_present",
            passed=checks["final_response_present"],
            decisive=contract.final_response_required,
        ),
    ]
    failed = [item for item in evidence if item["decisive"] and not item["passed"]]
    passed = not failed
    return {
        "schema": "zyra.goal-delivery-verification/v2",
        "passed": passed,
        "checks": checks,
        "contract": expected_projection,
        "path_evidence": path_evidence,
        "provenance_index_evidence": provenance_index_evidence,
        "evidence": evidence,
        "decision": {
            "policy": "contract_evidence_priority",
            "priority": [
                "external_state_and_contract",
                "deterministic_verification",
                "tool_artifact_and_workspace_receipts",
                "model_final_response",
            ],
            "decisive_failures": [item["name"] for item in failed],
            "reason": (
                "all decisive contract evidence passed"
                if passed
                else "one or more decisive higher-priority contract checks failed"
            ),
        },
        "workspace_root_redacted": True,
    }


def _required_skills(
    goal: str,
    required_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    """Compile explicit skill obligations and unambiguous typed deliverables."""

    required: list[str] = []
    for name in _KNOWN_SKILL_NAMES:
        quoted = re.search(rf"[`\"'“‘]{re.escape(name)}[`\"'”’]", goal, re.IGNORECASE)
        if quoted is None:
            continue
        start = max(0, quoted.start() - 64)
        end = min(len(goal), quoted.end() + 64)
        clause = goal[start:end]
        if re.search(
            r"(?:使用|调用|运行|执行|use|invoke|run|execute|required?|must)",
            clause,
            re.IGNORECASE,
        ):
            required.append(name)
    if (
        "trace-summary" not in required
        and (
            (
                re.search(r"(?:trace|追踪|轨迹)[\s/_-]*(?:摘要|summary)", goal, re.IGNORECASE)
                and re.search(r"(?:使用|保存|生成|use|save|create)", goal, re.IGNORECASE)
            )
            or (
                _TRACE_DELIVERY_REQUIREMENT.search(goal)
                and not _NEGATED_TRACE_DELIVERY_REQUIREMENT.search(goal)
            )
        )
    ):
        required.append("trace-summary")
    if "report-writing" not in required and any(
        re.search(
            r"(?:^|[-_.])(?:report|reports)(?:$|[-_.])|报告",
            PurePosixPath(path).stem,
            re.IGNORECASE,
        )
        for path in required_paths
    ):
        required.append("report-writing")
    return tuple(name for name in _KNOWN_SKILL_NAMES if name in required)


def validate_provenance_index(path: Path | None, relative: str) -> dict[str, Any]:
    """Validate the minimum portable contract of a JSON source/evidence index."""

    missing_digest: list[str] = []
    missing_method: list[str] = []
    indexed_paths: list[str] = []
    error = ""
    document: Any = None
    try:
        if path is None or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("index is missing or exceeds 4 MiB")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            indexed = str(value.get("path") or "").strip().replace("\\", "/")
            if indexed and indexed != relative.replace("\\", "/"):
                indexed_paths.append(indexed)
                digest = str(value.get("sha256") or value.get("digest") or "").strip()
                method = str(
                    value.get("extraction_method")
                    or value.get("extractionMethod")
                    or value.get("method")
                    or ""
                ).strip()
                normalized_digest = digest.removeprefix("sha256:")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized_digest):
                    missing_digest.append(indexed)
                if not method:
                    missing_method.append(indexed)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    if document is not None:
        visit(document)
    passed = bool(indexed_paths) and not error and not missing_digest and not missing_method
    return {
        "path": relative,
        "passed": passed,
        "indexed_path_count": len(indexed_paths),
        "missing_digest_paths": sorted(set(missing_digest))[:100],
        "missing_extraction_method_paths": sorted(set(missing_method))[:100],
        "error": error,
        "physical_location_redacted": True,
    }


def _safe_relative_path(value: str) -> str:
    rendered = str(value or "").strip().replace("\\", "/")
    if rendered.startswith("/app/"):
        rendered = rendered.removeprefix("/app/")
    if (
        not rendered
        or rendered.startswith("/")
        or rendered.startswith("//")
        or any(character.isspace() for character in rendered)
        or "@" in rendered
        or re.match(r"^[A-Za-z]:/", rendered)
        or re.fullmatch(r"\d+(?:\.\d+)+", rendered)
    ):
        return ""
    candidate = PurePosixPath(rendered)
    if candidate.is_absolute() or ".." in candidate.parts:
        return ""
    return candidate.as_posix()


def _path_has_delivery_context(goal: str, position: int, path: str) -> bool:
    """Distinguish requested outputs from inputs, commands and schema references."""

    normalized = path.casefold()
    if normalized.startswith(("submission/", "work/")):
        return True
    start = 0
    end = len(goal)
    preceding = list(_PATH_CONTEXT_BOUNDARY.finditer(goal, 0, position))
    if preceding:
        start = preceding[-1].end()
    following = _PATH_CONTEXT_BOUNDARY.search(goal, position)
    if following is not None:
        end = following.start()
    clause = goal[start:end]
    if not _workspace_change_requested(clause):
        return False
    if normalized.startswith(("schemas/", "inputs/")):
        before_path = goal[start:position]
        return bool(
            re.search(
                r"(?:修改|更新|编辑|写入|生成|创建|replace|modify|update|edit|write|generate|create)"
                r"[^。.!！?？;；\r\n]{0,80}$",
                before_path,
                re.IGNORECASE,
            )
        )
    return True


def _evidence(
    *,
    tier: int,
    source: str,
    name: str,
    passed: bool,
    decisive: bool,
) -> dict[str, Any]:
    return {
        "tier": tier,
        "source": source,
        "name": name,
        "passed": bool(passed),
        "decisive": bool(decisive),
    }


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
    "validate_provenance_index",
]
