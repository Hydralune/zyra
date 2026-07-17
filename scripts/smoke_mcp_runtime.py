from __future__ import annotations

"""Smoke the canonical TypeScript E02 MCP/permission/command custody path."""

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for package in ("core", "runtime", "integrations"):
    package_path = ROOT / "packages" / package
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.e02_ports import TypeScriptE02ApiPort  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zyra-e02-mcp-smoke-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        arguments = {
            "project_root": ROOT,
            "workspace_root": workspace,
            "state_path": root / "state" / "e02.json",
            "artifact_root": root / "artifacts",
        }
        port = TypeScriptE02ApiPort(**arguments)
        try:
            health = port.health()
            mcp = port.mcp_get(("mcp", "health"))
            tools = port.tools(namespace="mcp")
            runtime_receipt = port.execute(
                "e02_health",
                {},
                identity={
                    "tool_call_id": "smoke-e02-health",
                    "operation": "read",
                    "actor_id": "smoke",
                },
            )
            command_receipt = port.execute(
                "command",
                {"input": "/mcp tools"},
                identity={
                    "tool_call_id": "smoke-command-mcp",
                    "command_name": "mcp",
                    "operation": "read",
                    "actor_id": "smoke",
                },
            )
        finally:
            port.close()

        restored = TypeScriptE02ApiPort(**arguments)
        try:
            restored_health = restored.health()
        finally:
            restored.close()

        command_invocation = (
            command_receipt.get("receipt", {})
            .get("result", {})
            .get("output", {})
            .get("invocation", {})
        )
        report = {
            "ok": True,
            "canonical_entrypoint": health.get("canonical_entrypoint"),
            "canonical_owner": health.get("canonical_owner"),
            "mcp_status": mcp.get("status"),
            "mcp_state_owner": mcp.get("body", {}).get("state_owner"),
            "mcp_tool_count": tools.get("count"),
            "runtime_receipt_owner": runtime_receipt.get("receipt", {}).get("owner"),
            "command_name": command_invocation.get("commandName"),
            "command_status": command_invocation.get("status"),
            "command_permission": command_invocation.get("permission", {}).get("effect"),
            "restored_before_bootstrap": (
                restored_health.get("runtime", {}).get("restoredBeforeBootstrap")
            ),
            "python_permission_fallback": health.get("python_permission_fallback"),
            "python_mcp_fallback": health.get("python_mcp_fallback"),
            "python_command_fallback": health.get("python_command_fallback"),
        }
        expected = {
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "canonical_owner": "typescript",
            "mcp_status": 200,
            "mcp_state_owner": "McpRuntimeCoordinator",
            "runtime_receipt_owner": "typescript-e02-control",
            "command_name": "mcp",
            "command_status": "completed",
            "command_permission": "allow",
            "restored_before_bootstrap": True,
            "python_permission_fallback": False,
            "python_mcp_fallback": False,
            "python_command_fallback": False,
        }
        for key, value in expected.items():
            if report.get(key) != value:
                raise RuntimeError(f"E02 smoke mismatch for {key}: {report.get(key)!r} != {value!r}")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
