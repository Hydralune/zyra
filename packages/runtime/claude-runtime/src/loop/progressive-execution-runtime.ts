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
                6,
                2,
                12,
              )
              : 0,
        ),
        targetedRepairReserveVersion: 4,
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
            6,
            2,
            12,
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
          6,
          2,
          12,
        ),
      );
      this.state.repairContextId = repairContextId;
      this.record("resumed_repair_context_rehydrated");
    } else if (repairContextId) {
      this.state.repairContextId = repairContextId;
    }
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
    const verificationPassed = verificationDriving
      && verificationResultPassed(response, background);
    const verificationScope = String(
      request.metadata.progressive_verification_scope ?? "",
    ).trim();
    if (response.ok) this.state.realActionCount += 1;
    if (mutated) this.state.workspaceMutationCount += 1;
    if (repairMutated) this.state.repairMutationCount += 1;
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
      if (repairMutated) this.state.targetedRepairInspectionAllowance = 0;
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
            6,
            2,
            12,
          ),
        );
      } else {
        this.record("post_delivery_verification_repeated_without_workspace_change");
      }
      this.record("post_delivery_verification_failed");
    }
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
      ) {
        // A concrete edit/build/service attempt can fail because the target
        // changed or a path was wrong. Permit one bounded observation to
        // re-anchor the next attempt; successful delivery or consumption
        // closes the allowance again.
        this.state.recoveryInspectionAllowance = 1;
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
    const noDeliveryObservationNudgeDue = this.state.consecutiveNoDeliveryObservations
      >= observationNudgeAfter
      && this.state.consecutiveNoDeliveryObservations
        - this.state.lastActionNudgeNoDeliveryObservationCount
        >= observationNudgeAfter;
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

function restoreVerificationFailures(value: unknown): UnresolvedVerificationFailure[] {
  if (!Array.isArray(value)) return [];
  const restored: UnresolvedVerificationFailure[] = [];
  for (const item of value) {
    const failure = asObject(item);
    const scope = String(failure.scope ?? "").trim();
    if (!scope) continue;
    const kind = String(failure.failureKind ?? "reported_failure");
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
    });
  }
  return mergeVerificationFailures([], restored);
}

function mergeVerificationFailures(
  current: readonly UnresolvedVerificationFailure[],
  incoming: readonly UnresolvedVerificationFailure[],
): UnresolvedVerificationFailure[] {
  const merged = new Map<string, UnresolvedVerificationFailure>();
  for (const failure of [...current, ...incoming]) {
    merged.delete(failure.scope);
    merged.set(failure.scope, structuredClone(failure));
  }
  return [...merged.values()].slice(-32);
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
  const countMatch = text.match(/["']?failed["']?\s*:\s*([1-9]\d*)/iu)
    ?? text.match(/\b([1-9]\d*)\s+failed\b/iu);
  const rawReturnCode = response.output.return_code
    ?? response.output.exit_code
    ?? response.metadata.return_code
    ?? response.metadata.exit_code;
  const returnCode = rawReturnCode === undefined || rawReturnCode === null
    ? 0
    : Number(rawReturnCode);
  const failureKind: UnresolvedVerificationFailure["failureKind"] = !response.ok
    ? "transport_failure"
    : Number.isFinite(returnCode) && returnCode !== 0
      ? "nonzero_exit"
      : failedChecks.size > 0
        ? "reported_checks"
        : "reported_failure";
  return {
    scope,
    failedChecks: [...failedChecks].slice(0, 12),
    failedCount: countMatch ? Number(countMatch[1]) : null,
    failureKind,
    attemptCount: (existing?.attemptCount ?? 0) + 1,
    lastObservedWorkspaceMutationCount: workspaceMutationCount,
  };
}

function safeCheckName(value: string): string {
  return value.trim().replace(/[^a-z0-9._:/-]+/giu, "-").replace(/^-+|-+$/gu, "").slice(0, 120);
}

function verificationDebtReason(state: ProgressiveExecutionSnapshot): string {
  const details = state.unresolvedVerificationFailures
    .slice(-4)
    .map((failure) => {
      const checks = failure.failedChecks.length > 0
        ? ` failed checks=${failure.failedChecks.join(",")}`
        : failure.failedCount !== null
          ? ` failed count=${failure.failedCount}`
          : ` failure=${failure.failureKind}`;
      return `${failure.scope}${checks}`;
    });
  const suffix = details.length > 0 ? `: ${details.join("; ")}` : "";
  return `${state.unresolvedVerificationScopes.length} earlier failed verification scope(s) remain unresolved${suffix}; make a targeted fix, then rerun the same semantic suites`;
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
