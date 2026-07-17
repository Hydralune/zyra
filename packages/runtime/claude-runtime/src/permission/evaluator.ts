import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  deterministicId,
  digest,
  optionalObject,
} from "../e02/canonical.ts";
import type {
  E02Clock,
  E02RuntimeIdentity,
  PermissionApprovalResponse,
  PermissionDecisionRecord,
  PermissionEffect,
  PermissionMode,
  PermissionRiskAssessment,
  PermissionRuleRecord,
  PermissionScope,
} from "../e02/contracts.ts";
import { PermissionContinuationRuntime } from "./continuation-runtime.ts";
import { StandingGrantRuntime } from "./grant-runtime.ts";
import { PermissionHookRuntime, type PermissionHookPipelineResult } from "./hook-runtime.ts";
import {
  defaultScope,
  PermissionIdentity,
  type PermissionIdentityInput,
  type PermissionIdentityRecord,
  safeWorkspaceBinding,
} from "./model.ts";
import { PermissionModeRuntime, type PermissionModeState } from "./mode-runtime.ts";
import { PermissionJournal } from "./permission-journal.ts";
import { PermissionRiskRuntime, type PermissionClassifier } from "./risk-runtime.ts";
import { PermissionRuleIndex, type PermissionRuleResolution } from "./rule-index.ts";
import { PermissionRuleParser } from "./rule-parser.ts";

export interface PermissionEvaluatorOptions {
  runtime: E02RuntimeIdentity;
  mode?: Partial<PermissionModeState>;
  rules?: readonly (string | JsonObject | PermissionRuleRecord)[];
  classifier?: PermissionClassifier | null;
  clock?: E02Clock;
  askTtlMs?: number;
  denialAbortLimit?: number;
  restored?: JsonObject | null;
}

export interface PermissionEvaluationInput extends PermissionIdentityInput {
  signal?: AbortSignal;
}

export interface PermissionApprovalResult extends JsonObject {
  accepted: boolean;
  effect: "allow" | "deny";
  requestId: string;
  decision: PermissionDecisionRecord | null;
  errorCode: string | null;
  errorMessage: string | null;
}

interface EffectSelection {
  effect: PermissionEffect;
  reasonCode: string;
  reason: string;
  matchedRules: PermissionRuleRecord[];
  grantId: string | null;
}

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
  "agent_status",
]);

const EDIT_TOOLS = new Set(["file_write", "file_edit", "notebook_edit"]);

export class PermissionEvaluator {
  readonly runtime: E02RuntimeIdentity;
  readonly parser = new PermissionRuleParser();
  readonly rules = new PermissionRuleIndex();
  readonly modes: PermissionModeRuntime;
  readonly risks: PermissionRiskRuntime;
  readonly hooks: PermissionHookRuntime;
  readonly grants: StandingGrantRuntime;
  readonly continuations: PermissionContinuationRuntime;
  readonly journal: PermissionJournal;
  private readonly clock: E02Clock;
  private readonly askTtlMs: number;
  private readonly denialAbortLimit: number;
  private readonly decisions = new Map<string, PermissionDecisionRecord>();
  private denialCount = 0;

  constructor(options: PermissionEvaluatorOptions) {
    this.runtime = cloneJson(options.runtime);
    this.clock = options.clock ?? (() => new Date().toISOString());
    this.askTtlMs = options.askTtlMs ?? 15 * 60_000;
    this.denialAbortLimit = options.denialAbortLimit ?? 3;
    this.modes = new PermissionModeRuntime(options.mode);
    this.risks = new PermissionRiskRuntime(options.classifier ?? null);
    this.hooks = new PermissionHookRuntime(this.clock);
    this.grants = new StandingGrantRuntime(this.clock);
    this.continuations = new PermissionContinuationRuntime(this.clock);
    this.journal = new PermissionJournal(this.runtime, this.clock);
    const rules = (options.rules ?? []).map((value) => isRuleRecord(value)
      ? cloneJson(value)
      : this.parser.parse(value as string | JsonObject, { now: this.clock() }));
    this.rules.replace(rules);
    if (options.restored && Object.keys(options.restored).length) this.restore(options.restored);
  }

  evaluate(input: PermissionEvaluationInput): PermissionDecisionRecord {
    const identity = PermissionIdentity.create(input);
    const risk = this.risks.classify(identity);
    const hookResult = this.hooks.runBeforeToolSync(identity, risk);
    const finalRisk = hookResult.identity.argumentsDigest === identity.argumentsDigest
      ? risk
      : this.risks.classify(hookResult.identity);
    return this.finalize(identity, hookResult, finalRisk);
  }

  async evaluateAsync(input: PermissionEvaluationInput): Promise<PermissionDecisionRecord> {
    const identity = PermissionIdentity.create(input);
    const initialRisk = await this.risks.classifyAsync(identity);
    const hookResult = await this.hooks.runBeforeTool(identity, initialRisk, input.signal);
    const finalRisk = hookResult.identity.argumentsDigest === identity.argumentsDigest
      ? initialRisk
      : await this.risks.classifyAsync(hookResult.identity);
    return this.finalize(identity, hookResult, finalRisk);
  }

  finalize(
    originalIdentity: PermissionIdentityRecord,
    hookResult: PermissionHookPipelineResult,
    risk: PermissionRiskAssessment,
  ): PermissionDecisionRecord {
    const identity = hookResult.identity;
    const prepared = this.journal.prepare(identity, this.rules.revision);
    if (prepared.kind === "return_committed" && prepared.committedReceipt) {
      const decision = prepared.committedReceipt.output.decision;
      if (!decision || typeof decision !== "object" || Array.isArray(decision)) throw new Error("committed permission receipt lacks decision");
      return cloneJson(decision as unknown as PermissionDecisionRecord);
    }
    const resolution = this.rules.resolve(identity, this.clock());
    const selection = this.selectEffect(identity, risk, resolution, hookResult);
    let effect = selection.effect;
    let reasonCode = selection.reasonCode;
    let reason = selection.reason;
    if (effect === "ask" && this.modes.convertsAskToDeny()) {
      effect = "deny";
      reasonCode = this.modes.mode === "sealed"
        ? "sealed_ask_denied"
        : this.modes.state.headless
          ? "headless_ask_denied"
          : "noninteractive_ask_denied";
      reason = "ASK cannot be satisfied in the active permission mode and was deterministically denied";
    }
    const replanRequired = effect === "deny" && (risk.level === "high" || risk.level === "unknown" || this.modes.mode === "sealed");
    const recoveryInput = replanRequired
      ? {
        kind: "permission_denial",
        reason_code: reasonCode,
        request_fingerprint: identity.requestFingerprint,
        denied_capability: {
          namespace: identity.context.namespace,
          tool_name: identity.context.toolName,
          server_id: identity.context.serverId,
          operation: identity.context.operation,
        },
        constraints: [
          "choose a lower-risk capability",
          "remove the denied side effect",
          "remain within the bound workspace and active policy",
        ],
        owner: "downstream-recovery-planner",
        e02_plans_or_routes: false,
      }
      : null;
    const decisionBase = {
      canonicalOwner: "typescript" as const,
      effect,
      reasonCode,
      reason,
      mode: this.modes.mode,
      modeRevision: this.modes.revision,
      policyRevision: this.rules.revision,
      policyDigest: this.rules.policyDigest,
      requestFingerprint: identity.requestFingerprint,
      originalArgumentsDigest: originalIdentity.argumentsDigest,
      finalArgumentsDigest: identity.argumentsDigest,
      finalArguments: identity.context.arguments,
      matchedRuleIds: selection.matchedRules.map((rule) => rule.ruleId),
      matchedRuleSources: selection.matchedRules.map((rule) => rule.source),
      risk,
      hooks: hookResult.audits,
      requestBinding: {
        run_id: identity.context.runId,
        task_id: identity.context.taskId,
        session_id: identity.context.sessionId,
        session_revision: identity.context.sessionRevision,
        worker_request_id: identity.context.workerRequestId,
        tool_call_id: identity.context.toolCallId,
        namespace: identity.context.namespace,
        tool_name: identity.context.toolName,
        server_id: identity.context.serverId,
        command_name: identity.context.commandName,
        resource_uri: identity.context.resourceUri,
        operation: identity.context.operation,
        workspace_root: identity.context.workspaceRoot,
        request_fingerprint: identity.requestFingerprint,
        arguments_digest: identity.argumentsDigest,
      },
      recoveryInput,
      replanRequired,
      humanInterventionCount: 0,
      continuationRequestId: null as string | null,
      evaluatedAt: this.clock(),
      metadata: {
        runtime_owner: "zyra-typescript-claude-runtime",
        hook_failed_closed: hookResult.failedClosed,
        grant_id: selection.grantId,
        denial_abort_limit: this.denialAbortLimit,
        python_policy_fallback: false,
      },
    };
    const decisionId = deterministicId("permission-decision", decisionBase, 40);
    const decision: PermissionDecisionRecord = { decisionId, ...decisionBase };
    if (effect === "ask") {
      const continuation = this.continuations.park(identity, decision, {
        ttlMs: this.askTtlMs,
        metadata: {
          transition_id: prepared.transition.transitionId,
          policy_digest: this.rules.policyDigest,
        },
      });
      decision.continuationRequestId = continuation.requestId;
      decision.metadata = {
        ...decision.metadata,
        continuation_expires_at: continuation.expiresAt,
      };
    }
    if (effect === "deny") {
      this.denialCount += 1;
      decision.metadata = {
        ...decision.metadata,
        denial_count: this.denialCount,
        permission_abort_loop: this.denialCount >= this.denialAbortLimit,
      };
    } else {
      this.denialCount = 0;
    }
    const receipt = this.journal.commit(prepared.transition.transitionId, decision);
    this.journal.acknowledge(receipt.transitionId, receipt.commitHash);
    this.decisions.set(decision.decisionId, cloneJson(decision));
    const winner = selection.matchedRules[0];
    if (winner && winner.maxUses !== null) this.rules.incrementUse(winner.ruleId, this.rules.revision);
    return cloneJson(decision);
  }

  resumeApproval(response: PermissionApprovalResponse): PermissionApprovalResult {
    const result = this.continuations.resume(response, {
      policyRevision: this.rules.revision,
      modeRevision: this.modes.revision,
    });
    if (!result.accepted) {
      const rejection = optionalObject(result.continuation.metadata.last_rejected_response);
      return {
        accepted: false,
        effect: response.effect,
        requestId: response.requestId,
        decision: null,
        errorCode: typeof rejection.code === "string" ? rejection.code : "approval_rejected",
        errorMessage: typeof rejection.message === "string" ? rejection.message : "permission approval response was rejected",
      };
    }
    const original = this.decisions.get(result.continuation.decisionId);
    if (!original) throw new Error(`permission approval refers to unknown decision ${result.continuation.decisionId}`);
    const resumedBase = {
      ...cloneJson(original),
      effect: result.effect,
      reasonCode: result.effect === "allow" ? "exact_approval_allow" : "exact_approval_deny",
      reason: result.effect === "allow"
        ? "exact durable approval matched request/session/revision/tool call"
        : "exact durable response denied the request",
      continuationRequestId: result.continuation.requestId,
      evaluatedAt: response.respondedAt,
      metadata: {
        ...original.metadata,
        approval_response_id: response.responseId,
        approval_response_digest: result.responseDigest,
        approval_responder: response.responder,
        exact_approval_binding: true,
      },
    } satisfies Omit<PermissionDecisionRecord, "decisionId"> & { decisionId?: string };
    const decisionId = deterministicId("permission-resume-decision", resumedBase, 40);
    const resumed: PermissionDecisionRecord = { ...resumedBase, decisionId };
    this.continuations.consume(result.continuation.requestId, result.effect === "allow" ? "approved" : "denied");
    this.decisions.set(decisionId, cloneJson(resumed));
    return {
      accepted: true,
      effect: result.effect,
      requestId: response.requestId,
      decision: resumed,
      errorCode: null,
      errorMessage: null,
    };
  }

  replaceRules(
    values: readonly (string | JsonObject | PermissionRuleRecord)[],
    expectedRevision = this.rules.revision,
  ): number {
    const parsed = values.map((value) => isRuleRecord(value)
      ? cloneJson(value)
      : this.parser.parse(value as string | JsonObject, { now: this.clock() }));
    return this.rules.replace(parsed, expectedRevision);
  }

  transitionMode(
    mode: PermissionMode,
    input: Parameters<PermissionModeRuntime["transition"]>[1],
  ): ReturnType<PermissionModeRuntime["transition"]> {
    return this.modes.transition(mode, input);
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-permission-evaluator/v1",
      runtime: this.runtime,
      mode: this.modes.snapshot(),
      rules: this.rules.snapshot(),
      grants: this.grants.snapshot(),
      continuations: this.continuations.snapshot(),
      hooks: this.hooks.snapshot(),
      journal: this.journal.snapshot() as unknown as JsonObject,
      decisions: [...this.decisions.values()].map(cloneJson).sort((left, right) => left.decisionId.localeCompare(right.decisionId)),
      denial_count: this.denialCount,
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  restore(snapshot: JsonObject): void {
    if (snapshot.version !== "zyra.e02-permission-evaluator/v1") throw new Error("unsupported permission evaluator snapshot");
    const expectedHash = digest({
      version: snapshot.version,
      runtime: snapshot.runtime,
      mode: snapshot.mode,
      rules: snapshot.rules,
      grants: snapshot.grants,
      continuations: snapshot.continuations,
      hooks: snapshot.hooks,
      journal: snapshot.journal,
      decisions: snapshot.decisions,
      denial_count: snapshot.denial_count,
    });
    if (expectedHash !== snapshot.snapshot_hash) throw new Error("permission evaluator snapshot hash mismatch");
    const runtime = snapshot.runtime as unknown as E02RuntimeIdentity;
    if (runtime.runId !== this.runtime.runId || runtime.sessionId !== this.runtime.sessionId) {
      throw new Error("permission evaluator restore runtime binding mismatch");
    }
    this.modes.restore(snapshot.mode as JsonObject);
    this.rules.restore(snapshot.rules as JsonObject);
    this.grants.restore(snapshot.grants as JsonObject);
    this.continuations.restore(snapshot.continuations as JsonObject);
    this.journal.restore(snapshot.journal as unknown as ReturnType<PermissionJournal["snapshot"]>, this.runtime.epoch);
    this.decisions.clear();
    for (const decision of Array.isArray(snapshot.decisions) ? snapshot.decisions as unknown as PermissionDecisionRecord[] : []) {
      this.decisions.set(decision.decisionId, cloneJson(decision));
    }
    this.denialCount = Number(snapshot.denial_count ?? 0);
    if (!Number.isSafeInteger(this.denialCount) || this.denialCount < 0) throw new Error("permission denial count is invalid");
  }

  private selectEffect(
    identity: PermissionIdentityRecord,
    risk: PermissionRiskAssessment,
    resolution: PermissionRuleResolution,
    hooks: PermissionHookPipelineResult,
  ): EffectSelection {
    if (hooks.forcedEffect === "deny" || hooks.failedClosed) {
      return {
        effect: "deny",
        reasonCode: "before_tool_hook_denied",
        reason: hooks.forcedReason || "before-tool hook denied the request",
        matchedRules: [],
        grantId: null,
      };
    }
    const winner = resolution.winner;
    if (winner) {
      if (winner.effect === "deny") {
        return {
          effect: "deny",
          reasonCode: "explicit_deny_rule",
          reason: winner.reason,
          matchedRules: [winner],
          grantId: null,
        };
      }
      if (winner.effect === "ask") {
        return {
          effect: "ask",
          reasonCode: "explicit_ask_rule",
          reason: winner.reason,
          matchedRules: [winner],
          grantId: null,
        };
      }
    }
    if (hooks.forcedEffect === "ask") {
      return {
        effect: "ask",
        reasonCode: "before_tool_hook_ask",
        reason: hooks.forcedReason || "before-tool hook requires approval",
        matchedRules: winner ? [winner] : [],
        grantId: null,
      };
    }
    const grant = this.grants.consume(identity, this.grants.revision);
    if (grant) {
      return {
        effect: "allow",
        reasonCode: "standing_grant_allow",
        reason: grant.reason,
        matchedRules: [],
        grantId: grant.grant.grantId,
      };
    }
    if (winner?.effect === "allow") {
      return {
        effect: "allow",
        reasonCode: "explicit_allow_rule",
        reason: winner.reason,
        matchedRules: [winner],
        grantId: null,
      };
    }
    return this.defaultEffect(identity, risk);
  }

  private defaultEffect(identity: PermissionIdentityRecord, risk: PermissionRiskAssessment): EffectSelection {
    const context = identity.context;
    const mode = this.modes.mode;
    const readOnly = READ_ONLY_TOOLS.has(context.toolName)
      || context.operation === "read"
      || optionalObject(context.metadata).read_only === true;
    const edit = EDIT_TOOLS.has(context.toolName) || context.operation === "write";
    if (!safeWorkspaceBinding(identity)) {
      return selection("deny", "workspace_boundary_denied", "requested filesystem path is outside the bound workspace");
    }
    if (mode === "sealed") {
      if (readOnly && risk.level === "low") {
        return selection("allow", "sealed_low_risk_allow", "sealed allowlist permits this deterministic low-risk read");
      }
      return selection(
        "deny",
        risk.level === "unknown" ? "sealed_unknown_deny" : "sealed_high_risk_deny",
        "sealed autonomous policy deterministically denies unknown or side-effecting capability",
      );
    }
    if (mode === "plan") {
      return readOnly
        ? selection("allow", "plan_read_allow", "plan mode permits read-only context gathering")
        : selection("deny", "plan_effect_denied", "plan mode denies side effects");
    }
    if (this.modes.permitsBypass()) {
      return selection("allow", "managed_bypass_allow", "managed bypass permits the request after immutable deny/hook checks");
    }
    if (readOnly && risk.level !== "high") {
      return selection("allow", "read_only_allow", "read-only capability is allowed by the TypeScript permission runtime");
    }
    if (edit) {
      if (mode === "acceptEdits" || mode === "auto") {
        return risk.level === "high"
          ? selection("deny", "high_risk_edit_denied", "high-risk workspace edit is denied")
          : selection("allow", "workspace_edit_allow", "active mode permits bounded workspace edits");
      }
      return selection("ask", "workspace_edit_ask", "workspace edit requires exact approval");
    }
    if (risk.level === "high" || risk.level === "unknown") {
      return mode === "dontAsk" || mode === "auto"
        ? selection("deny", "autonomous_high_risk_deny", "autonomous mode denies unruled high-risk or unknown capability")
        : selection("ask", "high_risk_ask", "high-risk capability requires exact approval");
    }
    if (mode === "auto") {
      return selection("allow", "auto_low_risk_allow", "deterministic risk evaluation permits low-risk autonomous execution");
    }
    if (mode === "dontAsk") {
      return selection("deny", "dont_ask_unknown_deny", "dontAsk mode denies unruled capability");
    }
    return selection("ask", "default_unruled_ask", "unruled capability requires exact approval");
  }
}

function selection(effect: PermissionEffect, reasonCode: string, reason: string): EffectSelection {
  return { effect, reasonCode, reason, matchedRules: [], grantId: null };
}

function isRuleRecord(value: unknown): value is PermissionRuleRecord {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && typeof (value as PermissionRuleRecord).ruleId === "string"
    && typeof (value as PermissionRuleRecord).effect === "string"
    && typeof (value as PermissionRuleRecord).scope === "object");
}

export function exactGrantScope(decision: PermissionDecisionRecord): PermissionScope {
  const binding = decision.requestBinding;
  return {
    ...defaultScope("tool"),
    kind: "tool",
    toolPattern: String(binding.tool_name ?? ""),
    namespacePattern: String(binding.namespace ?? ""),
    serverPattern: String(binding.server_id ?? ""),
    commandPattern: String(binding.command_name ?? ""),
    resourcePattern: String(binding.resource_uri ?? ""),
    operationPattern: String(binding.operation ?? ""),
    workspacePattern: String(binding.workspace_root ?? ""),
    sessionPattern: String(binding.session_id ?? ""),
    argumentPattern: JSON.stringify(decision.finalArguments),
  };
}
