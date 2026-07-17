import {
  asObject,
  asString,
  type JsonObject,
  type RuntimeRunInput,
} from "../contracts.ts";
import type { AgentBudget, AgentDefinition, AgentScope } from "./contracts.ts";
import { normalizeBudget } from "./definitions.ts";
import { canonicalDigest } from "./memory.ts";

const PERMISSION_RANK: Record<string, number> = {
  deny: 0,
  plan: 1,
  default: 2,
  accept_edits: 3,
  auto: 4,
  bypass: 5,
  sealed: 2,
};

export function deriveAgentScope(
  input: RuntimeRunInput,
  definition: AgentDefinition,
  argumentsValue: JsonObject,
): AgentScope {
  const parentTools = input.tools.map((tool) => tool.name);
  const requested = tokens(
    argumentsValue.tools ?? argumentsValue.requested_tools,
  );
  const declared = definition.tools.length > 0 ? definition.tools : parentTools;
  const candidates = requested.length > 0 ? requested : declared;
  const denied = new Set(definition.deniedTools);
  const childTools = candidates.filter(
    (tool) => parentTools.includes(tool) && !denied.has(tool),
  );
  const missing = candidates.filter((tool) => !parentTools.includes(tool));
  if (missing.length > 0) {
    throw new Error(
      "agent tool scope expands parent authority: " + missing.join(","),
    );
  }
  const required = tokens(argumentsValue.required_tools);
  const unavailable = required.filter((tool) => !childTools.includes(tool));
  if (unavailable.length > 0) {
    throw new Error(
      "required child tools are unavailable: " + unavailable.join(","),
    );
  }
  const policy = asObject(input.config.permissionPolicy);
  const parentMode = normalizePermissionMode(
    policy.mode ?? policy.permission_mode ?? asObject(policy.mode).mode,
    "default",
  );
  const requestedMode = normalizePermissionMode(
    argumentsValue.permission_mode ?? definition.permissionMode,
    parentMode,
  );
  if (
    (PERMISSION_RANK[requestedMode] ?? 99) > (PERMISSION_RANK[parentMode] ?? 0)
  ) {
    throw new Error("agent permission mode expands parent authority");
  }
  const metadata = asObject(input.metadata);
  const constraints = asObject(input.config.runtimeConstraints);
  const depth =
    Number(
      metadata.agent_depth ??
        constraints.agentDepth ??
        constraints.agent_depth ??
        0,
    ) + 1;
  const lineage = tokens(
    metadata.agent_lineage ??
      constraints.agentLineage ??
      constraints.agent_lineage,
  );
  const cycleKey = canonicalDigest([
    definition.digest,
    canonicalDigest(asString(argumentsValue.prompt)),
  ]);
  if (lineage.includes(cycleKey)) {
    throw new Error("agent cycle detected");
  }
  const parentBudget = normalizeBudget(
    asObject(constraints.agentBudget ?? constraints.agent_budget),
  );
  const requestedBudget = normalizeBudget(asObject(argumentsValue.budget));
  const budget = narrowBudget(
    narrowBudget(parentBudget, definition.budget),
    requestedBudget,
  );
  if (depth > budget.maxDepth) {
    throw new Error("agent depth exceeds inherited budget");
  }
  const toolCatalogDigest = canonicalDigest(
    input.tools.map((tool) => [
      tool.name,
      tool.execution_provenance ?? {},
      tool.input_schema,
    ]),
  );
  const permissionCeilingDigest = canonicalDigest({
    mode: parentMode,
    policy_digest: policy.policy_digest ?? policy.digest ?? "",
    session_id: input.sessionId,
  });
  const payload = {
    parent_tools: parentTools,
    child_tools: childTools,
    denied_tools: [...denied],
    permission_mode: requestedMode,
    permission_ceiling_digest: permissionCeilingDigest,
    tool_catalog_digest: toolCatalogDigest,
    depth,
    lineage: [...lineage, cycleKey],
    cycle_key: cycleKey,
    budget,
  };
  return {
    parentTools,
    childTools,
    deniedTools: [...denied],
    permissionMode: requestedMode,
    permissionCeilingDigest,
    toolCatalogDigest,
    depth,
    lineage: payload.lineage,
    cycleKey,
    budget,
    digest: canonicalDigest(payload),
  };
}

export function narrowBudget(
  parent: AgentBudget,
  child: AgentBudget,
): AgentBudget {
  return {
    maxTurns: Math.min(parent.maxTurns, child.maxTurns),
    maxToolCalls: Math.min(parent.maxToolCalls, child.maxToolCalls),
    maxInputTokens: Math.min(parent.maxInputTokens, child.maxInputTokens),
    maxOutputTokens: Math.min(parent.maxOutputTokens, child.maxOutputTokens),
    maxResultChars: Math.min(parent.maxResultChars, child.maxResultChars),
    maxWallTimeMs: Math.min(parent.maxWallTimeMs, child.maxWallTimeMs),
    maxChildren: Math.min(parent.maxChildren, child.maxChildren),
    maxDepth: Math.min(parent.maxDepth, child.maxDepth),
  };
}

function normalizePermissionMode(value: unknown, fallback: string): string {
  const raw = asString(value, fallback);
  const aliases: Record<string, string> = {
    acceptEdits: "accept_edits",
    dontAsk: "default",
    bypassPermissions: "bypass",
    inherit: fallback,
  };
  return aliases[raw] ?? raw;
}

function tokens(value: unknown): string[] {
  return Array.isArray(value)
    ? [...new Set(value.map((item) => String(item).trim()).filter(Boolean))]
    : [];
}
