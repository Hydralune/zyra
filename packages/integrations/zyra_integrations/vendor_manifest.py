from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VendorModule:
    name: str
    purpose: str
    paths: tuple[str, ...]
    target_boundary: str


@dataclass(frozen=True, slots=True)
class VendorSnapshot:
    name: str
    root: Path
    modules: tuple[VendorModule, ...] = field(default_factory=tuple)

    def missing_paths(self) -> list[Path]:
        missing: list[Path] = []
        for module in self.modules:
            for relative_path in module.paths:
                candidate = self.root / relative_path
                if not candidate.exists():
                    missing.append(candidate)
        return missing


def claude_code_best_snapshot(project_root: Path) -> VendorSnapshot:
    vendor_root = project_root / "vendor" / "claude-code-best"
    return VendorSnapshot(
        name="claude-code-best",
        root=vendor_root,
        modules=(
            VendorModule(
                name="query-engine",
                purpose="Session lifecycle, query loop, streaming model/tool control.",
                paths=("src/QueryEngine.ts", "src/query.ts"),
                target_boundary="zyra CodeWorkerRuntime sidecar",
            ),
            VendorModule(
                name="tool-runtime",
                purpose="Tool registry, tool schema, tool use context, and tool result budget.",
                paths=("src/Tool.ts", "src/tools.ts", "src/tools/BashTool", "src/tools/FileReadTool"),
                target_boundary="zyra ToolRegistry and CodeWorkerRuntime",
            ),
            VendorModule(
                name="permission-runtime",
                purpose="Allow/deny/ask handling, permission context, and approval handlers.",
                paths=("src/hooks/toolPermission",),
                target_boundary="zyra ToolPermissionRuntime",
            ),
            VendorModule(
                name="compact-runtime",
                purpose="Auto compact, reactive compact, micro compact, and restore hooks.",
                paths=("src/services/compact",),
                target_boundary="zyra Memory and CodeWorkerRuntime context management",
            ),
            VendorModule(
                name="mcp-runtime",
                purpose="MCP client, connection manager, auth, resources, tools, and prompts.",
                paths=("src/services/mcp",),
                target_boundary="zyra MCP integration adapter",
            ),
            VendorModule(
                name="commands-runtime",
                purpose="Slash command registry, safe command filtering, skills/commands composition.",
                paths=("src/commands.ts", "src/commands/compact", "src/commands/mcp", "src/commands/skills"),
                target_boundary="zyra ControlCommand and command panel",
            ),
            VendorModule(
                name="skill-runtime",
                purpose="SkillTool, Markdown skills, allowed tools, and dynamic skills.",
                paths=("src/tools/SkillTool", "src/skills"),
                target_boundary="zyra SkillRuntime",
            ),
            VendorModule(
                name="subagent-runtime",
                purpose="AgentTool, forked subagents, built-in verification agent, and agent memory.",
                paths=("src/tools/AgentTool",),
                target_boundary="zyra SubagentRuntime",
            ),
        ),
    )


def browser_use_snapshot(project_root: Path) -> VendorSnapshot:
    vendor_root = project_root / "vendor" / "browser-use"
    return VendorSnapshot(
        name="browser-use",
        root=vendor_root,
        modules=(
            VendorModule(
                name="browser-agent",
                purpose="Browser task agent loop, prompts, message views, and judge hooks.",
                paths=("browser_use/agent",),
                target_boundary="zyra BrowserWorker",
            ),
            VendorModule(
                name="browser-control",
                purpose="Browser session control, browser state, DOM extraction, and action execution.",
                paths=("browser_use/browser", "browser_use/controller", "browser_use/dom"),
                target_boundary="zyra BrowserWorker runtime adapter",
            ),
            VendorModule(
                name="browser-artifacts",
                purpose="Screenshots, filesystem handling, telemetry, and token accounting for browser traces.",
                paths=(
                    "browser_use/screenshots",
                    "browser_use/filesystem",
                    "browser_use/telemetry",
                    "browser_use/tokens",
                ),
                target_boundary="zyra ArtifactStore and event trace",
            ),
            VendorModule(
                name="browser-tools-skills",
                purpose="Browser-use tools, MCP hooks, and browser skills.",
                paths=("browser_use/tools", "browser_use/mcp", "browser_use/skills"),
                target_boundary="zyra ToolRegistry and SkillRuntime",
            ),
            VendorModule(
                name="browser-sandbox",
                purpose="Sandbox integration and remote/browser execution isolation.",
                paths=("browser_use/sandbox",),
                target_boundary="zyra Worker isolation and resource scheduler",
            ),
        ),
    )


def validate_vendor_snapshot(snapshot: VendorSnapshot) -> None:
    if not snapshot.root.exists():
        raise AssertionError(f"Vendor root does not exist: {snapshot.root}")
    missing = snapshot.missing_paths()
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise AssertionError(f"Vendor snapshot {snapshot.name} is incomplete:\n{formatted}")
