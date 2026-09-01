import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  constantTimeDigestEquals,
  digest,
  wildcardMatches,
} from "../e02/canonical.ts";
import type {
  PermissionRequestContext,
  PermissionRuleRecord,
  PermissionRuleSource,
} from "../e02/contracts.ts";
import type { PermissionIdentityRecord } from "./model.ts";
import { scopeSpecificity } from "./model.ts";

export interface PermissionRuleMatch extends JsonObject {
  rule: PermissionRuleRecord;
  matched: boolean;
  expired: boolean;
  exhausted: boolean;
  disabled: boolean;
  specificity: number;
  precedence: number;
  mismatchReasons: string[];
}
export interface PermissionRuleResolution extends JsonObject {
  winner: PermissionRuleRecord | null;
  matches: PermissionRuleMatch[];
  evaluatedRuleIds: string[];
  policyRevision: number;
  policyDigest: string;
}

export type PersistentPermissionDecisionScope = "session" | "workspace";
export const EXACT_PERMISSION_DECISION_SCHEMA = "zyra.permission-decision-binding/v1";

export function permissionDecisionBindingDigest(
  decisionScope: PersistentPermissionDecisionScope,
  context: Pick<
    PermissionRequestContext,
    | "sessionId"
    | "toolName"
    | "namespace"
    | "serverId"
    | "commandName"
    | "resourceUri"
    | "operation"
    | "workspaceRoot"
    | "arguments"
  >,
): string {
  return digest({
    schema: EXACT_PERMISSION_DECISION_SCHEMA,
    decision_scope: decisionScope,
    ...(decisionScope === "session" ? { session_id: context.sessionId } : {}),
    workspace_root: context.workspaceRoot,
    tool_name: context.toolName,
    namespace: context.namespace,
    server_id: context.serverId,
    command_name: context.commandName,
    resource_uri: context.resourceUri,
    operation: context.operation,
    arguments: context.arguments,
  });
}

const SOURCE_PRECEDENCE: Record<PermissionRuleSource, number> = {
  managed: 900,
  policy: 800,
  user: 700,
  project: 650,
  workspace: 600,
  plugin: 500,
  command: 450,
  session: 400,
  "standing-grant": 300,
};

const EFFECT_PRECEDENCE = {
  deny: 30,
  ask: 20,
  allow: 10,
} as const;

export class PermissionRuleIndex {
  private revisionValue = 0;
  private digestValue = digest([]);
  private readonly rules = new Map<string, PermissionRuleRecord>();
  private readonly bySource = new Map<PermissionRuleSource, Set<string>>();

  get revision(): number {
    return this.revisionValue;
  }

  get policyDigest(): string {
    return this.digestValue;
  }

  replace(rules: readonly PermissionRuleRecord[], expectedRevision = this.revisionValue): number {
    if (expectedRevision !== this.revisionValue) {
      throw new Error(`permission rule index revision conflict: expected ${expectedRevision}, observed ${this.revisionValue}`);
    }
    const next = new Map<string, PermissionRuleRecord>();
    for (const rule of rules) {
      validateRule(rule);
      const existing = next.get(rule.ruleId);
      if (existing && !constantTimeDigestEquals(digest(existing), digest(rule))) {
        throw new Error(`duplicate permission rule id ${rule.ruleId}`);
      }
      next.set(rule.ruleId, cloneJson(rule));
    }
    const nextDigest = digest([...next.values()].sort(compareRuleRecords));
    if (constantTimeDigestEquals(nextDigest, this.digestValue)) return this.revisionValue;
    this.rules.clear();
    this.bySource.clear();
    for (const [ruleId, rule] of next) {
      this.rules.set(ruleId, rule);
      const sourceIds = this.bySource.get(rule.source) ?? new Set<string>();
      sourceIds.add(ruleId);
      this.bySource.set(rule.source, sourceIds);
    }
    this.revisionValue += 1;
    this.digestValue = nextDigest;
    return this.revisionValue;
  }

  upsert(rule: PermissionRuleRecord, expectedRevision = this.revisionValue): number {
    const rules = this.list();
    const index = rules.findIndex((candidate) => candidate.ruleId === rule.ruleId);
    if (index >= 0) rules[index] = cloneJson(rule);
    else rules.push(cloneJson(rule));
    return this.replace(rules, expectedRevision);
  }

  remove(ruleId: string, expectedRevision = this.revisionValue): number {
    if (!this.rules.has(ruleId)) return this.revisionValue;
    return this.replace(this.list().filter((rule) => rule.ruleId !== ruleId), expectedRevision);
  }

  incrementUse(ruleId: string, expectedRevision = this.revisionValue): PermissionRuleRecord {
    const rule = this.rules.get(ruleId);
    if (!rule) throw new Error(`unknown permission rule ${ruleId}`);
    const now = new Date().toISOString();
    const next: PermissionRuleRecord = {
      ...rule,
      useCount: rule.useCount + 1,
      revision: rule.revision + 1,
      updatedAt: now,
    };
    this.upsert(next, expectedRevision);
    return cloneJson(next);
  }

  resolve(identity: PermissionIdentityRecord, now = new Date().toISOString()): PermissionRuleResolution {
    const matches = this.list().map((rule) => evaluateRule(rule, identity, now));
    const candidates = matches.filter((match) => match.matched).sort(compareMatches);
    return {
      winner: candidates[0]?.rule ?? null,
      matches,
      evaluatedRuleIds: matches.map((match) => match.rule.ruleId),
      policyRevision: this.revisionValue,
      policyDigest: this.digestValue,
    };
  }

  list(source?: PermissionRuleSource): PermissionRuleRecord[] {
    const ids = source ? this.bySource.get(source) ?? new Set<string>() : new Set(this.rules.keys());
    return [...ids]
      .map((id) => this.rules.get(id))
      .filter((rule): rule is PermissionRuleRecord => Boolean(rule))
      .map(cloneJson)
      .sort(compareRuleRecords);
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-permission-rule-index/v1",
      revision: this.revisionValue,
      rules: this.list(),
      policy_digest: this.digestValue,
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  restore(snapshot: JsonObject): void {
    if (snapshot.version !== "zyra.e02-permission-rule-index/v1") throw new Error("unsupported permission rule index snapshot");
    const rules = Array.isArray(snapshot.rules) ? snapshot.rules as unknown as PermissionRuleRecord[] : [];
    const revision = numberValue(snapshot.revision, "permission rule revision");
    const policyDigest = stringValue(snapshot.policy_digest, "permission policy digest");
    const expectedSnapshotHash = digest({
      version: snapshot.version,
      revision,
      rules,
      policy_digest: policyDigest,
    });
    if (snapshot.snapshot_hash !== expectedSnapshotHash) throw new Error("permission rule index snapshot hash mismatch");
    const computedPolicyDigest = digest([...rules].sort(compareRuleRecords));
    if (!constantTimeDigestEquals(computedPolicyDigest, policyDigest)) throw new Error("permission rule index policy digest mismatch");
    this.rules.clear();
    this.bySource.clear();
    for (const rule of rules) {
      validateRule(rule);
      this.rules.set(rule.ruleId, cloneJson(rule));
      const sourceIds = this.bySource.get(rule.source) ?? new Set<string>();
      sourceIds.add(rule.ruleId);
      this.bySource.set(rule.source, sourceIds);
    }
    this.revisionValue = revision;
    this.digestValue = policyDigest;
  }
}

function evaluateRule(rule: PermissionRuleRecord, identity: PermissionIdentityRecord, now: string): PermissionRuleMatch {
  const context = identity.context;
  const scope = rule.scope;
  const mismatchReasons: string[] = [];
  const disabled = !rule.enabled;
  const expired = Boolean(rule.expiresAt && Date.parse(rule.expiresAt) <= Date.parse(now));
  const exhausted = rule.maxUses !== null && rule.useCount >= rule.maxUses;
  if (disabled) mismatchReasons.push("disabled");
  if (expired) mismatchReasons.push("expired");
  if (exhausted) mismatchReasons.push("exhausted");
  match(scope.toolPattern, context.toolName, "tool", mismatchReasons);
  match(scope.namespacePattern, context.namespace, "namespace", mismatchReasons);
  match(scope.serverPattern, context.serverId, "server", mismatchReasons);
  match(scope.commandPattern, context.commandName, "command", mismatchReasons);
  match(scope.resourcePattern, context.resourceUri, "resource", mismatchReasons);
  match(scope.operationPattern, context.operation, "operation", mismatchReasons);
  match(scope.workspacePattern, context.workspaceRoot, "workspace", mismatchReasons, true);
  match(scope.sessionPattern, context.sessionId, "session", mismatchReasons);
  match(scope.argumentPattern, JSON.stringify(context.arguments), "arguments", mismatchReasons, true);
  const exactDecisionSchema = rule.metadata.operator_decision_schema;
  if (exactDecisionSchema === EXACT_PERMISSION_DECISION_SCHEMA) {
    const decisionScope = rule.metadata.operator_decision_scope;
    const expectedDigest = rule.metadata.operator_decision_binding_digest;
    if (
      (decisionScope !== "session" && decisionScope !== "workspace")
      || typeof expectedDigest !== "string"
      || !/^[0-9a-f]{64}$/.test(expectedDigest)
      || !constantTimeDigestEquals(
        expectedDigest,
        permissionDecisionBindingDigest(decisionScope, context),
      )
    ) {
      mismatchReasons.push("operator_decision_binding_mismatch");
    }
  }
  const specificity = scopeSpecificity(scope);
  const precedence = rule.priority * 1_000_000
    + SOURCE_PRECEDENCE[rule.source] * 1_000
    + EFFECT_PRECEDENCE[rule.effect] * 10
    + specificity;
  return {
    rule,
    matched: mismatchReasons.length === 0,
    expired,
    exhausted,
    disabled,
    specificity,
    precedence,
    mismatchReasons,
  };
}

function match(pattern: string, value: string, label: string, failures: string[], caseSensitive = false): void {
  if (!wildcardMatches(pattern, value, caseSensitive)) failures.push(`${label}_mismatch`);
}

function compareMatches(left: PermissionRuleMatch, right: PermissionRuleMatch): number {
  return right.precedence - left.precedence
    || compareRuleRecords(left.rule, right.rule);
}

function compareRuleRecords(left: PermissionRuleRecord, right: PermissionRuleRecord): number {
  return right.priority - left.priority
    || SOURCE_PRECEDENCE[right.source] - SOURCE_PRECEDENCE[left.source]
    || EFFECT_PRECEDENCE[right.effect] - EFFECT_PRECEDENCE[left.effect]
    || scopeSpecificity(right.scope) - scopeSpecificity(left.scope)
    || left.ruleId.localeCompare(right.ruleId);
}

function validateRule(rule: PermissionRuleRecord): void {
  if (!rule.ruleId) throw new Error("permission rule id is required");
  if (!Number.isSafeInteger(rule.priority)) throw new Error(`permission rule ${rule.ruleId} priority is invalid`);
  if (!Number.isSafeInteger(rule.revision) || rule.revision < 0) throw new Error(`permission rule ${rule.ruleId} revision is invalid`);
  if (!Number.isSafeInteger(rule.useCount) || rule.useCount < 0) throw new Error(`permission rule ${rule.ruleId} use count is invalid`);
  if (rule.maxUses !== null && (!Number.isSafeInteger(rule.maxUses) || rule.maxUses < 1)) {
    throw new Error(`permission rule ${rule.ruleId} max uses is invalid`);
  }
}

function numberValue(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error(`${label} is invalid`);
  return value as number;
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw new Error(`${label} is invalid`);
  return value;
}

export function contextFromIdentity(identity: PermissionIdentityRecord): PermissionRequestContext {
  return cloneJson(identity.context);
}
