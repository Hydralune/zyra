from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from zyra_core import new_id

from .digests import digest_object
from .errors import SubagentCycleDetected, SubagentDepthExceeded
from .models import AgentContextMode, SubagentContextSnapshot


@dataclass(frozen=True, slots=True)
class ParentContextInput:
    parent_session_id: str
    parent_task_id: str
    parent_worker_request_id: str
    messages: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    invoked_skill_refs: tuple[dict[str, Any], ...] = ()
    content_replacement_refs: tuple[str, ...] = ()
    context_epoch: int = 0
    compact_boundary_id: str = ""
    rendered_system_prompt: str = ""
    ancestry: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(copy.deepcopy(dict(item)) for item in self.messages))
        object.__setattr__(self, "artifacts", tuple(str(item) for item in self.artifacts))
        object.__setattr__(self, "evidence", tuple(str(item) for item in self.evidence))
        object.__setattr__(self, "invoked_skill_refs", tuple(copy.deepcopy(dict(item)) for item in self.invoked_skill_refs))
        object.__setattr__(self, "content_replacement_refs", tuple(str(item) for item in self.content_replacement_refs))
        object.__setattr__(self, "ancestry", tuple(str(item) for item in self.ancestry))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ForkedMessagePrefix:
    messages: tuple[dict[str, Any], ...]
    placeholder_tool_result_ids: tuple[str, ...]
    cache_prefix_digest: str
    recursive_fork_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [copy.deepcopy(item) for item in self.messages],
            "placeholder_tool_result_ids": list(self.placeholder_tool_result_ids),
            "cache_prefix_digest": self.cache_prefix_digest,
            "recursive_fork_allowed": self.recursive_fork_allowed,
        }


class ForkContextBuilder:
    """Build cache-stable fork context without mutating parent messages."""

    def build(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        directive: str,
        already_in_fork: bool,
    ) -> ForkedMessagePrefix:
        if already_in_fork:
            raise SubagentCycleDetected(("fork",), "fork")
        cloned = [copy.deepcopy(dict(item)) for item in messages]
        placeholder_ids: list[str] = []
        normalized: list[dict[str, Any]] = []
        for message in cloned:
            normalized.append(message)
            if str(message.get("role")) != "assistant":
                continue
            for block in _content_blocks(message):
                if str(block.get("type")) != "tool_use":
                    continue
                tool_use_id = str(block.get("id") or block.get("tool_use_id") or "")
                if not tool_use_id:
                    continue
                placeholder_ids.append(tool_use_id)
                normalized.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": "Execution delegated to a forked Zyra subagent.",
                        "is_error": False,
                        "metadata": {"placeholder": True, "cache_stable": True},
                    }],
                    "metadata": {"fork_placeholder": True},
                })
        prefix_digest = digest_object(normalized)
        normalized.append({
            "role": "user",
            "content": directive,
            "metadata": {"fork_directive": True, "cache_prefix_digest": prefix_digest},
        })
        return ForkedMessagePrefix(
            messages=tuple(normalized),
            placeholder_tool_result_ids=tuple(placeholder_ids),
            cache_prefix_digest=prefix_digest,
            recursive_fork_allowed=False,
        )


class SubagentContextFactory:
    """Create isolated child snapshots and enforce ancestry invariants."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self.fork_builder = ForkContextBuilder()

    def create(
        self,
        parent: ParentContextInput,
        *,
        child_task_id: str,
        agent_type: str,
        objective_digest: str,
        mode: AgentContextMode,
        maximum_depth: int,
        directive: str,
    ) -> SubagentContextSnapshot:
        if self.disabled:
            raise RuntimeError("SubagentContextFactory is disabled")
        depth = len(parent.ancestry) + 1
        if depth > maximum_depth:
            raise SubagentDepthExceeded(depth, maximum_depth)
        cycle_key = digest_object({
            "agent_type": agent_type,
            "objective_digest": objective_digest,
            "artifact_refs": parent.artifacts,
            "evidence_refs": parent.evidence,
        })
        ancestry_keys = set(parent.metadata.get("cycle_keys") or ())
        if cycle_key in ancestry_keys:
            raise SubagentCycleDetected(parent.ancestry, agent_type)

        message_refs: list[str] = []
        cache_prefix_digest = ""
        metadata = {
            **copy.deepcopy(parent.metadata),
            "cycle_key": cycle_key,
            "cycle_keys": [*sorted(ancestry_keys), cycle_key],
            "child_task_id": child_task_id,
            "isolated_mutable_state": True,
            "parent_messages_copied": False,
            "parent_exact_grants_copied": False,
        }
        if mode == AgentContextMode.FORK:
            fork = self.fork_builder.build(
                parent.messages,
                directive=directive,
                already_in_fork=bool(parent.metadata.get("in_fork", False)),
            )
            cache_prefix_digest = fork.cache_prefix_digest
            metadata["forked_messages"] = [copy.deepcopy(item) for item in fork.messages]
            metadata["fork_placeholder_tool_result_ids"] = list(fork.placeholder_tool_result_ids)
            metadata["in_fork"] = True
            message_refs = [
                str(item.get("message_id") or item.get("uuid") or digest_object(item))
                for item in parent.messages
            ]
        elif mode == AgentContextMode.RESUME:
            message_refs = [str(item.get("message_id") or item.get("uuid") or "") for item in parent.messages]
            metadata["resume_from_sidechain_only"] = True
        else:
            # Isolated child sees only explicit refs and the self-contained
            # directive. Parent transcript text never enters child metadata.
            metadata["directive_digest"] = digest_object(directive)

        snapshot_payload = {
            "parent_session_id": parent.parent_session_id,
            "parent_task_id": parent.parent_task_id,
            "parent_worker_request_id": parent.parent_worker_request_id,
            "mode": mode.value,
            "context_epoch": parent.context_epoch,
            "compact_boundary_id": parent.compact_boundary_id,
            "message_refs": message_refs,
            "artifact_refs": parent.artifacts,
            "evidence_refs": parent.evidence,
            "invoked_skill_refs": parent.invoked_skill_refs,
            "content_replacement_refs": parent.content_replacement_refs,
            "system_prompt_digest": digest_object(parent.rendered_system_prompt),
            "cache_prefix_digest": cache_prefix_digest,
            "ancestry": (*parent.ancestry, child_task_id),
            "depth": depth,
            "metadata": metadata,
        }
        return SubagentContextSnapshot(
            snapshot_id=new_id("subctx"),
            parent_session_id=parent.parent_session_id,
            parent_task_id=parent.parent_task_id,
            parent_worker_request_id=parent.parent_worker_request_id,
            mode=mode,
            context_epoch=parent.context_epoch,
            compact_boundary_id=parent.compact_boundary_id,
            message_refs=tuple(message_refs),
            artifact_refs=parent.artifacts,
            evidence_refs=parent.evidence,
            invoked_skill_refs=parent.invoked_skill_refs,
            content_replacement_refs=parent.content_replacement_refs,
            system_prompt_digest=digest_object(parent.rendered_system_prompt),
            cache_prefix_digest=cache_prefix_digest,
            ancestry=(*parent.ancestry, child_task_id),
            depth=depth,
            metadata={**metadata, "snapshot_digest": digest_object(snapshot_payload)},
        )

    def resume_input(
        self,
        snapshot: SubagentContextSnapshot,
        *,
        sidechain_entries: Sequence[Mapping[str, Any]],
        continuation: str,
    ) -> ParentContextInput:
        return ParentContextInput(
            parent_session_id=snapshot.parent_session_id,
            parent_task_id=snapshot.parent_task_id,
            parent_worker_request_id=snapshot.parent_worker_request_id,
            messages=tuple(copy.deepcopy(dict(item)) for item in sidechain_entries),
            artifacts=snapshot.artifact_refs,
            evidence=snapshot.evidence_refs,
            invoked_skill_refs=snapshot.invoked_skill_refs,
            content_replacement_refs=snapshot.content_replacement_refs,
            context_epoch=snapshot.context_epoch,
            compact_boundary_id=snapshot.compact_boundary_id,
            ancestry=snapshot.ancestry[:-1],
            metadata={
                **copy.deepcopy(snapshot.metadata),
                "resume": True,
                "continuation_digest": digest_object(continuation),
            },
        )


def _content_blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return [item for item in content if isinstance(item, Mapping)]
    return []
