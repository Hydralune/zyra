from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from zyra_runtime import default_tool_registry

from .models import (
    AgentContextMode,
    AgentExecutionMode,
    PermissionMode,
    SubagentSpawnRequest,
)
from .runtime import SubagentRuntime


@dataclass(frozen=True, slots=True)
class SubagentSkillForkPort:
    """03D replacement for the process-local 03C fork request queue."""

    runtime: SubagentRuntime
    parent_tools_provider: Callable[[Any], Sequence[str]] | None = None
    workspace_root: str = ""

    def dispatch(self, request: Any) -> Any:
        # Import lazily so workers do not make SkillRuntime a hard dependency.
        from zyra_skills.subagent_contract import SkillForkReceipt

        parent_tools = tuple(
            self.parent_tools_provider(request)
            if self.parent_tools_provider is not None
            else (item.name for item in default_tool_registry().list())
        )
        selectors = request.policy_snapshot.effective_tools
        requested_tools = tuple(
            selector.name
            for selector in selectors or ()
            if getattr(selector, "namespace", "") in {"*", "builtin", "local"}
        )
        mcp_servers = tuple(
            selector.server_id
            for selector in selectors or ()
            if getattr(selector, "namespace", "") == "mcp" and selector.server_id
        )
        task_id = f"skillfork:{request.invocation_id}"
        spawn = self.runtime.spawn(
            SubagentSpawnRequest(
                run_id=request.run_id,
                parent_task_id=request.task_id,
                parent_session_id=request.parent_session_id,
                parent_worker_request_id=f"skill:{request.invocation_id}",
                agent_type=request.agent_type or "general-purpose",
                prompt=(
                    f"Execute immutable skill `{request.version_ref.immutable_ref}`. "
                    f"The skill body is available through `{request.body_ref}`. "
                    "Return a structured result with artifact and evidence references."
                ),
                parent_tools=parent_tools,
                parent_permission_mode=PermissionMode.DEFAULT,
                requested_tools=requested_tools,
                available_mcp_servers=mcp_servers,
                requested_mcp_servers=mcp_servers,
                context_mode=AgentContextMode.ISOLATED,
                execution_mode=AgentExecutionMode.BACKGROUND,
                workspace_root=self.workspace_root or self.runtime.config.workspace_root,
                context_payload={
                    "artifact_refs": list(request.attachment_refs),
                    "evidence_refs": list(request.context_refs),
                    "invoked_skill_refs": [{
                        "invocation_id": request.invocation_id,
                        "immutable_ref": request.version_ref.immutable_ref,
                        "body_ref": request.body_ref,
                        "policy_snapshot_digest": request.policy_snapshot.policy_digest,
                    }],
                    "metadata": {"skill_fork": True},
                },
                constraints={
                    "required_tools": list(requested_tools),
                    "skill_fork": True,
                    "skill_invocation_id": request.invocation_id,
                    "skill_body_ref": request.body_ref,
                    "parent_grants_inherited": False,
                },
                idempotency_key=f"skillfork:{request.invocation_id}",
                task_id=task_id,
                metadata={"root_task_id": request.task_id, "skill_version_ref": request.version_ref.immutable_ref},
            )
        )
        record = self.runtime.get(task_id)
        return SkillForkReceipt(
            fork_request_id=f"skillfork:{request.invocation_id}",
            task_id=record.task_id,
            execution_ref=record.execution_ref,
            status=record.status.value,
        )
