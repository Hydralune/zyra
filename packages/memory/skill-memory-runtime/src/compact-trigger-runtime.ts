import {
  cloneJson,
  contractError,
  digest,
  normalizeCompactPolicy,
  normalizeIdentity,
  nowIso,
  stableId,
  timestamp,
  uniqueStrings,
  type CompactPolicy,
  type CompactTriggerObservation,
  type CompactTriggerReceipt,
  type JsonObject,
  type RuntimeIdentity,
} from "./contracts.ts";

export interface CompactTriggerFailure {
  failureId: string;
  triggerId: string;
  compactGeneration: number;
  code: string;
  message: string;
  retryable: boolean;
  failedAt: string;
  consecutiveFailures: number;
  metadata: JsonObject;
  digest: string;
}

export interface CompactTriggerSnapshot {
  version: "zyra.compact-trigger/v1";
  identity: RuntimeIdentity;
  policy: CompactPolicy;
  revision: number;
  receipts: CompactTriggerReceipt[];
  failures: CompactTriggerFailure[];
  lastCompactedAt: string | null;
  lastObservedTokens: number;
  lastGeneration: number;
  checksum: string;
}

export class CompactTriggerRuntime {
  readonly identity: RuntimeIdentity;
  private policy: CompactPolicy;
  private readonly receipts = new Map<string, CompactTriggerReceipt>();
  private readonly failures = new Map<string, CompactTriggerFailure>();
  private readonly now: () => Date;
  private revision = 0;
  private lastCompactedAt: string | null = null;
  private lastObservedTokens = 0;
  private lastGeneration = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    policy?: Partial<CompactPolicy>;
    now?: () => Date;
    snapshot?: CompactTriggerSnapshot | null;
  }) {
    this.identity = normalizeIdentity(options.identity);
    this.policy = normalizeCompactPolicy(options.policy);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  observe(inputValue: CompactTriggerObservation): CompactTriggerReceipt {
    this.assertEnabled();
    const input = normalizeObservation(inputValue, this.identity, this.policy, this.now);
    const triggerId = input.triggerId || stableId(
      "compact-trigger",
      input.identity.sessionId,
      input.kind,
      input.compactGeneration,
      input.observedAt,
      input.currentTokens,
    );
    const existing = this.receipts.get(triggerId);
    if (existing) return cloneJson(existing);
    if (input.compactGeneration < this.lastGeneration) {
      throw contractError("compact_trigger_generation_regression", "compact trigger generation regressed", {
        observed_generation: input.compactGeneration,
        current_generation: this.lastGeneration,
      });
    }
    const active = new Set(input.activeToolCallIds);
    const pending = new Set(input.pendingToolResultIds);
    const unresolvedToolPairs = [...active].filter((id) => pending.has(id) || pending.size === 0);
    const safeToCut = unresolvedToolPairs.length === 0;
    const threshold = Math.min(
      input.contextWindow - input.reservedOutputTokens,
      input.thresholdTokens > 0
        ? input.thresholdTokens
        : Math.floor(input.contextWindow * this.policy.thresholdRatio),
    );
    const overflow = Math.floor(input.contextWindow * this.policy.overflowRatio);
    const effectiveTokens = Math.max(0, input.currentTokens);
    const headroomTokens = Math.max(0, input.contextWindow - input.reservedOutputTokens - effectiveTokens);
    const decision = this.decide(input, effectiveTokens, threshold, overflow, safeToCut);
    const receiptWithoutDigest = {
      triggerId,
      decision: decision.decision,
      reason: decision.reason,
      effectiveTokens,
      headroomTokens,
      safeToCut,
      deferredToolPairIds: unresolvedToolPairs.sort(),
      compactGeneration: input.compactGeneration,
      observedAt: input.observedAt,
      metadata: {
        ...input.metadata,
        trigger_kind: input.kind,
        query_source: input.querySource,
        threshold_tokens: threshold,
        overflow_tokens: overflow,
        current_tokens: effectiveTokens,
        context_window: input.contextWindow,
        reserved_output_tokens: input.reservedOutputTokens,
        active_tool_call_count: active.size,
        pending_tool_result_count: pending.size,
        idle_milliseconds: input.idleMilliseconds,
        source: "oh-my-pi trigger modes adapted to Zyra compact owner",
      },
    } satisfies Omit<CompactTriggerReceipt, "receiptDigest">;
    const receipt: CompactTriggerReceipt = {
      ...receiptWithoutDigest,
      receiptDigest: digest(receiptWithoutDigest),
    };
    this.receipts.set(triggerId, receipt);
    this.lastObservedTokens = effectiveTokens;
    this.lastGeneration = Math.max(this.lastGeneration, input.compactGeneration);
    if (receipt.decision === "compact") this.lastCompactedAt = receipt.observedAt;
    this.revision += 1;
    return cloneJson(receipt);
  }

  recordFailure(input: {
    triggerId: string;
    compactGeneration: number;
    code: string;
    message: string;
    retryable: boolean;
    metadata?: JsonObject;
  }): CompactTriggerFailure {
    const receipt = this.receipts.get(input.triggerId);
    if (!receipt) throw contractError("compact_trigger_not_found", `compact trigger not found: ${input.triggerId}`);
    if (receipt.decision !== "compact") throw contractError("compact_trigger_failure_without_attempt", "cannot fail a trigger that did not request compaction");
    const priorFailures = [...this.failures.values()].filter((item) => item.compactGeneration === input.compactGeneration).length;
    const failedAt = nowIso(this.now);
    const unsigned = {
      failureId: stableId("compact-trigger-failure", input.triggerId, input.code, priorFailures + 1),
      triggerId: input.triggerId,
      compactGeneration: input.compactGeneration,
      code: input.code.trim() || "compact_failed",
      message: input.message.trim().slice(0, 4_096) || "compaction failed",
      retryable: Boolean(input.retryable),
      failedAt,
      consecutiveFailures: priorFailures + 1,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const failure: CompactTriggerFailure = { ...unsigned, digest: digest(unsigned) };
    this.failures.set(failure.failureId, failure);
    this.revision += 1;
    return cloneJson(failure);
  }

  recordSuccess(triggerId: string, compactGeneration: number): void {
    const receipt = this.receipts.get(triggerId);
    if (!receipt) throw contractError("compact_trigger_not_found", `compact trigger not found: ${triggerId}`);
    if (receipt.decision !== "compact") throw contractError("compact_trigger_success_without_attempt", "cannot succeed a trigger that did not request compaction");
    if (compactGeneration < receipt.compactGeneration) throw contractError("compact_trigger_success_generation", "successful compact generation regressed");
    this.lastGeneration = compactGeneration;
    this.lastCompactedAt = nowIso(this.now);
    this.revision += 1;
  }

  configure(policy: Partial<CompactPolicy>): CompactPolicy {
    this.policy = normalizeCompactPolicy({ ...this.policy, ...policy });
    this.revision += 1;
    return cloneJson(this.policy);
  }

  history(limit = 1_000): CompactTriggerReceipt[] {
    return [...this.receipts.values()]
      .sort((left, right) => left.observedAt.localeCompare(right.observedAt))
      .slice(-Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  failureHistory(limit = 1_000): CompactTriggerFailure[] {
    return [...this.failures.values()]
      .sort((left, right) => left.failedAt.localeCompare(right.failedAt))
      .slice(-Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  health(): JsonObject {
    const receipts = [...this.receipts.values()];
    return {
      canonical_owner: "CompactTriggerRuntime",
      revision: this.revision,
      receipt_count: receipts.length,
      compact_count: receipts.filter((item) => item.decision === "compact").length,
      defer_count: receipts.filter((item) => item.decision === "defer").length,
      circuit_open_count: receipts.filter((item) => item.decision === "circuit_open").length,
      failure_count: this.failures.size,
      last_compacted_at: this.lastCompactedAt,
      last_observed_tokens: this.lastObservedTokens,
      compact_generation: this.lastGeneration,
      trigger_modes: ["threshold", "overflow", "mid_turn", "idle", "manual", "reactive"],
      safe_cut_required: true,
    };
  }

  snapshot(): CompactTriggerSnapshot {
    const unsigned: Omit<CompactTriggerSnapshot, "checksum"> = {
      version: "zyra.compact-trigger/v1",
      identity: cloneJson(this.identity),
      policy: cloneJson(this.policy),
      revision: this.revision,
      receipts: [...this.receipts.values()].sort((left, right) => left.observedAt.localeCompare(right.observedAt)).map(cloneJson),
      failures: [...this.failures.values()].sort((left, right) => left.failedAt.localeCompare(right.failedAt)).map(cloneJson),
      lastCompactedAt: this.lastCompactedAt,
      lastObservedTokens: this.lastObservedTokens,
      lastGeneration: this.lastGeneration,
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: CompactTriggerSnapshot): void {
    if (snapshotValue.version !== "zyra.compact-trigger/v1") throw contractError("compact_trigger_snapshot_version", "unsupported compact trigger snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("compact_trigger_snapshot_checksum", "compact trigger snapshot checksum mismatch");
    const restoredIdentity = normalizeIdentity(snapshotValue.identity);
    if (restoredIdentity.sessionId !== this.identity.sessionId || restoredIdentity.taskId !== this.identity.taskId) {
      throw contractError("compact_trigger_snapshot_binding", "compact trigger snapshot belongs to another task/session");
    }
    this.policy = normalizeCompactPolicy(snapshotValue.policy);
    this.receipts.clear();
    for (const receipt of snapshotValue.receipts) {
      const { receiptDigest, ...receiptUnsigned } = receipt;
      if (digest(receiptUnsigned) !== receiptDigest) throw contractError("compact_trigger_receipt_digest", `compact trigger receipt digest mismatch: ${receipt.triggerId}`);
      if (this.receipts.has(receipt.triggerId)) throw contractError("compact_trigger_snapshot_duplicate", `duplicate trigger id ${receipt.triggerId}`);
      this.receipts.set(receipt.triggerId, cloneJson(receipt));
    }
    this.failures.clear();
    for (const failure of snapshotValue.failures) {
      const { digest: failureDigest, ...failureUnsigned } = failure;
      if (digest(failureUnsigned) !== failureDigest) throw contractError("compact_trigger_failure_digest", `compact trigger failure digest mismatch: ${failure.failureId}`);
      this.failures.set(failure.failureId, cloneJson(failure));
    }
    this.revision = snapshotValue.revision;
    this.lastCompactedAt = snapshotValue.lastCompactedAt;
    this.lastObservedTokens = snapshotValue.lastObservedTokens;
    this.lastGeneration = snapshotValue.lastGeneration;
  }

  private decide(
    input: CompactTriggerObservation,
    effectiveTokens: number,
    threshold: number,
    overflow: number,
    safeToCut: boolean,
  ): { decision: CompactTriggerReceipt["decision"]; reason: string } {
    if (input.consecutiveFailures >= this.policy.maximumConsecutiveFailures) {
      return { decision: "circuit_open", reason: "maximum_consecutive_compaction_failures" };
    }
    if (effectiveTokens < this.policy.minimumCompactTokens && input.kind !== "manual") {
      return { decision: "reject", reason: "below_minimum_compact_tokens" };
    }
    const demanded = input.kind === "manual"
      || input.kind === "overflow"
      || input.kind === "reactive"
      || (input.kind === "threshold" && effectiveTokens >= threshold)
      || (input.kind === "mid_turn" && effectiveTokens >= threshold)
      || (input.kind === "idle" && input.idleMilliseconds >= this.policy.idleTriggerMilliseconds && effectiveTokens >= threshold);
    if (!demanded && effectiveTokens >= overflow) {
      if (!safeToCut) return { decision: "defer", reason: "overflow_waiting_for_tool_result" };
      return { decision: "compact", reason: "overflow_guard" };
    }
    if (!demanded) return { decision: "reject", reason: "trigger_condition_not_met" };
    if (!safeToCut) return { decision: "defer", reason: "tool_call_result_pair_in_flight" };
    if (input.kind === "manual") return { decision: "compact", reason: "manual_request" };
    if (input.kind === "overflow" || input.kind === "reactive") return { decision: "compact", reason: `${input.kind}_context_pressure` };
    if (input.kind === "mid_turn") return { decision: "compact", reason: "mid_turn_safe_cut" };
    if (input.kind === "idle") return { decision: "compact", reason: "idle_threshold_reached" };
    return { decision: "compact", reason: "token_threshold_reached" };
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_COMPACT_RESTORE_MEMORY_BRIDGE === "1") {
      throw contractError("compact_restore_memory_bridge_disabled", "06C compact restore memory bridge is disabled");
    }
  }
}

function normalizeObservation(
  value: CompactTriggerObservation,
  identity: RuntimeIdentity,
  policy: CompactPolicy,
  now: () => Date,
): CompactTriggerObservation {
  const normalizedIdentity = normalizeIdentity(value.identity);
  if (normalizedIdentity.taskId !== identity.taskId || normalizedIdentity.sessionId !== identity.sessionId) {
    throw contractError("compact_trigger_binding", "compact trigger belongs to another task/session");
  }
  if (!["threshold", "overflow", "mid_turn", "idle", "manual", "reactive"].includes(value.kind)) {
    throw contractError("compact_trigger_kind", `unsupported compact trigger kind ${value.kind}`);
  }
  const numeric = (input: number, label: string): number => {
    if (!Number.isSafeInteger(input) || input < 0) throw contractError("compact_trigger_metric", `${label} must be a non-negative integer`);
    return input;
  };
  const contextWindow = numeric(value.contextWindow || policy.contextWindow, "context window");
  if (contextWindow < 1) throw contractError("compact_trigger_context_window", "context window must be positive");
  const reservedOutputTokens = numeric(value.reservedOutputTokens || policy.reservedOutputTokens, "reserved output tokens");
  if (reservedOutputTokens >= contextWindow) throw contractError("compact_trigger_reserve", "reserved output tokens must be smaller than the context window");
  return {
    triggerId: value.triggerId?.trim(),
    identity: normalizedIdentity,
    kind: value.kind,
    currentTokens: numeric(value.currentTokens, "current tokens"),
    contextWindow,
    reservedOutputTokens,
    thresholdTokens: numeric(value.thresholdTokens, "threshold tokens"),
    activeToolCallIds: uniqueStrings(value.activeToolCallIds),
    pendingToolResultIds: uniqueStrings(value.pendingToolResultIds),
    idleMilliseconds: numeric(value.idleMilliseconds, "idle milliseconds"),
    compactGeneration: numeric(value.compactGeneration, "compact generation"),
    consecutiveFailures: numeric(value.consecutiveFailures, "consecutive failures"),
    querySource: value.querySource.trim() || "unknown",
    observedAt: value.observedAt ? timestamp(value.observedAt, "compact observed timestamp") : nowIso(now),
    metadata: cloneJson(value.metadata ?? {}),
  };
}
