from __future__ import annotations

"""Assemble a real child CodeWorker session from immutable 03D contracts."""

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_core import AgentMessage, AgentRole, MessageIntent
from zyra_runtime import ToolRegistry, WorkerRequest

from .models import AgentContextMode, SubagentDispatchRequest
from .parent_scope import ChildExecutionScope, ChildScopeDeriver, permission_mode_for_code_worker


class SessionAssemblyError(RuntimeError):
    pass


class SessionAssemblyDisabled(SessionAssemblyError):
    pass


class SessionAssemblyViolation(SessionAssemblyError):
    pass


class ChildAttachmentKind(StrEnum):
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"
    MEMORY = "memory"
    SKILL = "skill"
    CONTEXT_REPLACEMENT = "content_replacement"


@dataclass(frozen=True, slots=True)
class ChildAttachment:
    kind: ChildAttachmentKind
    ref: str
    title: str = ""
    digest: str = ""
    media_type: str = ""
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("child attachment ref is required")
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "ref": self.ref,
            "title": self.title,
            "digest": self.digest,
            "media_type": self.media_type,
            "summary": self.summary,
            "metadata": _safe_mapping(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ChildSessionEnvelope:
    child_session_id: str
    root_task_id: str
    logical_task_id: str
    parent_task_id: str
    context_mode: AgentContextMode
    system_prompt: str
    messages: tuple[AgentMessage, ...]
    attachments: tuple[ChildAttachment, ...]
    constraints: Mapping[str, Any]
    metadata: Mapping[str, Any]
    model_name: str
    effort: str
    tool_names: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    skill_refs: tuple[str, ...]
    hook_refs: tuple[str, ...]

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_session_id": self.child_session_id,
            "root_task_id": self.root_task_id,
            "logical_task_id": self.logical_task_id,
            "parent_task_id": self.parent_task_id,
            "context_mode": self.context_mode.value,
            "system_prompt_digest": _digest_text(self.system_prompt),
            "messages": [
                {
                    "sender_role": item.sender_role.value,
                    "receiver_role": item.receiver_role.value,
                    "intent": item.intent.value,
                    "content": item.content,
                    "summary": item.summary,
                    "artifact_refs": [getattr(ref, "artifact_id", str(ref)) for ref in item.artifact_refs],
                    "metadata": _safe_mapping(item.metadata),
                }
                for item in self.messages
            ],
            "attachments": [item.to_dict() for item in self.attachments],
            "constraints": _safe_mapping(self.constraints),
            "metadata": _safe_mapping(self.metadata),
            "model_name": self.model_name,
            "effort": self.effort,
            "tool_names": list(self.tool_names),
            "mcp_servers": list(self.mcp_servers),
            "skill_refs": list(self.skill_refs),
            "hook_refs": list(self.hook_refs),
        }


@dataclass(frozen=True, slots=True)
class SessionAssemblyResult:
    envelope: ChildSessionEnvelope
    worker_request: WorkerRequest
    registry: ToolRegistry
    runtime_services: Mapping[str, Any]


class ChildWorkerSessionAssembler:
    """Translate SubagentDispatchRequest into the actual CodeWorker inputs."""

    def __init__(
        self,
        *,
        scope_deriver: ChildScopeDeriver | None = None,
        attachment_resolver: Callable[[str, ChildAttachmentKind], Mapping[str, Any] | None] | None = None,
        skill_validator: Callable[[str], bool] | None = None,
        hook_validator: Callable[[str], bool] | None = None,
        maximum_messages: int = 256,
        maximum_message_chars: int = 512_000,
        disabled: bool = False,
    ) -> None:
        self.scope_deriver = scope_deriver or ChildScopeDeriver()
        self.attachment_resolver = attachment_resolver
        self.skill_validator = skill_validator
        self.hook_validator = hook_validator
        self.maximum_messages = max(8, int(maximum_messages))
        self.maximum_message_chars = max(16_000, int(maximum_message_chars))
        self.disabled = bool(disabled)

    def assemble(
        self,
        request: SubagentDispatchRequest,
        *,
        registry: ToolRegistry,
        child_scope: ChildExecutionScope | None = None,
        cancel_check: Callable[[], bool] | None = None,
        continuation_drain: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        typed_yield_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        skill_fork_port: Any | None = None,
    ) -> SessionAssemblyResult:
        if self.disabled:
            raise SessionAssemblyDisabled("ChildWorkerSessionAssembler is disabled")
        root_task_id = str(request.metadata.get("root_task_id") or request.parent_task_id)
        child_session_id = str(request.metadata.get("child_session_id") or f"subagent:{request.task_id}")
        if not child_session_id or child_session_id == request.parent_task_id:
            raise SessionAssemblyViolation("child session identity is invalid")
        context = request.context
        attachments = self._attachments(context)
        system_prompt = self._system_prompt(request)
        messages = self._messages(request, root_task_id, attachments)
        model_name = str(request.constraints.get("model_name") or request.constraints.get("model") or "zyra-local-code-model")
        effort = str(request.constraints.get("effort") or "default")
        permission_mode, permission_headless = permission_mode_for_code_worker(request.permission.child_mode)
        mcp_servers = tuple(request.permission.mcp_servers)
        skills = tuple(str(item) for item in request.constraints.get("agent_skills") or context.invoked_skill_refs or ())
        hooks = tuple(str(item) for item in request.constraints.get("agent_hooks") or ())
        self._validate_refs(skills, hooks)
        tool_names = tuple(item.name for item in registry.list())
        if tuple(tool_names) != tuple(request.tool_scope.child_tools):
            raise SessionAssemblyViolation("assembler registry differs from derived child tool scope")
        if child_scope is not None:
            self.scope_deriver.assert_projected_registry(child_scope, registry)
            if model_name != child_scope.model:
                raise SessionAssemblyViolation("child model differs from signed child scope")
            if set(mcp_servers) - set(child_scope.mcp_servers):
                raise SessionAssemblyViolation("child MCP projection expands signed child scope")
        yield_schema = request.constraints.get("typed_yield_schema")
        constraints = {
            **copy.deepcopy(request.constraints),
            "session_id": child_session_id,
            "skill_session_id": child_session_id,
            "subagent_task_id": request.task_id,
            "parent_task_id": request.parent_task_id,
            "subagent_depth": context.depth,
            "subagent_context_mode": context.mode.value,
            "max_turns": request.budget.max_turns,
            "max_tool_calls": request.budget.max_tool_calls,
            "model_input_token_limit": request.budget.max_input_tokens,
            "model_output_token_limit": request.budget.max_output_tokens,
            "turn_tool_result_budget_chars": min(
                request.budget.max_result_chars,
                int(request.constraints.get("turn_tool_result_budget_chars") or request.budget.max_result_chars),
            ),
            "permission_mode": permission_mode,
            "permission_headless": permission_headless,
            "permission_interactive": not permission_headless,
            "parent_permission_rule_ids": list(request.permission.inherited_rule_ids),
            "parent_permission_denials": list(request.permission.inherited_denials),
            "exact_grants_inherited": request.permission.exact_grants_inherited,
            "allowed_mcp_servers": list(mcp_servers),
            "mcp_include_servers": list(mcp_servers),
            "mcp_network_allowed": bool(
                (child_scope.network_allowed if child_scope is not None else request.isolation.metadata.get("network_allowed", False))
                and mcp_servers
            ),
            "child_tool_scope": request.tool_scope.to_dict(),
            "expected_child_tool_names": list(tool_names),
            "model_name": model_name,
            "effort": effort,
            "agent_system_prompt": system_prompt,
            "agent_skills": list(skills),
            "agent_hooks": list(hooks),
            "agent_memory_scope": str(request.constraints.get("agent_memory_scope") or "none"),
            "invoked_skill_refs": list(skills),
            "content_replacement_refs": list(context.content_replacement_refs),
            "context_epoch": context.context_epoch,
            "compact_boundary_id": context.compact_boundary_id,
            "fork_cache_prefix_digest": str(context.metadata.get("fork_cache_prefix_digest") or ""),
            "disable_skill_tool_projection": not bool(
                {"skill", "list_skills", "read_skill_resource"}.intersection(tool_names)
            ),
            "typed_yield_required": bool(yield_schema or request.constraints.get("typed_yield_required", False)),
            "typed_yield_schema": copy.deepcopy(yield_schema) if isinstance(yield_schema, Mapping) else {},
            "workspace_root": child_scope.workspace_root if child_scope is not None else request.isolation.workspace_root,
            "effective_cwd": request.isolation.effective_cwd,
            "writable_paths": list(child_scope.writable_paths if child_scope is not None else (request.isolation.workspace_root,)),
            "read_only_paths": list(child_scope.readable_paths if child_scope is not None else (request.isolation.workspace_root,)),
            "network_allowed": bool(
                child_scope.network_allowed if child_scope is not None else request.isolation.metadata.get("network_allowed", False)
            ),
        }
        metadata = {
            "logical_subagent_task_id": request.task_id,
            "parent_task_id": request.parent_task_id,
            "dispatch_id": request.dispatch_id,
            "agent_definition_id": request.agent_definition_id,
            "context_snapshot_id": context.snapshot_id,
            "tool_scope_digest": request.tool_scope.digest,
            "permission_digest": request.permission.digest,
            "attachment_refs": [item.ref for item in attachments],
            "attachment_digest": _digest([item.to_dict() for item in attachments]),
            "raw_parent_transcript_injected": False,
            "child_session_assembler": "M1-S03D-02",
            "child_scope_digest": child_scope.digest if child_scope else "",
        }
        runtime_services = {
            "cancel_check": cancel_check or (lambda: False),
            "continuation_drain": continuation_drain or (lambda: ()),
            "typed_yield_sink": typed_yield_sink,
            "child_scope_registry_assert": (
                (lambda projected: self.scope_deriver.assert_projected_registry(child_scope, projected))
                if child_scope is not None else self._assert_expected_registry(tool_names)
            ),
            "expected_child_tool_names": tool_names,
            "child_scope": child_scope.to_dict() if child_scope else {},
            "skill_fork_port": skill_fork_port,
        }
        envelope = ChildSessionEnvelope(
            child_session_id=child_session_id,
            root_task_id=root_task_id,
            logical_task_id=request.task_id,
            parent_task_id=request.parent_task_id,
            context_mode=context.mode,
            system_prompt=system_prompt,
            messages=messages,
            attachments=attachments,
            constraints=constraints,
            metadata=metadata,
            model_name=model_name,
            effort=effort,
            tool_names=tool_names,
            mcp_servers=mcp_servers,
            skill_refs=skills,
            hook_refs=hooks,
        )
        worker_request = WorkerRequest(
            run_id=request.run_id,
            task_id=root_task_id,
            worker_name=request.worker_name,
            messages=list(messages),
            node_id=str(request.metadata.get("node_id") or "") or None,
            constraints=constraints,
            metadata={**metadata, "child_session_envelope_digest": envelope.digest},
        )
        return SessionAssemblyResult(
            envelope=envelope,
            worker_request=worker_request,
            registry=registry,
            runtime_services=runtime_services,
        )

    def _messages(
        self,
        request: SubagentDispatchRequest,
        root_task_id: str,
        attachments: Sequence[ChildAttachment],
    ) -> tuple[AgentMessage, ...]:
        context = request.context
        values: list[AgentMessage] = []
        if context.mode is AgentContextMode.FORK:
            forked = context.metadata.get("forked_messages")
            if isinstance(forked, Sequence) and not isinstance(forked, (str, bytes)):
                for item in forked:
                    if not isinstance(item, Mapping):
                        continue
                    content = str(item.get("content") or item.get("summary") or "")
                    if not content:
                        continue
                    values.append(self._message(
                        request,
                        root_task_id,
                        content=content,
                        summary="Cache-safe forked parent prefix",
                        metadata={
                            "forked": True,
                            "source_message_id": str(item.get("message_id") or ""),
                            "placeholder_tool_results": bool(item.get("placeholder_tool_results", False)),
                        },
                    ))
        elif context.mode is AgentContextMode.RESUME:
            resumed = context.metadata.get("resume_messages")
            if isinstance(resumed, Sequence) and not isinstance(resumed, (str, bytes)):
                for item in resumed:
                    if isinstance(item, Mapping) and str(item.get("content") or ""):
                        values.append(self._message(
                            request,
                            root_task_id,
                            content=str(item.get("content")),
                            summary="Bounded resume capsule message",
                            metadata={"resumed": True, **_safe_mapping(item.get("metadata"))},
                        ))
        attachment_summary = "\n".join(
            f"- {item.kind.value}: {item.ref} ({item.summary or item.title or 'reference'})"
            for item in attachments
        )
        directive = request.prompt
        if attachment_summary:
            directive += "\n\nBounded references available to this child:\n" + attachment_summary
        values.append(self._message(
            request,
            root_task_id,
            content=directive,
            summary="Bounded subagent dispatch",
            metadata={
                "directive": True,
                "context_mode": context.mode.value,
                "context_snapshot_id": context.snapshot_id,
            },
        ))
        if len(values) > self.maximum_messages:
            raise SessionAssemblyViolation("child message count exceeds assembler limit")
        if sum(len(item.content) for item in values) > self.maximum_message_chars:
            raise SessionAssemblyViolation("child message content exceeds assembler budget")
        return tuple(values)

    @staticmethod
    def _message(
        request: SubagentDispatchRequest,
        root_task_id: str,
        *,
        content: str,
        summary: str,
        metadata: Mapping[str, Any],
    ) -> AgentMessage:
        return AgentMessage(
            run_id=request.run_id,
            task_id=root_task_id,
            sender_role=AgentRole.PLANNER,
            receiver_role=AgentRole.WORKER,
            intent=MessageIntent.REQUEST,
            content=content,
            summary=summary,
            artifact_refs=[],
            metadata={
                "subagent_task_id": request.task_id,
                "parent_task_id": request.parent_task_id,
                **_safe_mapping(metadata),
            },
        )

    def _attachments(self, context: Any) -> tuple[ChildAttachment, ...]:
        result = []
        groups = (
            (ChildAttachmentKind.ARTIFACT, tuple(getattr(context, "artifact_refs", ()) or ())),
            (ChildAttachmentKind.EVIDENCE, tuple(getattr(context, "evidence_refs", ()) or ())),
            (ChildAttachmentKind.SKILL, tuple(getattr(context, "invoked_skill_refs", ()) or ())),
            (ChildAttachmentKind.CONTEXT_REPLACEMENT, tuple(getattr(context, "content_replacement_refs", ()) or ())),
        )
        for kind, refs in groups:
            for ref in refs:
                selected = self._resolve_attachment(str(ref), kind)
                result.append(selected)
        deduped = {}
        for item in result:
            deduped[(item.kind.value, item.ref)] = item
        return tuple(deduped[key] for key in sorted(deduped))

    def _resolve_attachment(self, ref: str, kind: ChildAttachmentKind) -> ChildAttachment:
        if not ref.strip():
            raise SessionAssemblyViolation("empty attachment ref")
        raw = self.attachment_resolver(ref, kind) if self.attachment_resolver else None
        raw = raw if isinstance(raw, Mapping) else {}
        resolved_ref = str(raw.get("ref") or raw.get("artifact_id") or ref)
        if resolved_ref != ref and not str(raw.get("canonical_ref") or "") == ref:
            raise SessionAssemblyViolation("attachment resolver changed ref identity without canonical binding")
        return ChildAttachment(
            kind=kind,
            ref=ref,
            title=str(raw.get("title") or ""),
            digest=str(raw.get("digest") or ""),
            media_type=str(raw.get("media_type") or raw.get("mime_type") or ""),
            summary=str(raw.get("summary") or "")[:2048],
            metadata={"resolved": bool(raw), "body_injected": False},
        )

    def _system_prompt(self, request: SubagentDispatchRequest) -> str:
        context = request.context
        definition_prompt = str(request.constraints.get("agent_system_prompt") or "").strip()
        rendered_parent = str(context.metadata.get("rendered_system_prompt") or "")
        if context.mode is AgentContextMode.FORK and rendered_parent:
            base = rendered_parent
            source = "parent_cache_safe_prefix"
        else:
            base = definition_prompt or "You are a bounded Zyra child worker."
            source = "agent_definition"
        policy = [
            "Return results only through the supplied typed-yield contract.",
            "Do not broaden tools, permission, MCP, workspace, model, skill or hook scope.",
            "Do not message siblings or broadcast free-form conversation.",
            "Large content must remain in artifacts and be referenced by id.",
        ]
        if request.permission.child_mode.value == "deny":
            policy.append("This child is sealed: unknown or side-effecting actions are deterministically denied.")
        return base.rstrip() + "\n\n# Zyra child execution boundary\n" + "\n".join(f"- {item}" for item in policy) + f"\n- system prompt source: {source}"

    def _validate_refs(self, skills: Iterable[str], hooks: Iterable[str]) -> None:
        if self.skill_validator is not None:
            invalid = [item for item in skills if not self.skill_validator(item)]
            if invalid:
                raise SessionAssemblyViolation("unknown or disabled child skills: " + ", ".join(invalid))
        if self.hook_validator is not None:
            invalid = [item for item in hooks if not self.hook_validator(item)]
            if invalid:
                raise SessionAssemblyViolation("unknown or disabled child hooks: " + ", ".join(invalid))

    @staticmethod
    def _assert_expected_registry(expected: Sequence[str]) -> Callable[[ToolRegistry], None]:
        expected_names = tuple(expected)

        def check(registry: ToolRegistry) -> None:
            actual = tuple(item.name for item in registry.list())
            if set(actual) - set(expected_names):
                raise SessionAssemblyViolation(
                    "CodeWorker registry expanded after child assembly: " + ", ".join(sorted(set(actual) - set(expected_names)))
                )

        return check


def extract_explicit_typed_yield(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = metadata.get("typed_yield")
    if not isinstance(value, Mapping):
        return None
    required = {"kind", "data", "sequence"}
    if not required.issubset(value):
        raise SessionAssemblyViolation("typed_yield metadata is incomplete")
    return copy.deepcopy(dict(value))


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
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
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


__all__ = [
    "ChildAttachment",
    "ChildAttachmentKind",
    "ChildSessionEnvelope",
    "ChildWorkerSessionAssembler",
    "SessionAssemblyDisabled",
    "SessionAssemblyError",
    "SessionAssemblyResult",
    "SessionAssemblyViolation",
    "extract_explicit_typed_yield",
]
