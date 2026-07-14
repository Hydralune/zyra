import {
  asBoolean,
  asObject,
  asString,
  positiveInteger,
  type JsonObject,
  type RuntimeRunInput,
} from "../contracts.ts";
import {
  DEFAULT_AGENT_BUDGET,
  type AgentBudget,
  type AgentDefinition,
} from "./contracts.ts";
import { canonicalDigest } from "./memory.ts";

const DEFAULT_DEFINITIONS: JsonObject[] = [
  {
    name: "general-purpose",
    description: "General bounded child CodeWorker.",
    tools: [],
    denied_tools: [],
    permission_mode: "inherit",
    background: false,
    isolation: "workspace",
    memory_scope: "task",
  },
  {
    name: "explore",
    description: "Read-oriented repository exploration child.",
    tools: [],
    denied_tools: ["file_write", "file_edit"],
    permission_mode: "inherit",
    background: true,
    isolation: "workspace",
    memory_scope: "task",
  },
  {
    name: "verify",
    description: "Bounded verification child.",
    tools: [],
    denied_tools: [],
    permission_mode: "inherit",
    background: false,
    isolation: "workspace",
    memory_scope: "task",
  },
];

export class AgentDefinitionRegistry {
  private readonly definitions = new Map<string, AgentDefinition>();
  readonly generation: string;

  constructor(values: JsonObject[]) {
    for (const value of values) {
      const definition = normalizeDefinition(value);
      if (this.definitions.has(definition.name)) {
        throw new Error("duplicate agent definition: " + definition.name);
      }
      this.definitions.set(definition.name, definition);
    }
    this.generation = canonicalDigest(
      [...this.definitions.values()].map((item) => item.digest),
    );
  }

  static fromInput(input: RuntimeRunInput): AgentDefinitionRegistry {
    const constraints = asObject(input.config.runtimeConstraints);
    const configured = constraints.typescriptAgentDefinitions
      ?? constraints.typescript_agent_definitions;
    const values: JsonObject[] = [];
    if (Array.isArray(configured)) {
      values.push(...configured.map((item) => asObject(item)));
    } else {
      const record = asObject(configured);
      for (const [name, raw] of Object.entries(record)) {
        values.push({ name, ...asObject(raw) });
      }
    }
    return new AgentDefinitionRegistry(values.length > 0 ? values : DEFAULT_DEFINITIONS);
  }

  get(name: string): AgentDefinition {
    const selected = this.definitions.get(name);
    if (!selected) {
      throw new Error("unknown agent definition: " + name);
    }
    return selected;
  }

  list(): AgentDefinition[] {
    return [...this.definitions.values()].sort((left, right) => left.name.localeCompare(right.name));
  }

  snapshot(): JsonObject {
    return {
      generation: this.generation,
      definitions: this.list().map((item) => ({
        name: item.name,
        description: item.description,
        version: item.version,
        tools: item.tools,
        denied_tools: item.deniedTools,
        permission_mode: item.permissionMode,
        mcp_servers: item.mcpServers,
        skills: item.skills,
        model: item.model,
        effort: item.effort,
        background: item.background,
        isolation: item.isolation,
        memory_scope: item.memoryScope,
        budget: budgetPayload(item.budget),
        digest: item.digest,
      })),
    };
  }
}

function normalizeDefinition(raw: JsonObject): AgentDefinition {
  const name = asString(raw.name ?? raw.agent_type).trim();
  const description = asString(raw.description).trim();
  if (!name || /\s/.test(name)) {
    throw new Error("agent definition name must be a non-empty token");
  }
  if (!description) {
    throw new Error("agent definition requires a description: " + name);
  }
  const tools = tokens(raw.tools);
  const deniedTools = tokens(raw.denied_tools ?? raw.disallowed_tools);
  const overlap = tools.filter((tool) => deniedTools.includes(tool));
  if (overlap.length > 0) {
    throw new Error("agent allow/deny overlap: " + overlap.join(","));
  }
  const isolation = asString(raw.isolation, "workspace");
  if (!["none", "workspace", "worktree", "sandbox", "remote"].includes(isolation)) {
    throw new Error("invalid agent isolation kind: " + isolation);
  }
  const budget = normalizeBudget(asObject(raw.budget));
  const payload = {
    name,
    description,
    version: asString(raw.version, "1"),
    tools,
    denied_tools: deniedTools,
    permission_mode: asString(raw.permission_mode ?? raw.permissionMode, "inherit"),
    mcp_servers: tokens(raw.mcp_servers ?? raw.mcpServers),
    skills: tokens(raw.skills),
    model: asString(raw.model, "inherit"),
    effort: asString(raw.effort, "inherit"),
    background: asBoolean(raw.background),
    isolation,
    memory_scope: asString(raw.memory_scope ?? raw.memory, "task"),
    budget: budgetPayload(budget),
    metadata: asObject(raw.metadata),
  };
  return {
    name,
    description,
    version: payload.version,
    tools,
    deniedTools,
    permissionMode: payload.permission_mode,
    mcpServers: payload.mcp_servers,
    skills: payload.skills,
    model: payload.model,
    effort: payload.effort,
    background: payload.background,
    isolation: isolation as AgentDefinition["isolation"],
    memoryScope: payload.memory_scope,
    budget,
    metadata: payload.metadata,
    digest: canonicalDigest(payload),
  };
}

export function normalizeBudget(raw: JsonObject): AgentBudget {
  return {
    maxTurns: positiveInteger(raw.maxTurns ?? raw.max_turns, DEFAULT_AGENT_BUDGET.maxTurns),
    maxToolCalls: positiveInteger(raw.maxToolCalls ?? raw.max_tool_calls, DEFAULT_AGENT_BUDGET.maxToolCalls),
    maxInputTokens: positiveInteger(raw.maxInputTokens ?? raw.max_input_tokens, DEFAULT_AGENT_BUDGET.maxInputTokens),
    maxOutputTokens: positiveInteger(raw.maxOutputTokens ?? raw.max_output_tokens, DEFAULT_AGENT_BUDGET.maxOutputTokens),
    maxResultChars: positiveInteger(raw.maxResultChars ?? raw.max_result_chars, DEFAULT_AGENT_BUDGET.maxResultChars),
    maxWallTimeMs: positiveInteger(raw.maxWallTimeMs ?? raw.max_wall_time_ms, DEFAULT_AGENT_BUDGET.maxWallTimeMs),
    maxChildren: positiveInteger(raw.maxChildren ?? raw.max_children, DEFAULT_AGENT_BUDGET.maxChildren),
    maxDepth: positiveInteger(raw.maxDepth ?? raw.max_depth, DEFAULT_AGENT_BUDGET.maxDepth),
  };
}

export function budgetPayload(value: AgentBudget): JsonObject {
  return {
    max_turns: value.maxTurns,
    max_tool_calls: value.maxToolCalls,
    max_input_tokens: value.maxInputTokens,
    max_output_tokens: value.maxOutputTokens,
    max_result_chars: value.maxResultChars,
    max_wall_time_ms: value.maxWallTimeMs,
    max_children: value.maxChildren,
    max_depth: value.maxDepth,
  };
}

function tokens(value: unknown): string[] {
  return Array.isArray(value)
    ? [...new Set(value.map((item) => String(item).trim()).filter(Boolean))]
    : [];
}
