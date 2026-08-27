import type { JsonObject, JsonValue } from "../contracts.ts";

export type SkillSourceKind =
  | "managed"
  | "user"
  | "project"
  | "plugin"
  | "bundled"
  | "session";

export type SkillAvailability = "available" | "disabled" | "shadowed" | "invalid" | "missing";
export type SkillInvocationMode = "inline" | "fork" | "background";
export type SkillResourceKind = "markdown" | "text" | "json" | "yaml" | "image" | "binary";

export interface SkillSourceRoot {
  sourceId: string;
  kind: SkillSourceKind;
  rootPath: string;
  priority: number;
  enabled: boolean;
  recursive: boolean;
  followSymlinks: boolean;
  maximumDepth: number;
  includePatterns: string[];
  excludePatterns: string[];
  pluginId: string | null;
  revision: number;
  metadata: JsonObject;
}

export interface SkillSourceFile {
  sourceId: string;
  sourceKind: SkillSourceKind;
  sourcePriority: number;
  pluginId: string | null;
  skillDirectory: string;
  manifestPath: string;
  relativePath: string;
  realPath: string;
  sizeBytes: number;
  modifiedAtMs: number;
  inode: string | null;
  contentDigest: string;
  discoveredAt: string;
  metadata: JsonObject;
}

export interface SkillArgumentDescriptor {
  name: string;
  description: string;
  required: boolean;
  type: "string" | "number" | "integer" | "boolean" | "json" | "path" | "enum";
  defaultValue: JsonValue | null;
  enumValues: JsonValue[];
  minimum: number | null;
  maximum: number | null;
  pattern: string | null;
  positional: boolean;
  rest: boolean;
  sensitive: boolean;
}

export interface SkillToolScope {
  allowed: string[];
  denied: string[];
  namespaces: string[];
  mcpServers: string[];
  readOnly: boolean;
  inheritParent: boolean;
  maximumCalls: number | null;
  maximumParallel: number;
  requireApproval: string[];
}

export interface SkillContextPolicy {
  inheritConversation: boolean;
  inheritSystem: boolean;
  inheritMemory: boolean;
  includeWorkspaceInstructions: boolean;
  includeMcpInstructions: boolean;
  includeFiles: string[];
  excludeFiles: string[];
  maximumInputTokens: number;
  maximumResourceTokens: number;
  maximumOutputTokens: number;
  compactionStrategy: "reject" | "truncate_resources" | "compact_parent";
}

export interface SkillExecutionPolicy {
  mode: SkillInvocationMode;
  agent: string | null;
  model: string | null;
  maximumSkillDepth?: number;
  timeoutMs: number;
  maximumTurns: number;
  maximumCostMicros: number | null;
  workingDirectory: string | null;
  sandbox: "inherit" | "workspace_read" | "workspace_write" | "isolated";
  permissionMode: string | null;
  allowNetwork: boolean;
  persistTranscript: boolean;
  persistArtifacts: boolean;
}

export interface SkillResourceDescriptor {
  resourceId: string;
  path: string;
  kind: SkillResourceKind;
  required: boolean;
  maximumBytes: number;
  charset: string | null;
  mediaType: string | null;
  digest: string | null;
  metadata: JsonObject;
}

export interface SkillHookDescriptor {
  hookId: string;
  event: "before_invoke" | "after_invoke" | "before_tool" | "after_tool" | "on_failure";
  command: string | null;
  module: string | null;
  timeoutMs: number;
  failClosed: boolean;
  mutationAllowed: boolean;
  priority: number;
  metadata: JsonObject;
}

export interface SkillDescriptor {
  skillId: string;
  name: string;
  displayName: string;
  description: string;
  version: string;
  license: string | null;
  author: string | null;
  tags: string[];
  aliases: string[];
  source: SkillSourceFile;
  availability: SkillAvailability;
  disabledReason: string | null;
  body: string;
  bodyDigest: string;
  frontmatterDigest: string;
  descriptorDigest: string;
  arguments: SkillArgumentDescriptor[];
  toolScope: SkillToolScope;
  context: SkillContextPolicy;
  execution: SkillExecutionPolicy;
  resources: SkillResourceDescriptor[];
  hooks: SkillHookDescriptor[];
  environmentHandles: Record<string, string>;
  metadata: JsonObject;
  warnings: string[];
  parsedAt: string;
}

export interface SkillRevision {
  revisionId: string;
  revision: number;
  parentRevisionId: string | null;
  digest: string;
  skills: SkillDescriptor[];
  added: string[];
  updated: string[];
  removed: string[];
  shadowed: string[];
  invalid: string[];
  committedAt: string;
  metadata: JsonObject;
}

export interface SkillRegistrySnapshot {
  version: "zyra.skill-registry/v2";
  revision: number;
  headRevisionId: string | null;
  revisions: SkillRevision[];
  activeSkills: SkillDescriptor[];
  aliasIndex: Record<string, string>;
  sourceIndex: Record<string, string[]>;
  digest: string;
  capturedAt: string;
}

export interface SkillResourceContent {
  resourceId: string;
  skillId: string;
  path: string;
  kind: SkillResourceKind;
  mediaType: string | null;
  sizeBytes: number;
  digest: string;
  text: string | null;
  bytesBase64: string | null;
  tokenEstimate: number;
  loadedAt: string;
  metadata: JsonObject;
}

export interface SkillInvocationIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  invocationId: string;
}

export interface SkillInvocationRequest {
  identity: SkillInvocationIdentity;
  skillName: string;
  registryRevision: number;
  arguments: JsonObject;
  parentContext: JsonObject;
  parentToolScope: SkillToolScope;
  workspaceRoot: string;
  metadata: JsonObject;
}

export interface SkillInvocationPlan {
  invocationId: string;
  skillId: string;
  skillName: string;
  registryRevision: number;
  descriptorDigest: string;
  identity: SkillInvocationIdentity;
  arguments: JsonObject;
  renderedBody: string;
  resources: SkillResourceContent[];
  context: JsonObject;
  effectiveToolScope: SkillToolScope;
  execution: SkillExecutionPolicy;
  inputTokenEstimate: number;
  resourceTokenEstimate: number;
  createdAt: string;
  metadata: JsonObject;
}

export interface SkillInvocationResult {
  invocationId: string;
  skillId: string;
  status: "completed" | "failed" | "cancelled" | "background";
  output: JsonValue;
  artifacts: JsonObject[];
  toolCalls: number;
  inputTokens: number;
  outputTokens: number;
  costMicros: number;
  startedAt: string;
  completedAt: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface SkillReloadScan {
  scanId: string;
  baseRevision: number;
  roots: SkillSourceRoot[];
  sources: SkillSourceFile[];
  descriptors: SkillDescriptor[];
  errors: JsonObject[];
  addedPaths: string[];
  updatedPaths: string[];
  removedPaths: string[];
  unchangedPaths: string[];
  startedAt: string;
  completedAt: string;
  digest: string;
}
