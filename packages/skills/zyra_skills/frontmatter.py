from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import SkillFrontmatterError
from .models import (
    SKILL_SCHEMA,
    SkillContextBudget,
    SkillDeclaredMetadata,
    SkillHookSpec,
    SkillInvocationSpec,
    ToolSelector,
    validate_skill_name,
)


MAX_FRONTMATTER_BYTES = 64_000
MAX_FRONTMATTER_LINES = 1_000
MAX_FRONTMATTER_DEPTH = 12
MAX_SCALAR_LENGTH = 16_000
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
NUMBER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

KNOWN_FIELDS = {
    "schema",
    "name",
    "display-name",
    "display_name",
    "description",
    "when-to-use",
    "when_to_use",
    "version",
    "argument-hint",
    "argument_hint",
    "arguments",
    "user-invocable",
    "user_invocable",
    "model-invocable",
    "model_invocable",
    "disable-model-invocation",
    "disable_model_invocation",
    "allowed-tools",
    "allowed_tools",
    "invocation",
    "context",
    "agent",
    "max-skill-depth",
    "max_skill_depth",
    "context-budget",
    "context_budget",
    "resources",
    "paths",
    "hooks",
    "model",
    "effort",
}


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
    metadata: SkillDeclaredMetadata
    body: str
    frontmatter: dict[str, Any]
    frontmatter_text: str


@dataclass(slots=True)
class _Frame:
    indent: int
    container: dict[str, Any] | list[Any]
    pending_key: str = ""


class SafeFrontmatterParser:
    """Strict, dependency-free YAML subset for SKILL.md security fields.

    It accepts mappings, nested mappings, lists, JSON-style inline values,
    booleans, null, numbers, and quoted/plain strings. YAML aliases, tags,
    anchors, merge keys, multi-documents, and executable/custom types are
    rejected. Duplicate keys are always errors.
    """

    def parse(self, text: str) -> dict[str, Any]:
        if len(text.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
            raise SkillFrontmatterError("skill frontmatter exceeds byte limit")
        lines = text.splitlines()
        if len(lines) > MAX_FRONTMATTER_LINES:
            raise SkillFrontmatterError("skill frontmatter exceeds line limit")
        root: dict[str, Any] = {}
        frames: list[_Frame] = [_Frame(indent=-1, container=root)]
        for line_number, raw_line in enumerate(lines, start=1):
            if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
                raise SkillFrontmatterError("tabs are forbidden in frontmatter indentation", detail={"line": line_number})
            content = _strip_comment(raw_line).rstrip()
            if not content.strip():
                continue
            stripped = content.lstrip(" ")
            indent = len(content) - len(stripped)
            if indent % 2:
                raise SkillFrontmatterError("frontmatter indentation must use two-space levels", detail={"line": line_number})
            if indent // 2 > MAX_FRONTMATTER_DEPTH:
                raise SkillFrontmatterError("frontmatter nesting is too deep", detail={"line": line_number})
            _reject_yaml_features(stripped, line_number)
            while len(frames) > 1 and indent <= frames[-1].indent:
                frames.pop()
            parent = frames[-1]
            if parent.pending_key:
                target: dict[str, Any] | list[Any]
                if stripped.startswith("- ") or stripped == "-":
                    target = []
                else:
                    target = {}
                assert isinstance(parent.container, dict)
                parent.container[parent.pending_key] = target
                parent.pending_key = ""
                frame = _Frame(indent=indent - 1, container=target)
                frames.append(frame)
                parent = frame
            if stripped.startswith("- ") or stripped == "-":
                self._parse_list_line(frames, parent, indent, stripped, line_number)
            else:
                self._parse_mapping_line(frames, parent, indent, stripped, line_number)
        for frame in frames:
            if frame.pending_key:
                assert isinstance(frame.container, dict)
                frame.container[frame.pending_key] = {}
                frame.pending_key = ""
        return root

    def _parse_mapping_line(
        self,
        frames: list[_Frame],
        parent: _Frame,
        indent: int,
        stripped: str,
        line_number: int,
    ) -> None:
        if not isinstance(parent.container, dict):
            raise SkillFrontmatterError("mapping entry found inside a scalar list", detail={"line": line_number})
        key, value_text = _split_key_value(stripped, line_number)
        if key in parent.container or key == parent.pending_key:
            raise SkillFrontmatterError("duplicate frontmatter key", detail={"line": line_number, "key": key})
        if value_text == "":
            parent.pending_key = key
            return
        value = _parse_scalar(value_text, line_number)
        parent.container[key] = value
        if isinstance(value, (dict, list)):
            frames.append(_Frame(indent=indent, container=value))

    def _parse_list_line(
        self,
        frames: list[_Frame],
        parent: _Frame,
        indent: int,
        stripped: str,
        line_number: int,
    ) -> None:
        if not isinstance(parent.container, list):
            raise SkillFrontmatterError("list item found outside a list", detail={"line": line_number})
        value_text = stripped[1:].strip()
        if not value_text:
            nested: dict[str, Any] = {}
            parent.container.append(nested)
            frames.append(_Frame(indent=indent, container=nested))
            return
        if _looks_like_mapping(value_text):
            key, remainder = _split_key_value(value_text, line_number)
            nested = {}
            parent.container.append(nested)
            frame = _Frame(indent=indent, container=nested)
            frames.append(frame)
            if remainder:
                nested[key] = _parse_scalar(remainder, line_number)
            else:
                frame.pending_key = key
            return
        parent.container.append(_parse_scalar(value_text, line_number))


def parse_skill_document(text: str, *, expected_name: str | None = None) -> ParsedSkillDocument:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise SkillFrontmatterError("SKILL.md must start with a frontmatter delimiter")
    delimiter = normalized.find("\n---\n", 4)
    if delimiter < 0:
        raise SkillFrontmatterError("SKILL.md frontmatter closing delimiter is missing")
    frontmatter_text = normalized[4:delimiter]
    body = normalized[delimiter + 5 :]
    if body.startswith("---\n") or "\n---\n---\n" in body:
        raise SkillFrontmatterError("multiple YAML documents are forbidden")
    raw = SafeFrontmatterParser().parse(frontmatter_text)
    metadata = metadata_from_frontmatter(raw, expected_name=expected_name)
    return ParsedSkillDocument(metadata=metadata, body=body, frontmatter=raw, frontmatter_text=frontmatter_text)


def metadata_from_frontmatter(
    raw: Mapping[str, Any],
    *,
    expected_name: str | None = None,
) -> SkillDeclaredMetadata:
    item = {str(key): value for key, value in raw.items()}
    unknown = [key for key in item if key not in KNOWN_FIELDS and not key.startswith("x-")]
    if unknown:
        raise SkillFrontmatterError("unknown skill frontmatter fields", detail={"fields": sorted(unknown)})
    name = validate_skill_name(_required_string(item, "name"))
    if expected_name is not None and name != validate_skill_name(expected_name):
        raise SkillFrontmatterError(
            "frontmatter name must match the skill directory name",
            detail={"name": name, "directory": expected_name},
        )
    description = _required_string(item, "description")
    schema = _string(item.get("schema") or SKILL_SCHEMA, "schema")
    invocation_raw = _mapping_value(item.get("invocation"), "invocation")
    context = _string(item.get("context") or "", "context")
    if context:
        if context not in {"inline", "fork"}:
            raise SkillFrontmatterError("context must be inline or fork")
        invocation_raw.setdefault("mode", context)
    if item.get("agent") is not None:
        invocation_raw.setdefault("agent", _string(item.get("agent"), "agent"))
    if item.get("max-skill-depth") is not None or item.get("max_skill_depth") is not None:
        invocation_raw.setdefault("max-skill-depth", item.get("max-skill-depth", item.get("max_skill_depth")))
    try:
        invocation = SkillInvocationSpec.from_dict(invocation_raw, context=context)
    except (TypeError, ValueError) as error:
        raise SkillFrontmatterError("invalid skill invocation contract", detail={"error": str(error)}) from error
    allowed_present = "allowed-tools" in item or "allowed_tools" in item
    raw_allowed = item.get("allowed-tools", item.get("allowed_tools"))
    allowed_tools = None
    if allowed_present:
        allowed_tools = _parse_tool_selectors(raw_allowed)
    raw_budget = item.get("context-budget", item.get("context_budget"))
    try:
        budget = SkillContextBudget.from_dict(_mapping_value(raw_budget, "context-budget"))
    except (TypeError, ValueError) as error:
        raise SkillFrontmatterError("invalid skill context budget", detail={"error": str(error)}) from error
    resources = _string_sequence(item.get("resources"), "resources")
    paths = _string_sequence(item.get("paths"), "paths")
    for path in paths:
        if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            raise SkillFrontmatterError("skill path condition may not escape the workspace", detail={"path": path})
    hooks = _parse_hooks(item.get("hooks"))
    user_invocable = _boolean(item.get("user-invocable", item.get("user_invocable", True)), "user-invocable")
    disable_model = _boolean(
        item.get("disable-model-invocation", item.get("disable_model_invocation", False)),
        "disable-model-invocation",
    )
    model_invocable = _boolean(item.get("model-invocable", item.get("model_invocable", not disable_model)), "model-invocable")
    if disable_model:
        model_invocable = False
    extensions = {key: value for key, value in item.items() if key.startswith("x-")}
    try:
        return SkillDeclaredMetadata(
            schema=schema,
            name=name,
            display_name=_string(item.get("display-name", item.get("display_name", "")), "display-name"),
            description=description,
            when_to_use=_string(item.get("when-to-use", item.get("when_to_use", "")), "when-to-use"),
            declared_version=_string(item.get("version") or "0", "version"),
            argument_hint=_string(item.get("argument-hint", item.get("argument_hint", "")), "argument-hint"),
            user_invocable=user_invocable,
            model_invocable=model_invocable,
            path_conditions=paths,
            invocation=invocation,
            allowed_tools=allowed_tools,
            context_budget=budget,
            resources=resources,
            hooks=hooks,
            model=_string(item.get("model") or "", "model"),
            effort=_string(item.get("effort") or "", "effort"),
            extensions=extensions,
        )
    except (TypeError, ValueError) as error:
        raise SkillFrontmatterError("invalid skill metadata", detail={"error": str(error)}) from error


def parse_metadata_prefix(text: str, *, expected_name: str | None = None) -> SkillDeclaredMetadata:
    """Parse only the bounded frontmatter prefix used during discovery."""
    if not text.startswith("---\n"):
        raise SkillFrontmatterError("SKILL.md must start with frontmatter")
    delimiter = text.find("\n---\n", 4)
    if delimiter < 0:
        raise SkillFrontmatterError("frontmatter closing delimiter not found in discovery prefix")
    raw = SafeFrontmatterParser().parse(text[4:delimiter])
    return metadata_from_frontmatter(raw, expected_name=expected_name)


def _parse_tool_selectors(value: object) -> tuple[ToolSelector, ...]:
    if value is None:
        return ()
    entries: list[object]
    if isinstance(value, str):
        entries = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    elif isinstance(value, list):
        entries = value
    else:
        raise SkillFrontmatterError("allowed-tools must be a string or list")
    selectors: list[ToolSelector] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, (str, Mapping)):
            raise SkillFrontmatterError("allowed-tools entries must be strings or mappings")
        try:
            selector = ToolSelector.parse(entry)
        except (TypeError, ValueError) as error:
            raise SkillFrontmatterError("invalid allowed-tools selector", detail={"entry": str(entry), "error": str(error)}) from error
        identity = json.dumps(selector.to_dict(), sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            selectors.append(selector)
    return tuple(selectors)


def _parse_hooks(value: object) -> tuple[SkillHookSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SkillFrontmatterError("hooks must be a list of declarative hook mappings")
    hooks: list[SkillHookSpec] = []
    identifiers: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise SkillFrontmatterError("hook entry must be a mapping", detail={"index": index})
        try:
            hook = SkillHookSpec.from_dict(entry, index=index)
        except (TypeError, ValueError) as error:
            raise SkillFrontmatterError("invalid skill hook", detail={"index": index, "error": str(error)}) from error
        if hook.hook_id in identifiers:
            raise SkillFrontmatterError("duplicate skill hook id", detail={"hook_id": hook.hook_id})
        identifiers.add(hook.hook_id)
        hooks.append(hook)
    return tuple(hooks)


def _required_string(item: Mapping[str, Any], key: str) -> str:
    if key not in item:
        raise SkillFrontmatterError(f"required frontmatter field is missing: {key}")
    value = _string(item[key], key).strip()
    if not value:
        raise SkillFrontmatterError(f"frontmatter field may not be empty: {key}")
    return value


def _string(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SkillFrontmatterError(f"{field} must be a string")
    if len(value) > MAX_SCALAR_LENGTH:
        raise SkillFrontmatterError(f"{field} exceeds scalar length limit")
    return value


def _boolean(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise SkillFrontmatterError(f"{field} must be a boolean")


def _mapping_value(value: object, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SkillFrontmatterError(f"{field} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _string_sequence(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        entries: Iterable[object] = [value]
    elif isinstance(value, list):
        entries = value
    else:
        raise SkillFrontmatterError(f"{field} must be a string or list")
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise SkillFrontmatterError(f"{field} entries must be non-empty strings")
        result.append(entry.strip())
    return tuple(dict.fromkeys(result))


def _split_key_value(value: str, line_number: int) -> tuple[str, str]:
    quote = ""
    depth = 0
    for index, char in enumerate(value):
        if quote:
            if char == quote and (index == 0 or value[index - 1] != "\\"):
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == ":" and depth == 0:
            key = value[:index].strip()
            if not KEY_PATTERN.fullmatch(key) or key == "<<":
                raise SkillFrontmatterError("invalid frontmatter key", detail={"line": line_number, "key": key})
            return key, value[index + 1 :].strip()
    raise SkillFrontmatterError("frontmatter mapping entry is missing ':'", detail={"line": line_number})


def _parse_scalar(value: str, line_number: int) -> object:
    raw = value.strip()
    if len(raw) > MAX_SCALAR_LENGTH:
        raise SkillFrontmatterError("frontmatter scalar exceeds length limit", detail={"line": line_number})
    _reject_yaml_features(raw, line_number)
    if raw.startswith(("[", "{")):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SkillFrontmatterError("inline collection must be valid JSON", detail={"line": line_number}) from error
        _validate_collection(parsed, depth=0, line_number=line_number)
        return parsed
    if raw.startswith(('"', "'")):
        if len(raw) < 2 or raw[-1] != raw[0]:
            raise SkillFrontmatterError("unterminated quoted scalar", detail={"line": line_number})
        if raw[0] == '"':
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                raise SkillFrontmatterError("invalid quoted scalar", detail={"line": line_number}) from error
        return raw[1:-1].replace("''", "'")
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if NUMBER_PATTERN.fullmatch(raw):
        return float(raw) if "." in raw else int(raw)
    if raw in {"|", ">"} or raw.startswith(("|", ">")):
        raise SkillFrontmatterError("multiline YAML scalars are not supported", detail={"line": line_number})
    return raw


def _validate_collection(value: object, *, depth: int, line_number: int) -> None:
    if depth > MAX_FRONTMATTER_DEPTH:
        raise SkillFrontmatterError("inline collection is too deeply nested", detail={"line": line_number})
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
                raise SkillFrontmatterError("invalid inline mapping key", detail={"line": line_number})
            _validate_collection(item, depth=depth + 1, line_number=line_number)
    elif isinstance(value, list):
        for item in value:
            _validate_collection(item, depth=depth + 1, line_number=line_number)
    elif isinstance(value, str) and len(value) > MAX_SCALAR_LENGTH:
        raise SkillFrontmatterError("inline scalar exceeds length limit", detail={"line": line_number})


def _strip_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _reject_yaml_features(value: str, line_number: int) -> None:
    stripped = value.strip()
    if stripped.startswith("---") or stripped.startswith("..."):
        raise SkillFrontmatterError("nested YAML documents are forbidden", detail={"line": line_number})
    tokens = re.split(r"\s+", stripped)
    if any(token.startswith(("!", "&", "*")) for token in tokens):
        raise SkillFrontmatterError("YAML tags, anchors, and aliases are forbidden", detail={"line": line_number})
    if "<<:" in stripped:
        raise SkillFrontmatterError("YAML merge keys are forbidden", detail={"line": line_number})


def _looks_like_mapping(value: str) -> bool:
    try:
        _split_key_value(value, 0)
        return True
    except SkillFrontmatterError:
        return False
