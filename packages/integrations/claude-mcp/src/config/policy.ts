import type { JsonObject } from "../contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type { McpServerConfigRecord, McpTransportKind } from "./config-store.ts";

export type McpPolicyEffect = "allow" | "deny" | "ask";
export type McpPolicyOperation =
  | "connect"
  | "initialize"
  | "tools/list"
  | "tools/call"
  | "resources/list"
  | "resources/read"
  | "resources/subscribe"
  | "prompts/list"
  | "prompts/get"
  | "sampling/createMessage"
  | "elicitation/create"
  | "tasks/get"
  | "tasks/result"
  | "tasks/cancel"
  | "logging/setLevel"
  | "completion/complete"
  | "custom";

export interface McpPolicyRule {
  ruleId: string;
  effect: McpPolicyEffect;
  priority: number;
  serverPattern: string;
  transportPattern: string;
  operationPattern: string;
  capabilityPattern: string;
  resourcePattern: string;
  workspacePattern: string;
  sourcePattern: string;
  interactiveOnly: boolean;
  autonomousOnly: boolean;
  expiresAt: string | null;
  reason: string;
  metadata: JsonObject;
}

export interface McpPolicyContext {
  serverId: string;
  transport: McpTransportKind;
  operation: McpPolicyOperation;
  capabilityName: string;
  resourceUri: string;
  workspaceRoot: string;
  source: string;
  interactive: boolean;
  sealedAutonomous: boolean;
  networkReachable: boolean;
  authenticated: boolean;
  config: McpServerConfigRecord;
  metadata: JsonObject;
}

export interface McpPolicyDecision {
  decisionId: string;
  effect: McpPolicyEffect;
  reasonCode: string;
  reason: string;
  serverId: string;
  operation: McpPolicyOperation;
  matchedRuleIds: string[];
  policyRevision: number;
  policyDigest: string;
  requestDigest: string;
  replanRequired: boolean;
  recoveryInput: JsonObject | null;
  metadata: JsonObject;
}

export interface McpPolicySnapshot {
  version: "zyra.mcp-server-policy/v1";
  revision: number;
  digest: string;
  rules: McpPolicyRule[];
  defaultInteractiveEffect: McpPolicyEffect;
  defaultAutonomousEffect: Exclude<McpPolicyEffect, "ask">;
}

export class McpServerPolicy {
  private revisionValue = 0;
  private rulesValue: McpPolicyRule[] = [];
  private digestValue = "";
  private defaultInteractiveEffect: McpPolicyEffect = "ask";
  private defaultAutonomousEffect: Exclude<McpPolicyEffect, "ask"> = "deny";

  constructor(snapshot?: McpPolicySnapshot | null) {
    if (snapshot) this.restore(snapshot);
    else this.digestValue = this.computeDigest();
  }

  get revision(): number {
    return this.revisionValue;
  }

  get digest(): string {
    return this.digestValue;
  }

  replace(
    rules: readonly McpPolicyRule[],
    expectedRevision = this.revisionValue,
    defaults: {
      interactive?: McpPolicyEffect;
      autonomous?: Exclude<McpPolicyEffect, "ask">;
    } = {},
  ): McpPolicySnapshot {
    if (expectedRevision !== this.revisionValue) throw policyConflict(expectedRevision, this.revisionValue);
    const normalized = rules.map(normalizeRule);
    const ids = new Set<string>();
    for (const rule of normalized) {
      if (ids.has(rule.ruleId)) throw policyError("duplicate_policy_rule", `duplicate MCP policy rule ${rule.ruleId}`);
      ids.add(rule.ruleId);
    }
    this.rulesValue = normalized.sort(compareRule);
    this.defaultInteractiveEffect = defaults.interactive ?? this.defaultInteractiveEffect;
    this.defaultAutonomousEffect = defaults.autonomous ?? this.defaultAutonomousEffect;
    this.revisionValue += 1;
    this.digestValue = this.computeDigest();
    return this.snapshot();
  }

  evaluate(contextValue: McpPolicyContext): McpPolicyDecision {
    const context = normalizeContext(contextValue);
    const requestDigest = sha256({
      server_id: context.serverId,
      transport: context.transport,
      operation: context.operation,
      capability_name: context.capabilityName,
      resource_uri: context.resourceUri,
      workspace_root: context.workspaceRoot,
      source: context.source,
      interactive: context.interactive,
      sealed_autonomous: context.sealedAutonomous,
      config_digest: context.config.configDigest,
    });
    const applicable = this.rulesValue.filter((rule) => ruleMatches(rule, context));
    const winner = applicable[0] ?? null;
    let effect: McpPolicyEffect;
    let reasonCode: string;
    let reason: string;
    if (!context.config.enabled) {
      effect = "deny";
      reasonCode = "server_disabled";
      reason = `MCP server ${context.serverId} is disabled`;
    } else if (!operationAllowedByConfig(context.operation, context.config)) {
      effect = "deny";
      reasonCode = "operation_denied_by_server_config";
      reason = `operation ${context.operation} is outside server config allowlist`;
    } else if (!context.networkReachable && context.transport !== "stdio" && context.transport !== "in_process") {
      effect = "deny";
      reasonCode = "network_policy_unreachable";
      reason = "MCP network destination is not reachable under active policy";
    } else if (winner) {
      effect = winner.effect;
      reasonCode = `matched_${winner.effect}_rule`;
      reason = winner.reason || `matched MCP policy rule ${winner.ruleId}`;
    } else {
      effect = context.interactive ? this.defaultInteractiveEffect : this.defaultAutonomousEffect;
      reasonCode = `default_${effect}`;
      reason = `MCP policy defaulted to ${effect}`;
    }
    if (context.sealedAutonomous && effect === "ask") {
      effect = "deny";
      reasonCode = "sealed_policy_denied_ask";
      reason = "sealed autonomous MCP policy cannot pause for approval";
    }
    if (!context.interactive && effect === "ask") {
      effect = "deny";
      reasonCode = "headless_policy_denied_ask";
      reason = "headless MCP policy cannot pause for approval";
    }
    const replanRequired = effect === "deny";
    const recoveryInput = replanRequired
      ? canonicalJson({
        kind: "mcp_policy_denial",
        server_id: context.serverId,
        operation: context.operation,
        capability_name: context.capabilityName,
        reason_code: reasonCode,
        reason,
        rejected_request_digest: requestDigest,
        retry_same_request: false,
        replan_required: true,
        e02_plans_or_routes: false,
      }) as JsonObject
      : null;
    return {
      decisionId: deterministicMcpId("mcp-policy-decision", {
        revision: this.revisionValue,
        request_digest: requestDigest,
        effect,
        reason_code: reasonCode,
      }),
      effect,
      reasonCode,
      reason,
      serverId: context.serverId,
      operation: context.operation,
      matchedRuleIds: winner ? [winner.ruleId] : [],
      policyRevision: this.revisionValue,
      policyDigest: this.digestValue,
      requestDigest,
      replanRequired,
      recoveryInput,
      metadata: {
        authenticated: context.authenticated,
        network_reachable: context.networkReachable,
        human_intervention_count: 0,
        matched_rule_count: applicable.length,
      },
    };
  }

  requireAllowed(context: McpPolicyContext): McpPolicyDecision {
    const decision = this.evaluate(context);
    if (decision.effect !== "allow") {
      throw new McpRuntimeError({
        failureId: decision.decisionId,
        category: "policy",
        code: decision.reasonCode,
        message: decision.reason,
        serverId: decision.serverId,
        operation: decision.operation,
        retryable: false,
        disposition: "replan",
        details: {
          decision: canonicalJson(decision),
          recovery_input: decision.recoveryInput,
        },
      });
    }
    return decision;
  }

  snapshot(): McpPolicySnapshot {
    return {
      version: "zyra.mcp-server-policy/v1",
      revision: this.revisionValue,
      digest: this.digestValue,
      rules: this.rulesValue.map(cloneJson),
      defaultInteractiveEffect: this.defaultInteractiveEffect,
      defaultAutonomousEffect: this.defaultAutonomousEffect,
    };
  }

  restore(snapshot: McpPolicySnapshot): void {
    if (snapshot.version !== "zyra.mcp-server-policy/v1") throw policyError("unsupported_policy_snapshot", "unsupported MCP policy snapshot version");
    const rules = snapshot.rules.map(normalizeRule).sort(compareRule);
    const calculated = sha256({
      revision: snapshot.revision,
      rules,
      default_interactive_effect: snapshot.defaultInteractiveEffect,
      default_autonomous_effect: snapshot.defaultAutonomousEffect,
    });
    const legacyRulesDigest = sha256(snapshot.rules);
    const legacyEmptySnapshot = snapshot.revision === 0
      && snapshot.rules.length === 0
      && snapshot.defaultInteractiveEffect === "ask"
      && snapshot.defaultAutonomousEffect === "deny";
    if (
      calculated !== snapshot.digest
      && (!legacyEmptySnapshot || legacyRulesDigest !== snapshot.digest)
    ) {
      throw policyError("policy_snapshot_digest_mismatch", "MCP policy snapshot digest mismatch");
    }
    this.revisionValue = snapshot.revision;
    this.rulesValue = rules;
    this.defaultInteractiveEffect = snapshot.defaultInteractiveEffect;
    this.defaultAutonomousEffect = snapshot.defaultAutonomousEffect;
    this.digestValue = calculated;
  }

  private computeDigest(): string {
    return sha256({
      revision: this.revisionValue,
      rules: this.rulesValue,
      default_interactive_effect: this.defaultInteractiveEffect,
      default_autonomous_effect: this.defaultAutonomousEffect,
    });
  }
}

function normalizeRule(rule: McpPolicyRule): McpPolicyRule {
  if (!rule.ruleId) throw policyError("invalid_policy_rule", "MCP policy rule id is required");
  if (rule.effect !== "allow" && rule.effect !== "deny" && rule.effect !== "ask") {
    throw policyError("invalid_policy_effect", `MCP policy rule ${rule.ruleId} effect is invalid`);
  }
  if (!Number.isSafeInteger(rule.priority)) throw policyError("invalid_policy_priority", `MCP policy rule ${rule.ruleId} priority must be integer`);
  if (rule.interactiveOnly && rule.autonomousOnly) throw policyError("unreachable_policy_rule", `MCP policy rule ${rule.ruleId} cannot be interactive-only and autonomous-only`);
  if (rule.expiresAt !== null && Number.isNaN(Date.parse(rule.expiresAt))) throw policyError("invalid_policy_expiration", `MCP policy rule ${rule.ruleId} expiration is invalid`);
  return cloneJson({
    ...rule,
    serverPattern: normalizePattern(rule.serverPattern),
    transportPattern: normalizePattern(rule.transportPattern),
    operationPattern: normalizePattern(rule.operationPattern),
    capabilityPattern: normalizePattern(rule.capabilityPattern),
    resourcePattern: normalizePattern(rule.resourcePattern),
    workspacePattern: normalizePattern(rule.workspacePattern),
    sourcePattern: normalizePattern(rule.sourcePattern),
    metadata: rule.metadata ?? {},
  });
}

function normalizeContext(context: McpPolicyContext): McpPolicyContext {
  if (context.serverId !== context.config.serverId) throw policyError("policy_server_mismatch", "policy context server id differs from config");
  return cloneJson({ ...context, metadata: context.metadata ?? {} });
}

function compareRule(left: McpPolicyRule, right: McpPolicyRule): number {
  if (left.effect === "deny" && right.effect !== "deny") return -1;
  if (right.effect === "deny" && left.effect !== "deny") return 1;
  if (left.priority !== right.priority) return right.priority - left.priority;
  const specificity = ruleSpecificity(right) - ruleSpecificity(left);
  return specificity || left.ruleId.localeCompare(right.ruleId);
}

function ruleSpecificity(rule: McpPolicyRule): number {
  return [rule.serverPattern, rule.transportPattern, rule.operationPattern, rule.capabilityPattern, rule.resourcePattern, rule.workspacePattern, rule.sourcePattern]
    .reduce((total, pattern) => total + pattern.replace(/[?*]/g, "").length, 0);
}

function ruleMatches(rule: McpPolicyRule, context: McpPolicyContext): boolean {
  if (rule.expiresAt && Date.parse(rule.expiresAt) <= Date.now()) return false;
  if (rule.interactiveOnly && !context.interactive) return false;
  if (rule.autonomousOnly && context.interactive) return false;
  return wildcard(rule.serverPattern, context.serverId)
    && wildcard(rule.transportPattern, context.transport)
    && wildcard(rule.operationPattern, context.operation)
    && wildcard(rule.capabilityPattern, context.capabilityName)
    && wildcard(rule.resourcePattern, context.resourceUri)
    && wildcard(rule.workspacePattern, context.workspaceRoot)
    && wildcard(rule.sourcePattern, context.source);
}

function operationAllowedByConfig(operation: string, config: McpServerConfigRecord): boolean {
  if (config.deniedOperations.some((pattern) => wildcard(pattern, operation))) return false;
  return config.allowedOperations.some((pattern) => wildcard(pattern, operation));
}

function normalizePattern(value: string): string {
  const pattern = (value || "*").normalize("NFKC");
  if (/\0|[\r\n]/.test(pattern)) throw policyError("invalid_policy_pattern", "MCP policy pattern contains control characters");
  if (pattern.length > 4_096) throw policyError("invalid_policy_pattern", "MCP policy pattern is too long");
  return pattern;
}

function wildcard(pattern: string, value: string): boolean {
  const expected = pattern.toLowerCase();
  const source = value.toLowerCase();
  let p = 0;
  let v = 0;
  let star = -1;
  let retry = -1;
  while (v < source.length) {
    if (p < expected.length && (expected[p] === "?" || expected[p] === source[v])) {
      p += 1;
      v += 1;
    } else if (p < expected.length && expected[p] === "*") {
      star = p;
      retry = v;
      p += 1;
    } else if (star >= 0) {
      p = star + 1;
      retry += 1;
      v = retry;
    } else {
      return false;
    }
  }
  while (p < expected.length && expected[p] === "*") p += 1;
  return p === expected.length;
}

function policyConflict(expected: number, actual: number): McpRuntimeError {
  return new McpRuntimeError({
    failureId: `mcp-policy-conflict-${expected}-${actual}`,
    category: "conflict",
    code: "policy_revision_conflict",
    message: `MCP policy revision ${expected} does not match ${actual}`,
    retryable: true,
    disposition: "retry_same_connection",
    details: { expected_revision: expected, actual_revision: actual },
  });
}

function policyError(code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-policy", { code, message }),
    category: "policy",
    code,
    message,
    retryable: false,
    disposition: "replan",
  });
}
