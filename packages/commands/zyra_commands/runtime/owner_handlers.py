from __future__ import annotations

"""Canonical state-owner handlers for RuntimeControlDispatcher."""

import copy
import hashlib
import json
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol, Sequence

from zyra_core import now_iso

from .dispatcher import RuntimeControlContext
from .schemas import ControlCommandDescriptor, ControlCommandRequest, ControlResult
from .session_control import SessionAction, SessionControlRuntime, SessionMutationRequest


class OwnerHandlerError(RuntimeError):
    pass


class OwnerUnavailable(OwnerHandlerError):
    pass


class OwnerAuthorizationDenied(OwnerHandlerError, PermissionError):
    pass


class OwnerRevisionConflict(OwnerHandlerError):
    pass


class OwnerMutationKind(StrEnum):
    SESSION = "session"
    MCP = "mcp"
    PERMISSION = "permission"
    MODEL = "model"
    PLUGIN = "plugin"
    SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class OwnerAuthorizationReceipt:
    authorization_id: str
    owner: str
    action: str
    run_id: str
    task_id: str
    session_id: str
    actor_id: str
    granted: bool
    exact: bool
    one_shot: bool
    authority_digest: str
    reason: str = ""
    expires_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.authorization_id or not self.owner or not self.action:
            raise ValueError("owner authorization identity is required")
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def require(self, *, owner: str, action: str, request: ControlCommandRequest) -> None:
        if not self.granted:
            raise OwnerAuthorizationDenied(self.reason or "owner authorization denied")
        if self.owner != owner or self.action != action:
            raise OwnerAuthorizationDenied("owner authorization scope mismatch")
        if (self.run_id, self.task_id, self.session_id) != (request.run_id, request.task_id, request.session_id):
            raise OwnerAuthorizationDenied("owner authorization identity mismatch")
        if not self.exact or not self.one_shot:
            raise OwnerAuthorizationDenied("mutating owner operation requires exact one-shot authorization")

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "owner": self.owner,
            "action": self.action,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "granted": self.granted,
            "exact": self.exact,
            "one_shot": self.one_shot,
            "authority_digest": self.authority_digest,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
        }


class ExactOwnerAuthorizer(Protocol):
    def __call__(
        self,
        owner: str,
        action: str,
        request: ControlCommandRequest,
        arguments: Mapping[str, Any],
    ) -> OwnerAuthorizationReceipt: ...


class OwnerAdapter(Protocol):
    def execute(self, action: str, arguments: Mapping[str, Any], request: ControlCommandRequest) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class CommandOwnerServices:
    session: SessionControlRuntime | None = None
    mcp: OwnerAdapter | Callable[..., Mapping[str, Any]] | None = None
    permission: OwnerAdapter | Callable[..., Mapping[str, Any]] | None = None
    model: OwnerAdapter | Callable[..., Mapping[str, Any]] | None = None
    plugin: OwnerAdapter | Callable[..., Mapping[str, Any]] | None = None
    subagent: OwnerAdapter | Callable[..., Mapping[str, Any]] | None = None
    authorizer: ExactOwnerAuthorizer | None = None
    registry_refresh: Callable[[], Mapping[str, Any]] | None = None


class CanonicalOwnerHandlerSet:
    def __init__(self, services: CommandOwnerServices, *, disabled: bool = False) -> None:
        self.services = services
        self.disabled = bool(disabled)

    def handlers(self) -> dict[str, Callable[[ControlCommandRequest, ControlCommandDescriptor, RuntimeControlContext], ControlResult]]:
        self._require_enabled()
        return {
            "session.clear": self.session_clear,
            "session.branch": self.session_branch,
            "session.rewind": self.session_rewind,
            "session.resume": self.session_resume,
            "mcp.control": self.mcp_control,
            "mcp.prompt": self.mcp_prompt,
            "permission.control": self.permission_control,
            "permission.plan": self.permission_plan,
            "provider.model": self.model_control,
            "skill_plugin.hooks": self.plugin_control,
            "skill_plugin.command": self.plugin_command,
            "skill.command": self.plugin_command,
            "subagent.control": self.subagent_control,
        }

    def session_clear(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        return self._session(request, SessionAction.CLEAR, {})

    def session_branch(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        return self._session(request, SessionAction.BRANCH, {
            "branch_name": str(parsed.get("name") or parsed.get("raw") or "").strip(),
        })

    def session_rewind(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        target = str(parsed.get("checkpoint_ref") or parsed.get("target") or parsed.get("raw") or "").strip()
        if not target:
            raise OwnerHandlerError("/rewind requires a canonical checkpoint ref")
        return self._session(request, SessionAction.REWIND, {"checkpoint_ref": target})

    def session_resume(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        target = str(parsed.get("target_session_id") or parsed.get("target") or parsed.get("raw") or "").strip()
        if not target:
            raise OwnerHandlerError("/resume requires a canonical session id")
        checkpoint_ref = str(parsed.get("checkpoint_ref") or "").strip()
        return self._session(
            request,
            SessionAction.RESUME,
            {
                "target_session_id": target,
                **({"checkpoint_ref": checkpoint_ref} if checkpoint_ref else {}),
            },
        )

    def mcp_control(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        action, target, arguments = self._mcp_action(parsed)
        mutation = action not in {"status", "health", "servers", "server", "catalog", "tools", "resources", "prompts", "tasks", "elicitations"}
        authorization = self._authorize("McpClientRuntime", action, request, arguments) if mutation else None
        result = self._call(self.services.mcp, action, {**arguments, "target": target, "authorization": authorization.to_dict() if authorization else {}} , request)
        return self._result(
            result,
            default_text=f"MCP {action} {target}".strip(),
            owner="McpClientRuntime",
            authorization=authorization,
        )

    def mcp_prompt(self, request: ControlCommandRequest, descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        server_id = str(descriptor.metadata.get("mcp_server_id") or "")
        prompt_name = str(descriptor.metadata.get("mcp_prompt_name") or descriptor.canonical_name)
        if not server_id:
            raise OwnerHandlerError("MCP prompt descriptor lacks canonical server identity")
        authorization = self._authorize("McpClientRuntime", "prompt_get", request, request.arguments)
        result = self._call(self.services.mcp, "prompt_get", {
            "target": server_id,
            "name": prompt_name,
            "arguments": self._arguments(request),
            "authorization": authorization.to_dict(),
        }, request)
        return self._result(result, default_text=f"MCP prompt {prompt_name}", owner="McpClientRuntime", authorization=authorization)

    def permission_control(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        argv = self._argv(parsed)
        action = str(parsed.get("action") or (argv[0] if argv else "inspect")).casefold()
        read_actions = {"inspect", "status", "list", "rules", "requests", "decisions", "mode"}
        authorization = None
        if action not in read_actions:
            authorization = self._authorize("PermissionStateStore", action, request, parsed)
        result = self._call(self.services.permission, action, {
            **parsed,
            "authorization": authorization.to_dict() if authorization else {},
        }, request)
        return self._result(result, default_text=f"Permission {action}", owner="PermissionStateStore", authorization=authorization)

    def permission_plan(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        authorization = self._authorize("PermissionStateStore", "set_mode:plan", request, request.arguments)
        result = self._call(self.services.permission, "set_mode", {
            "mode": "plan",
            "authorization": authorization.to_dict(),
        }, request)
        return self._result(result, default_text="Permission mode set to plan", owner="PermissionStateStore", authorization=authorization)

    def model_control(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        model = str(parsed.get("model") or parsed.get("raw") or "").strip()
        if not model or model.casefold() in {"show", "status", "list"}:
            result = self._call(self.services.model, "inspect", parsed, request)
            return self._result(result, default_text="Current model", owner="SessionModelSelection", authorization=None)
        authorization = self._authorize("SessionModelSelection", "set_model", request, {"model": model})
        result = self._call(self.services.model, "set_model", {
            **parsed,
            "model": model,
            "authorization": authorization.to_dict(),
        }, request)
        # A model owner may delegate session revision mutation to the canonical
        # session runtime.  The adapter must return model_effective=true.
        if not bool(result.get("model_effective", False)):
            raise OwnerHandlerError("model owner did not prove the next worker request consumes the selection")
        return self._result(result, default_text=f"Model switched to {model}", owner="SessionModelSelection", authorization=authorization)

    def plugin_control(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        argv = self._argv(parsed)
        action = str(parsed.get("action") or (argv[0] if argv else "inspect")).casefold()
        read_actions = {"inspect", "status", "list", "hooks", "plugins"}
        authorization = None
        if action not in read_actions:
            authorization = self._authorize("SkillPluginRuntime", action, request, parsed)
        result = self._call(self.services.plugin, action, {
            **parsed,
            "authorization": authorization.to_dict() if authorization else {},
        }, request)
        if bool(result.get("changed")):
            if self.services.registry_refresh is None:
                raise OwnerUnavailable("plugin changed but command registry refresh is unavailable")
            result = {**result, "registry_refresh": _safe_mapping(self.services.registry_refresh())}
        return self._result(result, default_text=f"Plugin/hook {action}", owner="SkillPluginRuntime", authorization=authorization)

    def plugin_command(self, request: ControlCommandRequest, descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        authorization = self._authorize("SkillPluginRuntime", "invoke", request, request.arguments)
        result = self._call(self.services.plugin, "invoke", {
            "command": descriptor.canonical_name,
            "source_id": descriptor.source.source_id,
            "arguments": self._arguments(request),
            "authorization": authorization.to_dict(),
        }, request)
        return self._result(result, default_text=descriptor.canonical_name, owner="SkillPluginRuntime", authorization=authorization)

    def subagent_control(self, request: ControlCommandRequest, _descriptor: ControlCommandDescriptor, _context: RuntimeControlContext) -> ControlResult:
        parsed = self._arguments(request)
        action = str(parsed.get("action") or "status").casefold()
        target = str(parsed.get("task_id") or request.target_subagent_task_id or "")
        if not target:
            raise OwnerHandlerError("subagent control requires target task_id")
        authorization = None
        if action not in {"status", "inspect"}:
            authorization = self._authorize("SubagentTaskStore", action, request, parsed)
        result = self._call(self.services.subagent, action, {
            **parsed,
            "task_id": target,
            "authorization": authorization.to_dict() if authorization else {},
        }, request)
        return self._result(result, default_text=f"Subagent {action}", owner="SubagentTaskStore", authorization=authorization)

    def _session(self, request: ControlCommandRequest, action: SessionAction, arguments: Mapping[str, Any]) -> ControlResult:
        runtime = self.services.session
        if runtime is None:
            raise OwnerUnavailable("canonical session control runtime is unavailable")
        authorization = self._authorize("CanonicalSessionStore", action.value, request, arguments)
        receipt = runtime.execute(SessionMutationRequest(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key or f"session:{request.request_id}",
            action=action,
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            arguments={**_safe_mapping(request.arguments), **_safe_mapping(arguments)},
            expected_revision=request.expected_session_revision,
            actor_id=str(request.metadata.get("actor_id") or "control-command"),
            causation_id=request.command_id,
        ))
        effect = receipt.effect
        return ControlResult(
            display_text=f"Session {action.value} committed.",
            data={"transaction": receipt.to_dict(), "owner": "CanonicalSessionStore"},
            artifact_refs=tuple(effect.artifact_refs) if effect else (),
            metadata={
                "owner": "CanonicalSessionStore",
                "before_checkpoint": receipt.before.checkpoint_ref,
                "after_checkpoint": receipt.after.checkpoint_ref if receipt.after else "",
                "owner_revision": receipt.after.revision if receipt.after else receipt.before.revision,
                "authorization": authorization.to_dict(),
            },
        )

    def _authorize(
        self,
        owner: str,
        action: str,
        request: ControlCommandRequest,
        arguments: Mapping[str, Any],
    ) -> OwnerAuthorizationReceipt:
        if self.services.authorizer is None:
            raise OwnerAuthorizationDenied(f"exact authorizer unavailable for {owner}:{action}")
        receipt = self.services.authorizer(owner, action, request, arguments)
        if not isinstance(receipt, OwnerAuthorizationReceipt):
            raise OwnerAuthorizationDenied("owner authorizer returned invalid receipt")
        receipt.require(owner=owner, action=action, request=request)
        return receipt

    @staticmethod
    def _call(
        owner: OwnerAdapter | Callable[..., Mapping[str, Any]] | None,
        action: str,
        arguments: Mapping[str, Any],
        request: ControlCommandRequest,
    ) -> Mapping[str, Any]:
        if owner is None:
            raise OwnerUnavailable(f"canonical owner adapter unavailable for action {action}")
        method = getattr(owner, "execute", None)
        if callable(method):
            value = method(action, arguments, request)
        elif callable(owner):
            value = owner(action=action, arguments=arguments, request=request)
        else:
            raise OwnerUnavailable(f"canonical owner adapter is not executable for action {action}")
        if not isinstance(value, Mapping):
            raise OwnerHandlerError("canonical owner returned a non-mapping result")
        return _safe_mapping(value)

    @staticmethod
    def _result(
        value: Mapping[str, Any],
        *,
        default_text: str,
        owner: str,
        authorization: OwnerAuthorizationReceipt | None,
    ) -> ControlResult:
        ok = bool(value.get("ok", True))
        if not ok:
            raise OwnerHandlerError(str(value.get("error") or value.get("summary") or f"{owner} action failed"))
        artifacts = tuple(str(item) for item in value.get("artifact_refs") or ())
        return ControlResult(
            display_text=str(value.get("summary") or default_text),
            data={**_safe_mapping(value), "state_owner": owner},
            artifact_refs=artifacts,
            usage=_safe_mapping(value.get("usage")),
            metadata={
                "state_owner": owner,
                "owner_revision": value.get("revision"),
                "owner_receipt": _safe_mapping(value.get("receipt")),
                "authorization": authorization.to_dict() if authorization else {},
                "event_only_fallback": False,
            },
        )

    @staticmethod
    def _arguments(request: ControlCommandRequest) -> dict[str, Any]:
        return _safe_mapping(request.arguments)

    @staticmethod
    def _argv(arguments: Mapping[str, Any]) -> list[str]:
        raw_argv = arguments.get("argv")
        if isinstance(raw_argv, Sequence) and not isinstance(raw_argv, (str, bytes)):
            return [str(item) for item in raw_argv]
        try:
            return shlex.split(str(arguments.get("raw") or ""))
        except ValueError as error:
            raise OwnerHandlerError(f"invalid command arguments: {error}") from error

    @classmethod
    def _mcp_action(cls, arguments: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
        argv = cls._argv(arguments)
        action = str(arguments.get("action") or (argv[0] if argv else "status")).casefold().replace("-", "_")
        aliases = {"list": "servers", "toggle_on": "enable", "toggle_off": "disable"}
        action = aliases.get(action, action)
        target = str(arguments.get("target") or arguments.get("server_id") or (argv[1] if len(argv) > 1 else ""))
        payload = {key: value for key, value in arguments.items() if key not in {"raw", "argv", "action", "target", "server_id"}}
        return action, target, payload

    def _require_enabled(self) -> None:
        if self.disabled:
            raise OwnerUnavailable("CanonicalOwnerHandlerSet is disabled")


def deterministic_owner_authorization(
    *,
    owner: str,
    action: str,
    request: ControlCommandRequest,
    actor_id: str,
    authority: Mapping[str, Any],
    granted: bool,
    reason: str = "",
) -> OwnerAuthorizationReceipt:
    return OwnerAuthorizationReceipt(
        authorization_id=f"ownerauth:{request.request_id}:{owner}:{action}",
        owner=owner,
        action=action,
        run_id=request.run_id,
        task_id=request.task_id,
        session_id=request.session_id,
        actor_id=actor_id,
        granted=granted,
        exact=True,
        one_shot=True,
        authority_digest=_digest(_safe_mapping(authority)),
        reason=reason,
        metadata={"permission_owner": "PermissionStateStore", "reusable": False},
    )


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


__all__ = [
    "CanonicalOwnerHandlerSet",
    "CommandOwnerServices",
    "ExactOwnerAuthorizer",
    "OwnerAdapter",
    "OwnerAuthorizationDenied",
    "OwnerAuthorizationReceipt",
    "OwnerHandlerError",
    "OwnerMutationKind",
    "OwnerRevisionConflict",
    "OwnerUnavailable",
    "deterministic_owner_authorization",
]
