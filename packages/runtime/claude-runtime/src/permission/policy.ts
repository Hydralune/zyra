import type { JsonObject } from "../contracts.ts";
import { asObject, asString } from "../contracts.ts";
import type {
  E02RuntimeIdentity,
  PermissionDecisionRecord,
  PermissionMode,
  PermissionRuleRecord,
} from "../e02/contracts.ts";
import { PermissionEvaluator } from "./evaluator.ts";
import type {
  PermissionToolContext,
  TypeScriptPermissionDecision,
  TypeScriptPermissionPolicy,
} from "./contracts.ts";
export class TypeScriptPermissionEvaluator {
  readonly policy: TypeScriptPermissionPolicy;
  readonly runtime: PermissionEvaluator;

  constructor(
    policyValue: unknown,
    defaults: {
      sessionId: string;
      workspaceRoot: string;
      runId?: string;
      taskId?: string;
      workerRequestId?: string;
      epoch?: number;
      restored?: JsonObject | null;
    },
  ) {
    this.policy = normalizeCompatibilityPolicy(policyValue, defaults);
    const identity: E02RuntimeIdentity = {
      runtimeId: "zyra-e02-permission-runtime",
      runId: defaults.runId || "permission-run",
      taskId: defaults.taskId || "permission-task",
      sessionId: defaults.sessionId,
      workerRequestId: defaults.workerRequestId || "permission-worker-request",
      epoch: defaults.epoch ?? 1,
    };
    this.runtime = new PermissionEvaluator({
      runtime: identity,
      mode: {
        mode: normalizeMode(this.policy.mode),
        revision: this.policy.modeRevision,
        interactive: this.policy.interactive,
        headless: this.policy.headless,
        sealedAutonomous: this.policy.mode === "sealed",
        bypassAvailable: this.policy.mode === "bypassPermissions" || this.policy.mode === "bypass",
        autoClassifierEnabled: false,
      },
      rules: this.policy.rules.map(compatibilityRule),
      restored: defaults.restored,
    });
  }

  evaluate(context: PermissionToolContext): TypeScriptPermissionDecision {
    return compatibilityDecision(this.runtime.evaluate(toEvaluation(context, this.policy.workspaceRoot)));
  }

  async evaluateAsync(context: PermissionToolContext, signal?: AbortSignal): Promise<TypeScriptPermissionDecision> {
    return compatibilityDecision(await this.runtime.evaluateAsync({
      ...toEvaluation(context, this.policy.workspaceRoot),
      signal,
    }));
  }

  snapshot(): JsonObject {
    return {
      canonical_owner: "typescript",
      version: "zyra.e02-permission-compatibility/v1",
      mode: this.runtime.modes.mode,
      mode_revision: this.runtime.modes.revision,
      policy_revision: this.runtime.rules.revision,
      policy_digest: this.runtime.rules.policyDigest,
      runtime: this.runtime.snapshot(),
      python_policy_fallback: false,
    };
  }
}

function toEvaluation(context: PermissionToolContext, workspaceRoot: string) {
  const metadata = context.metadata;
  const sessionRevision = typeof metadata.session_revision === "number" ? metadata.session_revision : 0;
  return {
    runId: context.runId,
    taskId: context.taskId,
    sessionId: context.sessionId,
    sessionRevision,
    workerRequestId: asString(metadata.worker_request_id) || context.toolCallId,
    toolCallId: context.toolCallId,
    toolName: context.toolName,
    namespace: context.namespace,
    serverId: context.serverId,
    commandName: asString(metadata.command_name),
    resourceUri: asString(metadata.resource_uri),
    operation: context.operation,
    workspaceRoot,
    arguments: context.arguments,
    metadata: {
      ...metadata,
      version: context.version,
      schema_digest: context.schemaDigest,
    },
  };
}

function compatibilityDecision(decision: PermissionDecisionRecord): TypeScriptPermissionDecision {
  return {
    canonical_owner: "typescript",
    effect: decision.effect,
    reason_code: decision.reasonCode,
    reason: decision.reason,
    mode: decision.mode,
    arguments_digest: decision.finalArgumentsDigest,
    request_fingerprint: decision.requestFingerprint,
    rule_snapshot_id: `permission-policy-revision-${decision.policyRevision}`,
    mode_revision: decision.modeRevision,
    matched_rule_ids: decision.matchedRuleIds,
    matched_rule_sources: decision.matchedRuleSources,
    recovery_alternatives: decision.effect === "deny"
      ? ["revise the request to a lower-risk operation", "select a capability allowed by the active policy"]
      : [],
    scope: decision.requestBinding,
    request_binding: decision.requestBinding,
    policy_digest: decision.policyDigest,
    evaluated_at: decision.evaluatedAt,
    metadata: {
      ...decision.metadata,
      decision_id: decision.decisionId,
      original_arguments_digest: decision.originalArgumentsDigest,
      final_arguments_digest: decision.finalArgumentsDigest,
      final_arguments: decision.finalArguments,
      risk: decision.risk,
      hook_audits: decision.hooks,
      continuation_request_id: decision.continuationRequestId,
      recovery_input: decision.recoveryInput,
      replan_required: decision.replanRequired,
      human_intervention_count: decision.humanInterventionCount,
    },
  };
}

function normalizeCompatibilityPolicy(
  value: unknown,
  defaults: { sessionId: string; workspaceRoot: string },
): TypeScriptPermissionPolicy {
  const object = asObject(value);
  const rulesValue = Array.isArray(object.rules) ? object.rules : [];
  const rules = rulesValue.map((item, index) => {
    const rule = asObject(item);
    const effect = asString(rule.effect);
    if (!new Set(["allow", "deny", "ask"]).has(effect)) return null;
    return {
      ruleId: asString(rule.rule_id) || asString(rule.ruleId) || `compat-rule-${index}`,
      effect: effect as "allow" | "deny" | "ask",
      source: asString(rule.source) || "session",
      toolPattern: asString(rule.tool_pattern) || "*",
      namespacePattern: asString(rule.namespace_pattern) || "*",
      serverPattern: asString(rule.server_pattern) || "*",
      operationPattern: asString(rule.operation_pattern) || "*",
      argumentPattern: asString(rule.argument_pattern) || "*",
      priority: typeof rule.priority === "number" ? rule.priority : 0,
      enabled: rule.enabled !== false,
      maxUses: typeof rule.max_uses === "number" ? rule.max_uses : null,
      useCount: typeof rule.use_count === "number" ? rule.use_count : 0,
      scope: asObject(rule.scope),
      reason: asString(rule.reason),
      metadata: asObject(rule.metadata),
    };
  }).filter(Boolean) as TypeScriptPermissionPolicy["rules"];
  const mode = asString(object.mode) || "default";
  return {
    canonicalOwner: "typescript",
    mode,
    interactive: object.interactive !== false,
    headless: object.headless === true,
    workspaceRoot: asString(object.workspace_root) || defaults.workspaceRoot,
    sessionId: asString(object.session_id) || defaults.sessionId,
    ruleSnapshotId: asString(object.rule_snapshot_id),
    modeRevision: typeof object.mode_revision === "number" ? object.mode_revision : 0,
    rules,
    policyDigest: asString(object.policy_digest),
  };
}

function compatibilityRule(rule: TypeScriptPermissionPolicy["rules"][number]): PermissionRuleRecord {
  const now = new Date().toISOString();
  const scopeValue = rule.scope;
  return {
    ruleId: rule.ruleId,
    effect: rule.effect,
    source: normalizeSource(rule.source),
    scope: {
      kind: "tool",
      toolPattern: rule.toolPattern,
      namespacePattern: rule.namespacePattern,
      serverPattern: rule.serverPattern,
      commandPattern: asString(scopeValue.command_name) || "*",
      resourcePattern: asString(scopeValue.resource_uri) || "*",
      operationPattern: rule.operationPattern,
      workspacePattern: asString(scopeValue.workspace_root) || "*",
      sessionPattern: asString(scopeValue.session_id) || "*",
      argumentPattern: rule.argumentPattern || "*",
    },
    priority: rule.priority,
    enabled: rule.enabled,
    reason: rule.reason,
    expiresAt: null,
    maxUses: rule.maxUses,
    useCount: rule.useCount,
    revision: 0,
    createdAt: now,
    updatedAt: now,
    metadata: rule.metadata,
  };
}

function normalizeMode(value: string): PermissionMode {
  const aliases: Record<string, PermissionMode> = {
    accept_edits: "acceptEdits",
    dont_ask: "dontAsk",
    bypass: "bypassPermissions",
    bypass_permissions: "bypassPermissions",
  };
  return aliases[value] ?? value as PermissionMode;
}

function normalizeSource(value: string): PermissionRuleRecord["source"] {
  return new Set(["managed", "policy", "user", "project", "workspace", "plugin", "command", "session", "standing-grant"]).has(value)
    ? value as PermissionRuleRecord["source"]
    : "session";
}
