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
import {
  isLegacyAgentTool,
  TypeScriptAgentRuntime,
  type AgentCapabilityResult,
  type AgentExecutionContext,
} from "./agents/index.ts";

export type CapabilityExecutionResult =
  | McpExecutionResult
  | SkillExecutionResult
  | AgentCapabilityResult;

export class TypeScriptCapabilityRuntime {
  private readonly mcp: TypeScriptMcpRuntime;
  private readonly skills: TypeScriptSkillRuntime;
  private readonly agents: TypeScriptAgentRuntime;

  private constructor(
    mcp: TypeScriptMcpRuntime,
    skills: TypeScriptSkillRuntime,
    agents: TypeScriptAgentRuntime,
  ) {
    this.mcp = mcp;
    this.skills = skills;
    this.agents = agents;
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
      new TypeScriptAgentRuntime(input),
    );
    await Promise.all([runtime.mcp.open(), runtime.skills.open()]);
    return runtime;
  }

  mergeToolSpecs(existing: ToolSpecContract[]): ToolSpecContract[] {
    const retained = existing.filter(
      (tool) => !isLegacyMcpTool(tool) && !isLegacySkillTool(tool) && !isLegacyAgentTool(tool),
    );
    const local = [
      ...this.mcp.toolSpecs(),
      ...this.mcp.catalogToolSpecs(),
      ...this.skills.toolSpecs(),
      ...this.agents.toolSpecs(),
    ] as ToolSpecContract[];
    const byName = new Map<string, ToolSpecContract>();
    for (const tool of [...retained, ...local]) {
      byName.set(tool.name, tool);
    }
    return [...byName.values()].sort((left, right) => left.name.localeCompare(right.name));
  }

  owns(toolName: string): boolean {
    return this.mcp.owns(toolName) || this.skills.owns(toolName) || this.agents.owns(toolName);
  }

  owner(toolName: string): string {
    if (this.mcp.owns(toolName)) {
      return "typescript-mcp";
    }
    return this.agents.owns(toolName) ? "typescript-agent" : "typescript-skill";
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    agentContext?: AgentExecutionContext,
  ): Promise<CapabilityExecutionResult> {
    if (this.mcp.owns(toolName)) {
      return this.mcp.execute(toolName, argumentsValue);
    }
    if (this.agents.owns(toolName)) {
      if (!agentContext) {
        throw new Error("AgentTool execution requires a child QueryEngine context");
      }
      return this.agents.execute(toolName, argumentsValue, agentContext);
    }
    return this.skills.execute(toolName, argumentsValue);
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    await this.agents.drainBackground(context);
  }

  snapshot(): JsonObject {
    return {
      version: "zyra.typescript-capability-runtime.v1",
      canonical_owner: "typescript",
      mcp: this.mcp.snapshot(),
      skills: this.skills.snapshot(),
      agents: this.agents.snapshot(),
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
