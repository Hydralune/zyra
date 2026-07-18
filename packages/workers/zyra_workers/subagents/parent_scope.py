from __future__ import annotations

"""Server-issued parent execution scope and child monotonic narrowing.

Subagent callers are untrusted at the API/tool boundary.  They may request a
smaller tool, MCP, workspace, model or permission scope, but cannot declare
what the parent owns.  A snapshot is built from canonical Zyra owners and
signed by this store before any child isolation, budget reservation or worker
dispatch happens.
"""

import copy
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso
from zyra_runtime import ToolRegistry

from .models import PermissionMode


class ParentScopeError(RuntimeError):
    pass


class ParentScopeDisabled(ParentScopeError):
    pass


class ParentScopeConflict(ParentScopeError):
    pass


class ParentScopeExpired(ParentScopeError):
    pass


class ParentScopeViolation(ParentScopeError, PermissionError):
    def __init__(self, findings: Sequence["ScopeFinding"]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(item.message for item in self.findings if item.blocking))


class ScopeFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ScopeFinding:
    code: str
    message: str
    field: str
    blocking: bool = True
    severity: ScopeFindingSeverity = ScopeFindingSeverity.ERROR
    requested: Any = None
    allowed: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "blocking": self.blocking,
            "severity": self.severity.value,
            "requested": _safe_value(self.requested),
            "allowed": _safe_value(self.allowed),
        }


@dataclass(frozen=True, slots=True)
class ParentToolDescriptor:
    name: str
    provenance: str
    read_only: bool
    destructive: bool
    open_world: bool
    concurrency_safe: bool
    mcp_server_id: str = ""
    capability_digest: str = ""

    def __post_init__(self) -> None:
        normalized = str(self.name).strip()
        if not normalized:
            raise ValueError("tool name is required")
        object.__setattr__(self, "name", normalized)

    @property
    def canonical_name(self) -> str:
        return self.name.casefold()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provenance": self.provenance,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "open_world": self.open_world,
            "concurrency_safe": self.concurrency_safe,
            "mcp_server_id": self.mcp_server_id,
            "capability_digest": self.capability_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParentToolDescriptor":
        return cls(
            name=str(value.get("name") or ""),
            provenance=str(value.get("provenance") or ""),
            read_only=bool(value.get("read_only", False)),
            destructive=bool(value.get("destructive", False)),
            open_world=bool(value.get("open_world", True)),
            concurrency_safe=bool(value.get("concurrency_safe", False)),
            mcp_server_id=str(value.get("mcp_server_id") or ""),
            capability_digest=str(value.get("capability_digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class ParentExecutionScopeSnapshot:
    snapshot_id: str
    run_id: str
    parent_task_id: str
    parent_session_id: str
    session_revision: int
    tool_generation: int
    tools: tuple[ParentToolDescriptor, ...]
    permission_mode: PermissionMode
    permission_revision: int
    permission_rule_ids: tuple[str, ...]
    deny_rule_ids: tuple[str, ...]
    deny_tool_names: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    mcp_catalog_generations: Mapping[str, int]
    model_allowlist: tuple[str, ...]
    effective_model: str
    workspace_root: str
    writable_roots: tuple[str, ...]
    readable_roots: tuple[str, ...]
    network_allowed: bool
    skill_refs: tuple[str, ...] = ()
    hook_refs: tuple[str, ...] = ()
    context_epoch: int = 0
    compact_boundary_id: str = ""
    expires_revision: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    issued_at: str = field(default_factory=now_iso)
    signature: str = ""

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "run_id", "parent_task_id", "parent_session_id", "workspace_root"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.session_revision < 0 or self.permission_revision < 0 or self.tool_generation < 0:
            raise ValueError("scope revisions cannot be negative")
        root = str(Path(self.workspace_root).resolve())
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "writable_roots", _canonical_paths(self.writable_roots, root))
        object.__setattr__(self, "readable_roots", _canonical_paths(self.readable_roots, root))
        object.__setattr__(self, "tools", tuple(sorted(self.tools, key=lambda item: item.canonical_name)))
        object.__setattr__(self, "permission_rule_ids", _unique_strings(self.permission_rule_ids))
        object.__setattr__(self, "deny_rule_ids", _unique_strings(self.deny_rule_ids))
        object.__setattr__(self, "deny_tool_names", tuple(sorted({str(item).casefold() for item in self.deny_tool_names if str(item).strip()})))
        object.__setattr__(self, "mcp_servers", _unique_strings(self.mcp_servers))
        object.__setattr__(self, "model_allowlist", _unique_strings(self.model_allowlist))
        object.__setattr__(self, "skill_refs", _unique_strings(self.skill_refs))
        object.__setattr__(self, "hook_refs", _unique_strings(self.hook_refs))
        object.__setattr__(self, "mcp_catalog_generations", {
            str(key): max(0, int(value)) for key, value in self.mcp_catalog_generations.items()
        })
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.tools)

    @property
    def tool_map(self) -> dict[str, ParentToolDescriptor]:
        return {item.canonical_name: item for item in self.tools}

    @property
    def unsigned_digest(self) -> str:
        return _digest(self.to_dict(include_signature=False))

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        value = {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "parent_session_id": self.parent_session_id,
            "session_revision": self.session_revision,
            "tool_generation": self.tool_generation,
            "tools": [item.to_dict() for item in self.tools],
            "permission_mode": self.permission_mode.value,
            "permission_revision": self.permission_revision,
            "permission_rule_ids": list(self.permission_rule_ids),
            "deny_rule_ids": list(self.deny_rule_ids),
            "deny_tool_names": list(self.deny_tool_names),
            "mcp_servers": list(self.mcp_servers),
            "mcp_catalog_generations": dict(sorted(self.mcp_catalog_generations.items())),
            "model_allowlist": list(self.model_allowlist),
            "effective_model": self.effective_model,
            "workspace_root": self.workspace_root,
            "writable_roots": list(self.writable_roots),
            "readable_roots": list(self.readable_roots),
            "network_allowed": self.network_allowed,
            "skill_refs": list(self.skill_refs),
            "hook_refs": list(self.hook_refs),
            "context_epoch": self.context_epoch,
            "compact_boundary_id": self.compact_boundary_id,
            "expires_revision": self.expires_revision,
            "metadata": _safe_mapping(self.metadata),
            "issued_at": self.issued_at,
        }
        if include_signature:
            value["signature"] = self.signature
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParentExecutionScopeSnapshot":
        return cls(
            snapshot_id=str(value.get("snapshot_id") or ""),
            run_id=str(value.get("run_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            session_revision=max(0, int(value.get("session_revision") or 0)),
            tool_generation=max(0, int(value.get("tool_generation") or 0)),
            tools=tuple(
                ParentToolDescriptor.from_dict(item)
                for item in value.get("tools") or ()
                if isinstance(item, Mapping)
            ),
            permission_mode=PermissionMode(str(value.get("permission_mode") or PermissionMode.DENY.value)),
            permission_revision=max(0, int(value.get("permission_revision") or 0)),
            permission_rule_ids=tuple(str(item) for item in value.get("permission_rule_ids") or ()),
            deny_rule_ids=tuple(str(item) for item in value.get("deny_rule_ids") or ()),
            deny_tool_names=tuple(str(item) for item in value.get("deny_tool_names") or ()),
            mcp_servers=tuple(str(item) for item in value.get("mcp_servers") or ()),
            mcp_catalog_generations={
                str(key): int(item) for key, item in _mapping(value.get("mcp_catalog_generations")).items()
            },
            model_allowlist=tuple(str(item) for item in value.get("model_allowlist") or ()),
            effective_model=str(value.get("effective_model") or ""),
            workspace_root=str(value.get("workspace_root") or ""),
            writable_roots=tuple(str(item) for item in value.get("writable_roots") or ()),
            readable_roots=tuple(str(item) for item in value.get("readable_roots") or ()),
            network_allowed=bool(value.get("network_allowed", False)),
            skill_refs=tuple(str(item) for item in value.get("skill_refs") or ()),
            hook_refs=tuple(str(item) for item in value.get("hook_refs") or ()),
            context_epoch=max(0, int(value.get("context_epoch") or 0)),
            compact_boundary_id=str(value.get("compact_boundary_id") or ""),
            expires_revision=(
                int(value["expires_revision"]) if value.get("expires_revision") is not None else None
            ),
            metadata=_safe_mapping(value.get("metadata")),
            issued_at=str(value.get("issued_at") or now_iso()),
            signature=str(value.get("signature") or ""),
        )


@dataclass(frozen=True, slots=True)
class ChildScopeRequest:
    requested_tools: tuple[str, ...] = ()
    requested_mcp_servers: tuple[str, ...] = ()
    requested_permission_mode: PermissionMode | None = None
    requested_model: str = ""
    requested_writable_paths: tuple[str, ...] = ()
    requested_readable_paths: tuple[str, ...] = ()
    requested_network: bool = False
    requested_skill_refs: tuple[str, ...] = ()
    requested_hook_refs: tuple[str, ...] = ()
    expected_session_revision: int | None = None
    expected_permission_revision: int | None = None
    expected_tool_generation: int | None = None
    expected_mcp_generations: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_tools", _unique_strings(self.requested_tools))
        object.__setattr__(self, "requested_mcp_servers", _unique_strings(self.requested_mcp_servers))
        object.__setattr__(self, "requested_skill_refs", _unique_strings(self.requested_skill_refs))
        object.__setattr__(self, "requested_hook_refs", _unique_strings(self.requested_hook_refs))
        object.__setattr__(self, "expected_mcp_generations", {
            str(key): max(0, int(value)) for key, value in self.expected_mcp_generations.items()
        })


@dataclass(frozen=True, slots=True)
class ChildExecutionScope:
    parent_snapshot_id: str
    parent_snapshot_digest: str
    tools: tuple[ParentToolDescriptor, ...]
    permission_mode: PermissionMode
    permission_rule_ids: tuple[str, ...]
    deny_rule_ids: tuple[str, ...]
    deny_tool_names: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    mcp_catalog_generations: Mapping[str, int]
    model: str
    workspace_root: str
    writable_paths: tuple[str, ...]
    readable_paths: tuple[str, ...]
    network_allowed: bool
    skill_refs: tuple[str, ...]
    hook_refs: tuple[str, ...]
    findings: tuple[ScopeFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.tools)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict(include_findings=False))

    def to_dict(self, *, include_findings: bool = True) -> dict[str, Any]:
        value = {
            "parent_snapshot_id": self.parent_snapshot_id,
            "parent_snapshot_digest": self.parent_snapshot_digest,
            "tools": [item.to_dict() for item in self.tools],
            "permission_mode": self.permission_mode.value,
            "permission_rule_ids": list(self.permission_rule_ids),
            "deny_rule_ids": list(self.deny_rule_ids),
            "deny_tool_names": list(self.deny_tool_names),
            "mcp_servers": list(self.mcp_servers),
            "mcp_catalog_generations": dict(sorted(self.mcp_catalog_generations.items())),
            "model": self.model,
            "workspace_root": self.workspace_root,
            "writable_paths": list(self.writable_paths),
            "readable_paths": list(self.readable_paths),
            "network_allowed": self.network_allowed,
            "skill_refs": list(self.skill_refs),
            "hook_refs": list(self.hook_refs),
            "created_at": self.created_at,
        }
        if include_findings:
            value["findings"] = [item.to_dict() for item in self.findings]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChildExecutionScope":
        return cls(
            parent_snapshot_id=str(value.get("parent_snapshot_id") or ""),
            parent_snapshot_digest=str(value.get("parent_snapshot_digest") or ""),
            tools=tuple(
                ParentToolDescriptor.from_dict(item)
                for item in value.get("tools") or ()
                if isinstance(item, Mapping)
            ),
            permission_mode=PermissionMode(str(value.get("permission_mode") or PermissionMode.DENY.value)),
            permission_rule_ids=tuple(str(item) for item in value.get("permission_rule_ids") or ()),
            deny_rule_ids=tuple(str(item) for item in value.get("deny_rule_ids") or ()),
            deny_tool_names=tuple(str(item) for item in value.get("deny_tool_names") or ()),
            mcp_servers=tuple(str(item) for item in value.get("mcp_servers") or ()),
            mcp_catalog_generations={str(key): int(item) for key, item in _mapping(value.get("mcp_catalog_generations")).items()},
            model=str(value.get("model") or ""),
            workspace_root=str(value.get("workspace_root") or ""),
            writable_paths=tuple(str(item) for item in value.get("writable_paths") or ()),
            readable_paths=tuple(str(item) for item in value.get("readable_paths") or ()),
            network_allowed=bool(value.get("network_allowed", False)),
            skill_refs=tuple(str(item) for item in value.get("skill_refs") or ()),
            hook_refs=tuple(str(item) for item in value.get("hook_refs") or ()),
            findings=tuple(
                ScopeFinding(
                    code=str(item.get("code") or "scope_finding"),
                    message=str(item.get("message") or ""),
                    field=str(item.get("field") or ""),
                    blocking=bool(item.get("blocking", False)),
                    severity=ScopeFindingSeverity(str(item.get("severity") or ScopeFindingSeverity.INFO.value)),
                    requested=item.get("requested"),
                    allowed=item.get("allowed"),
                )
                for item in value.get("findings") or ()
                if isinstance(item, Mapping)
            ),
            created_at=str(value.get("created_at") or now_iso()),
        )


class ParentScopeStore:
    schema = "zyra.parent-execution-scope/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_path = self.path.with_suffix(self.path.suffix + ".key")
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._snapshots: dict[str, ParentExecutionScopeSnapshot] = {}
        self._latest_by_session: dict[str, str] = {}
        self._secret = self._load_or_create_secret()
        self._load()

    def issue(self, snapshot: ParentExecutionScopeSnapshot) -> ParentExecutionScopeSnapshot:
        self._require_enabled()
        with self._lock:
            unsigned = replace(snapshot, signature="")
            signature = self._sign(unsigned.to_dict(include_signature=False))
            signed = replace(unsigned, signature=signature)
            self._snapshots[signed.snapshot_id] = signed
            self._latest_by_session[signed.parent_session_id] = signed.snapshot_id
            self._persist()
            return copy.deepcopy(signed)

    def get(self, snapshot_id: str, *, current_session_revision: int | None = None) -> ParentExecutionScopeSnapshot:
        self._require_enabled()
        with self._lock:
            value = self._snapshots.get(snapshot_id)
            if value is None:
                raise ParentScopeConflict(f"parent scope snapshot not found: {snapshot_id}")
            self.verify(value, current_session_revision=current_session_revision)
            return copy.deepcopy(value)

    def latest(self, parent_session_id: str, *, current_session_revision: int | None = None) -> ParentExecutionScopeSnapshot:
        self._require_enabled()
        with self._lock:
            snapshot_id = self._latest_by_session.get(parent_session_id)
            if not snapshot_id:
                raise ParentScopeConflict(f"no parent scope for session {parent_session_id}")
            return self.get(snapshot_id, current_session_revision=current_session_revision)

    def verify(self, snapshot: ParentExecutionScopeSnapshot, *, current_session_revision: int | None = None) -> None:
        expected = self._sign(snapshot.to_dict(include_signature=False))
        if not snapshot.signature or not hmac.compare_digest(snapshot.signature, expected):
            raise ParentScopeConflict("parent scope signature mismatch")
        if snapshot.expires_revision is not None and current_session_revision is not None:
            if current_session_revision > snapshot.expires_revision:
                raise ParentScopeExpired(
                    f"parent scope expired at revision {snapshot.expires_revision}; current {current_session_revision}"
                )
        if current_session_revision is not None and current_session_revision < snapshot.session_revision:
            raise ParentScopeConflict("current session revision predates parent scope")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D ParentScopeStore",
                "snapshots": [
                    item.to_dict() for item in sorted(self._snapshots.values(), key=lambda value: value.snapshot_id)
                ],
                "latest_by_session": dict(sorted(self._latest_by_session.items())),
            }
            return {**body, "checksum": _digest(body)}

    def _sign(self, value: Mapping[str, Any]) -> str:
        payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "hmac-sha256:" + hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _load_or_create_secret(self) -> bytes:
        if self.secret_path.exists():
            value = self.secret_path.read_bytes()
            if len(value) < 32:
                raise ParentScopeError("parent scope signing key is invalid")
            return value
        value = secrets.token_bytes(64)
        self.secret_path.write_bytes(value)
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return value

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ParentScopeError(f"cannot load parent scopes: {error}") from error
        if not isinstance(value, Mapping):
            raise ParentScopeError("parent scope store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and not hmac.compare_digest(expected, _digest(body)):
            raise ParentScopeError("parent scope store checksum mismatch")
        for item in value.get("snapshots") or ():
            if not isinstance(item, Mapping):
                continue
            snapshot = ParentExecutionScopeSnapshot.from_dict(item)
            self.verify(snapshot)
            self._snapshots[snapshot.snapshot_id] = snapshot
        raw_latest = value.get("latest_by_session")
        if isinstance(raw_latest, Mapping):
            self._latest_by_session = {str(key): str(item) for key, item in raw_latest.items()}

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ParentScopeDisabled("ParentScopeStore is disabled")


class ParentScopeProjectionPort:
    """Build a scope only from server-side owner projections."""

    def __init__(self, store: ParentScopeStore, *, disabled: bool = False) -> None:
        self.store = store
        self.disabled = bool(disabled)

    def build(
        self,
        *,
        run_id: str,
        parent_task_id: str,
        parent_session_id: str,
        session_revision: int,
        tool_registry: ToolRegistry,
        tool_generation: int,
        permission_mode: PermissionMode,
        permission_revision: int,
        permission_rules: Iterable[Any],
        mcp_catalog: Iterable[Any],
        model_allowlist: Iterable[str],
        effective_model: str,
        workspace_root: str | Path,
        writable_roots: Iterable[str | Path],
        readable_roots: Iterable[str | Path],
        network_allowed: bool,
        skill_refs: Iterable[str] = (),
        hook_refs: Iterable[str] = (),
        context_epoch: int = 0,
        compact_boundary_id: str = "",
        expires_after_revisions: int = 1,
        metadata: Mapping[str, Any] | None = None,
    ) -> ParentExecutionScopeSnapshot:
        if self.disabled:
            raise ParentScopeDisabled("parent scope projection port is disabled")
        tools = tuple(self._tool_descriptor(item) for item in tool_registry.list())
        rule_ids: list[str] = []
        deny_ids: list[str] = []
        deny_tools: list[str] = []
        for rule in permission_rules:
            raw = _object_mapping(rule)
            rule_id = str(raw.get("rule_id") or raw.get("id") or "")
            if rule_id:
                rule_ids.append(rule_id)
            effect = str(raw.get("effect") or raw.get("decision") or "").casefold()
            if effect == "deny":
                if rule_id:
                    deny_ids.append(rule_id)
                tool = _mapping(raw.get("tool"))
                tool_name = str(tool.get("name") or raw.get("tool_name") or "")
                if tool_name:
                    deny_tools.append(tool_name)
        mcp_servers: list[str] = []
        mcp_generations: dict[str, int] = {}
        for item in mcp_catalog:
            raw = _object_mapping(item)
            server_id = str(raw.get("server_id") or raw.get("name") or "")
            if not server_id:
                continue
            state = str(raw.get("state") or raw.get("status") or "connected").casefold()
            disabled = bool(raw.get("disabled", False))
            if not disabled and state not in {"disabled", "rejected", "needs-auth", "failed"}:
                mcp_servers.append(server_id)
            mcp_generations[server_id] = max(0, int(raw.get("generation") or raw.get("capability_generation") or 0))
        root = str(Path(workspace_root).resolve())
        snapshot = ParentExecutionScopeSnapshot(
            snapshot_id=new_id("parentscope"),
            run_id=run_id,
            parent_task_id=parent_task_id,
            parent_session_id=parent_session_id,
            session_revision=max(0, int(session_revision)),
            tool_generation=max(0, int(tool_generation)),
            tools=tools,
            permission_mode=permission_mode,
            permission_revision=max(0, int(permission_revision)),
            permission_rule_ids=tuple(rule_ids),
            deny_rule_ids=tuple(deny_ids),
            deny_tool_names=tuple(deny_tools),
            mcp_servers=tuple(mcp_servers),
            mcp_catalog_generations=mcp_generations,
            model_allowlist=tuple(str(item) for item in model_allowlist),
            effective_model=str(effective_model),
            workspace_root=root,
            writable_roots=tuple(str(item) for item in writable_roots),
            readable_roots=tuple(str(item) for item in readable_roots),
            network_allowed=bool(network_allowed),
            skill_refs=tuple(str(item) for item in skill_refs),
            hook_refs=tuple(str(item) for item in hook_refs),
            context_epoch=max(0, int(context_epoch)),
            compact_boundary_id=str(compact_boundary_id),
            expires_revision=max(0, int(session_revision)) + max(0, int(expires_after_revisions)),
            metadata={
                "authority": "server-side canonical owner projections",
                "client_declared_ceiling": False,
                **_safe_mapping(metadata),
            },
        )
        return self.store.issue(snapshot)

    @staticmethod
    def _tool_descriptor(spec: Any) -> ParentToolDescriptor:
        raw = _object_mapping(spec)
        name = str(raw.get("name") or getattr(spec, "name", ""))
        annotations = _mapping(raw.get("annotations"))
        provenance = str(raw.get("source") or raw.get("provenance") or "zyra-tool-registry")
        mcp_server = str(raw.get("mcp_server_id") or raw.get("server_id") or "")
        if not mcp_server and name.casefold().startswith("mcp__"):
            parts = name.split("__", 2)
            mcp_server = parts[1] if len(parts) > 2 else ""
        return ParentToolDescriptor(
            name=name,
            provenance=provenance,
            read_only=bool(raw.get("read_only", annotations.get("readOnlyHint", False))),
            destructive=bool(raw.get("destructive", annotations.get("destructiveHint", False))),
            open_world=bool(raw.get("open_world", annotations.get("openWorldHint", True))),
            concurrency_safe=bool(raw.get("concurrency_safe", raw.get("is_concurrency_safe", False))),
            mcp_server_id=mcp_server,
            capability_digest=_digest(raw),
        )


class ChildScopeDeriver:
    _MODE_CAPABILITIES: dict[PermissionMode, frozenset[str]] = {
        PermissionMode.DENY: frozenset(),
        PermissionMode.PLAN: frozenset({"read", "search", "inspect", "plan"}),
        PermissionMode.DEFAULT: frozenset({"read", "search", "inspect", "ask", "write_with_grant", "network_with_grant", "execute_with_grant"}),
        PermissionMode.ACCEPT_EDITS: frozenset({"read", "search", "inspect", "ask", "write_with_grant", "edit_auto"}),
        PermissionMode.AUTO: frozenset({"read", "search", "inspect", "ask", "write_with_grant", "edit_auto", "sealed_auto"}),
        PermissionMode.BYPASS: frozenset({"read", "search", "inspect", "ask", "write_with_grant", "edit_auto", "sealed_auto", "bypass"}),
    }

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = bool(disabled)

    def derive(self, parent: ParentExecutionScopeSnapshot, request: ChildScopeRequest) -> ChildExecutionScope:
        if self.disabled:
            raise ParentScopeDisabled("ChildScopeDeriver is disabled")
        findings: list[ScopeFinding] = []
        self._check_revisions(parent, request, findings)
        parent_tools = parent.tool_map
        requested_names = request.requested_tools or parent.tool_names
        selected_tools: list[ParentToolDescriptor] = []
        for name in requested_names:
            canonical = name.casefold()
            descriptor = parent_tools.get(canonical)
            if descriptor is None:
                findings.append(ScopeFinding(
                    "child_tool_not_in_parent",
                    f"requested tool {name!r} is not in the canonical parent scope",
                    "requested_tools",
                    requested=name,
                    allowed=parent.tool_names,
                ))
                continue
            if canonical in parent.deny_tool_names:
                findings.append(ScopeFinding(
                    "child_tool_denied_by_parent",
                    f"requested tool {name!r} is denied by the parent policy",
                    "requested_tools",
                    requested=name,
                ))
                continue
            selected_tools.append(descriptor)
        requested_servers = request.requested_mcp_servers or parent.mcp_servers
        selected_servers = []
        for server in requested_servers:
            if server not in parent.mcp_servers:
                findings.append(ScopeFinding(
                    "child_mcp_not_in_parent",
                    f"requested MCP server {server!r} is not active in the parent scope",
                    "requested_mcp_servers",
                    requested=server,
                    allowed=parent.mcp_servers,
                ))
            else:
                selected_servers.append(server)
        filtered_tools = []
        for descriptor in selected_tools:
            if descriptor.mcp_server_id and descriptor.mcp_server_id not in selected_servers:
                continue
            filtered_tools.append(descriptor)
        selected_mode = request.requested_permission_mode or self._default_child_mode(parent.permission_mode)
        if not self._mode_narrows(parent.permission_mode, selected_mode):
            findings.append(ScopeFinding(
                "child_permission_expansion",
                f"requested permission mode {selected_mode.value} expands parent mode {parent.permission_mode.value}",
                "requested_permission_mode",
                requested=selected_mode.value,
                allowed=parent.permission_mode.value,
            ))
            selected_mode = PermissionMode.DENY
        selected_model = request.requested_model or parent.effective_model
        if parent.model_allowlist and selected_model not in parent.model_allowlist:
            findings.append(ScopeFinding(
                "child_model_not_allowed",
                f"requested model {selected_model!r} is outside the parent model allowlist",
                "requested_model",
                requested=selected_model,
                allowed=parent.model_allowlist,
            ))
        writable = self._narrow_paths(
            parent.workspace_root,
            request.requested_writable_paths or parent.writable_roots,
            parent.writable_roots,
            "requested_writable_paths",
            findings,
        )
        readable = self._narrow_paths(
            parent.workspace_root,
            request.requested_readable_paths or parent.readable_roots,
            parent.readable_roots,
            "requested_readable_paths",
            findings,
        )
        network_allowed = bool(request.requested_network and parent.network_allowed)
        if request.requested_network and not parent.network_allowed:
            findings.append(ScopeFinding(
                "child_network_expansion",
                "child requested network access not present in parent scope",
                "requested_network",
                requested=True,
                allowed=False,
            ))
        skills = self._subset(
            request.requested_skill_refs or parent.skill_refs,
            parent.skill_refs,
            "requested_skill_refs",
            "child_skill_not_in_parent",
            findings,
        )
        hooks = self._subset(
            request.requested_hook_refs or parent.hook_refs,
            parent.hook_refs,
            "requested_hook_refs",
            "child_hook_not_in_parent",
            findings,
        )
        if any(item.blocking for item in findings):
            raise ParentScopeViolation(findings)
        return ChildExecutionScope(
            parent_snapshot_id=parent.snapshot_id,
            parent_snapshot_digest=parent.unsigned_digest,
            tools=tuple(sorted(filtered_tools, key=lambda item: item.canonical_name)),
            permission_mode=selected_mode,
            permission_rule_ids=parent.permission_rule_ids,
            deny_rule_ids=parent.deny_rule_ids,
            deny_tool_names=parent.deny_tool_names,
            mcp_servers=tuple(selected_servers),
            mcp_catalog_generations={key: parent.mcp_catalog_generations[key] for key in selected_servers},
            model=selected_model,
            workspace_root=parent.workspace_root,
            writable_paths=writable,
            readable_paths=readable,
            network_allowed=network_allowed,
            skill_refs=skills,
            hook_refs=hooks,
            findings=tuple(findings),
        )

    def assert_projected_registry(self, scope: ChildExecutionScope, registry: ToolRegistry) -> None:
        actual = {str(item.name).casefold() for item in registry.list()}
        allowed = {item.canonical_name for item in scope.tools}
        extra = sorted(actual - allowed)
        missing = sorted(allowed - actual)
        findings = []
        if extra:
            findings.append(ScopeFinding(
                "child_registry_reexpanded",
                "MCP/tool projection added tools outside the derived child scope",
                "registry",
                requested=extra,
                allowed=sorted(allowed),
            ))
        if missing:
            findings.append(ScopeFinding(
                "child_registry_missing",
                "child registry dropped required scoped tools",
                "registry",
                requested=missing,
                allowed=sorted(allowed),
            ))
        if findings:
            raise ParentScopeViolation(findings)

    def _check_revisions(
        self,
        parent: ParentExecutionScopeSnapshot,
        request: ChildScopeRequest,
        findings: list[ScopeFinding],
    ) -> None:
        checks = (
            ("expected_session_revision", request.expected_session_revision, parent.session_revision),
            ("expected_permission_revision", request.expected_permission_revision, parent.permission_revision),
            ("expected_tool_generation", request.expected_tool_generation, parent.tool_generation),
        )
        for field_name, expected, actual in checks:
            if expected is not None and expected != actual:
                findings.append(ScopeFinding(
                    "child_scope_revision_stale",
                    f"{field_name} is stale: expected {expected}, actual {actual}",
                    field_name,
                    requested=expected,
                    allowed=actual,
                ))
        for server, expected in request.expected_mcp_generations.items():
            actual = parent.mcp_catalog_generations.get(server)
            if actual != expected:
                findings.append(ScopeFinding(
                    "child_mcp_generation_stale",
                    f"MCP generation for {server!r} is stale",
                    "expected_mcp_generations",
                    requested=expected,
                    allowed=actual,
                ))

    def _mode_narrows(self, parent: PermissionMode, child: PermissionMode) -> bool:
        parent_caps = self._MODE_CAPABILITIES.get(parent, frozenset())
        child_caps = self._MODE_CAPABILITIES.get(child, frozenset())
        return child_caps.issubset(parent_caps)

    @staticmethod
    def _default_child_mode(parent: PermissionMode) -> PermissionMode:
        if parent is PermissionMode.DENY:
            return parent
        if parent is PermissionMode.PLAN:
            return PermissionMode.PLAN
        return PermissionMode.DEFAULT

    @staticmethod
    def _narrow_paths(
        workspace_root: str,
        requested: Iterable[str],
        allowed: Iterable[str],
        field_name: str,
        findings: list[ScopeFinding],
    ) -> tuple[str, ...]:
        root = Path(workspace_root).resolve()
        allowed_paths = [Path(item).resolve() for item in allowed]
        selected: list[str] = []
        for value in requested:
            path = Path(value)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            within_root = _is_within(path, root)
            within_allowed = any(_is_within(path, parent) for parent in allowed_paths)
            if not within_root or not within_allowed:
                findings.append(ScopeFinding(
                    "child_workspace_expansion",
                    f"path {path} is outside the canonical parent scope",
                    field_name,
                    requested=str(path),
                    allowed=[str(item) for item in allowed_paths],
                ))
                continue
            selected.append(str(path))
        return _unique_strings(selected)

    @staticmethod
    def _subset(
        requested: Iterable[str],
        allowed: Iterable[str],
        field_name: str,
        code: str,
        findings: list[ScopeFinding],
    ) -> tuple[str, ...]:
        allowed_set = set(_unique_strings(allowed))
        selected = []
        for item in _unique_strings(requested):
            if item not in allowed_set:
                findings.append(ScopeFinding(
                    code,
                    f"{item!r} is not present in the canonical parent scope",
                    field_name,
                    requested=item,
                    allowed=sorted(allowed_set),
                ))
            else:
                selected.append(item)
        return tuple(selected)


def scoped_tool_registry(parent_registry: ToolRegistry, scope: ChildExecutionScope) -> ToolRegistry:
    allowed = {item.canonical_name for item in scope.tools}
    return ToolRegistry([item for item in parent_registry.list() if str(item.name).casefold() in allowed])


def permission_mode_for_code_worker(mode: PermissionMode) -> tuple[str, bool]:
    """Map logical child mode without turning DENY into CodeWorker default."""

    if mode is PermissionMode.DENY:
        return "sealed", True
    if mode is PermissionMode.PLAN:
        return "plan", True
    if mode is PermissionMode.ACCEPT_EDITS:
        return "acceptEdits", False
    return "default", False


def _canonical_paths(values: Iterable[str], root: str) -> tuple[str, ...]:
    base = Path(root).resolve()
    selected = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if not _is_within(path, base):
            raise ValueError(f"scope path escapes workspace root: {path}")
        selected.append(str(path))
    return _unique_strings(selected or (str(base),))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        selected = str(value).strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return tuple(result)


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            selected = method()
            if isinstance(selected, Mapping):
                return dict(selected)
    result = {}
    for name in ("name", "source", "annotations", "read_only", "destructive", "open_world", "concurrency_safe"):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return _object_mapping(value) or str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "ChildExecutionScope",
    "ChildScopeDeriver",
    "ChildScopeRequest",
    "ParentExecutionScopeSnapshot",
    "ParentScopeProjectionPort",
    "ParentScopeConflict",
    "ParentScopeDisabled",
    "ParentScopeError",
    "ParentScopeExpired",
    "ParentScopeStore",
    "ParentScopeViolation",
    "ParentToolDescriptor",
    "ScopeFinding",
    "ScopeFindingSeverity",
    "permission_mode_for_code_worker",
    "scoped_tool_registry",
]
