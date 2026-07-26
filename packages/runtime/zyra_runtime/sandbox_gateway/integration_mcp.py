from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence, TYPE_CHECKING

from ..executor import DynamicToolProvenance, ToolCall, ToolResult
from .artifact_port import FileArtifactRequest
from .redaction import redact_for_event
from .models import (
    GatewayLifecycleState,
    OperationKind,
    ProvenanceKind,
    TrustLevel,
)
from .integration_models import (
    FailureClass,
    GatewayMcpExchange,
    GatewayOutcome,
    RecoveryAction,
    WorkerGatewayIdentity,
    canonical_value,
    content_digest,
    stable_identifier,
)

if TYPE_CHECKING:
    from .integration_factory import GatewayRuntimeBundle


@dataclass(frozen=True, slots=True)
class McpResultInspection:
    allowed: bool
    quarantine: bool
    content_bytes: int
    binary_values: int
    secret_findings: int
    control_mutations: tuple[str, ...]
    result_digest: str
    safe_value: Any
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class McpGatewayBoundary:
    """Wraps projected MCP handlers without changing their provenance identity."""

    def __init__(self, bundle: "GatewayRuntimeBundle") -> None:
        self.bundle = bundle
        self._exchanges: dict[str, GatewayMcpExchange] = {}

    def execute(
        self,
        call: ToolCall,
        handler: Callable[[ToolCall], ToolResult],
        *,
        provenance: DynamicToolProvenance,
        authorized: bool,
    ) -> ToolResult:
        identity = self._identity(call)
        self._ensure_session(identity)
        policy = self.bundle.policy_runtime.evaluate_mcp_call(
            server_id=provenance.server_id,
            tool_name=provenance.tool_name,
            arguments=call.arguments,
            external_boundary=provenance.external_boundary,
        )
        exchange = GatewayMcpExchange(
            exchange_id=stable_identifier(
                "gateway-mcp-exchange",
                identity.binding_digest,
                call.tool_call_id,
                policy.subject_digest,
            ),
            identity=identity,
            server_id=provenance.server_id,
            tool_name=provenance.tool_name,
            tool_call_id=call.tool_call_id,
            request_digest=content_digest(
                {
                    "server_id": provenance.server_id,
                    "tool_name": provenance.tool_name,
                    "arguments": call.arguments,
                    "version": provenance.version,
                }
            ),
            metadata={
                "namespace": provenance.namespace,
                "version": provenance.version,
                "external_boundary": provenance.external_boundary,
                "authorized": authorized,
            },
        )
        self._exchanges[exchange.exchange_id] = exchange
        if policy.hard_denied:
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=exchange.exchange_id,
                failure_class=FailureClass.POLICY,
                code="mcp_gateway_policy_denied",
                reason=policy.reason,
                retryable=False,
                recovery_actions=(RecoveryAction.REDUCE_SCOPE, RecoveryAction.REPLAN),
                causation_id=call.tool_call_id,
                metadata={"server_id": provenance.server_id, "tool_name": provenance.tool_name},
            )
            completed = exchange.complete(
                response={"error": "mcp_gateway_policy_denied"},
                outcome=GatewayOutcome.DENIED,
                metadata={"failure_signal_id": signal.signal_id},
            )
            self._exchanges[exchange.exchange_id] = completed
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="MCP call was denied by SandboxGateway",
                error="mcp_gateway_policy_denied",
                metadata={
                    "sandbox_gateway_routed": "true",
                    "sandbox_gateway_mcp_exchange_id": exchange.exchange_id,
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                },
            )
        if policy.requires_permission and not authorized:
            signal = self.bundle.signal_emitter.permission_blocked(
                identity,
                invocation_id=exchange.exchange_id,
                reason="MCP call has no consumed exact permission grant",
                sealed=self.bundle.sealed,
                causation_id=call.tool_call_id,
            )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="MCP call requires an exact permission grant",
                error="permission_grant_required",
                metadata={
                    "sandbox_gateway_routed": "true",
                    "sandbox_gateway_mcp_exchange_id": exchange.exchange_id,
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                },
            )
        started = time.time()
        try:
            result = handler(call)
        except Exception as error:  # noqa: BLE001
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=exchange.exchange_id,
                failure_class=FailureClass.BACKEND,
                code="mcp_handler_failed",
                reason=f"{type(error).__name__}: {error}",
                retryable=True,
                recovery_actions=(RecoveryAction.RETRY, RecoveryAction.REPLAN),
                causation_id=call.tool_call_id,
                metadata={"server_id": provenance.server_id, "tool_name": provenance.tool_name},
            )
            completed = exchange.complete(
                response={"error": type(error).__name__},
                outcome=GatewayOutcome.FAILED,
                metadata={"failure_signal_id": signal.signal_id},
            )
            self._exchanges[exchange.exchange_id] = completed
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="MCP handler failed behind SandboxGateway",
                error="mcp_handler_failed",
                metadata={
                    "sandbox_gateway_routed": "true",
                    "sandbox_gateway_mcp_exchange_id": exchange.exchange_id,
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                },
            )
        if not isinstance(result, ToolResult):
            raise TypeError("MCP dynamic handler returned a non-ToolResult value")
        if result.tool_call_id != call.tool_call_id:
            raise ValueError("MCP dynamic handler changed the tool call identity")
        inspection = self.inspect_result(result.output)
        artifact_refs: list[str] = []
        provenance_record = self.bundle.provenance_registry.derive(
            kind=ProvenanceKind.MCP,
            source_id=provenance.server_id,
            parent_refs=tuple(
                item
                for item in (str(call.metadata.get("provenance_ref") or ""),)
                if item
            ),
            content=json.dumps(canonical_value(inspection.safe_value), sort_keys=True),
            trust=TrustLevel.UNTRUSTED,
            untrusted_instructions=True,
            metadata={
                "tool_name": provenance.tool_name,
                "tool_version": provenance.version,
                "request_digest": exchange.request_digest,
            },
        )
        if inspection.quarantine or inspection.content_bytes > 64 * 1024:
            artifact_ref = self._spill_result(
                exchange,
                provenance_record,
                inspection,
            )
            if artifact_ref:
                artifact_refs.append(artifact_ref)
        outcome = (
            GatewayOutcome.DENIED
            if not inspection.allowed
            else GatewayOutcome.QUARANTINED
            if inspection.quarantine
            else GatewayOutcome.COMMITTED
        )
        completed = exchange.complete(
            response=inspection.safe_value,
            outcome=outcome,
            provenance_ref=provenance_record.provenance_id,
            artifact_refs=artifact_refs,
            redaction_count=inspection.secret_findings,
            metadata={
                "content_bytes": inspection.content_bytes,
                "binary_values": inspection.binary_values,
                "control_mutations": list(inspection.control_mutations),
                "duration_ms": round((time.time() - started) * 1000, 3),
            },
        )
        self._exchanges[exchange.exchange_id] = completed
        if not inspection.allowed:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="MCP result was denied by SandboxGateway",
                output={
                    "artifact_refs": artifact_refs,
                    "mcp_exchange": completed.safe_dict(),
                },
                error="mcp_result_policy_denied",
                metadata={
                    "sandbox_gateway_routed": "true",
                    "sandbox_gateway_mcp_exchange_id": exchange.exchange_id,
                    "sandbox_gateway_mcp_result_quarantined": str(inspection.quarantine).lower(),
                },
            )
        safe_output = inspection.safe_value
        if inspection.quarantine:
            safe_output = {
                "quarantined": True,
                "reason": inspection.reason,
                "artifact_refs": artifact_refs,
                "content_digest": inspection.result_digest,
            }
        return replace(
            result,
            output=safe_output if isinstance(safe_output, dict) else {"value": safe_output},
            metadata={
                **result.metadata,
                "sandbox_gateway_routed": "true",
                "sandbox_gateway_mcp_exchange_id": exchange.exchange_id,
                "sandbox_gateway_mcp_response_digest": completed.response_digest,
                "sandbox_gateway_mcp_provenance_ref": provenance_record.provenance_id,
                "sandbox_gateway_mcp_result_quarantined": str(inspection.quarantine).lower(),
                "sandbox_gateway_mcp_redaction_count": str(inspection.secret_findings),
                "sandbox_gateway_mcp_artifact_refs": ",".join(artifact_refs),
            },
        )

    def inspect_result(self, value: Any) -> McpResultInspection:
        counters = {"binary": 0, "secret": 0}
        control_mutations: list[str] = []
        safe = self._sanitize(value, path=(), counters=counters, control_mutations=control_mutations)
        serialized = json.dumps(canonical_value(safe), ensure_ascii=True, sort_keys=True).encode("utf-8")
        content_bytes = len(serialized)
        over_budget = content_bytes > self.bundle.policy_runtime.config.maximum_mcp_result_bytes
        quarantine = bool(counters["binary"] or control_mutations or over_budget)
        allowed = not control_mutations and not over_budget
        reason = (
            "MCP result attempts to mutate protected control state"
            if control_mutations
            else "MCP result exceeds configured budget"
            if over_budget
            else "MCP binary result requires quarantine"
            if counters["binary"]
            else "MCP result passed gateway inspection"
        )
        return McpResultInspection(
            allowed=allowed,
            quarantine=quarantine,
            content_bytes=content_bytes,
            binary_values=counters["binary"],
            secret_findings=counters["secret"],
            control_mutations=tuple(control_mutations),
            result_digest=content_digest(serialized),
            safe_value=safe,
            reason=reason,
            metadata={"bounded": not over_budget},
        )

    def exchanges(self) -> tuple[GatewayMcpExchange, ...]:
        return tuple(self._exchanges.values())

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime": "McpGatewayBoundary",
            "exchange_count": len(self._exchanges),
            "canonical_mcp_owner": "McpMainPathRuntime",
            "gateway_policy_digest": self.bundle.policy_runtime.policy_digest,
            "binary_quarantine": True,
            "control_mutation_denied": True,
        }

    def _identity(self, call: ToolCall) -> WorkerGatewayIdentity:
        access = _current_access(self.bundle.workspace_edit_port)
        workspace_id = str(getattr(access, "workspace_id", "") or "")
        owner_epoch = int(getattr(access, "owner_epoch", 0) or 0)
        session_id = str(call.metadata.get("session_id") or "") or stable_identifier(
            "gateway-session",
            call.run_id,
            call.task_id,
            "McpMainPathRuntime",
            workspace_id,
        )
        return WorkerGatewayIdentity(
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=str(call.node_id or ""),
            worker_id="McpMainPathRuntime",
            session_id=session_id,
            request_id=str(call.metadata.get("worker_request_id") or ""),
            workspace_id=workspace_id,
            owner_epoch=owner_epoch,
            backend_id="mcp-transport",
        )

    def _sanitize(
        self,
        value: Any,
        *,
        path: tuple[str, ...],
        counters: dict[str, int],
        control_mutations: list[str],
    ) -> Any:
        if isinstance(value, bytes):
            counters["binary"] += 1
            return {
                "binary": True,
                "content_digest": content_digest(value),
                "content_bytes": len(value),
            }
        if isinstance(value, bytearray):
            return self._sanitize(bytes(value), path=path, counters=counters, control_mutations=control_mutations)
        if isinstance(value, Mapping):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                lowered = name.casefold()
                current = (*path, name)
                if lowered in {
                    "permission_mode",
                    "permission_rules",
                    "mcp_config",
                    "mcp_servers",
                    "project_rules",
                    "shell_profile",
                    "allowed_commands",
                }:
                    control_mutations.append(".".join(current))
                if any(token in lowered for token in ("token", "secret", "password", "credential", "cookie")):
                    counters["secret"] += 1
                    safe[name] = "[REDACTED]"
                else:
                    safe[name] = self._sanitize(
                        item,
                        path=current,
                        counters=counters,
                        control_mutations=control_mutations,
                    )
            return safe
        if isinstance(value, (list, tuple)):
            return [
                self._sanitize(
                    item,
                    path=(*path, str(index)),
                    counters=counters,
                    control_mutations=control_mutations,
                )
                for index, item in enumerate(value)
            ]
        if isinstance(value, str):
            redacted = redact_for_event(value)
            if redacted != value:
                counters["secret"] += 1
            return redacted
        return canonical_value(value)

    def _spill_result(
        self,
        exchange: GatewayMcpExchange,
        provenance: Any,
        inspection: McpResultInspection,
    ) -> str:
        if self.bundle.artifact_port is None:
            if self.bundle.required:
                raise RuntimeError("sandbox_gateway_artifact_port_unavailable")
            return ""
        content = json.dumps(
            canonical_value(inspection.safe_value),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        request = FileArtifactRequest(
            request_id=stable_identifier("gateway-mcp-artifact", exchange.exchange_id, inspection.result_digest),
            session_id=exchange.identity.session_id,
            logical_path=f"artifacts/mcp/{exchange.exchange_id.replace(':', '_')}.json",
            content=content,
            content_type="application/json",
            provenance=provenance,
            operation=OperationKind.ARTIFACT_EXPORT,
            mount_kind="task",
            executable_allowed=False,
            archive_expansion_allowed=False,
            idempotency_key=stable_identifier("mcp-result", exchange.exchange_id),
            causation_id=exchange.tool_call_id,
            metadata={"mcp_result": True, "quarantine_requested": inspection.quarantine},
        )
        receipt = self.bundle.artifact_port.commit(request)
        return receipt.artifact_ref or receipt.quarantine_id

    def _ensure_session(self, identity: WorkerGatewayIdentity) -> None:
        """Bind MCP artifact effects to the same canonical gateway session."""

        try:
            record = self.bundle.state_store.require_session(identity.session_id)
        except Exception:  # noqa: BLE001 - absence is the create path.
            record = self.bundle.runtime.create_session(
                session_id=identity.session_id,
                run_id=identity.run_id,
                task_id=identity.task_id,
                workspace_id=identity.workspace_id,
                worker_id=identity.worker_id,
                metadata={
                    "identity_binding_digest": identity.binding_digest,
                    "canonical_gateway_owner": "SandboxGatewayRuntime",
                    "mcp_boundary": True,
                },
            )
        if record.state is GatewayLifecycleState.CREATED:
            record = self.bundle.runtime.prepare_session(identity.session_id)
        if record.state not in {
            GatewayLifecycleState.READY,
            GatewayLifecycleState.BUSY,
        }:
            raise RuntimeError(
                f"sandbox MCP session is not executable: {record.state.value}"
            )


def _current_access(port: Any) -> Any:
    current = getattr(port, "current_access", None)
    return current() if callable(current) else None


__all__ = [
    "McpGatewayBoundary",
    "McpResultInspection",
]
