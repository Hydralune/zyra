from __future__ import annotations

import copy
import fnmatch
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Mapping, Sequence

from .digests import digest_object
from .integration_errors import (
    SkillForkReceiptMismatch,
    SkillForkScopeError,
    SkillForkToolDenied,
)
from .models import SkillForkRequest, SkillPolicySnapshot, ToolSelector, utc_now
from .policy import ToolUseIdentity
from .subagent_contract import SkillForkReceipt


@dataclass(frozen=True, slots=True)
class SkillForkToolScope:
    fork_request_id: str
    invocation_id: str
    parent_session_id: str
    parent_agent_id: str
    child_session_id: str
    child_agent_id: str
    version_ref: str
    policy_snapshot: SkillPolicySnapshot
    visible_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    parent_grants_inherited: bool
    scope_digest: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.parent_grants_inherited:
            raise SkillForkScopeError("forked skill scope cannot inherit parent grants")
        if set(self.visible_tools).intersection(self.denied_tools):
            raise SkillForkScopeError("forked skill tool scope contains allow/deny overlap")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fork_request_id": self.fork_request_id,
            "invocation_id": self.invocation_id,
            "parent_session_id": self.parent_session_id,
            "parent_agent_id": self.parent_agent_id,
            "child_session_id": self.child_session_id,
            "child_agent_id": self.child_agent_id,
            "version_ref": self.version_ref,
            "policy_snapshot": self.policy_snapshot.to_dict(),
            "visible_tools": list(self.visible_tools),
            "denied_tools": list(self.denied_tools),
            "parent_grants_inherited": self.parent_grants_inherited,
            "scope_digest": self.scope_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillForkDispatchRecord:
    fork_request: SkillForkRequest
    receipt: SkillForkReceipt
    tool_scope: SkillForkToolScope
    dispatch_digest: str
    dispatched_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fork_request": self.fork_request.to_dict(),
            "receipt": self.receipt.to_dict(),
            "tool_scope": self.tool_scope.to_dict(),
            "dispatch_digest": self.dispatch_digest,
            "dispatched_at": self.dispatched_at,
            "body_in_parent": False,
            "parent_grant_in_child": False,
        }


class SkillForkToolScopeRuntime:
    """Builds a deny-only child tool view for the future 03D dispatcher."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, SkillForkDispatchRecord] = {}

    def build(
        self,
        request: SkillForkRequest,
        receipt: SkillForkReceipt,
        *,
        available_tools: Sequence[Any],
    ) -> SkillForkDispatchRecord:
        expected_request_id = f"skillfork:{request.invocation_id}"
        if receipt.fork_request_id == "" or receipt.fork_request_id != expected_request_id:
            # The foundation queue intentionally uses invocation_id as the
            # durable request key.  03D may replace the receipt type but must
            # preserve this exact causal binding.
            raise SkillForkReceiptMismatch(
                "fork receipt does not match the skill invocation",
                detail={
                    "invocation_id": request.invocation_id,
                    "fork_request_id": receipt.fork_request_id,
                },
            )
        child_session_id = f"skill-child:{request.invocation_id}"
        child_agent_id = request.agent_type or f"skill-agent:{request.invocation_id}"
        visible: list[str] = []
        denied: list[str] = []
        for tool in available_tools:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            metadata = getattr(tool, "metadata", {})
            identity = ToolUseIdentity(
                namespace=str(metadata.get("tool_namespace") or metadata.get("namespace") or "builtin"),
                name=name,
                server_id=str(metadata.get("server_id") or ""),
                operation=str(metadata.get("operation") or name),
                path=str(metadata.get("path") or ""),
                domain=str(metadata.get("domain") or ""),
            )
            if self._allowed(identity, request.policy_snapshot.effective_selectors):
                visible.append(name)
            else:
                denied.append(name)
        payload = {
            "fork_request_id": receipt.fork_request_id,
            "invocation_id": request.invocation_id,
            "parent_session_id": request.parent_session_id,
            "child_session_id": child_session_id,
            "version_ref": request.version_ref.immutable_ref,
            "policy_digest": request.policy_snapshot.policy_digest,
            "visible_tools": sorted(visible),
            "denied_tools": sorted(denied),
            "parent_grants_inherited": False,
        }
        scope = SkillForkToolScope(
            fork_request_id=receipt.fork_request_id,
            invocation_id=request.invocation_id,
            parent_session_id=request.parent_session_id,
            parent_agent_id=request.parent_agent_id,
            child_session_id=child_session_id,
            child_agent_id=child_agent_id,
            version_ref=request.version_ref.immutable_ref,
            policy_snapshot=request.policy_snapshot,
            visible_tools=tuple(sorted(visible)),
            denied_tools=tuple(sorted(denied)),
            parent_grants_inherited=False,
            scope_digest=digest_object(payload),
        )
        record = SkillForkDispatchRecord(
            fork_request=request,
            receipt=receipt,
            tool_scope=scope,
            dispatch_digest=digest_object(
                {
                    "request": request.to_dict(),
                    "receipt": receipt.to_dict(),
                    "scope": scope.to_dict(),
                }
            ),
        )
        with self._lock:
            existing = self._records.get(request.invocation_id)
            if existing is not None:
                if existing.dispatch_digest != record.dispatch_digest:
                    raise SkillForkReceiptMismatch("fork dispatch changed after publication")
                return existing
            self._records[request.invocation_id] = record
        return record

    def authorize(
        self,
        invocation_id: str,
        identity: ToolUseIdentity,
    ) -> None:
        record = self.get(invocation_id)
        if not self._allowed(identity, record.tool_scope.policy_snapshot.effective_selectors):
            raise SkillForkToolDenied(
                "forked skill attempted a tool outside its exact ceiling",
                detail={
                    "invocation_id": invocation_id,
                    "tool": {
                        "namespace": identity.namespace,
                        "name": identity.name,
                        "server_id": identity.server_id,
                        "operation": identity.operation,
                        "path": identity.path,
                        "domain": identity.domain,
                    },
                    "policy_digest": record.tool_scope.policy_snapshot.policy_digest,
                },
            )

    def get(self, invocation_id: str) -> SkillForkDispatchRecord:
        with self._lock:
            value = self._records.get(str(invocation_id))
        if value is None:
            raise SkillForkScopeError(
                "forked skill tool scope was not found",
                detail={"invocation_id": invocation_id},
            )
        return value

    def complete(self, invocation_id: str) -> SkillForkDispatchRecord:
        with self._lock:
            value = self._records.pop(str(invocation_id), None)
        if value is None:
            raise SkillForkScopeError("forked skill dispatch was not found")
        return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = {key: value.to_dict() for key, value in self._records.items()}
        return {
            "owner": "M1-03C handoff; execution owner M1-03D",
            "records": values,
            "snapshot_digest": digest_object(values),
            "body_in_parent": False,
            "parent_grant_in_child": False,
        }

    def _allowed(
        self,
        identity: ToolUseIdentity,
        selectors: Sequence[ToolSelector],
    ) -> bool:
        if not selectors:
            return False
        return any(_selector_matches(selector, identity) for selector in selectors)


def _selector_matches(selector: ToolSelector, identity: ToolUseIdentity) -> bool:
    if not fnmatch.fnmatchcase(identity.namespace, selector.namespace):
        return False
    if not fnmatch.fnmatchcase(identity.name, selector.name):
        return False
    if selector.server_id is not None and not fnmatch.fnmatchcase(identity.server_id, selector.server_id):
        return False
    if selector.operations and not any(
        fnmatch.fnmatchcase(identity.operation, operation) for operation in selector.operations
    ):
        return False
    if selector.path_prefixes and identity.path and not any(
        identity.path.startswith(prefix) for prefix in selector.path_prefixes
    ):
        return False
    if selector.domains and identity.domain and not any(
        fnmatch.fnmatchcase(identity.domain, domain) for domain in selector.domains
    ):
        return False
    return True
