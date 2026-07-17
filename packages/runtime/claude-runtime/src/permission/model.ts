import { isAbsolute, relative, resolve } from "node:path";

import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  deterministicId,
  digest,
  normalizeIdentifier,
  normalizeName,
  optionalObject,
  optionalString,
} from "../e02/canonical.ts";
import type {
  PermissionRequestContext,
  PermissionScope,
  PermissionScopeKind,
} from "../e02/contracts.ts";

export interface PermissionIdentityInput {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision?: number;
  workerRequestId?: string;
  toolCallId: string;
  toolName: string;
  namespace?: string;
  serverId?: string;
  commandName?: string;
  resourceUri?: string;
  operation?: string;
  workspaceRoot?: string;
  arguments?: JsonObject;
  metadata?: JsonObject;
}

export interface PermissionIdentityRecord extends JsonObject {
  identityId: string;
  context: PermissionRequestContext;
  argumentsDigest: string;
  requestFingerprint: string;
  workspaceBinding: JsonObject;
  capabilityBinding: JsonObject;
}

const TOOL_ALIASES: Record<string, string> = {
  task: "Agent",
  killshell: "TaskStop",
  todowrite: "TaskCreate",
  webfetch: "WebFetch",
  websearch: "WebSearch",
  bash: "shell",
  read: "file_read",
  write: "file_write",
  edit: "file_edit",
};

export class PermissionIdentity {
  static create(input: PermissionIdentityInput): PermissionIdentityRecord {
    const runId = required(input.runId, "runId");
    const taskId = required(input.taskId, "taskId");
    const sessionId = required(input.sessionId, "sessionId");
    const toolCallId = required(input.toolCallId, "toolCallId");
    const workerRequestId = input.workerRequestId || toolCallId;
    const toolName = normalizeToolName(input.toolName);
    const namespace = normalizeNamespace(input.namespace, toolName);
    const serverId = input.serverId ? normalizeIdentifier(input.serverId, "serverId") : "";
    const commandName = normalizeOptionalName(input.commandName);
    const resourceUri = normalizeResourceUri(input.resourceUri);
    const operation = normalizeOperation(input.operation, toolName);
    const workspaceRoot = normalizeWorkspace(input.workspaceRoot);
    const argumentsValue = canonicalize(input.arguments ?? {}) as JsonObject;
    const metadata = canonicalize(input.metadata ?? {}) as JsonObject;
    const sessionRevision = normalizeRevision(input.sessionRevision);
    const context: PermissionRequestContext = {
      runId,
      taskId,
      sessionId,
      sessionRevision,
      workerRequestId,
      toolCallId,
      toolName,
      namespace,
      serverId,
      commandName,
      resourceUri,
      operation,
      workspaceRoot,
      arguments: argumentsValue,
      metadata,
    };
    const argumentsDigest = digest(argumentsValue);
    const workspaceBinding = workspacePathBinding(workspaceRoot, argumentsValue);
    const capabilityBinding: JsonObject = {
      namespace,
      tool_name: toolName,
      server_id: serverId,
      command_name: commandName,
      resource_uri: resourceUri,
      operation,
      schema_digest: optionalString(metadata.schema_digest) ?? "",
      capability_revision: numericMetadata(metadata.capability_revision),
      plugin_revision: numericMetadata(metadata.plugin_revision),
      skill_revision: numericMetadata(metadata.skill_revision),
    };
    const requestFingerprint = digest({
      runId,
      taskId,
      sessionId,
      sessionRevision,
      workerRequestId,
      toolCallId,
      argumentsDigest,
      workspaceBinding,
      capabilityBinding,
    });
    return {
      identityId: deterministicId("permission-identity", requestFingerprint, 40),
      context,
      argumentsDigest,
      requestFingerprint,
      workspaceBinding,
      capabilityBinding,
    };
  }

  static rebindArguments(identity: PermissionIdentityRecord, argumentsValue: JsonObject): PermissionIdentityRecord {
    return PermissionIdentity.create({
      ...identity.context,
      arguments: argumentsValue,
    });
  }

  static sameRequest(left: PermissionIdentityRecord, right: PermissionIdentityRecord): boolean {
    return left.requestFingerprint === right.requestFingerprint;
  }

  static assertResponseBinding(
    identity: PermissionIdentityRecord,
    response: {
      runId: string;
      sessionId: string;
      sessionRevision: number;
      workerRequestId: string;
      toolCallId: string;
    },
  ): void {
    const context = identity.context;
    const mismatches: string[] = [];
    if (response.runId !== context.runId) mismatches.push("run_id");
    if (response.sessionId !== context.sessionId) mismatches.push("session_id");
    if (response.sessionRevision !== context.sessionRevision) mismatches.push("session_revision");
    if (response.workerRequestId !== context.workerRequestId) mismatches.push("worker_request_id");
    if (response.toolCallId !== context.toolCallId) mismatches.push("tool_call_id");
    if (mismatches.length) throw new Error(`permission response binding mismatch: ${mismatches.join(",")}`);
  }
}

export function normalizeToolName(value: string): string {
  const raw = normalizeName(value, 180);
  return TOOL_ALIASES[raw.toLowerCase()] ?? raw;
}

export function normalizeNamespace(value: string | undefined, toolName: string): string {
  if (value) return normalizeIdentifier(value, "namespace");
  if (toolName.startsWith("mcp__") || toolName.startsWith("mcp_")) return "mcp";
  if (["skill", "list_skills", "read_skill_resource"].includes(toolName)) return "skill";
  if (["command", "list_commands"].includes(toolName)) return "command";
  if (["plugin_command", "list_plugins"].includes(toolName)) return "plugin";
  if (["Agent", "Task", "agent_status", "agent_cancel", "agent_resume"].includes(toolName)) return "agent";
  return "builtin";
}

export function normalizeOperation(value: string | undefined, toolName: string): string {
  if (value) return normalizeIdentifier(value, "operation");
  if (["file_read", "list_skills", "list_commands", "list_plugins"].includes(toolName)) return "read";
  if (["file_write", "file_edit", "notebook_edit"].includes(toolName)) return "write";
  if (/^(?:read|list|get|search|inspect|status)/i.test(toolName)) return "read";
  if (/^(?:write|edit|create|update|delete|remove|move|rename)/i.test(toolName)) return "write";
  return "execute";
}

export function defaultScope(kind: PermissionScopeKind = "tool"): PermissionScope {
  return {
    kind,
    toolPattern: "*",
    namespacePattern: "*",
    serverPattern: "*",
    commandPattern: "*",
    resourcePattern: "*",
    operationPattern: "*",
    workspacePattern: "*",
    sessionPattern: "*",
    argumentPattern: "*",
  };
}

export function scopeSpecificity(scope: PermissionScope): number {
  const weights: Array<[keyof PermissionScope, number]> = [
    ["sessionPattern", 128],
    ["workspacePattern", 64],
    ["serverPattern", 32],
    ["toolPattern", 16],
    ["commandPattern", 12],
    ["resourcePattern", 8],
    ["operationPattern", 4],
    ["namespacePattern", 2],
    ["argumentPattern", 1],
  ];
  return weights.reduce((sum, [key, weight]) => sum + (scope[key] !== "*" ? weight : 0), 0);
}

export function workspacePathBinding(workspaceRoot: string, argumentsValue: JsonObject): JsonObject {
  const candidates = [
    argumentsValue.path,
    argumentsValue.file_path,
    argumentsValue.target,
    argumentsValue.destination,
    argumentsValue.cwd,
    argumentsValue.directory,
  ].filter((value): value is string => typeof value === "string" && value.length > 0);
  if (!candidates.length) {
    return {
      workspace_root: workspaceRoot,
      paths: [],
      all_within_workspace: false,
      path_binding_required: false,
    };
  }
  const root = workspaceRoot ? resolve(workspaceRoot) : "";
  const paths = candidates.map((candidate) => {
    const absolute = root ? resolve(root, candidate) : resolve(candidate);
    const delta = root ? relative(root, absolute) : absolute;
    const within = Boolean(root) && (delta === "" || (!delta.startsWith("..") && !isAbsolute(delta)));
    return {
      input: candidate,
      absolute,
      relative: delta,
      within_workspace: within,
      digest: digest(absolute.toLowerCase()),
    };
  });
  return {
    workspace_root: root,
    paths,
    all_within_workspace: paths.every((path) => path.within_workspace),
    path_binding_required: true,
  };
}

export function safeWorkspaceBinding(identity: PermissionIdentityRecord): boolean {
  const binding = optionalObject(identity.workspaceBinding);
  return binding.path_binding_required !== true || binding.all_within_workspace === true;
}

function normalizeOptionalName(value: string | undefined): string {
  return value ? normalizeName(value, 256) : "";
}

function normalizeResourceUri(value: string | undefined): string {
  if (!value) return "";
  const normalized = normalizeName(value, 4_096);
  if (/\0|[\r\n]/.test(normalized)) throw new Error("resource URI contains control characters");
  return normalized;
}

function normalizeWorkspace(value: string | undefined): string {
  return value ? resolve(value) : "";
}

function normalizeRevision(value: number | undefined): number {
  if (value === undefined) return 0;
  if (!Number.isSafeInteger(value) || value < 0) throw new Error("session revision must be a non-negative safe integer");
  return value;
}

function numericMetadata(value: JsonObject["x"]): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function required(value: string, label: string): string {
  if (!value) throw new Error(`${label} is required`);
  return value;
}
