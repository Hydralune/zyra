from __future__ import annotations

from typing import Any, Mapping

from .integration_models import content_digest


def integration_source_custody() -> tuple[Mapping[str, Any], ...]:
    return (
        {
            "source_repository": "OpenHands",
            "source_role": "primary",
            "source_modules": (
                "openhands/runtime/impl/action_execution/action_execution_client.py",
                "openhands/runtime/impl/docker/docker_runtime.py",
                "openhands/events/action/commands.py",
            ),
            "mechanisms": (
                "sandbox lifecycle",
                "action execution boundary",
                "backend failure projection",
                "artifact transfer",
            ),
            "target_modules": (
                "zyra_runtime.sandbox_gateway.integration_factory",
                "zyra_runtime.sandbox_gateway.integration_tools",
                "zyra_runtime.sandbox_gateway.integration_control",
            ),
            "target_language": "Python",
            "canonical_owner": "SandboxGatewayRuntime",
            "migration_mode": "same-language productized adaptation",
            "production_owner": True,
        },
        {
            "source_repository": "OpenClaw",
            "source_role": "supplementary",
            "source_modules": (
                "src/gateway/server-methods",
                "src/agents/tool-policy.ts",
                "src/infra/exec-approvals.ts",
            ),
            "mechanisms": (
                "gateway policy boundary",
                "exact approval binding",
                "control cancellation",
                "remote dispatch receipt",
            ),
            "target_modules": (
                "zyra_runtime.sandbox_gateway.integration_policy",
                "zyra_runtime.sandbox_gateway.integration_permission",
                "zyra_runtime.sandbox_gateway.integration_remote",
            ),
            "target_language": "Python and TypeScript",
            "canonical_owner": "SandboxGatewayRuntime",
            "migration_mode": "mechanism-level supplementary adaptation",
            "production_owner": True,
        },
        {
            "source_repository": "oh-my-pi",
            "source_role": "supplementary",
            "source_modules": (
                "packages/coding-agent/src/core/tools",
                "packages/coding-agent/src/core/extension-runner.ts",
                "packages/coding-agent/src/modes/rpc",
            ),
            "mechanisms": (
                "structured argv",
                "Hashline stale edit fencing",
                "credential isolation",
                "host tool and RPC result boundary",
            ),
            "target_modules": (
                "zyra_runtime.sandbox_gateway.integration_tools",
                "zyra_runtime.sandbox_gateway.integration_host",
                "zyra_runtime.sandbox_gateway.integration_mcp",
                "@zyra/sandbox-gateway-control",
            ),
            "target_language": "Python and TypeScript",
            "canonical_owner": "SandboxGatewayRuntime",
            "migration_mode": "bounded supplement; no lifecycle ownership",
            "production_owner": True,
        },
        {
            "source_repository": "claude-code-best",
            "source_role": "conformance_only",
            "source_modules": (
                "src/tools",
                "src/hooks/toolPermission",
                "src/services/mcp",
            ),
            "mechanisms": (
                "tool loop integration",
                "permission continuation",
                "MCP dynamic tool provenance",
            ),
            "target_modules": (
                "zyra_workers.code_worker_runtime",
                "zyra_runtime.executor.ToolExecutor",
            ),
            "target_language": "TypeScript and Python integration glue",
            "canonical_owner": "TypeScriptClaudeQueryEngine and typescript.PermissionCoordinator",
            "migration_mode": "conformance and production-path connection only",
            "production_owner": False,
        },
        {
            "source_repository": "AgentScope/Hermes/opencode",
            "source_role": "reference_only",
            "source_modules": ("sandbox and gateway protocol surfaces",),
            "mechanisms": ("negative cases and protocol comparison",),
            "target_modules": ("gateway integration adversarial tests",),
            "target_language": "none",
            "canonical_owner": "none",
            "migration_mode": "no production migration quota",
            "production_owner": False,
        },
    )


def integration_source_custody_manifest() -> Mapping[str, Any]:
    entries = integration_source_custody()
    return {
        "schema": "zyra.gateway-integration-source-custody.v1",
        "canonical_gateway_owner": "SandboxGatewayRuntime",
        "permission_owner": "typescript.PermissionCoordinator",
        "workspace_owner": "WorkspaceManagerRuntime",
        "entries": [dict(item) for item in entries],
        "manifest_digest": content_digest(entries),
        "production_source_count": sum(1 for item in entries if item["production_owner"]),
        "second_gateway_count": 0,
    }


def assert_integration_source_custody() -> Mapping[str, Any]:
    manifest = integration_source_custody_manifest()
    entries = manifest["entries"]
    primary = [item for item in entries if item["source_role"] == "primary"]
    if len(primary) != 1 or primary[0]["source_repository"] != "OpenHands":
        raise RuntimeError("gateway integration must retain exactly one OpenHands primary")
    if manifest["second_gateway_count"] != 0:
        raise RuntimeError("gateway integration created a second canonical gateway")
    for entry in entries:
        if entry["source_role"] in {"conformance_only", "reference_only"} and entry["production_owner"]:
            raise RuntimeError("non-production source role received production ownership")
    return manifest


__all__ = [
    "assert_integration_source_custody",
    "integration_source_custody",
    "integration_source_custody_manifest",
]
