import { isAbsolute, relative, resolve } from "node:path";

import { asObject, asString, type JsonObject } from "../contracts.ts";
import { argumentsDigest, digestObject, requestFingerprint } from "./canonical.ts";
import type {
  PermissionEffect,
  PermissionToolContext,
  TypeScriptPermissionDecision,
  TypeScriptPermissionPolicy,
  TypeScriptPermissionRule,
} from "./contracts.ts";

const READ_ONLY_TOOLS = new Set([
  "file_read",
  "list_skills",
  "read_skill_resource",
  "list_commands",
  "list_plugins",
  "mcp_list_resources",
  "mcp_read_resource",
  "mcp_list_prompts",
  "mcp_get_prompt",
]);

const WRITE_TOOLS = new Set(["file_write", "file_edit"]);
const HIGH_RISK_TOOLS = new Set(["shell", "browser", "command", "plugin_command"]);

export class TypeScriptPermissionEvaluator {
  readonly policy: TypeScriptPermissionPolicy;

  constructor(policyValue: unknown, defaults: {
    sessionId: string;
    workspaceRoot: string;
  }) {
    this.policy = normalizePolicy(policyValue, defaults);
  }

  evaluate(context: PermissionToolContext): TypeScriptPermissionDecision {
    const digest = argumentsDigest(context.arguments);
    const fingerprint = requestFingerprint({
      sessionId: context.sessionId,
      runId: context.runId,
      taskId: context.taskId,
      toolCallId: context.toolCallId,
      namespace: context.namespace,
      toolName: context.toolName,
      serverId: context.serverId,
      version: context.version,
      schemaDigest: context.schemaDigest,
      argumentsDigest: digest,
    });
    const matched = this.policy.rules
      .filter((rule) => ruleMatches(rule, context, digest, fingerprint))
      .sort(compareRules);
    let effect: PermissionEffect;
    let reasonCode: string;
    let reason: string;
    if (matched.length > 0) {
      const winner = matched[0];
      effect = winner.effect;
      reasonCode = "typescript_explicit_rule";
      reason = winner.reason || `Matched TypeScript permission rule ${winner.ruleId}`;
    } else {
      ({ effect, reasonCode, reason } = this.defaultDecision(context));
    }
    if (effect === "ask" && (this.policy.headless || !this.policy.interactive)) {
      effect = "deny";
      reasonCode = "typescript_noninteractive_ask_denied";
      reason = "Interactive approval is unavailable; ASK was deterministically denied";
    }
    if (this.policy.mode === "sealed" && effect === "ask") {
      effect = "deny";
      reasonCode = "typescript_sealed_ask_denied";
      reason = "Sealed mode cannot pause for approval";
    }
    const scope: JsonObject = {
      kind: "action",
      session_id: context.sessionId,
      task_id: context.taskId,
      run_id: context.runId,
      workspace_root: this.policy.workspaceRoot,
      tool_namespace: context.namespace,
      tool_name: context.toolName,
      server_id: context.serverId,
      argument_digest: digest,
      request_fingerprint: fingerprint,
    };
    return {
      canonical_owner: "typescript",
      effect,
      reason_code: reasonCode,
      reason,
      mode: this.policy.mode,
      arguments_digest: digest,
      request_fingerprint: fingerprint,
      rule_snapshot_id: this.policy.ruleSnapshotId,
      mode_revision: this.policy.modeRevision,
      matched_rule_ids: matched.map((rule) => rule.ruleId),
      matched_rule_sources: matched.map((rule) => rule.source),
      recovery_alternatives: effect === "deny"
        ? [
          "revise the request to a lower-risk operation",
          "select a capability allowed by the active permission mode",
        ]
        : [],
      scope,
      request_binding: {
        session_id: context.sessionId,
        run_id: context.runId,
        task_id: context.taskId,
        tool_use_id: context.toolCallId,
        namespace: context.namespace,
        tool_name: context.toolName,
        server_id: context.serverId,
        version: context.version,
        schema_digest: context.schemaDigest,
        arguments_digest: digest,
        request_fingerprint: fingerprint,
      },
      policy_digest: this.policy.policyDigest,
      evaluated_at: new Date().toISOString(),
      metadata: {
        runtime_owner: "zyra-typescript-claude-runtime",
        operation: context.operation,
        rule_match_count: matched.length,
        no_python_policy_fallback: true,
      },
    };
  }

  snapshot(): JsonObject {
    return {
      canonical_owner: "typescript",
      mode: this.policy.mode,
      interactive: this.policy.interactive,
      headless: this.policy.headless,
      workspace_root: this.policy.workspaceRoot,
      rule_snapshot_id: this.policy.ruleSnapshotId,
      mode_revision: this.policy.modeRevision,
      rule_count: this.policy.rules.length,
      policy_digest: this.policy.policyDigest,
    };
  }

  private defaultDecision(context: PermissionToolContext): {
    effect: PermissionEffect;
    reasonCode: string;
    reason: string;
  } {
    const mode = this.policy.mode;
    const readOnly = READ_ONLY_TOOLS.has(context.toolName)
      || context.operation === "read"
      || context.operation === "read_only";
    const write = WRITE_TOOLS.has(context.toolName);
    const highRisk = HIGH_RISK_TOOLS.has(context.toolName)
      || context.namespace === "mcp"
      || context.toolName.startsWith("mcp__")
      || context.toolName === "skill";
    if (readOnly) {
      if (context.toolName === "file_read" && !this.safeWorkspacePath(context.arguments)) {
        return {
          effect: mode === "bypass" ? "allow" : "deny",
          reasonCode: "typescript_path_outside_workspace",
          reason: "File read path is outside the bound workspace",
        };
      }
      return {
        effect: "allow",
        reasonCode: "typescript_read_only_allow",
        reason: "Read-only capability is allowed by the TypeScript runtime policy",
      };
    }
    if (mode === "sealed" || mode === "plan") {
      return {
        effect: "deny",
        reasonCode: `typescript_${mode}_mutation_denied`,
        reason: `${mode} mode denies mutation and remote execution`,
      };
    }
    if (mode === "bypass") {
      return {
        effect: "allow",
        reasonCode: "typescript_bypass_allow",
        reason: "Bypass mode allows the capability after immutable rule evaluation",
      };
    }
    if (write) {
      if (!this.safeWorkspacePath(context.arguments)) {
        return {
          effect: "deny",
          reasonCode: "typescript_path_outside_workspace",
          reason: "File mutation path is outside the bound workspace",
        };
      }
      if (mode === "accept_edits" || mode === "auto") {
        return {
          effect: "allow",
          reasonCode: "typescript_workspace_edit_allow",
          reason: "Workspace edit is allowed by the active TypeScript mode",
        };
      }
      return {
        effect: "ask",
        reasonCode: "typescript_workspace_edit_ask",
        reason: "Workspace mutation requires an exact user approval",
      };
    }
    if (highRisk) {
      if (mode === "auto" || mode === "dont_ask") {
        return {
          effect: "deny",
          reasonCode: "typescript_high_risk_autonomous_deny",
          reason: "Autonomous mode deterministically denies unruled high-risk execution",
        };
      }
      return {
        effect: "ask",
        reasonCode: "typescript_high_risk_ask",
        reason: "High-risk capability requires an exact user approval",
      };
    }
    if (mode === "auto") {
      return {
        effect: "allow",
        reasonCode: "typescript_low_risk_auto_allow",
        reason: "Low-risk capability is allowed by autonomous policy",
      };
    }
    return {
      effect: mode === "dont_ask" ? "deny" : "ask",
      reasonCode: mode === "dont_ask"
        ? "typescript_unknown_dont_ask_deny"
        : "typescript_unknown_ask",
      reason: mode === "dont_ask"
        ? "Unknown capability is denied because asking is disabled"
        : "Unknown capability requires exact user approval",
    };
  }

  private safeWorkspacePath(argumentsValue: JsonObject): boolean {
    const raw = ["path", "file_path", "target", "destination"]
      .map((key) => asString(argumentsValue[key]))
      .find(Boolean);
    if (!raw) {
      return false;
    }
    const root = this.policy.workspaceRoot;
    if (!root) {
      return false;
    }
    const target = resolve(root, raw);
    const delta = relative(resolve(root), target);
    return delta === "" || (!delta.startsWith("..") && !isAbsolute(delta));
  }
}

function normalizePolicy(value: unknown, defaults: {
  sessionId: string;
  workspaceRoot: string;
}): TypeScriptPermissionPolicy {
  const object = asObject(value);
  const rulesValue = Array.isArray(object.rules) ? object.rules : [];
  const rules = rulesValue.map((item) => normalizeRule(item)).filter(Boolean) as TypeScriptPermissionRule[];
  const serializable: JsonObject = {
    mode: asString(object.mode) || "default",
    interactive: object.interactive !== false,
    headless: object.headless === true,
    workspace_root: asString(object.workspace_root) || defaults.workspaceRoot,
    session_id: asString(object.session_id) || defaults.sessionId,
    rule_snapshot_id: asString(object.rule_snapshot_id),
    mode_revision: typeof object.mode_revision === "number" ? object.mode_revision : 0,
    rules: rules.map((rule) => ({
      rule_id: rule.ruleId,
      effect: rule.effect,
      source: rule.source,
      tool_pattern: rule.toolPattern,
      namespace_pattern: rule.namespacePattern,
      server_pattern: rule.serverPattern,
      operation_pattern: rule.operationPattern,
      argument_pattern: rule.argumentPattern,
      priority: rule.priority,
      enabled: rule.enabled,
      max_uses: rule.maxUses,
      use_count: rule.useCount,
      scope: rule.scope,
    })),
  };
  return {
    canonicalOwner: "typescript",
    mode: asString(serializable.mode),
    interactive: serializable.interactive === true,
    headless: serializable.headless === true,
    workspaceRoot: asString(serializable.workspace_root),
    sessionId: asString(serializable.session_id),
    ruleSnapshotId: asString(serializable.rule_snapshot_id),
    modeRevision: typeof serializable.mode_revision === "number" ? serializable.mode_revision : 0,
    rules,
    policyDigest: asString(object.policy_digest) || digestObject(serializable),
  };
}

function normalizeRule(value: unknown): TypeScriptPermissionRule | null {
  const object = asObject(value);
  const effect = asString(object.effect);
  if (!(["allow", "deny", "ask"] as string[]).includes(effect)) {
    return null;
  }
  return {
    ruleId: asString(object.rule_id) || asString(object.ruleId),
    effect: effect as PermissionEffect,
    source: asString(object.source) || "session",
    toolPattern: asString(object.tool_pattern) || "*",
    namespacePattern: asString(object.namespace_pattern) || "*",
    serverPattern: asString(object.server_pattern) || "*",
    operationPattern: asString(object.operation_pattern) || "*",
    argumentPattern: asString(object.argument_pattern),
    priority: typeof object.priority === "number" ? object.priority : 0,
    enabled: object.enabled !== false,
    maxUses: typeof object.max_uses === "number" ? object.max_uses : null,
    useCount: typeof object.use_count === "number" ? object.use_count : 0,
    scope: asObject(object.scope),
    reason: asString(object.reason),
    metadata: asObject(object.metadata),
  };
}

function ruleMatches(
  rule: TypeScriptPermissionRule,
  context: PermissionToolContext,
  digest: string,
  fingerprint: string,
): boolean {
  if (!rule.enabled || (rule.maxUses !== null && rule.useCount >= rule.maxUses)) {
    return false;
  }
  if (!glob(rule.toolPattern, context.toolName)
    || !glob(rule.namespacePattern, context.namespace)
    || !glob(rule.serverPattern, context.serverId)
    || !glob(rule.operationPattern, context.operation)) {
    return false;
  }
  if (rule.argumentPattern && !glob(rule.argumentPattern, JSON.stringify(context.arguments))) {
    return false;
  }
  const scope = rule.scope;
  return (!asString(scope.session_id) || asString(scope.session_id) === context.sessionId)
    && (!asString(scope.task_id) || asString(scope.task_id) === context.taskId)
    && (!asString(scope.run_id) || asString(scope.run_id) === context.runId)
    && (!asString(scope.tool_name) || asString(scope.tool_name) === context.toolName)
    && (!asString(scope.tool_namespace) || asString(scope.tool_namespace) === context.namespace)
    && (!asString(scope.server_id) || asString(scope.server_id) === context.serverId)
    && (!asString(scope.argument_digest) || asString(scope.argument_digest) === digest)
    && (!asString(scope.request_fingerprint) || asString(scope.request_fingerprint) === fingerprint);
}

function compareRules(left: TypeScriptPermissionRule, right: TypeScriptPermissionRule): number {
  if (left.priority !== right.priority) {
    return right.priority - left.priority;
  }
  const sourceRank: Record<string, number> = {
    enterprise: 0,
    managed: 1,
    project: 2,
    workspace: 3,
    session: 4,
    user: 5,
  };
  const sourceDelta = (sourceRank[left.source] ?? 20) - (sourceRank[right.source] ?? 20);
  return sourceDelta || left.ruleId.localeCompare(right.ruleId);
}

function glob(pattern: string, value: string): boolean {
  if (!pattern || pattern === "*") {
    return true;
  }
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replaceAll("*", ".*").replaceAll("?", ".");
  return new RegExp(`^${escaped}$`, "i").test(value);
}
