import { createHash } from "node:crypto";

import type { JsonObject, ToolExecutionRequest, ToolExecutionResponse } from "../contracts.ts";
import { asBoolean, asObject } from "../contracts.ts";

export const PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION = "zyra.progressive-execution/v1";

export type ExecutionPhase =
  | "exploration"
  | "solution_formed"
  | "first_real_action"
  | "incremental_delivery"
  | "validation"
  | "closeout";

export interface UnresolvedVerificationFailure {
  scope: string;
  failedChecks: string[];
  failedCount: number | null;
  failureKind: "reported_checks" | "reported_failure" | "nonzero_exit" | "transport_failure";
  attemptCount: number;
  lastObservedWorkspaceMutationCount: number;
  diagnosticSummary?: string;
}

export interface ProgressiveExecutionSnapshot {
  version: typeof PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION;
  phase: ExecutionPhase;
  startedAt: number;
  lastEffectiveProgressAt: number;
  providerRounds: number;
  analysisOnlyRounds: number;
  repeatedAnalysisRounds: number;
  realActionCount: number;
  artifactCount: number;
  workspaceMutationCount: number;
  repairMutationCount: number;
  verificationCount: number;
  unresolvedVerificationScopes: string[];
  unresolvedVerificationFailures: UnresolvedVerificationFailure[];
  verificationNudgeCount: number;
  lastVerificationNudgeProviderRound: number;
  preDeliveryObservationCount: number;
  consecutivePreDeliveryObservations: number;
  noDeliveryObservationCount: number;
  consecutiveNoDeliveryObservations: number;
  actionNudgeCount: number;
  postDeliveryActionNudgeCount: number;
  lastActionNudgeObservationCount: number;
  lastActionNudgeNoDeliveryObservationCount: number;
  lastActionNudgeProviderRound: number;
  recoveryInspectionAllowance: number;
  targetedRepairInspectionAllowance: number;
  targetedRepairReserveVersion: number;
  verificationDiagnosticVersion: number;
  environmentRecoveryAwaitingVerification: boolean;
  repairContextId: string;
  activeBackgroundCount: number;
  requiredDeliveryMissing: boolean;
  lastAnalysisDigest: string;
  progressReasons: string[];
}

export interface ProgressiveDecision {
  action: "continue" | "nudge_action" | "nudge_verification" | "closeout";
  reason: string;
  remainingMilliseconds: number | null;
  contextRemainingCharacters: number | null;
  millisecondsSinceEffectiveProgress: number;
  pressure: number;
  snapshot: ProgressiveExecutionSnapshot;
}

interface ProgressiveOptions {
  now?: () => number;
  constraints?: JsonObject;
  deliveryContract?: JsonObject;
  restored?: JsonObject | ProgressiveExecutionSnapshot;
  continuityProgress?: JsonObject;
  repairContextId?: string;
}

export class ProgressiveExecutionRuntime {
  private readonly now: () => number;
  private readonly constraints: JsonObject;
  private readonly requiresWorkspaceMutation: boolean;
  private readonly requiresVerification: boolean;
  private state: ProgressiveExecutionSnapshot;

  constructor(options: ProgressiveOptions = {}) {
    this.now = options.now ?? Date.now;
    this.constraints = options.constraints ?? {};
    const restored = asObject(options.restored as unknown);
    const restoredTargetedReserveVersion = nonnegativeInteger(
      restored.targetedRepairReserveVersion,
    );
    const restoredVerificationDiagnosticVersion = nonnegativeInteger(
      restored.verificationDiagnosticVersion,
    );
    const legacyDiagnosticMigrationRequired = restored.version
      === PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION
      && restoredVerificationDiagnosticVersion < 3;
    const restoredHasVerificationDebt = (
      Array.isArray(restored.unresolvedVerificationScopes)
      && restored.unresolvedVerificationScopes.length > 0
    ) || (
      Array.isArray(restored.unresolvedVerificationFailures)
      && restored.unresolvedVerificationFailures.length > 0
    );
    const repairContextId = String(options.repairContextId ?? "").trim();
    const startedAt = this.now();
    const deliveryContract = asObject(options.deliveryContract);
    // `requires_delivery_artifact` is the adapter's fail-closed duplicate of
    // the workspace-mutation contract when optional metadata is lost.  It is
    // not permission for diagnostic attachments (browser snapshots, spilled
    // command output, screenshots) to masquerade as task delivery.
    this.requiresWorkspaceMutation = asBoolean(deliveryContract.workspace_mutation_required)
      || asBoolean(this.constraints.requires_delivery_artifact);
    const requiresDelivery = this.requiresWorkspaceMutation;
    this.requiresVerification = Object.prototype.hasOwnProperty.call(
      deliveryContract,
      "verification_required",
    )
      ? asBoolean(deliveryContract.verification_required)
      : requiresDelivery;
    const initial: ProgressiveExecutionSnapshot = {
        version: PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION,
        phase: "exploration",
        startedAt,
        lastEffectiveProgressAt: startedAt,
        providerRounds: 0,
        analysisOnlyRounds: 0,
        repeatedAnalysisRounds: 0,
        realActionCount: 0,
        artifactCount: 0,
        workspaceMutationCount: 0,
        repairMutationCount: 0,
        verificationCount: 0,
        unresolvedVerificationScopes: [],
        unresolvedVerificationFailures: [],
        verificationNudgeCount: 0,
        lastVerificationNudgeProviderRound: 0,
        preDeliveryObservationCount: 0,
        consecutivePreDeliveryObservations: 0,
        noDeliveryObservationCount: 0,
        consecutiveNoDeliveryObservations: 0,
        actionNudgeCount: 0,
        postDeliveryActionNudgeCount: 0,
        lastActionNudgeObservationCount: 0,
        lastActionNudgeNoDeliveryObservationCount: 0,
        lastActionNudgeProviderRound: 0,
        recoveryInspectionAllowance: 0,
        targetedRepairInspectionAllowance: 0,
        targetedRepairReserveVersion: 4,
        verificationDiagnosticVersion: 3,
        environmentRecoveryAwaitingVerification: false,
        repairContextId,
        activeBackgroundCount: 0,
        requiredDeliveryMissing: requiresDelivery,
        lastAnalysisDigest: "",
        progressReasons: [],
      };
    this.state = restored.version === PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION
      ? {
        ...initial,
        ...restored as unknown as ProgressiveExecutionSnapshot,
        preDeliveryObservationCount: nonnegativeInteger(
          restored.preDeliveryObservationCount,
        ),
        consecutivePreDeliveryObservations: nonnegativeInteger(
          restored.consecutivePreDeliveryObservations,
        ),
        noDeliveryObservationCount: nonnegativeInteger(
          restored.noDeliveryObservationCount,
        ),
        consecutiveNoDeliveryObservations: nonnegativeInteger(
          restored.consecutiveNoDeliveryObservations,
        ),
        actionNudgeCount: nonnegativeInteger(restored.actionNudgeCount),
        postDeliveryActionNudgeCount: nonnegativeInteger(
          restored.postDeliveryActionNudgeCount,
        ),
        lastActionNudgeObservationCount: nonnegativeInteger(
          restored.lastActionNudgeObservationCount,
        ),
        lastActionNudgeNoDeliveryObservationCount: nonnegativeInteger(
          restored.lastActionNudgeNoDeliveryObservationCount,
        ),
        lastActionNudgeProviderRound: nonnegativeInteger(
          restored.lastActionNudgeProviderRound,
        ),
        recoveryInspectionAllowance: nonnegativeInteger(
          restoredTargetedReserveVersion >= 4
            ? restored.recoveryInspectionAllowance
            : restoredHasVerificationDebt
              ? Math.max(
                nonnegativeInteger(restored.recoveryInspectionAllowance),
                boundedInteger(
                  this.constraints.post_verification_diagnostic_inspection_limit,
                  8,
                  4,
                  32,
                ),
              )
              : restored.recoveryInspectionAllowance,
        ),
        targetedRepairInspectionAllowance: nonnegativeInteger(
          restoredTargetedReserveVersion >= 4
            ? restored.targetedRepairInspectionAllowance
            : restoredHasVerificationDebt
              ? boundedInteger(
                this.constraints.targeted_repair_inspection_limit,
                12,
                4,
                24,
              )
              : 0,
        ),
        targetedRepairReserveVersion: 4,
        verificationDiagnosticVersion: 3,
        environmentRecoveryAwaitingVerification: asBoolean(
          restored.environmentRecoveryAwaitingVerification,
        ),
        // Snapshots written before repair-specific accounting used the total
        // workspace mutation count. Preserve their monotonic lineage once,
        // then stop verification-generated files from impersonating a fix.
        repairMutationCount: restored.repairMutationCount === undefined
          ? nonnegativeInteger(restored.workspaceMutationCount)
          : nonnegativeInteger(restored.repairMutationCount),
        verificationNudgeCount: nonnegativeInteger(
          restored.verificationNudgeCount,
        ),
        unresolvedVerificationScopes: Array.isArray(restored.unresolvedVerificationScopes)
          ? [...new Set(restored.unresolvedVerificationScopes.map(String).filter(Boolean))].slice(-32)
          : [],
        unresolvedVerificationFailures: restoreVerificationFailures(
          restored.unresolvedVerificationFailures,
          !legacyDiagnosticMigrationRequired,
        ),
        lastVerificationNudgeProviderRound: nonnegativeInteger(
          restored.lastVerificationNudgeProviderRound,
        ),
        // Gateway jobs are scoped to the runtime session that created them.
        // A resumed model session cannot safely infer that a persisted count
        // still represents live work, so rebuild it from new job results.
        activeBackgroundCount: 0,
        progressReasons: Array.isArray(restored.progressReasons)
          ? restored.progressReasons.map(String).slice(-64)
          : [],
      }
      : initial;
    const continuity = asObject(options.continuityProgress);
    const continuityDiagnosticMigrationRequired = nonnegativeInteger(
      continuity.targetedRepairReserveVersion,
    ) >= 4 && nonnegativeInteger(continuity.verificationDiagnosticVersion) < 3;
    this.state.repairMutationCount = Math.max(
      this.state.repairMutationCount,
      continuity.repairMutationCount === undefined
        ? nonnegativeInteger(continuity.workspaceMutationCount)
        : nonnegativeInteger(continuity.repairMutationCount),
    );
    if (
      asBoolean(continuity.requiredDeliveryMissing)
      && this.state.workspaceMutationCount === 0
      && this.state.artifactCount === 0
    ) {
      for (const field of [
        "providerRounds",
        "preDeliveryObservationCount",
        "consecutivePreDeliveryObservations",
        "actionNudgeCount",
        "lastActionNudgeObservationCount",
        "lastActionNudgeProviderRound",
      ] as const) {
        this.state[field] = Math.max(
          this.state[field],
          nonnegativeInteger(continuity[field]),
        );
      }
    } else if (
      continuity.requiredDeliveryMissing === false
      && (
        nonnegativeInteger(continuity.workspaceMutationCount) > 0
        || nonnegativeInteger(continuity.artifactCount) > 0
      )
    ) {
      for (const field of [
        "providerRounds",
        "realActionCount",
        "workspaceMutationCount",
        "verificationCount",
        "verificationNudgeCount",
        "lastVerificationNudgeProviderRound",
        "artifactCount",
        "noDeliveryObservationCount",
        "consecutiveNoDeliveryObservations",
        "actionNudgeCount",
        "postDeliveryActionNudgeCount",
        "lastActionNudgeNoDeliveryObservationCount",
      ] as const) {
        this.state[field] = Math.max(
          this.state[field],
          nonnegativeInteger(continuity[field]),
        );
      }
    }
    const continuityScopes = Array.isArray(continuity.unresolvedVerificationScopes)
      ? continuity.unresolvedVerificationScopes.map(String).filter(Boolean)
      : [];
    const continuityFailures = restoreVerificationFailures(
      continuity.unresolvedVerificationFailures,
      !legacyDiagnosticMigrationRequired && !continuityDiagnosticMigrationRequired,
    );
    if (continuityScopes.length > 0 || continuityFailures.length > 0) {
      this.state.unresolvedVerificationScopes = [...new Set([
        ...this.state.unresolvedVerificationScopes,
        ...continuityScopes,
        ...continuityFailures.map((failure) => failure.scope),
      ])].slice(-32);
      this.state.unresolvedVerificationFailures = mergeVerificationFailures(
        this.state.unresolvedVerificationFailures,
        continuityFailures,
      );
      this.state.verificationCount = 0;
      const continuityTargetedReserveVersion = nonnegativeInteger(
        continuity.targetedRepairReserveVersion,
      );
      this.state.targetedRepairInspectionAllowance = Math.max(
        this.state.targetedRepairInspectionAllowance,
        continuityTargetedReserveVersion >= 4
          ? nonnegativeInteger(continuity.targetedRepairInspectionAllowance)
          : boundedInteger(
            this.constraints.targeted_repair_inspection_limit,
            12,
            4,
            24,
          ),
      );
      if (continuityTargetedReserveVersion < 4) {
        this.state.recoveryInspectionAllowance = Math.max(
          this.state.recoveryInspectionAllowance,
          boundedInteger(
            this.constraints.post_verification_diagnostic_inspection_limit,
            8,
            4,
            32,
          ),
        );
      }
    }
    const previousRepairContextId = String(
      restored.repairContextId ?? continuity.repairContextId ?? "",
    ).trim();
    if (
      repairContextId
      && repairContextId !== previousRepairContextId
      && this.state.unresolvedVerificationScopes.length > 0
    ) {
      // A resumed provider context does not contain the complete source bytes
      // inspected by the previous process. Rehydrate only the named-source
      // reserve once for this new context; broad searches and alternate tests
      // remain exhausted, and checkpoint restores inside the same context do
      // not refill it again.
      this.state.targetedRepairInspectionAllowance = Math.max(
        this.state.targetedRepairInspectionAllowance,
        boundedInteger(
          this.constraints.targeted_repair_inspection_limit,
          12,
          4,
          24,
        ),
      );
      this.state.repairContextId = repairContextId;
      this.record("resumed_repair_context_rehydrated");
    } else if (repairContextId) {
      this.state.repairContextId = repairContextId;
    }
    const retryableInvocationScopes = new Set([
      ...retryableVerificationInvocationFailureScopes(
        restored.unresolvedVerificationFailures,
      ),
      ...retryableVerificationInvocationFailureScopes(
        continuity.unresolvedVerificationFailures,
      ),
    ]);
    this.state.unresolvedVerificationFailures = mergeVerificationFailures(
      [],
      this.state.unresolvedVerificationFailures,
    ).filter((failure) => !isRetryableStoredVerificationInvocationFailure(failure));
    this.state.unresolvedVerificationScopes = prioritizeVerificationScopes(
      this.state.unresolvedVerificationScopes,
      this.state.unresolvedVerificationFailures,
    ).filter((scope) => (
      !retryableInvocationScopes.has(scope)
      || this.state.unresolvedVerificationFailures.some((failure) => failure.scope === scope)
    ));
    const priorityFailure = this.state.unresolvedVerificationFailures[0];
    if (
      !this.state.environmentRecoveryAwaitingVerification
      && priorityFailure
      && priorityFailure.lastObservedWorkspaceMutationCount < this.state.repairMutationCount
    ) {
      // Snapshots written before this field existed still carry monotonic
      // repair counters even when bounded reason history was compacted. Any
      // repair newer than the priority failure must be verified before more
      // inspection, whether the repair changed source or recovered services.
      this.state.environmentRecoveryAwaitingVerification = true;
    }
    this.constrainActionableDiagnosticInspection();
    this.state.targetedRepairReserveVersion = 4;
    // The current task contract is authoritative after a checkpoint restore.
    // A stale or formerly unbound snapshot must not erase an outstanding
    // delivery obligation merely because it persisted `false`.
    this.state.requiredDeliveryMissing = requiresDelivery
      && this.state.workspaceMutationCount === 0;
  }

  observeProviderRound(text: string, proposedToolCalls: number): ProgressiveExecutionSnapshot {
    this.state.providerRounds += 1;
    const digest = hash(normalizeAnalysis(text));
    if (proposedToolCalls > 0) {
      this.state.phase = this.state.realActionCount > 0 ? this.state.phase : "solution_formed";
      this.state.analysisOnlyRounds = 0;
      this.record("provider_proposed_tool_action");
    } else {
      this.state.analysisOnlyRounds += 1;
      this.state.repeatedAnalysisRounds = digest && digest === this.state.lastAnalysisDigest
        ? this.state.repeatedAnalysisRounds + 1
        : 0;
    }
    this.state.lastAnalysisDigest = digest;
    return this.snapshot();
  }

  private constrainActionableDiagnosticInspection(): void {
    const diagnostic = this.state.unresolvedVerificationFailures[0]?.diagnosticSummary ?? "";
    if (!isActionableVerificationDiagnostic(diagnostic)) return;
    const previousRecoveryAllowance = this.state.recoveryInspectionAllowance;
    const previousTargetedAllowance = this.state.targetedRepairInspectionAllowance;
    // Once the verifier has produced a concrete exception, missing object, or
    // source location, broad diagnosis is complete. Keep two exact reads for
    // the implicated implementation and its contract/configuration boundary,
    // then require a repair. This prevents a precise root cause from reopening
    // a second broad exploration loop while preserving cross-file validation.
    this.state.recoveryInspectionAllowance = 0;
    this.state.targetedRepairInspectionAllowance = Math.min(
      this.state.targetedRepairInspectionAllowance,
      2,
    );
    if (
      previousRecoveryAllowance !== this.state.recoveryInspectionAllowance
      || previousTargetedAllowance !== this.state.targetedRepairInspectionAllowance
    ) {
      this.record("actionable_verification_diagnostic_bounded");
    }
  }

  observeToolResult(
    request: ToolExecutionRequest,
    response: ToolExecutionResponse,
    readOnly: boolean,
  ): ProgressiveExecutionSnapshot {
    const mutationCommitted = String(
      response.metadata.workspace_mutation_committed ?? "false",
    ).toLowerCase() === "true";
    const mutated = response.ok && mutationCommitted;
    const verificationDriving = asBoolean(
      request.metadata.progressive_verification_driving,
    );
    const repairDriving = Object.prototype.hasOwnProperty.call(
      request.metadata,
      "progressive_repair_driving",
    )
      ? asBoolean(request.metadata.progressive_repair_driving)
      : !verificationDriving;
    const environmentRecoveryDriving = asBoolean(
      request.metadata.progressive_environment_recovery_driving,
    );
    const repairMutated = mutated && repairDriving;
    const artifacts = response.artifacts.length;
    const background = String(
      response.metadata.background_status
      ?? response.output.background_status
      ?? response.output.status
      ?? "",
    ).toLowerCase();
    const backgroundRunning = ["running", "pending", "queued"].includes(background);
    const backgroundTerminal = ["completed", "failed", "cancelled", "stopped"].includes(background);
    const verificationInvocationFailed = verificationDriving
      && !backgroundRunning
      && isRetryableVerificationInvocationFailure(response);
    const verificationPassed = verificationDriving
      && !verificationInvocationFailed
      && verificationResultPassed(response, background);
    const verificationScope = String(
      request.metadata.progressive_verification_scope ?? "",
    ).trim();
    const environmentRecoverySucceeded = response.ok
      && environmentRecoveryDriving
      && !backgroundRunning
      && !repairMutated
      && (!verificationDriving || verificationPassed);
    if (response.ok) this.state.realActionCount += 1;
    if (mutated) this.state.workspaceMutationCount += 1;
    if (repairMutated) this.state.repairMutationCount += 1;
    if (
      environmentRecoverySucceeded
    ) {
      this.state.repairMutationCount += 1;
      this.state.verificationNudgeCount = 0;
      this.state.lastVerificationNudgeProviderRound = 0;
      this.state.environmentRecoveryAwaitingVerification = true;
      this.progress("verification_environment_recovery_committed");
    }
    if (
      verificationDriving
      && !backgroundRunning
      && !environmentRecoverySucceeded
    ) {
      this.state.environmentRecoveryAwaitingVerification = false;
    }
    if (artifacts > 0) this.state.artifactCount += artifacts;
    if (mutated) {
      // Every new delivery invalidates verification of the previous bytes.
      // A build/test command that also produces outputs can discharge the new
      // debt below, but an earlier read or test cannot.
      this.state.verificationCount = 0;
      this.state.verificationNudgeCount = 0;
      this.state.lastVerificationNudgeProviderRound = 0;
      this.state.phase = this.state.realActionCount === 1
        ? "first_real_action"
        : "incremental_delivery";
      this.state.requiredDeliveryMissing = false;
      this.state.consecutivePreDeliveryObservations = 0;
      this.state.consecutiveNoDeliveryObservations = 0;
      this.state.lastActionNudgeNoDeliveryObservationCount = 0;
      this.state.postDeliveryActionNudgeCount = 0;
      this.state.recoveryInspectionAllowance = 0;
      if (repairMutated) {
        // A repair often changes the exact source facts the model must use for
        // its next decision.  Keep a small, named-source validation window so
        // it can inspect the edited implementation and an adjacent contract or
        // state-machine boundary before rerunning the failing suite.  Clearing
        // this reserve forced blind follow-up edits merely to reopen reads,
        // which increased repair churn instead of encouraging verification.
        this.state.targetedRepairInspectionAllowance =
          this.state.unresolvedVerificationScopes.length > 0
            ? boundedInteger(
              this.constraints.post_repair_targeted_inspection_limit,
              4,
              2,
              12,
            )
            : 0;
      }
      this.progress("workspace_mutation_committed");
    } else if (response.ok && !backgroundRunning) {
      this.state.noDeliveryObservationCount += 1;
      this.state.consecutiveNoDeliveryObservations += 1;
      this.record(readOnly
        ? "read_only_observation_without_new_delivery"
        : "non_delivery_action");
      if (this.state.requiredDeliveryMissing) {
        this.state.preDeliveryObservationCount += 1;
        this.state.consecutivePreDeliveryObservations += 1;
        this.record(readOnly
          ? "pre_delivery_read_only_observation"
          : "pre_delivery_non_delivery_action");
      }
    }
    if (
      verificationDriving
      && this.state.requiredDeliveryMissing
      && !backgroundRunning
    ) {
      // A concrete build, test, simulation, or acceptance run commonly
      // discovers the exact evidence needed for the next edit.  Keep the
      // anti-wandering circuit, but open a bounded diagnostic window so the
      // model can inspect the reported files and symbols instead of being
      // forced to edit blindly.  This also covers shell pipelines whose final
      // formatter exits successfully while the underlying test output reports
      // failures.
      this.state.recoveryInspectionAllowance = Math.max(
        this.state.recoveryInspectionAllowance,
        boundedInteger(
          this.constraints.post_verification_diagnostic_inspection_limit,
          8,
          2,
          32,
        ),
      );
      this.record("pre_delivery_verification_diagnostic_window_opened");
    }
    if (
      verificationPassed
      && !this.state.requiredDeliveryMissing
    ) {
      this.state.phase = "validation";
      if (verificationScope) {
        this.state.unresolvedVerificationScopes = this.state.unresolvedVerificationScopes
          .filter((scope) => scope !== verificationScope);
        this.state.unresolvedVerificationFailures = this.state.unresolvedVerificationFailures
          .filter((failure) => failure.scope !== verificationScope);
      }
      if (this.state.unresolvedVerificationScopes.length > 0) {
        this.state.verificationCount = 0;
        this.record("post_delivery_verification_other_scope_still_failed");
      } else if (this.state.verificationCount === 0) {
        this.state.verificationCount = 1;
        this.state.consecutiveNoDeliveryObservations = 0;
        this.state.lastActionNudgeNoDeliveryObservationCount = 0;
        this.state.postDeliveryActionNudgeCount = 0;
        this.state.recoveryInspectionAllowance = 0;
        this.state.targetedRepairInspectionAllowance = 0;
        this.progress("post_delivery_verification_passed");
      } else {
        // A successful check only creates effective progress when it settles
        // verification debt for bytes delivered since the last check.  Reusing
        // an already-green build or test must not reopen a stalled inspection
        // loop without another workspace mutation.
        this.record("post_delivery_verification_repeated_without_delivery");
      }
    } else if (
      verificationInvocationFailed
      && !this.state.requiredDeliveryMissing
    ) {
      // Shell-dialect and command-wrapper failures happen before a build or
      // test can produce trustworthy behavioral evidence. They should prompt
      // an immediate corrected invocation, not become a semantic regression
      // that requires an unrelated workspace edit before the same scope can
      // run again. Preserve any older business verification debt unchanged.
      this.state.verificationCount = 0;
      this.state.verificationNudgeCount = 0;
      this.state.lastVerificationNudgeProviderRound = 0;
      this.record("verification_invocation_failed_before_behavioral_result");
    } else if (
      verificationDriving
      && !backgroundRunning
      && !this.state.requiredDeliveryMissing
    ) {
      // Some verification wrappers intentionally return zero after the
      // underlying job has settled so callers can always read its structured
      // report.  Transport success is not behavioral verification when that
      // report explicitly records a failed status or non-zero failure count.
      // A later failed suite also invalidates an earlier green suite for the
      // same delivered bytes: verification is a fail-closed obligation, not a
      // sticky bit that the first successful check can discharge forever.
      this.state.verificationCount = 0;
      if (
        verificationScope
        && !this.state.unresolvedVerificationScopes.includes(verificationScope)
      ) {
        this.state.unresolvedVerificationScopes.push(verificationScope);
        this.state.unresolvedVerificationScopes = this.state.unresolvedVerificationScopes.slice(-32);
      }
      // A terminal shell_wait can occasionally lose its originating command
      // scope across a fenced continuation even though structured failure
      // debt survived.  When there is exactly one outstanding scope, bind an
      // unscoped failed result to it.  With multiple debts, stay conservative:
      // never treat missing lineage as a new failure that earns another
      // diagnostic window on unchanged bytes.
      const failedVerificationScope = verificationScope
        || (this.state.unresolvedVerificationScopes.length === 1
          ? this.state.unresolvedVerificationScopes[0]
          : "");
      const existingFailure = this.state.unresolvedVerificationFailures
        .find((failure) => failure.scope === failedVerificationScope);
      const failureAlreadyObservedOnCurrentWorkspace = this.state.unresolvedVerificationFailures
        .some((failure) => (
          failure.lastObservedWorkspaceMutationCount === this.state.repairMutationCount
        ));
      const workspaceChangedSinceFailure = existingFailure !== undefined
        ? existingFailure.lastObservedWorkspaceMutationCount !== this.state.repairMutationCount
        : !failureAlreadyObservedOnCurrentWorkspace;
      if (failedVerificationScope) {
        const observedFailure = verificationFailure(
          failedVerificationScope,
          response,
          this.state.repairMutationCount,
          existingFailure,
        );
        this.state.unresolvedVerificationFailures = mergeVerificationFailures(
          this.state.unresolvedVerificationFailures,
          [observedFailure],
        );
        this.state.unresolvedVerificationScopes = prioritizeVerificationScopes(
          this.state.unresolvedVerificationScopes,
          this.state.unresolvedVerificationFailures,
        );
      }
      this.state.verificationNudgeCount = 0;
      this.state.lastVerificationNudgeProviderRound = 0;
      if (workspaceChangedSinceFailure) {
        this.state.recoveryInspectionAllowance = Math.max(
          this.state.recoveryInspectionAllowance,
          boundedInteger(
            this.constraints.post_verification_diagnostic_inspection_limit,
            8,
            4,
            32,
          ),
        );
        this.state.targetedRepairInspectionAllowance = Math.max(
          this.state.targetedRepairInspectionAllowance,
          boundedInteger(
            this.constraints.targeted_repair_inspection_limit,
            12,
            4,
            24,
          ),
        );
      } else {
        this.record("post_delivery_verification_repeated_without_workspace_change");
      }
      this.record("post_delivery_verification_failed");
    }
    if (
      !verificationDriving
      && response.ok
      && isVerificationDiagnosticInspectionRequest(request)
      && this.state.unresolvedVerificationFailures.length > 0
    ) {
      const diagnostic = verificationDiagnosticSummary(response);
      if (diagnostic) {
        const priorityFailure = this.state.unresolvedVerificationFailures[0];
        priorityFailure.diagnosticSummary = mergeDiagnosticSummaries(
          priorityFailure.diagnosticSummary,
          diagnostic,
        );
        this.record("verification_diagnostic_summary_retained");
      }
    }
    this.constrainActionableDiagnosticInspection();
    if (backgroundRunning && request.toolName !== "shell_wait") {
      this.state.activeBackgroundCount += 1;
    } else if (backgroundTerminal) {
      this.state.activeBackgroundCount = Math.max(0, this.state.activeBackgroundCount - 1);
    }
    if (!response.ok) {
      this.state.progressReasons.push(`tool_failed:${request.toolName}`);
      if (
        !readOnly
        && String(response.metadata.pre_delivery_inspection_blocked ?? "false").toLowerCase() !== "true"
        && String(response.metadata.alternate_verification_blocked ?? "false").toLowerCase() !== "true"
        && String(response.metadata.repeated_failed_verification_blocked ?? "false").toLowerCase() !== "true"
        && String(response.metadata.unresolved_verification_validation_only_write_blocked ?? "false").toLowerCase() !== "true"
        && String(response.metadata.unresolved_verification_harness_write_blocked ?? "false").toLowerCase() !== "true"
      ) {
        // A concrete edit/build/service attempt can fail because the target
        // changed or a path was wrong. Permit one bounded observation to
        // re-anchor the next attempt; successful delivery or consumption
        // closes the allowance again.
        // Do not collapse a wider diagnostic window opened by a failed
        // verification above.  The generic delivery fallback is a floor for
        // ordinary failed edits/builds, not a replacement for verification's
        // evidence-driven repair budget.
        this.state.recoveryInspectionAllowance = Math.max(
          this.state.recoveryInspectionAllowance,
          1,
        );
        this.record("failed_delivery_attempt_recovery_inspection_granted");
      }
    }
    return this.snapshot();
  }

  decide(contextCharacters: number, maximumContextCharacters: number): ProgressiveDecision {
    const deadline = finitePositive(this.constraints.external_deadline_epoch_ms);
    const configuredReserveSeconds = finitePositive(
      this.constraints.closeout_reserve_seconds,
    );
    const remainingMilliseconds = deadline === null ? null : Math.max(0, deadline - this.now());
    const reserve = configuredReserveSeconds === null ? null : configuredReserveSeconds * 1_000;
    const contextRemainingCharacters = maximumContextCharacters > 0
      ? Math.max(0, maximumContextCharacters - contextCharacters)
      : null;
    const contextPressure = maximumContextCharacters > 0
      ? contextCharacters / maximumContextCharacters
      : 0;
    const timePressure = remainingMilliseconds !== null && reserve !== null
      ? reserve / Math.max(1, remainingMilliseconds)
      : 0;
    const dynamicReserve = reserve === null
      ? null
      : reserve * Math.max(
        0.5,
        1
          + (this.state.activeBackgroundCount > 0 ? 0.5 : 0)
          + (this.state.realActionCount > 0 && this.state.verificationCount === 0 ? 0.25 : 0)
          - (this.state.requiredDeliveryMissing ? 0.25 : 0),
      );
    const progressAge = Math.max(0, this.now() - this.state.lastEffectiveProgressAt);
    const elapsed = Math.max(0, this.now() - this.state.startedAt);
    const availableWorkWindow = remainingMilliseconds !== null
      ? Math.max(0, remainingMilliseconds - (dynamicReserve ?? 0))
      : Math.max(30_000, elapsed);
    const adaptiveProgressWindow = Math.max(
      5_000,
      Math.min(120_000, availableWorkWindow * 0.2),
    );
    const observationNudgeAfter = boundedInteger(
      this.constraints.pre_delivery_observation_nudge_after,
      8,
      2,
      64,
    );
    const postDeliveryObservationNudgeAfter = boundedInteger(
      this.constraints.post_delivery_observation_nudge_after,
      Math.min(4, observationNudgeAfter),
      2,
      64,
    );
    const noProgressPressure = Math.min(1, this.state.analysisOnlyRounds / 4)
      + Math.min(1, this.state.repeatedAnalysisRounds / 2)
      + Math.min(
        1,
        this.state.consecutivePreDeliveryObservations / observationNudgeAfter,
      )
      + Math.min(1, progressAge / adaptiveProgressWindow);
    const deliveryPressure = this.state.requiredDeliveryMissing ? 0.35 : 0;
    const backgroundRelief = this.state.activeBackgroundCount > 0 ? 0.3 : 0;
    const pressure = Math.max(0, timePressure + contextPressure + noProgressPressure + deliveryPressure - backgroundRelief);
    const hardResourceBoundary = (
      remainingMilliseconds !== null
      && dynamicReserve !== null
      && remainingMilliseconds <= dynamicReserve
    )
      || contextPressure >= 0.97;
    if (hardResourceBoundary) {
      this.state.phase = "closeout";
      return this.decision(
        "closeout",
        this.state.activeBackgroundCount > 0
          ? "resource boundary reached; reconcile active background work and preserve current receipts"
          : this.state.requiredDeliveryMissing
            ? "resource boundary reached while a required delivery is still missing"
            : "resource boundary reached after durable progress",
        remainingMilliseconds,
        contextRemainingCharacters,
        pressure,
      );
    }
    const verificationDebt = this.requiresVerification
      && !this.state.requiredDeliveryMissing
      && this.state.workspaceMutationCount > 0
      && (
        this.state.verificationCount === 0
        || this.state.unresolvedVerificationScopes.length > 0
      );
    const maximumVerificationNudges = boundedInteger(
      this.constraints.post_delivery_verification_nudge_limit,
      3,
      1,
      8,
    );
    if (
      verificationDebt
      && this.state.verificationNudgeCount < maximumVerificationNudges
    ) {
      return this.decision(
        "nudge_verification",
        this.state.unresolvedVerificationScopes.length > 0
          ? verificationDebtReason(this.state)
          : "the latest delivered workspace state has no successful behavioral verification evidence",
        remainingMilliseconds,
        contextRemainingCharacters,
        pressure,
      );
    }
    const observationNudgeDue = this.state.consecutivePreDeliveryObservations >= observationNudgeAfter
      && this.state.consecutivePreDeliveryObservations - this.state.lastActionNudgeObservationCount
        >= observationNudgeAfter;
    const noDeliveryObservationNudgeAfter = this.state.requiredDeliveryMissing
      ? observationNudgeAfter
      : postDeliveryObservationNudgeAfter;
    const noDeliveryObservationNudgeDue = this.state.consecutiveNoDeliveryObservations
      >= noDeliveryObservationNudgeAfter
      && this.state.consecutiveNoDeliveryObservations
        - this.state.lastActionNudgeNoDeliveryObservationCount
        >= noDeliveryObservationNudgeAfter;
    const providerNudgeDue = this.state.actionNudgeCount === 0
      ? this.state.providerRounds >= 1
      : this.state.providerRounds - this.state.lastActionNudgeProviderRound >= 2;
    const preDeliveryNudgeDue = this.state.requiredDeliveryMissing
      && (
        observationNudgeDue
        || (
          providerNudgeDue
          && (
            this.state.analysisOnlyRounds >= 1
            || this.state.repeatedAnalysisRounds >= 1
            || progressAge >= adaptiveProgressWindow
            || timePressure >= 0.5
          )
        )
      );
    if (noDeliveryObservationNudgeDue || preDeliveryNudgeDue) {
      return this.decision(
        "nudge_action",
        noDeliveryObservationNudgeDue
          ? this.state.requiredDeliveryMissing
            ? "broad read-only inspection has continued without advancing the required delivery"
            : "broad inspection has continued without advancing beyond the last durable delivery"
          : "analysis is no longer producing enough new information before the first required delivery",
        remainingMilliseconds,
        contextRemainingCharacters,
        pressure,
      );
    }
    return this.decision(
      "continue",
      "resource margin and effective progress permit normal execution",
      remainingMilliseconds,
      contextRemainingCharacters,
      pressure,
    );
  }

  snapshot(): ProgressiveExecutionSnapshot {
    return structuredClone(this.state);
  }

  inspectionCircuitOpen(): boolean {
    // A concrete failed verification is already the signal that broad
    // orientation has ended.  Start charging its bounded diagnostic window
    // immediately instead of waiting for later no-progress nudges; otherwise
    // several unmetered read-only rounds can slip between the failure and the
    // circuit opening.  The failure remains scoped to the current delivered
    // bytes and is cleared only by a passing rerun of that semantic suite.
    if (this.state.unresolvedVerificationScopes.length > 0) return true;
    const preDelivery = this.state.requiredDeliveryMissing;
    const maximumNudges = boundedInteger(
      preDelivery
        ? this.constraints.pre_delivery_inspection_block_after_nudges
        : this.constraints.post_delivery_inspection_block_after_nudges,
      preDelivery ? 4 : 2,
      preDelivery ? 2 : 1,
      16,
    );
    return preDelivery
      ? this.state.actionNudgeCount >= maximumNudges
      : this.state.postDeliveryActionNudgeCount >= maximumNudges;
  }

  hasUnresolvedVerificationFailures(): boolean {
    return this.state.unresolvedVerificationScopes.length > 0;
  }

  verificationDebtSummary(): string {
    return this.state.unresolvedVerificationScopes.length > 0
      ? verificationDebtReason(this.state)
      : "";
  }

  verificationEnvironmentRecoveryRequired(): boolean {
    const diagnostic = this.state.unresolvedVerificationFailures[0]?.diagnosticSummary ?? "";
    const latestDiagnostic = diagnostic.split(" | ").at(-1) ?? diagnostic;
    return /(?:network (?:is )?unreachable|connection (?:refused|reset)|name or service not known|temporary failure in name resolution|no route to host|service unavailable|ECONNREFUSED|ENETUNREACH)/iu.test(latestDiagnostic);
  }

  verificationEnvironmentRecoveryAwaitingVerification(): boolean {
    return this.state.environmentRecoveryAwaitingVerification
      && this.state.unresolvedVerificationFailures.length > 0;
  }

  failedVerificationScopeAwaitingRepair(scope: string): boolean {
    const normalized = scope.trim();
    if (!normalized) return false;
    const failure = this.state.unresolvedVerificationFailures.find(
      (candidate) => candidate.scope === normalized,
    );
    return failure !== undefined
      && failure.lastObservedWorkspaceMutationCount >= this.state.repairMutationCount;
  }

  backgroundShellSlotsRemaining(): number {
    const maximum = boundedInteger(
      this.constraints.maximum_active_background_shells,
      2,
      1,
      8,
    );
    return Math.max(0, maximum - this.state.activeBackgroundCount);
  }

  consumeRecoveryInspectionAllowance(targetedRepair = false): boolean {
    if (!this.inspectionCircuitOpen()) {
      return false;
    }
    if (this.state.recoveryInspectionAllowance > 0) {
      this.state.recoveryInspectionAllowance -= 1;
      this.record("failed_delivery_attempt_recovery_inspection_consumed");
      return true;
    }
    if (
      targetedRepair
      && this.state.unresolvedVerificationScopes.length > 0
      && this.state.targetedRepairInspectionAllowance > 0
    ) {
      this.state.targetedRepairInspectionAllowance -= 1;
      this.record("targeted_repair_inspection_consumed");
      return true;
    }
    return false;
  }

  recordActionNudge(): ProgressiveExecutionSnapshot {
    this.state.actionNudgeCount += 1;
    if (!this.state.requiredDeliveryMissing) {
      this.state.postDeliveryActionNudgeCount += 1;
    }
    this.state.lastActionNudgeObservationCount = this.state.consecutivePreDeliveryObservations;
    this.state.lastActionNudgeNoDeliveryObservationCount =
      this.state.consecutiveNoDeliveryObservations;
    this.state.lastActionNudgeProviderRound = this.state.providerRounds;
    this.record("progressive_action_requested");
    return this.snapshot();
  }

  recordVerificationNudge(): ProgressiveExecutionSnapshot {
    this.state.verificationNudgeCount += 1;
    this.state.lastVerificationNudgeProviderRound = this.state.providerRounds;
    this.record("progressive_verification_requested");
    return this.snapshot();
  }

  private progress(reason: string): void {
    this.state.lastEffectiveProgressAt = this.now();
    this.record(reason);
  }

  private record(reason: string): void {
    this.state.progressReasons.push(reason);
    this.state.progressReasons = this.state.progressReasons.slice(-64);
  }

  private decision(
    action: ProgressiveDecision["action"],
    reason: string,
    remainingMilliseconds: number | null,
    contextRemainingCharacters: number | null,
    pressure: number,
  ): ProgressiveDecision {
    return {
      action,
      reason,
      remainingMilliseconds,
      contextRemainingCharacters,
      millisecondsSinceEffectiveProgress: Math.max(0, this.now() - this.state.lastEffectiveProgressAt),
      pressure,
      snapshot: this.snapshot(),
    };
  }
}

function restoreVerificationFailures(
  value: unknown,
  retainDiagnostics = true,
): UnresolvedVerificationFailure[] {
  if (!Array.isArray(value)) return [];
  const restored: UnresolvedVerificationFailure[] = [];
  for (const item of value) {
    const failure = asObject(item);
    const scope = String(failure.scope ?? "").trim();
    if (!scope) continue;
    const kind = String(failure.failureKind ?? "reported_failure");
    const retainedDiagnostic = retainedVerificationDiagnosticSummary(
      failure.diagnosticSummary,
    );
    const diagnosticSummary = retainDiagnostics
      || isRetryableVerificationInvocationDiagnostic(retainedDiagnostic)
      ? retainedDiagnostic
      : "";
    restored.push({
      scope,
      failedChecks: Array.isArray(failure.failedChecks)
        ? [...new Set(failure.failedChecks.map(String).map(safeCheckName).filter(Boolean))].slice(0, 12)
        : [],
      failedCount: failure.failedCount === null || failure.failedCount === undefined
        ? null
        : nonnegativeInteger(failure.failedCount),
      failureKind: [
        "reported_checks",
        "reported_failure",
        "nonzero_exit",
        "transport_failure",
      ].includes(kind)
        ? kind as UnresolvedVerificationFailure["failureKind"]
        : "reported_failure",
      attemptCount: Math.max(1, nonnegativeInteger(failure.attemptCount)),
      lastObservedWorkspaceMutationCount: nonnegativeInteger(
        failure.lastObservedWorkspaceMutationCount,
      ),
      ...(diagnosticSummary ? { diagnosticSummary } : {}),
    });
  }
  return mergeVerificationFailures([], restored);
}

function retryableVerificationInvocationFailureScopes(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => asObject(item))
    .filter((failure) => (
      String(failure.failureKind ?? "") === "transport_failure"
      && (
        isRetryableVerificationInvocationDiagnostic(
          String(failure.diagnosticSummary ?? ""),
        )
        || (
          (!Array.isArray(failure.failedChecks) || failure.failedChecks.length === 0)
          && (failure.failedCount === null || failure.failedCount === undefined)
          && !String(failure.diagnosticSummary ?? "").trim()
        )
      )
    ))
    .map((failure) => String(failure.scope ?? "").trim())
    .filter(Boolean);
}

function isRetryableStoredVerificationInvocationFailure(
  failure: UnresolvedVerificationFailure,
): boolean {
  return failure.failureKind === "transport_failure"
    && (
      isRetryableVerificationInvocationDiagnostic(failure.diagnosticSummary ?? "")
      || (
        failure.failedChecks.length === 0
        && failure.failedCount === null
        && !String(failure.diagnosticSummary ?? "").trim()
      )
    );
}

function isRetryableVerificationInvocationFailure(
  response: ToolExecutionResponse,
): boolean {
  const text = [
    response.output.stderr,
    response.output.stdout,
    response.output.code,
    response.output.reason,
    response.summary,
    response.error,
  ].filter((value): value is string => typeof value === "string").join("\n");
  return isRetryableVerificationInvocationDiagnostic(text);
}

function isRetryableVerificationInvocationDiagnostic(value: string): boolean {
  return /(?:^|\n)(?:sh|dash|ash|bash):[^\n]*\bbad substitution\b/iu.test(value)
    || /\bPIPESTATUS(?:\[[^\]]+\])?:\s*(?:parameter not set|unbound variable)\b/iu.test(value)
    || /\b(?:shell|command) was blocked by SandboxGateway\b/iu.test(value)
    || /\b(?:absolute, drive and UNC paths are denied|tool[_ -]?schema[_ -]?validation[_ -]?failed|schema[_ -]?error)\b/iu.test(value)
    // Dependency downloads happen before a build can produce trustworthy
    // behavioral evidence. Treat transient registry/network failures like a
    // failed invocation so the agent can retry or switch mirrors instead of
    // being forced to change unrelated business code. Keep this deliberately
    // narrower than generic application connectivity failures (for example a
    // test receiving HTTP 500), which still require environment recovery.
    || /\b(?:ReadTimeoutError|ConnectTimeoutError|ConnectionResetError|Temporary failure in name resolution)\b/iu.test(value)
    || /\b(?:files\.pythonhosted\.org|pypi\.org|registry\.npmjs\.org|registry-1\.docker\.io)\b[^\n]*(?:timed?\s*out|timeout|ECONNRESET|ETIMEDOUT|EAI_AGAIN)/iu.test(value)
    || /\b(?:ECONNRESET|ETIMEDOUT|EAI_AGAIN)\b[^\n]*(?:npm|pnpm|yarn|registry|package|download|fetch)/iu.test(value)
    || /\bCould not find a version that satisfies the requirement\b[^\n]*\(from versions:\s*none\)/iu.test(value);
}

function mergeVerificationFailures(
  current: readonly UnresolvedVerificationFailure[],
  incoming: readonly UnresolvedVerificationFailure[],
): UnresolvedVerificationFailure[] {
  const incomingScopes = new Set(incoming.map((failure) => failure.scope));
  const merged = new Map<string, UnresolvedVerificationFailure>();
  // Incoming observations outrank retained debt when both were observed on
  // the same workspace bytes. For legacy checkpoints without an observation
  // sequence, a first-seen failure is more likely to be the fresh regression
  // than a scope already retried many times; the stable input order settles
  // the remaining ties without flipping them on every process restart.
  for (const failure of [
    ...incoming,
    ...current.filter((failure) => !incomingScopes.has(failure.scope)),
  ]) {
    if (!merged.has(failure.scope)) {
      merged.set(failure.scope, structuredClone(failure));
    }
  }
  return [...merged.values()]
    .sort((left, right) => (
      right.lastObservedWorkspaceMutationCount
        - left.lastObservedWorkspaceMutationCount
      || left.attemptCount - right.attemptCount
    ))
    .slice(0, 32);
}

function prioritizeVerificationScopes(
  scopes: readonly string[],
  failures: readonly UnresolvedVerificationFailure[],
): string[] {
  return [...new Set([
    ...failures.map((failure) => failure.scope),
    ...scopes,
  ].map(String).filter(Boolean))].slice(0, 32);
}

function verificationFailure(
  scope: string,
  response: ToolExecutionResponse,
  workspaceMutationCount: number,
  existing?: UnresolvedVerificationFailure,
): UnresolvedVerificationFailure {
  const text = [response.output.stdout, response.output.stderr, response.summary]
    .filter((value): value is string => typeof value === "string")
    .join("\n");
  const failedChecks = new Set<string>();
  for (const match of text.matchAll(
    /["']?failed_(?:shards|tests|checks)["']?\s*:\s*\[([^\]]*)\]/giu,
  )) {
    for (const item of match[1].matchAll(/["']([^"']+)["']/gu)) {
      const selected = safeCheckName(item[1]);
      if (selected) failedChecks.add(selected);
    }
  }
  const reportedFailureCounts = [
    ...[...text.matchAll(/["']?failed["']?\s*:\s*([1-9]\d*)/giu)]
      .map((match) => Number(match[1])),
    ...[...text.matchAll(/\b([1-9]\d*)\s+failed\b/giu)]
      .map((match) => Number(match[1])),
  ].filter((value) => Number.isFinite(value));
  const rawReturnCode = response.output.return_code
    ?? response.output.exit_code
    ?? response.metadata.return_code
    ?? response.metadata.exit_code;
  const returnCode = rawReturnCode === undefined || rawReturnCode === null
    ? 0
    : Number(rawReturnCode);
  const failureKind: UnresolvedVerificationFailure["failureKind"] = failedChecks.size > 0
    ? "reported_checks"
    : !response.ok
      ? "transport_failure"
      : Number.isFinite(returnCode) && returnCode !== 0
        ? "nonzero_exit"
        : "reported_failure";
  // A concrete result from a new verification attempt supersedes diagnostics
  // from the previous attempt. Follow-up inspection output for this attempt is
  // still appended by observeToolResult, but stale transport failures must not
  // keep routing the repair strategy after the service has recovered.
  const observedDiagnostic = verificationDiagnosticSummary(response);
  const existingDiagnosticIsCurrent = existing?.lastObservedWorkspaceMutationCount
    === workspaceMutationCount;
  let diagnosticSummary = observedDiagnostic
    || (existingDiagnosticIsCurrent && failedChecks.size === 0
      ? retainedVerificationDiagnosticSummary(existing?.diagnosticSummary)
      : "");
  if (failedChecks.size === 0 && (existing?.failedChecks.length ?? 0) > 0) {
    diagnosticSummary = mergeDiagnosticSummaries(
      diagnosticSummary,
      `Previous verification attempt reported failed checks=${existing!.failedChecks.join(",")}`,
    );
  }
  return {
    scope,
    failedChecks: [...failedChecks].slice(0, 12),
    failedCount: reportedFailureCounts.length > 0
      ? Math.max(...reportedFailureCounts)
      : null,
    failureKind,
    attemptCount: (existing?.attemptCount ?? 0) + 1,
    lastObservedWorkspaceMutationCount: workspaceMutationCount,
    ...(diagnosticSummary ? { diagnosticSummary } : {}),
  };
}

function verificationDiagnosticSummary(response: ToolExecutionResponse): string {
  const lines = [response.output.stdout, response.output.stderr, response.summary]
    .filter((value): value is string => typeof value === "string")
    .join("\n")
    .split(/\r?\n/gu)
    .map((line) => line.replace(/\x1b\[[0-9;]*m/gu, "").trim())
    .filter((line) => line.length > 0 && isVerificationDiagnosticFragment(line));
  return safeDiagnosticSummary([...new Set(lines)].slice(-10).join(" | "));
}

function retainedVerificationDiagnosticSummary(value: unknown): string {
  const selected = safeDiagnosticSummary(value);
  if (!selected) return "";
  return safeDiagnosticSummary(
    selected
      .split(" | ")
      .map((fragment) => fragment.trim())
      .filter((fragment) => isVerificationDiagnosticFragment(fragment))
      .join(" | "),
  );
}

function isVerificationDiagnosticFragment(value: string): boolean {
  return /(?:\b(?:error|exception|traceback|undefined|invalid|mismatch|denied)\b|does not exist|no such (?:file|column|table)|timed? out|HTTP(?: Error)?\s+[45]\d\d|network (?:is )?unreachable|connection (?:refused|reset)|no route to host|name or service not known|temporary failure in name resolution|ECONNREFUSED|ENETUNREACH|\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Violation|UndefinedColumn):\s*\S|previous verification attempt reported failed checks=)/iu.test(value);
}

function mergeDiagnosticSummaries(current: string | undefined, incoming: string): string {
  if (!incoming) return safeDiagnosticSummary(current);
  const existing = safeDiagnosticSummary(current);
  if (!existing) return incoming;
  if (existing.includes(incoming)) return existing;
  return safeDiagnosticSummary(`${existing} | ${incoming}`);
}

function safeDiagnosticSummary(value: unknown): string {
  const selected = String(value ?? "").replace(/\s+/gu, " ").trim();
  return selected.slice(Math.max(0, selected.length - 1_600));
}

function isActionableVerificationDiagnostic(value: string): boolean {
  const diagnostic = safeDiagnosticSummary(value);
  const latest = diagnostic.split(" | ").at(-1)?.trim() ?? diagnostic;
  return /(?:\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Violation|UndefinedColumn):\s*\S|\b(?:undefined column|unknown column|does not exist|no such (?:file|column|table)|cannot find module|module not found|constraint\s+\S+\s+violated)\b|\b[^\s:]+\.(?:ts|tsx|js|jsx|py|go|rs|java|cs):\d+(?::\d+)?\b)/u.test(latest);
}

function isSourceInspectionRequest(request: ToolExecutionRequest): boolean {
  if (request.toolName !== "shell") return false;
  const command = String(request.arguments.command ?? "")
    .trim()
    .replace(/^cd\s+(?:["']\/workspace["']|\/workspace)\s*(?:&&|;)\s*/iu, "")
    .trim();
  return /^(?:cat|type|Get-Content|sed\s+-n|grep|rg|ls|find)\b/iu.test(command);
}

function isVerificationDiagnosticInspectionRequest(
  request: ToolExecutionRequest,
): boolean {
  // Background shell_wait results no longer carry the originating command.
  // Treating every successful wait output as diagnostic let background source
  // reads persist code fragments as verifier evidence. Only an explicit,
  // bounded runtime/schema diagnostic command may enrich verification debt.
  if (request.toolName !== "shell" || isSourceInspectionRequest(request)) return false;
  const command = String(request.arguments.command ?? "")
    .trim()
    .replace(/\s+/gu, " ");
  if (!command) return false;
  if (/\bdocker(?:\.exe)?\s+(?:compose\s+)?logs\b/iu.test(command)) return true;
  if (/\bjournalctl\b/iu.test(command)) return true;
  if (/\bpsql\b/iu.test(command)) {
    return /(?:\\d(?:[a-z+stvx]*)?\b|\bselect\b|\bshow\b|\binformation_schema\b|\bpg_catalog\b)/iu.test(command);
  }
  return /^(?:tail\s+(?:-n\s+)?\d+\s+|Get-Content\s+-Tail\s+\d+\s+)[^|;&]*\.log\b/iu.test(command);
}

function safeCheckName(value: string): string {
  return value.trim().replace(/[^a-z0-9._:/-]+/giu, "-").replace(/^-+|-+$/gu, "").slice(0, 120);
}

function verificationDebtReason(state: ProgressiveExecutionSnapshot): string {
  const details = state.unresolvedVerificationFailures
    .slice(0, 4)
    .map((failure) => {
      const checks = failure.failedChecks.length > 0
        ? ` failed checks=${failure.failedChecks.join(",")}`
        : failure.failedCount !== null
          ? ` failed count=${failure.failedCount}`
          : ` failure=${failure.failureKind}`;
      const diagnostic = failure.diagnosticSummary
        ? ` diagnostic=${failure.diagnosticSummary.slice(-400)}`
        : "";
      return `${failure.scope}${checks}${diagnostic}`;
    });
  const priority = details.length > 0
    ? ` Priority failure: ${details[0]}.`
    : "";
  const remaining = details.length > 1
    ? ` Older unresolved failures: ${details.slice(1).join("; ")}.`
    : "";
  const priorityFailure = state.unresolvedVerificationFailures[0];
  const opaqueFailureGuidance = priorityFailure && !priorityFailure.diagnosticSummary
    ? priorityFailure.attemptCount === 1
      ? " This first-seen regression has no retained root-cause detail yet; reproduce or inspect this priority failure before auditing older scopes."
      : " This repeated verifier result is intentionally opaque: its retained labels are not a line-level diagnostic. Do not invent or search for hidden error detail. If a repair rerun preserves the same labels, treat that hypothesis as insufficient and pivot to a distinct public contract or implementation boundary not already inspected."
    : "";
  return `${state.unresolvedVerificationScopes.length} failed verification scope(s) remain unresolved.${priority}${remaining}${opaqueFailureGuidance} Repair and rerun the priority scope before returning to older or less concrete failures`;
}

function finitePositive(value: unknown): number | null {
  const selected = Number(value);
  return Number.isFinite(selected) && selected > 0 ? selected : null;
}

function nonnegativeInteger(value: unknown): number {
  const selected = Number(value);
  return Number.isFinite(selected) ? Math.max(0, Math.floor(selected)) : 0;
}

function boundedInteger(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = Number(value);
  return Number.isFinite(selected)
    ? Math.max(minimum, Math.min(maximum, Math.floor(selected)))
    : fallback;
}

function normalizeAnalysis(value: string): string {
  return value.trim().replace(/\s+/g, " ").slice(0, 32_768);
}

function hash(value: string): string {
  return value ? createHash("sha256").update(value).digest("hex") : "";
}

function verificationResultPassed(
  response: ToolExecutionResponse,
  backgroundStatus: string,
): boolean {
  if (!response.ok) return false;
  // A background command being accepted by the gateway is not evidence that
  // its build or test passed. Only the terminal shell_wait result may settle
  // semantic verification debt; otherwise each launch briefly erases the
  // prior failure and makes the same failed bytes look like a fresh attempt.
  if (["running", "pending", "queued"].includes(backgroundStatus)) return false;
  if (["failed", "cancelled", "stopped", "error"].includes(backgroundStatus)) {
    return false;
  }
  for (const key of ["return_code", "exit_code"] as const) {
    const raw = response.output[key] ?? response.metadata[key];
    if (raw !== undefined && raw !== null && Number(raw) !== 0) return false;
  }

  const text = [
    response.output.stdout,
    response.output.stderr,
    response.summary,
  ]
    .filter((value): value is string => typeof value === "string")
    .join("\n");
  if (!text) return true;

  // A conventional successful test summary takes precedence over incidental
  // domain values such as an asserted object whose own status is `failed`.
  const conventionalFailures = /\b[1-9]\d*\s+failed\b/i.test(text)
    || /\b(?:failures?|errors?)\s*[:=]\s*[1-9]\d*\b/i.test(text)
    || /\b(?:build|test(?:s| suite)?)\s+failed\b/i.test(text);
  if (conventionalFailures) return false;

  // Shell wrappers commonly preserve diagnostic output but deliberately
  // return zero so the model can inspect it (`|| true`, `tee`, `tail`).  An
  // unhandled runtime traceback, explicit command error, or transport failure
  // is still a failed verification. For compound commands, honor ordering:
  // an expected diagnostic followed by a final passing test summary can pass,
  // while a traceback after `138 passed` must not inherit that earlier green.
  const passMatches = [...text.matchAll(/\b\d+\s+passed\b/gi)];
  const lastPassIndex = passMatches.at(-1)?.index ?? -1;
  const failureIndexes: number[] = [];
  const tracebackIndex = text.search(/\bTraceback \(most recent call last\):/i);
  if (tracebackIndex >= 0 && /\b(?:[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)|Exception):\s*[^\r\n]+/i.test(text.slice(tracebackIndex))) {
    failureIndexes.push(tracebackIndex);
  }
  for (const pattern of [
    /^\s*(?:ERROR|FATAL):\s+\S/im,
    /^\s*(?:[^\s:]+:\s+)?syntax error:\s+\S/im,
    /^\s*(?:request|operation|command)\s+failed:\s+\S/im,
    /\b(?:connection refused|network (?:is )?unreachable|no route to host|name or service not known|temporary failure in name resolution)\b/i,
  ]) {
    const index = text.search(pattern);
    if (index >= 0) failureIndexes.push(index);
  }
  if (failureIndexes.some((index) => index > lastPassIndex)) return false;
  if (lastPassIndex >= 0) return true;

  return !(
    /["']?status["']?\s*:\s*["']?(?:failed|error|cancelled|stopped)["']?/i.test(text)
    || /["']?failed["']?\s*:\s*[1-9]\d*\b/i.test(text)
    || /["']?failed_(?:shards|tests|checks)["']?\s*:\s*\[\s*["']/i.test(text)
  );
}
