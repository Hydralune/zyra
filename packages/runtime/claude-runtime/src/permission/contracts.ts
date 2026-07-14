import type { JsonObject } from "../contracts.ts";

export type PermissionEffect = "allow" | "deny" | "ask";

export interface TypeScriptPermissionRule {
  ruleId: string;
  effect: PermissionEffect;
  source: string;
  toolPattern: string;
  namespacePattern: string;
  serverPattern: string;
  operationPattern: string;
  argumentPattern: string;
  priority: number;
  enabled: boolean;
  maxUses: number | null;
  useCount: number;
  scope: JsonObject;
  reason: string;
  metadata: JsonObject;
}

export interface TypeScriptPermissionPolicy {
  canonicalOwner: "typescript";
  mode: string;
  interactive: boolean;
  headless: boolean;
  workspaceRoot: string;
  sessionId: string;
  ruleSnapshotId: string;
  modeRevision: number;
  rules: TypeScriptPermissionRule[];
  policyDigest: string;
}

export interface PermissionToolContext {
  runId: string;
  taskId: string;
  sessionId: string;
  toolCallId: string;
  toolName: string;
  namespace: string;
  serverId: string;
  version: string;
  schemaDigest: string;
  operation: string;
  arguments: JsonObject;
  metadata: JsonObject;
}

export interface TypeScriptPermissionDecision extends JsonObject {
  canonical_owner: "typescript";
  effect: PermissionEffect;
  reason_code: string;
  reason: string;
  mode: string;
  arguments_digest: string;
  request_fingerprint: string;
  rule_snapshot_id: string;
  mode_revision: number;
  matched_rule_ids: string[];
  matched_rule_sources: string[];
  recovery_alternatives: string[];
  scope: JsonObject;
  request_binding: JsonObject;
  policy_digest: string;
  evaluated_at: string;
  metadata: JsonObject;
}
