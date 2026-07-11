from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .admission import SkillCommandSafetyClassifier
from .errors import SkillPermissionDenied, SkillPermissionPending
from .models import SkillInvocationRequest, SkillRevision


class SkillInvocationPermissionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class SkillInvocationPermissionResult:
    effect: SkillInvocationPermissionEffect
    reason: str
    decision_id: str = ""
    request_id: str = ""
    events: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def require_allow(self) -> "SkillInvocationPermissionResult":
        if self.effect is SkillInvocationPermissionEffect.ASK:
            raise SkillPermissionPending(
                self.reason,
                detail={"decision_id": self.decision_id, "request_id": self.request_id},
            )
        if self.effect is SkillInvocationPermissionEffect.DENY:
            raise SkillPermissionDenied(
                self.reason,
                detail={"decision_id": self.decision_id, "request_id": self.request_id},
            )
        return self


class SkillInvocationPermissionPort(Protocol):
    def guard(
        self,
        request: SkillInvocationRequest,
        revision: SkillRevision,
    ) -> SkillInvocationPermissionResult: ...


class DenySkillInvocationPermission:
    def guard(self, request: SkillInvocationRequest, revision: SkillRevision) -> SkillInvocationPermissionResult:
        return SkillInvocationPermissionResult(
            effect=SkillInvocationPermissionEffect.DENY,
            reason="skill invocation permission port is not configured",
            metadata={"owner": "M1-03A", "fail_closed": True},
        )


class PreauthorizedBuiltinSkillPermission:
    """Narrow bootstrap policy for product-owned builtin prompt expansion.

    This does not authorize any downstream tool. It only allows loading a
    product-owned immutable builtin skill into context. Project/plugin/MCP
    skills remain deny/ask unless a 03A-backed port is supplied.
    """

    def guard(self, request: SkillInvocationRequest, revision: SkillRevision) -> SkillInvocationPermissionResult:
        admission = SkillCommandSafetyClassifier().classify(request, revision)
        if not admission.valid:
            return SkillInvocationPermissionResult(
                effect=SkillInvocationPermissionEffect.DENY,
                reason="builtin skill context expansion failed safe-property admission",
                metadata={"owner": "M1-03A", "admission": admission.to_dict()},
            )
        if not admission.bootstrap_allow:
            return SkillInvocationPermissionResult(
                effect=SkillInvocationPermissionEffect.DENY,
                reason="non-builtin skill invocation requires a 03A permission decision",
                metadata={
                    "owner": "M1-03A",
                    "source": str(revision.provenance.source_kind),
                    "admission": admission.to_dict(),
                },
            )
        return SkillInvocationPermissionResult(
            effect=SkillInvocationPermissionEffect.ALLOW,
            reason="immutable Zyra-owned builtin prompt expansion is preauthorized; tools remain under 03A",
            decision_id=f"builtin-skill:{request.invocation_id}",
            events=(
                {
                    "kind": "skill_invocation_permission_decided",
                    "effect": "allow",
                    "decision_id": f"builtin-skill:{request.invocation_id}",
                    "skill_ref": revision.version_ref.immutable_ref,
                    "downstream_grant": False,
                    "permission_owner": "M1-03A bootstrap policy",
                },
            ),
            metadata={
                "owner": "M1-03A bootstrap policy",
                "downstream_tools_authorized": False,
                "allowed_tools_semantics": "deny-only ceiling",
                "admission": admission.to_dict(),
            },
        )


class ToolPermissionRuntimeSkillGateway:
    """Adapter that asks the existing 03A ToolPermissionRuntime.

    The guard decision authorizes only skill context expansion. This adapter
    never exports or reuses the exact execution grant. Downstream tool calls
    are separately guarded and consumed by ToolExecutionRuntime, with the
    SkillAllowedTools permission hook installed as a deny-only ceiling.
    """

    def __init__(self, permission_runtime: Any, *, workspace_root: str) -> None:
        self.permission_runtime = permission_runtime
        self.workspace_root = workspace_root

    def guard(self, request: SkillInvocationRequest, revision: SkillRevision) -> SkillInvocationPermissionResult:
        try:
            from zyra_runtime.permission.models import PermissionEvaluationRequest, ToolIdentity
        except ImportError as error:
            raise SkillPermissionDenied("03A permission contracts are unavailable") from error
        evaluation = PermissionEvaluationRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            worker_request_id=request.worker_request_id or f"skill:{request.invocation_id}",
            tool_use_id=request.parent_tool_use_id or request.invocation_id,
            node_id=request.node_id,
            tool_identity=ToolIdentity(
                namespace="skill",
                name=revision.metadata.name,
                server_id=revision.provenance.source_namespace,
                version=revision.metadata.declared_version,
                schema_digest=revision.version_ref.policy_digest,
            ),
            arguments={
                "skill_ref": revision.version_ref.immutable_ref,
                "arguments": dict(request.arguments),
                "invocation_mode": str(revision.metadata.invocation.mode),
            },
            operation="skill_context_expansion",
            workspace_root=self.workspace_root,
            interactive=request.interactive,
            headless=request.headless,
            requires_interaction=str(revision.provenance.source_kind) in {"plugin", "mcp"},
            risk_tags=(
                "prompt_context_mutation",
                f"skill_source:{revision.provenance.source_kind}",
            ),
            attributes={
                "skill_id": revision.version_ref.skill_id,
                "qualified_name": revision.qualified_name,
                "content_digest": revision.version_ref.content_digest,
            },
            metadata={
                "owner_unit": "M1-03C",
                "permission_owner": "M1-03A",
                "downstream_tools_authorized": False,
            },
        )
        result = self.permission_runtime.guard(evaluation)
        effect = SkillInvocationPermissionEffect(str(result.decision.effect))
        # The issued grant represents a context-expansion decision, not a tool
        # capability. Invalidate it immediately; actual tool grants are issued
        # and consumed later by ToolExecutionRuntime after the skill hook.
        if result.execution_grant is not None:
            self.permission_runtime.grant_store.invalidate(
                result.execution_grant,
                reason="skill context expansion decision committed; no downstream grant transfer",
            )
        try:
            from zyra_core import to_jsonable
        except ImportError:
            to_jsonable = lambda value: value.to_dict() if hasattr(value, "to_dict") else value  # type: ignore[assignment]
        events = tuple(
            dict(payload) if isinstance((payload := to_jsonable(event)), dict) else {"value": payload}
            for event in result.events
        )
        return SkillInvocationPermissionResult(
            effect=effect,
            reason=result.decision.reason,
            decision_id=result.decision.decision_id,
            request_id=result.decision.request_id,
            events=events,
            metadata={
                "permission_owner": "M1-03A",
                "execution_grant_transferred": False,
                "decision_effect": str(result.decision.effect),
            },
        )
