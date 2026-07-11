from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterable, Sequence

from .digests import digest_object, estimate_tokens
from .models import (
    InvokedSkillState,
    SkillAttachment,
    SkillBody,
    SkillListingEntry,
    SkillMessageDelta,
    SkillPolicySnapshot,
    new_id,
)


@dataclass(frozen=True, slots=True)
class SkillListingProjection:
    session_id: str
    agent_id: str
    entries: tuple[dict[str, Any], ...]
    token_estimate: int
    names_only: bool
    suppressed: bool
    generation: int
    projection_id: str = field(default_factory=lambda: new_id("skilllist"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "entries": [dict(item) for item in self.entries],
            "token_estimate": self.token_estimate,
            "names_only": self.names_only,
            "suppressed": self.suppressed,
            "generation": self.generation,
        }


class SkillAttachmentRuntime:
    """Agent/session-aware progressive disclosure and resume suppression."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sent_names: dict[tuple[str, str], set[str]] = {}
        self._suppress_once: set[tuple[str, str]] = set()

    def suppress_next_listing(self, *, session_id: str, agent_id: str) -> None:
        with self._lock:
            self._suppress_once.add((session_id, agent_id))

    def listing(
        self,
        entries: Sequence[SkillListingEntry],
        *,
        session_id: str,
        agent_id: str,
        generation: int,
        context_window_tokens: int = 200_000,
        max_tokens: int = 4_096,
    ) -> SkillListingProjection:
        key = (session_id, agent_id)
        with self._lock:
            if key in self._suppress_once:
                self._suppress_once.remove(key)
                return SkillListingProjection(
                    session_id=session_id,
                    agent_id=agent_id,
                    entries=(),
                    token_estimate=0,
                    names_only=False,
                    suppressed=True,
                    generation=generation,
                )
            sent = self._sent_names.setdefault(key, set())
            candidates = [entry for entry in entries if entry.qualified_name not in sent]
        budget = min(max_tokens, max(128, context_window_tokens // 100))
        projected: list[dict[str, Any]] = []
        tokens = 0
        names_only = False
        for entry in candidates:
            full = {
                "name": entry.name,
                "qualified_name": entry.qualified_name,
                "description": entry.description,
                "when_to_use": entry.when_to_use,
                "source": str(entry.source_kind),
                "invocation_mode": str(entry.invocation_mode),
                "version": entry.declared_version,
                "content_digest": entry.content_digest,
            }
            cost = min(entry.token_estimate, estimate_tokens(str(full)))
            if tokens + cost <= budget:
                projected.append(full)
                tokens += cost
                continue
            compact = {
                "name": entry.name,
                "qualified_name": entry.qualified_name,
                "source": str(entry.source_kind),
            }
            compact_cost = estimate_tokens(str(compact))
            if tokens + compact_cost > budget:
                break
            projected.append(compact)
            tokens += compact_cost
            names_only = True
        with self._lock:
            self._sent_names.setdefault(key, set()).update(
                str(item.get("qualified_name") or "") for item in projected
            )
        return SkillListingProjection(
            session_id=session_id,
            agent_id=agent_id,
            entries=tuple(projected),
            token_estimate=tokens,
            names_only=names_only,
            suppressed=False,
            generation=generation,
        )

    def invocation_attachments(
        self,
        *,
        state: InvokedSkillState,
        body: SkillBody,
        policy_snapshot: SkillPolicySnapshot,
        resource_refs: Iterable[str] = (),
    ) -> tuple[SkillAttachment, ...]:
        body_attachment = SkillAttachment(
            attachment_id=new_id("skillattach"),
            kind="skill_body",
            immutable_ref=body.version_ref.immutable_ref,
            label=body.version_ref.qualified_name,
            token_estimate=body.token_estimate,
            metadata={
                "invocation_id": state.invocation_id,
                "content_digest": body.version_ref.content_digest,
                "body_digest": body.version_ref.body_digest,
                "truncated": body.truncated,
                "agent_id": state.agent_id,
            },
        )
        policy_attachment = SkillAttachment(
            attachment_id=new_id("skillattach"),
            kind="skill_permission_delta",
            immutable_ref=f"skill-policy://{policy_snapshot.snapshot_id}",
            label="allowed-tools ceiling",
            token_estimate=estimate_tokens(str(policy_snapshot.to_dict())),
            metadata={
                "invocation_id": state.invocation_id,
                "policy_digest": policy_snapshot.policy_digest,
                "permission_owner": "M1-03A",
                "grant": False,
            },
        )
        resources = tuple(
            SkillAttachment(
                attachment_id=new_id("skillattach"),
                kind="skill_resource_ref",
                immutable_ref=str(ref),
                label=str(ref).rsplit("/", 1)[-1],
                token_estimate=estimate_tokens(str(ref)),
                metadata={"invocation_id": state.invocation_id},
            )
            for ref in resource_refs
        )
        return (body_attachment, policy_attachment, *resources)

    def inline_message(
        self,
        *,
        body: SkillBody,
        attachments: Sequence[SkillAttachment],
        parent_tool_use_id: str,
        base_directory: str,
        arguments: dict[str, Any],
    ) -> SkillMessageDelta:
        rendered = body.text
        rendered = rendered.replace("${ZYRA_SKILL_DIR}", base_directory)
        rendered = rendered.replace("$ARGUMENTS", str(arguments))
        prefix = f"Base directory for this skill: {base_directory}\n\n"
        return SkillMessageDelta(
            role="user",
            content=prefix + rendered,
            parent_tool_use_id=parent_tool_use_id,
            meta=True,
            hidden_from_user=True,
            attachments=tuple(attachments),
        )

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            for key in [key for key in self._sent_names if key[0] == session_id]:
                self._sent_names.pop(key, None)
            self._suppress_once = {key for key in self._suppress_once if key[0] != session_id}


def attachment_delta_digest(attachments: Sequence[SkillAttachment]) -> str:
    return digest_object([item.to_dict() for item in attachments])
