import { resolve } from "node:path";

import {
  parseMcpServerConfigs,
  TypeScriptMcpRuntime,
  type McpExecutionResult,
} from "../../../integrations/claude-mcp/src/index.ts";
import {
  asObject,
  asString,
  type JsonObject,
  type RuntimeRunInput,
  type ToolSpecContract,
} from "./contracts.ts";
import {
  isLegacySkillTool,
  parseSkillRoots,
  TypeScriptSkillRuntime,
  type SkillExecutionResult,
} from "./skills/index.ts";

export type CapabilityExecutionResult = McpExecutionResult | SkillExecutionResult;

export class TypeScriptCapabilityRuntime {
  private readonly mcp: TypeScriptMcpRuntime;
  private readonly skills: TypeScriptSkillRuntime;

  private constructor(mcp: TypeScriptMcpRuntime, skills: TypeScriptSkillRuntime) {
    this.mcp = mcp;
    this.skills = skills;
  }

  static async open(input: RuntimeRunInput): Promise<TypeScriptCapabilityRuntime> {
    const constraints = asObject(input.config.runtimeConstraints);
    const mcpConfigs = parseMcpServerConfigs(
      constraints.typescriptMcpServers ?? constraints.typescript_mcp_servers,
    );
    const workspaceRoot = asString(constraints.workspaceRoot ?? constraints.workspace_root);
    const projectRoot = asString(constraints.projectRoot ?? constraints.project_root) || workspaceRoot;
    const defaultSkillRoots = [
      resolve(workspaceRoot || process.cwd(), ".zyra", "skills"),
      resolve(projectRoot || process.cwd(), "skills"),
      resolve(workspaceRoot || process.cwd(), ".claude", "skills"),
      resolve(projectRoot || process.cwd(), ".claude", "commands"),
      resolve(projectRoot || process.cwd(), ".claude", "plugins"),
    ];
    const skillRoots = parseSkillRoots(
      constraints.typescriptSkillRoots ?? constraints.typescript_skill_roots,
      defaultSkillRoots,
    );
    const runtime = new TypeScriptCapabilityRuntime(
      new TypeScriptMcpRuntime(mcpConfigs),
      new TypeScriptSkillRuntime(skillRoots),
    );
    await Promise.all([runtime.mcp.open(), runtime.skills.open()]);
    return runtime;
  }

  mergeToolSpecs(existing: ToolSpecContract[]): ToolSpecContract[] {
    const retained = existing.filter((tool) => !isLegacyMcpTool(tool) && !isLegacySkillTool(tool));
    const local = [
      ...this.mcp.toolSpecs(),
      ...this.mcp.catalogToolSpecs(),
      ...this.skills.toolSpecs(),
    ] as ToolSpecContract[];
    const byName = new Map<string, ToolSpecContract>();
    for (const tool of [...retained, ...local]) {
      byName.set(tool.name, tool);
    }
    return [...byName.values()].sort((left, right) => left.name.localeCompare(right.name));
  }

  owns(toolName: string): boolean {
    return this.mcp.owns(toolName) || this.skills.owns(toolName);
  }

  owner(toolName: string): string {
    return this.mcp.owns(toolName) ? "typescript-mcp" : "typescript-skill";
  }

  async execute(toolName: string, argumentsValue: JsonObject): Promise<CapabilityExecutionResult> {
    if (this.mcp.owns(toolName)) {
      return this.mcp.execute(toolName, argumentsValue);
    }
    return this.skills.execute(toolName, argumentsValue);
  }

  snapshot(): JsonObject {
    return {
      version: "zyra.typescript-capability-runtime.v1",
      canonical_owner: "typescript",
      mcp: this.mcp.snapshot(),
      skills: this.skills.snapshot(),
      python_runtime_fallback: false,
    };
  }

  async close(): Promise<void> {
    await this.mcp.close();
  }
}

function isLegacyMcpTool(tool: ToolSpecContract): boolean {
  const provenance = asObject(tool.execution_provenance);
  return tool.name.startsWith("mcp__")
    || tool.name.startsWith("mcp_")
    || asString(provenance.namespace) === "mcp"
    || tool.source.includes("mcp-projection")
    || tool.source.includes("python-mcp");
}
