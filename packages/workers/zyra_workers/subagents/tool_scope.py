from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from zyra_runtime import ToolExecutionContext, ToolRegistry, ToolSpec

from .digests import digest_object
from .errors import PermissionExpansionDenied, ToolScopeViolation
from .models import AgentDefinition, PermissionDerivation, PermissionMode, ToolScope


MODE_CAPABILITIES: dict[PermissionMode, frozenset[str]] = {
    PermissionMode.DENY: frozenset(),
    PermissionMode.PLAN: frozenset({"read", "inspect", "plan"}),
    PermissionMode.DEFAULT: frozenset({"read", "inspect", "ask", "write_with_grant", "external_with_grant"}),
    PermissionMode.ACCEPT_EDITS: frozenset({"read", "inspect", "ask", "write_with_grant", "external_with_grant", "edit_auto"}),
    PermissionMode.AUTO: frozenset({"read", "inspect", "ask", "write_with_grant", "external_with_grant", "edit_auto", "sealed_auto"}),
    PermissionMode.BYPASS: frozenset({"read", "inspect", "ask", "write_with_grant", "external_with_grant", "edit_auto", "sealed_auto", "bypass"}),
}

GLOBAL_SUBAGENT_DENY = frozenset(
    {
        "agent",
        "spawn_agent",
        "team_create",
        "team_delete",
        "send_message",
        "task_stop",
    }
)


@dataclass(frozen=True, slots=True)
class ToolScopeFinding:
    code: str
    message: str
    blocking: bool
    tool_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "tool_name": self.tool_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolScopeResolution:
    scope: ToolScope
    registry: ToolRegistry
    findings: tuple[ToolScopeFinding, ...]
    parent_generation: str

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scope": self.scope.to_dict(),
            "findings": [item.to_dict() for item in self.findings],
            "parent_generation": self.parent_generation,
        }


class ChildToolScopeRuntime:
    """Build the executable child registry as a strict intersection.

    Unlike the upstream AgentTool path, MCP tools are not unconditionally
    retained and memory helpers never inject new file tools. The exact same
    immutable registry is handed to the model-visible query runtime and the
    final ``ToolExecutor`` boundary.
    """

    def __init__(self, *, globally_denied: Iterable[str] = GLOBAL_SUBAGENT_DENY, disabled: bool = False) -> None:
        self.globally_denied = frozenset(str(item) for item in globally_denied)
        self.disabled = disabled

    def resolve(
        self,
        parent_registry: ToolRegistry,
        definition: AgentDefinition,
        *,
        requested_tools: Sequence[str] = (),
        required_tools: Sequence[str] = (),
        allowed_mcp_servers: Sequence[str] = (),
        maximum_tool_count: int | None = None,
    ) -> ToolScopeResolution:
        if self.disabled:
            raise ToolScopeViolation("ChildToolScopeRuntime is disabled")
        parent_specs = parent_registry.list()
        parent_by_name = {item.name: item for item in parent_specs}
        parent_names = tuple(parent_by_name)
        allowed_patterns = tuple(requested_tools or definition.tools or parent_names)
        denied_patterns = tuple(definition.disallowed_tools) + tuple(self.globally_denied)
        allowed_mcp = set(allowed_mcp_servers)
        findings: list[ToolScopeFinding] = []
        selected: list[ToolSpec] = []

        for spec in parent_specs:
            if not _matches_any(spec.name, allowed_patterns):
                continue
            if _matches_any(spec.name, denied_patterns):
                findings.append(ToolScopeFinding(
                    code="tool_explicitly_denied",
                    message=f"tool {spec.name} was removed by child deny policy",
                    blocking=False,
                    tool_name=spec.name,
                ))
                continue
            server_id = _mcp_server_id(spec)
            if server_id and server_id not in allowed_mcp:
                findings.append(ToolScopeFinding(
                    code="mcp_server_not_in_child_scope",
                    message=f"MCP tool {spec.name} belongs to a server outside child scope",
                    blocking=False,
                    tool_name=spec.name,
                    metadata={"server_id": server_id},
                ))
                continue
            selected.append(spec)

        selected_names = tuple(item.name for item in selected)
        missing_required = sorted(set(required_tools) - set(selected_names))
        for name in missing_required:
            findings.append(ToolScopeFinding(
                code="required_tool_missing",
                message=f"required child tool {name} is unavailable after scope derivation",
                blocking=True,
                tool_name=name,
            ))
        unknown_requested = sorted(
            name
            for name in requested_tools
            if not _has_pattern_match(name, parent_names)
        )
        for name in unknown_requested:
            findings.append(ToolScopeFinding(
                code="requested_tool_not_in_parent",
                message=f"requested child tool {name} is not present in the parent registry",
                blocking=True,
                tool_name=name,
            ))
        if maximum_tool_count is not None and len(selected) > maximum_tool_count:
            findings.append(ToolScopeFinding(
                code="child_tool_count_exceeded",
                message=f"child tool pool has {len(selected)} tools, limit is {maximum_tool_count}",
                blocking=True,
            ))
        if not set(selected_names).issubset(parent_by_name):
            raise ToolScopeViolation("child tool registry expanded beyond the parent registry")

        dynamic = {
            item.name: digest_object({
                "name": item.name,
                "source": item.source,
                "provenance": {
                    "namespace": getattr(item.execution_provenance, "namespace", ""),
                    "server_id": getattr(item.execution_provenance, "server_id", ""),
                    "version": getattr(item.execution_provenance, "version", ""),
                },
            })
            for item in selected
            if item.execution_provenance is not None
        }
        scope = ToolScope(
            parent_tools=parent_names,
            child_tools=selected_names,
            denied_tools=tuple(sorted(set(denied_patterns))),
            required_tools=tuple(required_tools),
            dynamic_tool_identities=dynamic,
        )
        return ToolScopeResolution(
            scope=scope,
            registry=ToolRegistry(selected),
            findings=tuple(findings),
            parent_generation=digest_object([
                {"name": item.name, "source": item.source, "metadata": dict(item.metadata)}
                for item in parent_specs
            ]),
        )

    def restricted_context(
        self,
        parent_context: ToolExecutionContext,
        resolution: ToolScopeResolution,
    ) -> ToolExecutionContext:
        if not resolution.ok:
            blockers = ", ".join(item.code for item in resolution.findings if item.blocking)
            raise ToolScopeViolation("child tool scope has blocking findings", blockers=blockers)
        dynamic_handlers = {
            name: handler
            for name, handler in parent_context.dynamic_handlers.items()
            if name in resolution.scope.child_tools
        }
        return ToolExecutionContext(
            workspace_root=parent_context.workspace_root,
            artifact_store=parent_context.artifact_store,
            permission_policy=parent_context.permission_policy,
            registry=resolution.registry,
            permission_store=parent_context.permission_store,
            event_reader=parent_context.event_reader,
            checkpoint_reader=parent_context.checkpoint_reader,
            max_inline_chars=parent_context.max_inline_chars,
            shell_timeout_seconds=parent_context.shell_timeout_seconds,
            dynamic_handlers=dynamic_handlers,
            runtime_services={**dict(parent_context.runtime_services), "subagent_tool_scope": resolution.scope},
        )


class PermissionDerivationRuntime:
    """Derive a child permission ceiling without copying execution grants."""

    def __init__(self, *, allow_auto: bool = False, allow_bypass: bool = False, disabled: bool = False) -> None:
        self.allow_auto = allow_auto
        self.allow_bypass = allow_bypass
        self.disabled = disabled

    def derive(
        self,
        *,
        parent_mode: PermissionMode,
        requested_mode: PermissionMode | None,
        definition_mode: PermissionMode,
        parent_rule_ids: Sequence[str] = (),
        parent_denials: Sequence[str] = (),
        available_mcp_servers: Sequence[str] = (),
        requested_mcp_servers: Sequence[str] = (),
        definition_mcp_servers: Sequence[str] = (),
    ) -> PermissionDerivation:
        if self.disabled:
            raise PermissionExpansionDenied("PermissionDerivationRuntime is disabled")
        target = requested_mode or definition_mode
        parent_caps = MODE_CAPABILITIES[parent_mode]
        child_caps = MODE_CAPABILITIES[target]
        if not child_caps.issubset(parent_caps):
            raise PermissionExpansionDenied(
                "child permission mode would expand the parent capability set",
                parent_mode=parent_mode.value,
                child_mode=target.value,
                extra=sorted(child_caps - parent_caps),
            )
        if target == PermissionMode.AUTO and not self.allow_auto:
            raise PermissionExpansionDenied("child auto permission mode is disabled")
        if target == PermissionMode.BYPASS and not self.allow_bypass:
            raise PermissionExpansionDenied("child bypass permission mode is disabled")
        available_mcp = set(available_mcp_servers)
        requested = set(requested_mcp_servers or definition_mcp_servers)
        if not requested.issubset(available_mcp):
            raise PermissionExpansionDenied(
                "child requested MCP servers outside the parent scope",
                requested=sorted(requested),
                available=sorted(available_mcp),
            )
        return PermissionDerivation(
            parent_mode=parent_mode,
            child_mode=target,
            inherited_rule_ids=tuple(parent_rule_ids),
            inherited_denials=tuple(parent_denials),
            exact_grants_inherited=False,
            mcp_servers=tuple(sorted(requested)),
            monotonic=True,
        )

    def assert_fresh(
        self,
        derivation: PermissionDerivation,
        *,
        current_parent_mode: PermissionMode,
        current_denials: Sequence[str],
        current_mcp_servers: Sequence[str],
    ) -> None:
        if not MODE_CAPABILITIES[derivation.child_mode].issubset(MODE_CAPABILITIES[current_parent_mode]):
            raise PermissionExpansionDenied(
                "parent permission mode narrowed after child derivation",
                parent_mode=current_parent_mode.value,
                child_mode=derivation.child_mode.value,
            )
        missing_denials = set(current_denials) - set(derivation.inherited_denials)
        if missing_denials:
            raise PermissionExpansionDenied(
                "child permission snapshot is stale after new parent denials",
                missing_denials=sorted(missing_denials),
            )
        if not set(derivation.mcp_servers).issubset(current_mcp_servers):
            raise PermissionExpansionDenied(
                "child permission snapshot references an unavailable MCP server",
                unavailable=sorted(set(derivation.mcp_servers) - set(current_mcp_servers)),
            )


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(pattern in {"*", name} or fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _has_pattern_match(pattern: str, names: Sequence[str]) -> bool:
    if pattern == "*":
        return bool(names)
    return any(fnmatch.fnmatchcase(name, pattern) for name in names)


def _mcp_server_id(spec: ToolSpec) -> str:
    provenance = spec.execution_provenance
    if provenance is not None and getattr(provenance, "namespace", "") == "mcp":
        return str(getattr(provenance, "server_id", ""))
    metadata = dict(spec.metadata)
    if metadata.get("tool_namespace") == "mcp" or metadata.get("namespace") == "mcp":
        return str(metadata.get("server_id") or metadata.get("server_name") or "")
    return ""

