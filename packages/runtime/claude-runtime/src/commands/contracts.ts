import type { JsonObject, JsonValue } from "../contracts.ts";

export type CommandSourceKind = "managed" | "user" | "project" | "plugin" | "builtin" | "session";
export type CommandHandlerKind = "builtin" | "local" | "skill" | "mcp_prompt" | "plugin" | "control";

export interface CommandArgument {
  name: string;
  description: string;
  required: boolean;
  positional: boolean;
  rest: boolean;
  type: "string" | "number" | "integer" | "boolean" | "json" | "path" | "enum";
  defaultValue: JsonValue | null;
  enumValues: JsonValue[];
}

export interface CommandOption {
  name: string;
  short: string | null;
  description: string;
  type: "string" | "number" | "integer" | "boolean" | "json" | "path" | "enum";
  required: boolean;
  repeatable: boolean;
  defaultValue: JsonValue | null;
  enumValues: JsonValue[];
}

export interface CommandHandlerDescriptor {
  kind: CommandHandlerKind;
  handlerId: string;
  modulePath: string | null;
  skillName: string | null;
  mcpServerId: string | null;
  mcpPromptName: string | null;
  pluginId: string | null;
  controlCommand: string | null;
  metadata: JsonObject;
}

export interface CommandPermissionDescriptor {
  operation: string;
  risk: "low" | "medium" | "high" | "critical";
  askInInteractive: boolean;
  denyInSealed: boolean;
  workspaceMutation: boolean;
  networkAccess: boolean;
  processExecution: boolean;
  scope: JsonObject;
}

export interface CommandDescriptor {
  commandId: string;
  name: string;
  aliases: string[];
  displayName: string;
  description: string;
  usage: string;
  examples: string[];
  category: string;
  sourceKind: CommandSourceKind;
  sourceId: string;
  sourcePath: string | null;
  sourcePriority: number;
  hidden: boolean;
  enabled: boolean;
  arguments: CommandArgument[];
  options: CommandOption[];
  handler: CommandHandlerDescriptor;
  permission: CommandPermissionDescriptor;
  body: string;
  descriptorDigest: string;
  metadata: JsonObject;
}

export interface CommandRevision {
  revisionId: string;
  revision: number;
  parentRevisionId: string | null;
  commands: CommandDescriptor[];
  added: string[];
  updated: string[];
  removed: string[];
  shadowed: string[];
  digest: string;
  committedAt: string;
  metadata: JsonObject;
}

export interface CommandInvocationIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  commandCallId: string;
}

export interface CommandInvocationRequest {
  identity: CommandInvocationIdentity;
  input: string;
  commandName: string | null;
  arguments: JsonValue[];
  options: JsonObject;
  registryRevision: number;
  workspaceRoot: string;
  interactive: boolean;
  sealedAutonomous: boolean;
  metadata: JsonObject;
}

export interface CommandPermissionDecision {
  effect: "allow" | "deny" | "ask";
  decisionId: string;
  reasonCode: string;
  reason: string;
  requestDigest: string;
  continuationId: string | null;
  replanRequired: boolean;
  recoveryInput: JsonObject | null;
  metadata: JsonObject;
}

export interface CommandInvocationResult {
  invocationId: string;
  commandId: string;
  commandName: string;
  registryRevision: number;
  status: "completed" | "denied" | "pending_approval" | "failed" | "cancelled";
  output: JsonValue;
  artifacts: JsonObject[];
  permission: CommandPermissionDecision;
  startedAt: string;
  completedAt: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface CommandRegistrySnapshot {
  version: "zyra.command-registry/v2";
  revision: number;
  headRevisionId: string | null;
  revisions: CommandRevision[];
  active: CommandDescriptor[];
  aliasIndex: Record<string, string>;
  digest: string;
  capturedAt: string;
}
