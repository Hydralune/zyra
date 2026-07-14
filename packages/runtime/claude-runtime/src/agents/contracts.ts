import type {
  ArtifactReceipt,
  JsonObject,
  RuntimeHost,
  RuntimeRunInput,
  RuntimeRunResult,
  ToolSpecContract,
} from "../contracts.ts";

export type AgentTaskStatus =
  | "created"
  | "validating"
  | "ready"
  | "dispatched"
  | "running"
  | "waiting"
  | "resuming"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentBudget {
  maxTurns: number;
  maxToolCalls: number;
  maxInputTokens: number;
  maxOutputTokens: number;
  maxResultChars: number;
  maxWallTimeMs: number;
  maxChildren: number;
  maxDepth: number;
}

export interface AgentDefinition {
  name: string;
  description: string;
  version: string;
  tools: string[];
  deniedTools: string[];
  permissionMode: string;
  mcpServers: string[];
  skills: string[];
  model: string;
  effort: string;
  background: boolean;
  isolation: "none" | "workspace" | "worktree" | "sandbox" | "remote";
  memoryScope: string;
  budget: AgentBudget;
  metadata: JsonObject;
  digest: string;
}

export interface AgentScope {
  parentTools: string[];
  childTools: string[];
  deniedTools: string[];
  permissionMode: string;
  permissionCeilingDigest: string;
  toolCatalogDigest: string;
  depth: number;
  lineage: string[];
  cycleKey: string;
  budget: AgentBudget;
  digest: string;
}

export interface AgentContextSnapshot {
  snapshotId: string;
  parentSessionId: string;
  parentTaskId: string;
  parentWorkerRequestId: string;
  mode: "isolated" | "fork" | "resume";
  messageRefs: string[];
  artifactRefs: string[];
  evidenceRefs: string[];
  ancestry: string[];
  depth: number;
  digest: string;
  metadata: JsonObject;
}

export interface AgentTask {
  taskId: string;
  parentTaskId: string;
  parentSessionId: string;
  childSessionId: string;
  runId: string;
  definition: AgentDefinition;
  promptDigest: string;
  executionMode: "foreground" | "background";
  status: AgentTaskStatus;
  revision: number;
  attempt: number;
  sequence: number;
  scope: AgentScope;
  context: AgentContextSnapshot;
  idempotencyKey: string;
  resultDigest: string;
  result: AgentToolResult | null;
  error: string;
  createdAt: string;
  updatedAt: string;
}

export interface AgentToolResult {
  ok: boolean;
  taskId: string;
  status: AgentTaskStatus;
  summary: string;
  artifacts: ArtifactReceipt[];
  usage: JsonObject;
  error: string;
  metadata: JsonObject;
}

export interface AgentExecutionContext {
  parentInput: RuntimeRunInput;
  host: RuntimeHost;
  runChild(input: RuntimeRunInput): Promise<RuntimeRunResult>;
}

export interface AgentCapabilityResult {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
}

export interface AgentToolSurface {
  toolSpecs(): ToolSpecContract[];
  owns(toolName: string): boolean;
  execute(
    toolName: string,
    argumentsValue: JsonObject,
    context: AgentExecutionContext,
  ): Promise<AgentCapabilityResult>;
  snapshot(): JsonObject;
  drainBackground(context: AgentExecutionContext): Promise<void>;
}

export const DEFAULT_AGENT_BUDGET: AgentBudget = {
  maxTurns: 12,
  maxToolCalls: 48,
  maxInputTokens: 64_000,
  maxOutputTokens: 16_000,
  maxResultChars: 120_000,
  maxWallTimeMs: 900_000,
  maxChildren: 4,
  maxDepth: 3,
};
