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
  verificationCount: number;
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
        verificationCount: 0,
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
          restored.recoveryInspectionAllowance,
        ),
        verificationNudgeCount: nonnegativeInteger(
          restored.verificationNudgeCount,
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
    if (response.ok) this.state.realActionCount += 1;
    if (mutated) this.state.workspaceMutationCount += 1;
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
      this.state.verificationCount += 1;
      this.state.phase = "validation";
      this.state.consecutiveNoDeliveryObservations = 0;
      this.state.lastActionNudgeNoDeliveryObservationCount = 0;
      this.state.postDeliveryActionNudgeCount = 0;
      this.progress("post_delivery_verification_passed");
    } else if (
      verificationDriving
      && response.ok
      && !backgroundRunning
      && !this.state.requiredDeliveryMissing
    ) {
      // Some verification wrappers intentionally return zero after the
      // underlying job has settled so callers can always read its structured
      // report.  Transport success is not behavioral verification when that
      // report explicitly records a failed status or non-zero failure count.
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
      && this.state.verificationCount === 0;
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
        "the latest delivered workspace state has no successful behavioral verification evidence",
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
    const maximumNudges = boundedInteger(
      this.constraints.pre_delivery_inspection_block_after_nudges,
      4,
      2,
      16,
    );
    return this.state.requiredDeliveryMissing
      ? this.state.actionNudgeCount >= maximumNudges
      : this.state.postDeliveryActionNudgeCount >= maximumNudges;
  }

  consumeRecoveryInspectionAllowance(): boolean {
    if (!this.inspectionCircuitOpen() || this.state.recoveryInspectionAllowance < 1) {
      return false;
    }
    this.state.recoveryInspectionAllowance -= 1;
    this.record("failed_delivery_attempt_recovery_inspection_consumed");
    return true;
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
  if (/\b\d+\s+passed\b/i.test(text)) return true;

  return !(
    /["']?status["']?\s*:\s*["']?(?:failed|error|cancelled|stopped)["']?/i.test(text)
    || /["']?failed["']?\s*:\s*[1-9]\d*\b/i.test(text)
    || /["']?failed_(?:shards|tests|checks)["']?\s*:\s*\[\s*["']/i.test(text)
  );
}
