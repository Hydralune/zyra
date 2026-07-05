from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from zyra_core import new_id, now_iso, to_jsonable


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"


class PermissionRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    effect: PermissionEffect
    reason: str
    permanent: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PermissionRule:
    operation: PermissionOperation
    pattern: str
    effect: PermissionEffect
    reason: str = ""
    rule_id: str = field(default_factory=lambda: new_id("permrule"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)

    def matches(self, operation: PermissionOperation, subject: str) -> bool:
        if self.operation != operation:
            return False
        pattern = self.pattern.lower()
        value = subject.lower()
        if any(char in pattern for char in "*?[]"):
            return fnmatch(value, pattern)
        return pattern in value


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    run_id: str
    task_id: str
    tool_call_id: str
    operation: PermissionOperation
    subject: str
    reason: str
    request_id: str = field(default_factory=lambda: new_id("permreq"))
    status: PermissionRequestStatus = PermissionRequestStatus.PENDING
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class JsonPermissionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list_rules(self) -> list[PermissionRule]:
        return [_permission_rule_from_json(item) for item in self._load().get("rules", [])]

    def add_rule(self, rule: PermissionRule) -> PermissionRule:
        data = self._load()
        rules = [item for item in data.get("rules", []) if item.get("rule_id") != rule.rule_id]
        rules.append(to_jsonable(rule))
        data["rules"] = rules
        self._save(data)
        return rule

    def decide(self, operation: PermissionOperation, subject: str) -> PermissionDecision | None:
        for rule in reversed(self.list_rules()):
            if rule.matches(operation, subject):
                return PermissionDecision(
                    rule.effect,
                    rule.reason or f"matched permission rule {rule.rule_id}",
                    permanent=True,
                    metadata={"rule_id": rule.rule_id, "pattern": rule.pattern},
                )
        return None

    def create_request(self, request: PermissionRequest) -> PermissionRequest:
        data = self._load()
        requests = [item for item in data.get("requests", []) if item.get("request_id") != request.request_id]
        requests.append(to_jsonable(request))
        data["requests"] = requests
        self._save(data)
        return request

    def list_requests(self, status: PermissionRequestStatus | None = None) -> list[PermissionRequest]:
        requests = [_permission_request_from_json(item) for item in self._load().get("requests", [])]
        if status is None:
            return requests
        return [request for request in requests if request.status == status]

    def resolve_request(
        self,
        request_id: str,
        status: PermissionRequestStatus,
        *,
        create_rule: bool = False,
    ) -> PermissionRequest | None:
        data = self._load()
        resolved: PermissionRequest | None = None
        updated_requests: list[dict[str, Any]] = []
        for item in data.get("requests", []):
            request = _permission_request_from_json(item)
            if request.request_id == request_id:
                request = PermissionRequest(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    tool_call_id=request.tool_call_id,
                    operation=request.operation,
                    subject=request.subject,
                    reason=request.reason,
                    request_id=request.request_id,
                    status=status,
                    created_at=request.created_at,
                    resolved_at=now_iso(),
                    metadata=request.metadata,
                )
                resolved = request
            updated_requests.append(to_jsonable(request))
        if resolved is None:
            return None
        data["requests"] = updated_requests
        self._save(data)
        if create_rule and status in {PermissionRequestStatus.APPROVED, PermissionRequestStatus.DENIED}:
            self.add_rule(
                PermissionRule(
                    operation=resolved.operation,
                    pattern=resolved.subject,
                    effect=PermissionEffect.ALLOW if status == PermissionRequestStatus.APPROVED else PermissionEffect.DENY,
                    reason=f"created from permission request {resolved.request_id}",
                    metadata={"permission_request_id": resolved.request_id},
                )
            )
        return resolved

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"rules": [], "requests": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"rules": [], "requests": []}
        if not isinstance(data, dict):
            return {"rules": [], "requests": []}
        data.setdefault("rules", [])
        data.setdefault("requests", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(slots=True)
class ToolPermissionPolicy:
    workspace_root: Path
    default_effect: PermissionEffect = PermissionEffect.ASK
    read_roots: list[Path] = field(default_factory=list)
    write_roots: list[Path] = field(default_factory=list)
    rules: list[PermissionRule] = field(default_factory=list)
    denied_shell_fragments: tuple[str, ...] = (
        "git reset --hard",
        "git checkout --",
        "Remove-Item -Recurse",
        "rm -rf",
    )

    @classmethod
    def for_workspace(
        cls,
        workspace_root: str | Path,
        *,
        rules: list[PermissionRule] | None = None,
    ) -> "ToolPermissionPolicy":
        root = Path(workspace_root).resolve()
        return cls(
            workspace_root=root,
            read_roots=[root],
            write_roots=[root],
            rules=rules or [],
        )

    def decide_read(self, path: str | Path) -> PermissionDecision:
        resolved = Path(path).resolve()
        if _is_inside_any(resolved, self.read_roots):
            return PermissionDecision(PermissionEffect.ALLOW, "path is inside readable workspace")
        return PermissionDecision(PermissionEffect.DENY, "path is outside readable workspace")

    def decide_write(self, path: str | Path) -> PermissionDecision:
        resolved = Path(path).resolve()
        if _is_inside_any(resolved, self.write_roots):
            return PermissionDecision(PermissionEffect.ALLOW, "path is inside writable workspace")
        return PermissionDecision(PermissionEffect.DENY, "path is outside writable workspace")

    def decide_shell(self, command: str) -> PermissionDecision:
        lowered = command.lower()
        for fragment in self.denied_shell_fragments:
            if fragment.lower() in lowered:
                return PermissionDecision(
                    PermissionEffect.DENY,
                    f"shell command contains denied fragment: {fragment}",
                )
        for rule in reversed(self.rules):
            if rule.matches(PermissionOperation.SHELL, command):
                return PermissionDecision(
                    rule.effect,
                    rule.reason or f"matched permission rule {rule.rule_id}",
                    permanent=True,
                    metadata={"rule_id": rule.rule_id, "pattern": rule.pattern},
                )
        return PermissionDecision(self.default_effect, "shell command requires runtime policy review")


def _is_inside_any(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _permission_rule_from_json(data: dict[str, Any]) -> PermissionRule:
    return PermissionRule(
        operation=_enum_or_default(PermissionOperation, data.get("operation"), PermissionOperation.SHELL),
        pattern=str(data.get("pattern") or ""),
        effect=_enum_or_default(PermissionEffect, data.get("effect"), PermissionEffect.ASK),
        reason=str(data.get("reason") or ""),
        rule_id=str(data.get("rule_id") or new_id("permrule")),
        created_at=str(data.get("created_at") or now_iso()),
        metadata={str(key): str(value) for key, value in dict(data.get("metadata") or {}).items()},
    )


def _permission_request_from_json(data: dict[str, Any]) -> PermissionRequest:
    return PermissionRequest(
        run_id=str(data.get("run_id") or ""),
        task_id=str(data.get("task_id") or ""),
        tool_call_id=str(data.get("tool_call_id") or ""),
        operation=_enum_or_default(PermissionOperation, data.get("operation"), PermissionOperation.SHELL),
        subject=str(data.get("subject") or ""),
        reason=str(data.get("reason") or ""),
        request_id=str(data.get("request_id") or new_id("permreq")),
        status=_enum_or_default(PermissionRequestStatus, data.get("status"), PermissionRequestStatus.PENDING),
        created_at=str(data.get("created_at") or now_iso()),
        resolved_at=None if data.get("resolved_at") is None else str(data.get("resolved_at")),
        metadata={str(key): str(value) for key, value in dict(data.get("metadata") or {}).items()},
    )


def _enum_or_default(enum_type: type[Any], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
