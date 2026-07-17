import type { JsonObject } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  deterministicId,
  digest,
  monotonicNow,
} from "../e02/index.ts";
import type {
  E02RuntimeIdentity,
  PermissionApprovalResponse,
  PermissionDecisionRecord,
  PermissionMode,
  PermissionRuleRecord,
} from "../e02/contracts.ts";
import {
  PermissionApprovalRuntime,
  type PermissionApprovalSnapshot,
  type PermissionApprovalTransport,
} from "./approval-runtime.ts";
import {
  PermissionAuditRuntime,
  type PermissionAuditSnapshot,
} from "./audit-runtime.ts";
import {
  PermissionEvaluator,
  type PermissionApprovalResult,
  type PermissionEvaluationInput,
  type PermissionEvaluatorOptions,
} from "./evaluator.ts";
import type {
  PermissionHookDescriptor,
  PermissionHookHandler,
} from "./hook-runtime.ts";
import type { PermissionModeState } from "./mode-runtime.ts";
import {
  PermissionSettingsRuntime,
  type PermissionSettingsSnapshot,
  type PermissionSettingsSource,
} from "./settings-runtime.ts";

export interface PermissionCoordinatorSnapshot {
  version: "zyra.permission-coordinator/v1";
  runtime: E02RuntimeIdentity;
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  settingsSources: PermissionSettingsSource[];
  evaluator: JsonObject;
  settings: PermissionSettingsSnapshot;
  approvals: PermissionApprovalSnapshot;
  audit: PermissionAuditSnapshot;
  lastPolicyDigest: string;
  lastSettingsRevision: number;
  decisionSequence: number;
  digest: string;
  capturedAt: string;
}

export interface PermissionCoordinatorOptions {
  runtime: E02RuntimeIdentity;
  workspaceRoot: string;
  mode?: Partial<PermissionModeState>;
  rules?: readonly (string | JsonObject | PermissionRuleRecord)[];
  settingsSources?: readonly PermissionSettingsSource[];
  classifier?: PermissionEvaluatorOptions["classifier"];
  approvalTransport?: PermissionApprovalTransport;
  interactive?: boolean;
  clock?: () => string;
  now?: () => Date;
  askTtlMs?: number;
  denialAbortLimit?: number;
  snapshot?: PermissionCoordinatorSnapshot | null;
}

export interface PermissionEnforcementInput extends PermissionEvaluationInput {
  awaitApprovalDelivery?: boolean;
}

export interface PermissionEnforcementResult {
  decision: PermissionDecisionRecord;
  allowed: boolean;
  blocked: boolean;
  pendingApproval: boolean;
  finalArguments: JsonObject;
  recoveryInput: JsonObject | null;
  replanRequired: boolean;
  approvalEnvelopeId: string | null;
  stateDigest: string;
}

export interface PermissionPolicyCommit {
  commitId: string;
  revisionBefore: number;
  revisionAfter: number;
  modeBefore: PermissionMode;
  modeAfter: PermissionMode;
  ruleCount: number;
  policyDigest: string;
  committedAt: string;
  metadata: JsonObject;
}

const rejectApprovalTransport: PermissionApprovalTransport = async (envelope) => {
  return {
    accepted: false,
    transport: "unavailable",
    transportRequestId: envelope.requestId,
    metadata: {
      reason: "no interactive approval transport is configured",
    },
  };
};

export class PermissionCoordinator {
  readonly evaluator: PermissionEvaluator;
  readonly settings: PermissionSettingsRuntime;
  readonly approvals: PermissionApprovalRuntime;
  readonly audit: PermissionAuditRuntime;
  private readonly runtime: E02RuntimeIdentity;
  private readonly workspaceRoot: string;
  private readonly settingsSources: PermissionSettingsSource[];
  private readonly interactive: boolean;
  private readonly clock: () => string;
  private readonly now: () => Date;
  private opened = false;
  private restoredBeforeBootstrap = false;
  private lastPolicyDigest = "";
  private lastSettingsRevision = 0;
  private decisionSequence = 0;
  private lastTimestamp: string | null = null;

  constructor(options: PermissionCoordinatorOptions) {
    validateRuntime(options.runtime);
    if (!options.workspaceRoot) {
      throw permissionCoordinatorError(
        "permission_workspace_missing",
        "permission coordinator requires a workspace root",
      );
    }
    this.runtime = cloneJson(options.runtime);
    this.workspaceRoot = options.workspaceRoot;
    this.settingsSources = (options.settingsSources ?? []).map(cloneJson);
    this.interactive = options.interactive !== false;
    this.clock = options.clock ?? (() => new Date().toISOString());
    this.now = options.now ?? (() => new Date(this.clock()));
    const snapshot = options.snapshot ?? null;
    if (snapshot) {
      this.validateSnapshotBinding(snapshot);
    }
    this.audit = new PermissionAuditRuntime({
      now: this.now,
      snapshot: snapshot?.audit ?? null,
    });
    this.settings = new PermissionSettingsRuntime({
      now: this.now,
      snapshot: snapshot?.settings ?? null,
    });
    this.evaluator = new PermissionEvaluator({
      runtime: this.runtime,
      mode: options.mode,
      rules: options.rules,
      classifier: options.classifier,
      clock: this.clock,
      askTtlMs: options.askTtlMs,
      denialAbortLimit: options.denialAbortLimit,
      restored: snapshot?.evaluator ?? null,
    });
    this.approvals = new PermissionApprovalRuntime({
      evaluator: this.evaluator,
      audit: this.audit,
      transport: options.approvalTransport ?? rejectApprovalTransport,
      now: this.now,
      snapshot: snapshot?.approvals ?? null,
    });
    if (snapshot) {
      this.opened = false;
      this.restoredBeforeBootstrap = true;
      this.lastPolicyDigest = snapshot.lastPolicyDigest;
      this.lastSettingsRevision = snapshot.lastSettingsRevision;
      this.decisionSequence = snapshot.decisionSequence;
    }
  }

  async open(): Promise<void> {
    if (this.opened) {
      return;
    }
    if (!this.restoredBeforeBootstrap && this.settingsSources.length) {
      await this.reloadSettings({
        reason: "bootstrap",
        runtime_id: this.runtime.runtimeId,
      });
    }
    if (!this.lastPolicyDigest) {
      this.lastPolicyDigest = digest({
        mode: this.evaluator.modes.snapshot(),
        rules: this.evaluator.rules.snapshot(),
      });
    }
    this.approvals.expire(this.now());
    this.opened = true;
  }

  registerHook(
    descriptor: Omit<PermissionHookDescriptor, "handler">,
    handler: PermissionHookHandler,
  ): void {
    this.evaluator.hooks.register({
      ...descriptor,
      handler,
    });
  }

  unregisterHook(hookId: string): boolean {
    return this.evaluator.hooks.unregister(hookId);
  }

  async enforce(
    input: PermissionEnforcementInput,
  ): Promise<PermissionEnforcementResult> {
    this.requireOpen();
    const decision = await this.evaluator.evaluateAsync(input);
    this.decisionSequence += 1;
    this.audit.decision(decision);
    let approvalEnvelopeId: string | null = null;
    if (decision.effect === "ask") {
      if (!this.interactive) {
        throw permissionCoordinatorError(
          "interactive_ask_in_noninteractive_runtime",
          "permission evaluator returned ASK in a noninteractive runtime",
          {
            decision_id: decision.decisionId,
            request_id: decision.continuationRequestId,
          },
        );
      }
      if (input.awaitApprovalDelivery !== false) {
        const envelope = await this.approvals.request(decision, input.signal);
        approvalEnvelopeId = envelope.envelopeId;
      }
    }
    return this.enforcementResult(decision, approvalEnvelopeId);
  }

  resume(
    responseValue: PermissionApprovalResponse,
  ): PermissionApprovalResult {
    this.requireOpen();
    const response = cloneJson(responseValue);
    const result = this.approvals.respond(response);
    if (result.accepted && result.decision) {
      this.decisionSequence += 1;
    }
    return result;
  }

  resumeAndEnforce(
    responseValue: PermissionApprovalResponse,
  ): PermissionEnforcementResult {
    const result = this.resume(responseValue);
    if (!result.accepted || !result.decision) {
      throw permissionCoordinatorError(
        result.errorCode ?? "approval_resume_rejected",
        result.errorMessage ?? "permission approval response was rejected",
        {
          request_id: result.requestId,
          effect: result.effect,
        },
      );
    }
    return this.enforcementResult(result.decision, this.approvals.get(result.requestId)?.envelopeId ?? null);
  }

  async reloadSettings(metadata: JsonObject = {}): Promise<PermissionPolicyCommit> {
    const revisionBefore = this.evaluator.rules.revision;
    const modeBefore = this.evaluator.modes.mode;
    const revision = await this.settings.load(
      this.settingsSources,
      this.lastSettingsRevision,
      metadata,
    );
    const effective = this.settings.effective(revision.revisionAfter);
    const revisionAfter = this.evaluator.replaceRules(
      effective.rules,
      revisionBefore,
    );
    let modeAfter = modeBefore;
    if (effective.mode && effective.mode !== modeBefore) {
      this.evaluator.transitionMode(effective.mode, {
        changedBy: "permission-settings-runtime",
        reason: "permission settings revision changed the active mode",
        expectedRevision: this.evaluator.modes.revision,
      });
      modeAfter = effective.mode;
    }
    this.lastSettingsRevision = revision.revisionAfter;
    this.lastPolicyDigest = digest({
      mode: this.evaluator.modes.snapshot(),
      rules: this.evaluator.rules.snapshot(),
      settings_revision: this.lastSettingsRevision,
    });
    const commit: PermissionPolicyCommit = {
      commitId: deterministicId("permission-policy-commit", {
        runtime: this.runtime.runtimeId,
        revision_before: revisionBefore,
        revision_after: revisionAfter,
        settings_revision: revision.revisionAfter,
        policy_digest: this.lastPolicyDigest,
      }, 40),
      revisionBefore,
      revisionAfter,
      modeBefore,
      modeAfter,
      ruleCount: effective.rules.length,
      policyDigest: this.lastPolicyDigest,
      committedAt: this.timestamp(),
      metadata: canonicalize({
        ...metadata,
        settings_revision_id: revision.revisionId,
        settings_failures: revision.failures,
      }) as JsonObject,
    };
    this.audit.append({
      kind: "policy_reloaded",
      runId: this.runtime.runId,
      taskId: this.runtime.taskId,
      sessionId: this.runtime.sessionId,
      workerRequestId: this.runtime.workerRequestId,
      toolCallId: null,
      decisionId: null,
      requestId: null,
      effect: null,
      reasonCode: "permission_settings_committed",
      stateDigest: commit.policyDigest,
      details: canonicalize(commit) as JsonObject,
    });
    return cloneJson(commit);
  }

  replaceRules(
    values: readonly (string | JsonObject | PermissionRuleRecord)[],
    expectedRevision = this.evaluator.rules.revision,
    metadata: JsonObject = {},
  ): PermissionPolicyCommit {
    const revisionBefore = this.evaluator.rules.revision;
    const modeBefore = this.evaluator.modes.mode;
    const revisionAfter = this.evaluator.replaceRules(values, expectedRevision);
    this.lastPolicyDigest = digest({
      mode: this.evaluator.modes.snapshot(),
      rules: this.evaluator.rules.snapshot(),
      settings_revision: this.lastSettingsRevision,
    });
    const commit: PermissionPolicyCommit = {
      commitId: deterministicId("permission-policy-commit", {
        runtime: this.runtime.runtimeId,
        revision_before: revisionBefore,
        revision_after: revisionAfter,
        policy_digest: this.lastPolicyDigest,
      }, 40),
      revisionBefore,
      revisionAfter,
      modeBefore,
      modeAfter: modeBefore,
      ruleCount: this.evaluator.rules.list().length,
      policyDigest: this.lastPolicyDigest,
      committedAt: this.timestamp(),
      metadata: canonicalize(metadata) as JsonObject,
    };
    this.audit.append({
      kind: "policy_reloaded",
      runId: this.runtime.runId,
      taskId: this.runtime.taskId,
      sessionId: this.runtime.sessionId,
      workerRequestId: this.runtime.workerRequestId,
      toolCallId: null,
      decisionId: null,
      requestId: null,
      effect: null,
      reasonCode: "permission_rules_replaced",
      stateDigest: commit.policyDigest,
      details: canonicalize(commit) as JsonObject,
    });
    return cloneJson(commit);
  }

  transitionMode(
    mode: PermissionMode,
    input: {
      actor: string;
      reason: string;
      expectedRevision?: number;
      metadata?: JsonObject;
    },
  ): ReturnType<PermissionEvaluator["transitionMode"]> {
    const transition = this.evaluator.transitionMode(mode, {
      changedBy: input.actor,
      reason: input.reason,
      expectedRevision: input.expectedRevision ?? this.evaluator.modes.revision,
    });
    this.lastPolicyDigest = digest({
      mode: this.evaluator.modes.snapshot(),
      rules: this.evaluator.rules.snapshot(),
      settings_revision: this.lastSettingsRevision,
    });
    this.audit.append({
      kind: "mode_changed",
      runId: this.runtime.runId,
      taskId: this.runtime.taskId,
      sessionId: this.runtime.sessionId,
      workerRequestId: this.runtime.workerRequestId,
      toolCallId: null,
      decisionId: null,
      requestId: null,
      effect: null,
      reasonCode: "permission_mode_changed",
      stateDigest: this.lastPolicyDigest,
      details: canonicalize(transition) as JsonObject,
    });
    return transition;
  }

  snapshot(): PermissionCoordinatorSnapshot {
    const withoutDigest = {
      version: "zyra.permission-coordinator/v1" as const,
      runtime: cloneJson(this.runtime),
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      settingsSources: this.settingsSources.map(cloneJson),
      evaluator: this.evaluator.snapshot(),
      settings: this.settings.snapshot(),
      approvals: this.approvals.snapshot(),
      audit: this.audit.snapshot(),
      lastPolicyDigest: this.lastPolicyDigest,
      lastSettingsRevision: this.lastSettingsRevision,
      decisionSequence: this.decisionSequence,
      capturedAt: this.timestamp(),
    };
    return {
      ...withoutDigest,
      digest: digest(withoutDigest),
    };
  }

  health(): JsonObject {
    const snapshot = this.snapshot();
    return {
      canonical_owner: "typescript",
      opened: this.opened,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      mode: this.evaluator.modes.mode,
      mode_revision: this.evaluator.modes.revision,
      policy_revision: this.evaluator.rules.revision,
      policy_digest: this.lastPolicyDigest,
      settings_revision: this.lastSettingsRevision,
      decision_sequence: this.decisionSequence,
      pending_approvals: this.approvals.list("delivered").length,
      pending_continuations: this.evaluator.continuations.pending().length,
      audit_head_hash: snapshot.audit.headHash,
      snapshot_digest: snapshot.digest,
      python_permission_fallback: false,
    };
  }

  close(): void {
    this.approvals.expire(this.now());
    this.opened = false;
  }

  private enforcementResult(
    decisionValue: PermissionDecisionRecord,
    approvalEnvelopeId: string | null,
  ): PermissionEnforcementResult {
    const decision = cloneJson(decisionValue);
    return {
      decision,
      allowed: decision.effect === "allow",
      blocked: decision.effect === "deny",
      pendingApproval: decision.effect === "ask",
      finalArguments: cloneJson(decision.finalArguments),
      recoveryInput: decision.recoveryInput
        ? cloneJson(decision.recoveryInput)
        : null,
      replanRequired: decision.replanRequired,
      approvalEnvelopeId,
      stateDigest: digest({
        decision,
        policy_digest: this.lastPolicyDigest,
        decision_sequence: this.decisionSequence,
      }),
    };
  }

  private validateSnapshotBinding(snapshot: PermissionCoordinatorSnapshot): void {
    if (snapshot.version !== "zyra.permission-coordinator/v1") {
      throw permissionCoordinatorError(
        "unsupported_permission_coordinator_snapshot",
        `unsupported permission coordinator snapshot ${snapshot.version}`,
      );
    }
    const { digest: expectedDigest, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expectedDigest) {
      throw permissionCoordinatorError(
        "permission_coordinator_snapshot_digest_mismatch",
        "permission coordinator snapshot digest does not match its payload",
      );
    }
    if (
      snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw permissionCoordinatorError(
        "permission_coordinator_restore_binding_mismatch",
        "permission coordinator snapshot belongs to a different execution binding",
      );
    }
    if (snapshot.runtime.epoch > this.runtime.epoch) {
      throw permissionCoordinatorError(
        "permission_coordinator_restore_epoch_regression",
        "permission coordinator cannot restore a snapshot from a future epoch",
      );
    }
  }

  private requireOpen(): void {
    if (!this.opened) {
      throw permissionCoordinatorError(
        "permission_coordinator_not_open",
        "permission coordinator must restore and open before evaluation",
      );
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateRuntime(runtime: E02RuntimeIdentity): void {
  if (
    !runtime.runtimeId
    || !runtime.runId
    || !runtime.taskId
    || !runtime.sessionId
    || !runtime.workerRequestId
  ) {
    throw permissionCoordinatorError(
      "permission_runtime_identity_incomplete",
      "permission runtime identity is incomplete",
    );
  }
  if (!Number.isSafeInteger(runtime.epoch) || runtime.epoch < 0) {
    throw permissionCoordinatorError(
      "permission_runtime_epoch_invalid",
      "permission runtime epoch must be a non-negative safe integer",
    );
  }
}

function permissionCoordinatorError(
  code: string,
  message: string,
  details: JsonObject = {},
): Error {
  const error = new Error(message);
  error.name = "PermissionCoordinatorError";
  Object.assign(error, {
    code,
    details: canonicalize(details),
  });
  return error;
}
