import {
  SNAPCOMPACT_EXPERIMENT_PROTOCOL,
  cloneJson,
  contractError,
  digest,
  nowIso,
  stableId,
  uniqueStrings,
  type JsonObject,
  type SnapcompactCapability,
  type SnapcompactEvaluation,
  type SnapcompactFrame,
} from "./contracts.ts";

export interface SnapcompactEvaluationInput {
  capabilityId: string;
  providerId: string;
  modelId: string;
  frames: SnapcompactFrame[];
  contextWindow: number;
  restoreBudgetTokens: number;
  metadata?: JsonObject;
}

export interface SnapcompactExperimentalSnapshot {
  version: "zyra.snapcompact-experimental-runtime/v1";
  capabilities: SnapcompactCapability[];
  evaluations: SnapcompactEvaluation[];
  checksum: string;
}

export class SnapcompactExperimentalRuntime {
  private readonly capabilities = new Map<string, SnapcompactCapability>();
  private readonly evaluations = new Map<string, SnapcompactEvaluation>();
  private readonly now: () => Date;

  constructor(now: () => Date = () => new Date()) {
    this.now = now;
  }

  register(value: SnapcompactCapability): SnapcompactCapability {
    const capability = normalizeCapability(value);
    const existing = this.capabilities.get(capability.capabilityId);
    if (existing) {
      if (digest(existing) !== digest(capability)) throw contractError("snapcompact_capability_conflict", `snapcompact capability conflicts: ${capability.capabilityId}`);
      return cloneJson(existing);
    }
    this.capabilities.set(capability.capabilityId, capability);
    return cloneJson(capability);
  }

  evaluate(inputValue: SnapcompactEvaluationInput): SnapcompactEvaluation {
    const input = normalizeEvaluationInput(inputValue);
    const capability = this.capabilities.get(input.capabilityId);
    if (!capability) throw contractError("snapcompact_capability_not_found", `snapcompact capability not found: ${input.capabilityId}`);
    const frames = input.frames.map((frame, index) => normalizeFrame(frame, index));
    const providerCompatible = matches(capability.providerIds, input.providerId);
    const modelCompatible = matches(capability.modelPatterns, input.modelId);
    const totalBytes = frames.reduce((total, frame) => total + frame.byteSize, 0);
    const withinFrameBudget = frames.length <= capability.maximumFrames
      && totalBytes <= capability.maximumTotalBytes
      && frames.every((frame) => frame.byteSize <= capability.maximumFrameBytes && capability.acceptedMediaTypes.includes(frame.mediaType));
    const experimentalEnabled = process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT === "1";
    const eligible = capability.enabled && experimentalEnabled && providerCompatible && modelCompatible && withinFrameBudget;
    const fallbackReason = !capability.enabled
      ? "capability_disabled"
      : !experimentalEnabled
        ? "experimental_feature_flag_disabled"
        : !providerCompatible
          ? "provider_incompatible"
          : !modelCompatible
            ? "model_incompatible"
            : !withinFrameBudget
              ? "frame_budget_exceeded"
              : input.restoreBudgetTokens <= 0 || input.contextWindow <= 0
                ? "invalid_restore_budget"
                : null;
    const evaluatedAt = nowIso(this.now);
    const unsigned = {
      evaluationId: stableId("snapcompact-evaluation", capability.capabilityId, input.providerId, input.modelId, frames.map((item) => item.frameHash)),
      capabilityId: capability.capabilityId,
      eligible: eligible && fallbackReason === null,
      providerCompatible,
      modelCompatible,
      withinFrameBudget,
      frames,
      totalBytes,
      fallbackReason,
      evaluatedAt,
      metadata: {
        ...input.metadata,
        provider_id: input.providerId,
        model_id: input.modelId,
        context_window: input.contextWindow,
        restore_budget_tokens: input.restoreBudgetTokens,
        experimental: true,
        default_path: false,
        compression_effectiveness_claimed: false,
        canonical_compact_owner: "02D ContextCompactionRuntime",
        canonical_restore_owner: "CompactRestoreMemoryBridge",
      },
    } satisfies Omit<SnapcompactEvaluation, "evaluationDigest">;
    const evaluation: SnapcompactEvaluation = { ...unsigned, evaluationDigest: digest(unsigned) };
    const existing = this.evaluations.get(evaluation.evaluationId);
    if (existing && existing.evaluationDigest !== evaluation.evaluationDigest) throw contractError("snapcompact_evaluation_conflict", "snapcompact evaluation id conflict");
    this.evaluations.set(evaluation.evaluationId, evaluation);
    return cloneJson(evaluation);
  }

  get(evaluationId: string): SnapcompactEvaluation | null {
    const evaluation = this.evaluations.get(evaluationId.trim());
    return evaluation ? cloneJson(evaluation) : null;
  }

  list(): SnapcompactEvaluation[] {
    return [...this.evaluations.values()].sort((left, right) => left.evaluatedAt.localeCompare(right.evaluatedAt)).map(cloneJson);
  }

  health(): JsonObject {
    const values = [...this.evaluations.values()];
    return {
      experimental: true,
      feature_flag_enabled: process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT === "1",
      capability_count: this.capabilities.size,
      evaluation_count: values.length,
      eligible_count: values.filter((item) => item.eligible).length,
      fallback_count: values.filter((item) => item.fallbackReason).length,
      default_path: false,
      effectiveness_claimed: false,
      frames_are_canonical_state: false,
    };
  }

  snapshot(): SnapcompactExperimentalSnapshot {
    const unsigned: Omit<SnapcompactExperimentalSnapshot, "checksum"> = {
      version: "zyra.snapcompact-experimental-runtime/v1",
      capabilities: [...this.capabilities.values()].sort((left, right) => left.capabilityId.localeCompare(right.capabilityId)).map(cloneJson),
      evaluations: this.list(),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: SnapcompactExperimentalSnapshot): void {
    if (snapshotValue.version !== "zyra.snapcompact-experimental-runtime/v1") throw contractError("snapcompact_snapshot_version", "unsupported snapcompact experimental snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("snapcompact_snapshot_checksum", "snapcompact experimental snapshot checksum mismatch");
    this.capabilities.clear();
    this.evaluations.clear();
    for (const capability of snapshotValue.capabilities) this.register(capability);
    for (const evaluation of snapshotValue.evaluations) {
      const { evaluationDigest, ...evaluationUnsigned } = evaluation;
      if (digest(evaluationUnsigned) !== evaluationDigest) throw contractError("snapcompact_evaluation_digest", `snapcompact evaluation digest mismatch: ${evaluation.evaluationId}`);
      this.evaluations.set(evaluation.evaluationId, cloneJson(evaluation));
    }
  }
}

function normalizeCapability(value: SnapcompactCapability): SnapcompactCapability {
  if (value.protocol !== SNAPCOMPACT_EXPERIMENT_PROTOCOL) throw contractError("snapcompact_protocol", "unsupported snapcompact capability protocol");
  const capabilityId = value.capabilityId?.trim();
  if (!capabilityId) throw contractError("snapcompact_capability_id", "snapcompact capability id is required");
  const integer = (input: number, label: string): number => {
    if (!Number.isSafeInteger(input) || input < 1) throw contractError("snapcompact_capability_budget", `${label} must be positive`);
    return input;
  };
  return {
    protocol: SNAPCOMPACT_EXPERIMENT_PROTOCOL,
    capabilityId,
    enabled: Boolean(value.enabled),
    providerIds: uniqueStrings(value.providerIds),
    modelPatterns: uniqueStrings(value.modelPatterns),
    maximumFrameBytes: integer(value.maximumFrameBytes, "maximum frame bytes"),
    maximumFrames: integer(value.maximumFrames, "maximum frames"),
    maximumTotalBytes: integer(value.maximumTotalBytes, "maximum total bytes"),
    acceptedMediaTypes: uniqueStrings(value.acceptedMediaTypes),
    rendererRevision: value.rendererRevision?.trim() || "unbound",
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function normalizeEvaluationInput(value: SnapcompactEvaluationInput): SnapcompactEvaluationInput {
  const capabilityId = value.capabilityId?.trim();
  const providerId = value.providerId?.trim();
  const modelId = value.modelId?.trim();
  if (!capabilityId || !providerId || !modelId) throw contractError("snapcompact_evaluation_identity", "snapcompact evaluation requires capability, provider and model ids");
  if (!Number.isSafeInteger(value.contextWindow) || value.contextWindow < 1) throw contractError("snapcompact_context_window", "snapcompact context window must be positive");
  if (!Number.isSafeInteger(value.restoreBudgetTokens) || value.restoreBudgetTokens < 0) throw contractError("snapcompact_restore_budget", "snapcompact restore budget must be non-negative");
  return {
    capabilityId,
    providerId,
    modelId,
    frames: value.frames.map(cloneJson),
    contextWindow: value.contextWindow,
    restoreBudgetTokens: value.restoreBudgetTokens,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function normalizeFrame(value: SnapcompactFrame, index: number): SnapcompactFrame {
  const frameId = value.frameId?.trim() || stableId("snapcompact-frame", index, value.frameHash);
  if (!Number.isSafeInteger(value.sequence) || value.sequence < 0) throw contractError("snapcompact_frame_sequence", "snapcompact frame sequence must be non-negative");
  if (!Number.isSafeInteger(value.width) || value.width < 1 || !Number.isSafeInteger(value.height) || value.height < 1) throw contractError("snapcompact_frame_dimensions", "snapcompact frame dimensions must be positive");
  if (!Number.isSafeInteger(value.byteSize) || value.byteSize < 1) throw contractError("snapcompact_frame_size", "snapcompact frame byte size must be positive");
  const frameHash = value.frameHash?.replace(/^sha256:/, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(frameHash)) throw contractError("snapcompact_frame_hash", "snapcompact frame hash must be sha256");
  return {
    frameId,
    sequence: value.sequence,
    mediaType: value.mediaType?.trim() || "image/png",
    width: value.width,
    height: value.height,
    byteSize: value.byteSize,
    frameHash,
    sourceMessageIds: uniqueStrings(value.sourceMessageIds),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function matches(patterns: string[], value: string): boolean {
  if (patterns.includes("*")) return true;
  return patterns.some((pattern) => {
    try { return new RegExp(pattern, "i").test(value); } catch { return pattern.toLowerCase() === value.toLowerCase(); }
  });
}
