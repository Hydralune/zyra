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
  preDeliveryObservationCount: number;
  consecutivePreDeliveryObservations: number;
  activeBackgroundCount: number;
  requiredDeliveryMissing: boolean;
  lastAnalysisDigest: string;
  progressReasons: string[];
}

export interface ProgressiveDecision {
  action: "continue" | "nudge_action" | "closeout";
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
}

export class ProgressiveExecutionRuntime {
  private readonly now: () => number;
  private readonly constraints: JsonObject;
  private state: ProgressiveExecutionSnapshot;

  constructor(options: ProgressiveOptions = {}) {
    this.now = options.now ?? Date.now;
    this.constraints = options.constraints ?? {};
    const restored = asObject(options.restored as unknown);
    const startedAt = this.now();
    const requiresDelivery = asBoolean(asObject(options.deliveryContract).workspace_mutation_required)
      || asBoolean(this.constraints.requires_delivery_artifact);
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
        preDeliveryObservationCount: 0,
        consecutivePreDeliveryObservations: 0,
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
        // Gateway jobs are scoped to the runtime session that created them.
        // A resumed model session cannot safely infer that a persisted count
        // still represents live work, so rebuild it from new job results.
        activeBackgroundCount: 0,
        progressReasons: Array.isArray(restored.progressReasons)
          ? restored.progressReasons.map(String).slice(-64)
          : [],
      }
      : initial;
    // The current task contract is authoritative after a checkpoint restore.
    // A stale or formerly unbound snapshot must not erase an outstanding
    // delivery obligation merely because it persisted `false`.
    this.state.requiredDeliveryMissing = requiresDelivery
      && this.state.workspaceMutationCount === 0
      && this.state.artifactCount === 0;
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
    const artifacts = response.artifacts.length;
    const background = String(
      response.metadata.background_status
      ?? response.output.background_status
      ?? response.output.status
      ?? "",
    ).toLowerCase();
    const backgroundRunning = ["running", "pending", "queued"].includes(background);
    const backgroundTerminal = ["completed", "failed", "cancelled", "stopped"].includes(background);
    if (response.ok) this.state.realActionCount += 1;
    if (mutated) this.state.workspaceMutationCount += 1;
    if (artifacts > 0) this.state.artifactCount += artifacts;
    if (mutated || artifacts > 0) {
      this.state.phase = this.state.realActionCount === 1
        ? "first_real_action"
        : "incremental_delivery";
      this.state.requiredDeliveryMissing = false;
      this.state.consecutivePreDeliveryObservations = 0;
      this.progress(mutated ? "workspace_mutation_committed" : "artifact_receipt_committed");
    } else if (
      response.ok
      && this.state.requiredDeliveryMissing
      && !backgroundRunning
    ) {
      this.state.preDeliveryObservationCount += 1;
      this.state.consecutivePreDeliveryObservations += 1;
      this.record(readOnly
        ? "pre_delivery_read_only_observation"
        : "pre_delivery_non_delivery_action");
    } else if (readOnly && response.ok && !this.state.requiredDeliveryMissing) {
      this.state.verificationCount += 1;
      this.state.phase = "validation";
      this.progress("post_action_validation_passed");
    }
    if (backgroundRunning && request.toolName !== "shell_wait") {
      this.state.activeBackgroundCount += 1;
    } else if (backgroundTerminal) {
      this.state.activeBackgroundCount = Math.max(0, this.state.activeBackgroundCount - 1);
    }
    if (!response.ok) this.state.progressReasons.push(`tool_failed:${request.toolName}`);
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
    if (
      this.state.requiredDeliveryMissing
      && (
        this.state.analysisOnlyRounds >= 1
        || this.state.repeatedAnalysisRounds >= 1
        || this.state.consecutivePreDeliveryObservations >= observationNudgeAfter
        || progressAge >= adaptiveProgressWindow
        || timePressure >= 0.5
      )
    ) {
      return this.decision(
        "nudge_action",
        this.state.consecutivePreDeliveryObservations >= observationNudgeAfter
          ? "broad read-only inspection has continued without advancing the required delivery"
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
