import type { JsonObject, JsonValue } from "../contracts.ts";
import type { SkillSourceRoot } from "../skills/contracts-v2.ts";

export type PluginSourceKind = "managed" | "user" | "project" | "marketplace" | "session";
export type PluginStatus = "discovered" | "loading" | "active" | "disabled" | "failed" | "unloading";
export type PluginCapabilityKind = "skill" | "command" | "hook" | "agent" | "mcp" | "resource";

export interface PluginDependency {
  pluginId: string;
  versionRange: string;
  optional: boolean;
  capabilities: PluginCapabilityKind[];
}

export interface PluginMcpServerDescriptor {
  serverId: string;
  configPath: string | null;
  config: JsonObject;
  enabledByDefault: boolean;
  permissionScope: JsonObject;
}

export interface PluginCommandDescriptor {
  name: string;
  path: string;
  aliases: string[];
  hidden: boolean;
  permission: string | null;
  metadata: JsonObject;
}

export interface PluginHookDescriptor {
  hookId: string;
  event: string;
  path: string | null;
  command: string | null;
  priority: number;
  timeoutMs: number;
  failClosed: boolean;
  canMutate: boolean;
  toolPatterns: string[];
  metadata: JsonObject;
}

export interface PluginAgentDescriptor {
  agentId: string;
  path: string;
  description: string;
  model: string | null;
  toolScope: JsonObject;
  metadata: JsonObject;
}

export interface PluginManifest {
  manifestVersion: 1;
  pluginId: string;
  name: string;
  version: string;
  description: string;
  author: string | null;
  homepage: string | null;
  license: string | null;
  sourceKind: PluginSourceKind;
  rootPath: string;
  manifestPath: string;
  enabled: boolean;
  minimumZyraVersion: string | null;
  dependencies: PluginDependency[];
  skillRoots: SkillSourceRoot[];
  commands: PluginCommandDescriptor[];
  hooks: PluginHookDescriptor[];
  agents: PluginAgentDescriptor[];
  mcpServers: PluginMcpServerDescriptor[];
  environmentHandles: Record<string, string>;
  permissions: JsonObject;
  metadata: JsonObject;
  manifestDigest: string;
}

export interface PluginCacheEntry {
  cacheKey: string;
  pluginId: string;
  pluginVersion: string;
  manifestDigest: string;
  sourceDigest: string;
  revision: number;
  status: PluginStatus;
  compiledCapabilities: JsonObject;
  error: JsonObject | null;
  createdAt: string;
  updatedAt: string;
  lastUsedAt: string;
  useCount: number;
  metadata: JsonObject;
}

export interface PluginRevision {
  revisionId: string;
  revision: number;
  parentRevisionId: string | null;
  manifests: PluginManifest[];
  cacheEntries: PluginCacheEntry[];
  added: string[];
  updated: string[];
  removed: string[];
  failed: string[];
  digest: string;
  committedAt: string;
  metadata: JsonObject;
}

export interface PluginHookContext {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  toolName: string;
  namespace: string;
  serverId: string;
  operation: string;
  arguments: JsonObject;
  argumentsDigest: string;
  metadata: JsonObject;
}

export interface PluginHookResult {
  hookExecutionId: string;
  pluginId: string;
  hookId: string;
  event: string;
  effect: "continue" | "deny" | "ask";
  reason: string;
  originalArgumentsDigest: string;
  finalArgumentsDigest: string;
  arguments: JsonObject;
  mutated: boolean;
  durationMs: number;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface PluginRuntimeRecord {
  pluginId: string;
  revision: number;
  revisionId: string;
  manifest: PluginManifest;
  status: PluginStatus;
  loadedAt: string | null;
  unloadedAt: string | null;
  activeInvocations: number;
  pendingReloadRevision: number | null;
  capabilityDigest: string;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface PluginRuntimeSnapshot {
  version: "zyra.plugin-runtime/v2";
  revision: number;
  headRevisionId: string | null;
  records: PluginRuntimeRecord[];
  revisions: PluginRevision[];
  cache: PluginCacheEntry[];
  digest: string;
  capturedAt: string;
}

export interface PluginLoadResult {
  pluginId: string;
  revision: number;
  revisionId: string;
  status: PluginStatus;
  manifestDigest: string;
  capabilityDigest: string;
  skillRoots: SkillSourceRoot[];
  commands: PluginCommandDescriptor[];
  hooks: PluginHookDescriptor[];
  agents: PluginAgentDescriptor[];
  mcpServers: PluginMcpServerDescriptor[];
  metadata: JsonObject;
}

export interface PluginHookExecutorInput {
  manifest: PluginManifest;
  hook: PluginHookDescriptor;
  context: PluginHookContext;
  signal: AbortSignal;
}

export interface PluginHookExecutorOutput {
  effect?: "continue" | "deny" | "ask";
  reason?: string;
  arguments?: JsonObject;
  metadata?: JsonObject;
}

export type PluginHookExecutor = (
  input: PluginHookExecutorInput,
) => Promise<PluginHookExecutorOutput>;
