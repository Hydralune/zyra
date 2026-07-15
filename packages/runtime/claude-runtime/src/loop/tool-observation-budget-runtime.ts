import { createHash } from "node:crypto";

export type ToolObservationContent = unknown;
export type ToolObservationDecisionKind = "preserve" | "replace";
export type ToolObservationDecisionReason =
  | "within_budget"
  | "per_observation_limit"
  | "round_budget"
  | "prior_replacement";

export interface ToolObservationBudgetPolicy {
  readonly maxRoundChars: number;
  readonly maxObservationChars: number;
  readonly replacementPreviewChars: number;
  readonly keepRecentObservations: number;
  readonly minimumReplacementSavings: number;
  readonly imageChargeChars: number;
  readonly artifactChargeChars: number;
  readonly errorReserveChars: number;
}

export interface RegisterToolObservationInput {
  readonly callId: string;
  readonly roundId: string;
  readonly toolName: string;
  readonly content: ToolObservationContent;
  readonly isError?: boolean;
  readonly artifactRefs?: readonly string[];
}

export interface ToolObservationCandidate {
  readonly callId: string;
  readonly roundId: string;
  readonly toolName: string;
  readonly sequence: number;
  readonly contentDigest: string;
  readonly contentChars: number;
  readonly chargedChars: number;
  readonly preview: string;
  readonly hasImage: boolean;
  readonly artifactRefs: readonly string[];
  readonly isError: boolean;
  readonly priorDecision?: ToolObservationDecisionKind;
}

export interface ToolObservationDecision {
  readonly callId: string;
  readonly roundId: string;
  readonly kind: ToolObservationDecisionKind;
  readonly reason: ToolObservationDecisionReason;
  readonly originalDigest: string;
  readonly originalChars: number;
  readonly renderedChars: number;
  readonly replacement?: string;
  readonly sequence: number;
}

export interface ToolObservationBudgetPlan {
  readonly roundId: string;
  readonly limitChars: number;
  readonly originalChars: number;
  readonly renderedChars: number;
  readonly replacementCount: number;
  readonly preservedCount: number;
  readonly decisions: readonly ToolObservationDecision[];
  readonly digest: string;
}

export interface ToolObservationBudgetProjection {
  readonly revision: number;
  readonly observationCount: number;
  readonly roundCount: number;
  readonly replacedCount: number;
  readonly originalChars: number;
  readonly renderedChars: number;
  readonly lastPlanDigest?: string;
}

interface StoredObservation {
  readonly callId: string;
  readonly roundId: string;
  readonly toolName: string;
  readonly content: ToolObservationContent;
  readonly contentDigest: string;
  readonly contentChars: number;
  readonly chargedChars: number;
  readonly preview: string;
  readonly hasImage: boolean;
  readonly artifactRefs: readonly string[];
  readonly isError: boolean;
  readonly sequence: number;
}

export interface ToolObservationBudgetSnapshotRecord {
  readonly callId: string;
  readonly roundId: string;
  readonly toolName: string;
  readonly content: ToolObservationContent;
  readonly contentDigest: string;
  readonly contentChars: number;
  readonly chargedChars: number;
  readonly preview: string;
  readonly hasImage: boolean;
  readonly artifactRefs: readonly string[];
  readonly isError: boolean;
  readonly sequence: number;
}

export interface ToolObservationBudgetSnapshot {
  readonly version: 1;
  readonly policy: ToolObservationBudgetPolicy;
  readonly records: readonly ToolObservationBudgetSnapshotRecord[];
  readonly decisions: readonly ToolObservationDecision[];
  readonly plans: readonly ToolObservationBudgetPlan[];
  readonly sequence: number;
  readonly revision: number;
  readonly checksum: string;
}

const DEFAULT_POLICY: ToolObservationBudgetPolicy = Object.freeze({
  maxRoundChars: 48_000,
  maxObservationChars: 18_000,
  replacementPreviewChars: 720,
  keepRecentObservations: 2,
  minimumReplacementSavings: 96,
  imageChargeChars: 8_000,
  artifactChargeChars: 480,
  errorReserveChars: 2_000,
});

const compareText = (left: string, right: string): number => left.localeCompare(right);

const stableObject = (value: unknown, seen = new Set<object>()): unknown => {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (typeof value === "bigint") return `${value.toString()}n`;
  if (typeof value === "undefined") return "[undefined]";
  if (typeof value === "function" || typeof value === "symbol") return String(value);
  if (Array.isArray(value)) {
    if (seen.has(value)) return "[circular]";
    seen.add(value);
    const result = value.map((entry) => stableObject(entry, seen));
    seen.delete(value);
    return result;
  }
  if (typeof value === "object") {
    if (seen.has(value)) return "[circular]";
    seen.add(value);
    const record = value as Record<string, unknown>;
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(record).sort(compareText)) result[key] = stableObject(record[key], seen);
    seen.delete(value);
    return result;
  }
  return String(value);
};

const stableStringify = (value: unknown): string => JSON.stringify(stableObject(value));
const digest = (value: unknown): string => createHash("sha256").update(stableStringify(value)).digest("hex");

const cloneValue = <T>(value: T, seen = new Map<object, unknown>()): T => {
  if (value === null || typeof value !== "object") return value;
  if (seen.has(value)) return seen.get(value) as T;
  if (Array.isArray(value)) {
    const result: unknown[] = [];
    seen.set(value, result);
    for (const entry of value) result.push(cloneValue(entry, seen));
    return result as T;
  }
  const result: Record<string, unknown> = {};
  seen.set(value, result);
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) result[key] = cloneValue(entry, seen);
  return result as T;
};

const positiveInteger = (value: number, label: string): number => {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${label} must be a positive safe integer`);
  return value;
};

const nonNegativeInteger = (value: number, label: string): number => {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${label} must be a non-negative safe integer`);
  return value;
};

const normalizePolicy = (policy: Partial<ToolObservationBudgetPolicy> = {}): ToolObservationBudgetPolicy => {
  const normalized: ToolObservationBudgetPolicy = {
    maxRoundChars: positiveInteger(policy.maxRoundChars ?? DEFAULT_POLICY.maxRoundChars, "maxRoundChars"),
    maxObservationChars: positiveInteger(
      policy.maxObservationChars ?? DEFAULT_POLICY.maxObservationChars,
      "maxObservationChars",
    ),
    replacementPreviewChars: positiveInteger(
      policy.replacementPreviewChars ?? DEFAULT_POLICY.replacementPreviewChars,
      "replacementPreviewChars",
    ),
    keepRecentObservations: nonNegativeInteger(
      policy.keepRecentObservations ?? DEFAULT_POLICY.keepRecentObservations,
      "keepRecentObservations",
    ),
    minimumReplacementSavings: nonNegativeInteger(
      policy.minimumReplacementSavings ?? DEFAULT_POLICY.minimumReplacementSavings,
      "minimumReplacementSavings",
    ),
    imageChargeChars: positiveInteger(policy.imageChargeChars ?? DEFAULT_POLICY.imageChargeChars, "imageChargeChars"),
    artifactChargeChars: positiveInteger(
      policy.artifactChargeChars ?? DEFAULT_POLICY.artifactChargeChars,
      "artifactChargeChars",
    ),
    errorReserveChars: nonNegativeInteger(
      policy.errorReserveChars ?? DEFAULT_POLICY.errorReserveChars,
      "errorReserveChars",
    ),
  };
  if (normalized.maxObservationChars > normalized.maxRoundChars) {
    throw new Error("maxObservationChars cannot exceed maxRoundChars");
  }
  if (normalized.replacementPreviewChars >= normalized.maxObservationChars) {
    throw new Error("replacementPreviewChars must be smaller than maxObservationChars");
  }
  return Object.freeze(normalized);
};

const assertIdentifier = (value: string, label: string): string => {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${label} cannot be empty`);
  return normalized;
};

const contentText = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (content === undefined) return "";
  return stableStringify(content);
};

const imageLike = (value: unknown, seen = new Set<object>()): boolean => {
  if (value === null || typeof value !== "object" || seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some((entry) => imageLike(entry, seen));
  const record = value as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type.toLowerCase() : "";
  const mediaType = typeof record.media_type === "string" ? record.media_type.toLowerCase() : "";
  if (type === "image" || type === "image_url" || mediaType.startsWith("image/")) return true;
  return Object.values(record).some((entry) => imageLike(entry, seen));
};

const previewText = (text: string, limit: number): string => {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) return normalized;
  if (limit <= 3) return normalized.slice(0, limit);
  return `${normalized.slice(0, limit - 3)}...`;
};

const uniqueSorted = (values: readonly string[]): readonly string[] =>
  [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort(compareText);

const decisionSort = (left: ToolObservationDecision, right: ToolObservationDecision): number =>
  left.sequence - right.sequence || compareText(left.callId, right.callId);

const planPayload = (
  plan: Omit<ToolObservationBudgetPlan, "digest">,
): Omit<ToolObservationBudgetPlan, "digest"> => ({ ...plan, decisions: [...plan.decisions].sort(decisionSort) });

const snapshotPayload = (
  snapshot: Omit<ToolObservationBudgetSnapshot, "checksum">,
): Omit<ToolObservationBudgetSnapshot, "checksum"> => ({
  ...snapshot,
  records: [...snapshot.records].sort(
    (left, right) => left.sequence - right.sequence || compareText(left.callId, right.callId),
  ),
  decisions: [...snapshot.decisions].sort(decisionSort),
  plans: [...snapshot.plans].sort((left, right) => compareText(left.roundId, right.roundId)),
});

export class ToolObservationBudgetRuntime {
  private policy: ToolObservationBudgetPolicy;
  private readonly records = new Map<string, StoredObservation>();
  private readonly decisions = new Map<string, ToolObservationDecision>();
  private readonly plans = new Map<string, ToolObservationBudgetPlan>();
  private sequence = 0;
  private revision = 0;

  public constructor(policy: Partial<ToolObservationBudgetPolicy> = {}) {
    this.policy = normalizePolicy(policy);
  }

  public currentPolicy(): ToolObservationBudgetPolicy {
    return { ...this.policy };
  }

  public register(input: RegisterToolObservationInput): ToolObservationCandidate {
    const callId = assertIdentifier(input.callId, "callId");
    const roundId = assertIdentifier(input.roundId, "roundId");
    const toolName = assertIdentifier(input.toolName, "toolName");
    const content = cloneValue(input.content);
    const text = contentText(content);
    const contentDigest = digest(content);
    const artifactRefs = uniqueSorted(input.artifactRefs ?? []);
    const hasImage = imageLike(content);
    const contentChars = text.length;
    const chargedChars =
      contentChars +
      (hasImage ? this.policy.imageChargeChars : 0) +
      artifactRefs.length * this.policy.artifactChargeChars;
    const existing = this.records.get(callId);
    if (existing) {
      if (
        existing.roundId !== roundId ||
        existing.toolName !== toolName ||
        existing.contentDigest !== contentDigest ||
        existing.isError !== Boolean(input.isError) ||
        stableStringify(existing.artifactRefs) !== stableStringify(artifactRefs)
      ) {
        throw new Error(`tool observation ${callId} was registered with different content`);
      }
      return this.toCandidate(existing);
    }
    const record: StoredObservation = {
      callId,
      roundId,
      toolName,
      content,
      contentDigest,
      contentChars,
      chargedChars,
      preview: previewText(text, this.policy.replacementPreviewChars),
      hasImage,
      artifactRefs,
      isError: Boolean(input.isError),
      sequence: this.sequence,
    };
    this.sequence += 1;
    this.records.set(callId, record);
    this.invalidateRound(roundId);
    this.revision += 1;
    return this.toCandidate(record);
  }

  public has(callId: string): boolean {
    return this.records.has(callId);
  }

  public candidate(callId: string): ToolObservationCandidate | undefined {
    const record = this.records.get(callId);
    return record ? this.toCandidate(record) : undefined;
  }

  public candidatesForRound(roundId: string): readonly ToolObservationCandidate[] {
    return this.roundRecords(roundId).map((record) => this.toCandidate(record));
  }

  public partitionCandidates(roundId: string): {
    readonly previouslyReplaced: readonly ToolObservationCandidate[];
    readonly previouslyPreserved: readonly ToolObservationCandidate[];
    readonly undecided: readonly ToolObservationCandidate[];
  } {
    const previouslyReplaced: ToolObservationCandidate[] = [];
    const previouslyPreserved: ToolObservationCandidate[] = [];
    const undecided: ToolObservationCandidate[] = [];
    for (const record of this.roundRecords(roundId)) {
      const candidate = this.toCandidate(record);
      const prior = this.decisions.get(record.callId);
      if (!prior) undecided.push(candidate);
      else if (prior.kind === "replace") previouslyReplaced.push(candidate);
      else previouslyPreserved.push(candidate);
    }
    return { previouslyReplaced, previouslyPreserved, undecided };
  }

  public enforceRound(roundId: string): ToolObservationBudgetPlan {
    const normalizedRoundId = assertIdentifier(roundId, "roundId");
    const records = this.roundRecords(normalizedRoundId);
    const provisional = new Map<string, ToolObservationDecision>();
    let originalChars = 0;
    let renderedChars = 0;
    for (const record of records) {
      originalChars += record.chargedChars;
      const prior = this.decisions.get(record.callId);
      if (prior?.kind === "replace") {
        const decision = cloneValue(prior);
        provisional.set(record.callId, decision);
        renderedChars += decision.renderedChars;
      } else if (record.chargedChars > this.policy.maxObservationChars) {
        const replacement = this.buildReplacement(record, "per_observation_limit");
        const decision = this.replacementDecision(record, replacement, "per_observation_limit");
        provisional.set(record.callId, decision);
        renderedChars += decision.renderedChars;
      } else {
        const decision = this.preserveDecision(record, "within_budget");
        provisional.set(record.callId, decision);
        renderedChars += decision.renderedChars;
      }
    }
    const errorReserve = records.some((record) => record.isError) ? this.policy.errorReserveChars : 0;
    const limit = Math.max(1, this.policy.maxRoundChars - errorReserve);
    const protectedCallIds = new Set(
      records
        .slice(Math.max(0, records.length - this.policy.keepRecentObservations))
        .map((record) => record.callId),
    );
    const selectable = records
      .filter((record) => provisional.get(record.callId)?.kind !== "replace")
      .filter((record) => !protectedCallIds.has(record.callId))
      .sort(
        (left, right) =>
          right.chargedChars - left.chargedChars ||
          left.sequence - right.sequence ||
          compareText(left.callId, right.callId),
      );
    while (renderedChars > limit && selectable.length > 0) {
      const record = selectable.shift();
      if (!record) break;
      const replacement = this.buildReplacement(record, "round_budget");
      const savings = record.chargedChars - replacement.length;
      if (savings < this.policy.minimumReplacementSavings) continue;
      const prior = provisional.get(record.callId);
      renderedChars -= prior?.renderedChars ?? record.chargedChars;
      const decision = this.replacementDecision(record, replacement, "round_budget");
      provisional.set(record.callId, decision);
      renderedChars += decision.renderedChars;
    }
    if (renderedChars > limit) {
      const protectedCandidates = records
        .filter((record) => provisional.get(record.callId)?.kind !== "replace")
        .sort(
          (left, right) =>
            right.chargedChars - left.chargedChars ||
            left.sequence - right.sequence ||
            compareText(left.callId, right.callId),
        );
      for (const record of protectedCandidates) {
        if (renderedChars <= limit) break;
        const replacement = this.buildReplacement(record, "round_budget");
        const savings = record.chargedChars - replacement.length;
        if (savings < this.policy.minimumReplacementSavings) continue;
        const prior = provisional.get(record.callId);
        renderedChars -= prior?.renderedChars ?? record.chargedChars;
        const decision = this.replacementDecision(record, replacement, "round_budget");
        provisional.set(record.callId, decision);
        renderedChars += decision.renderedChars;
      }
    }
    const decisions = [...provisional.values()].sort(decisionSort);
    const unsigned = planPayload({
      roundId: normalizedRoundId,
      limitChars: limit,
      originalChars,
      renderedChars,
      replacementCount: decisions.filter((decision) => decision.kind === "replace").length,
      preservedCount: decisions.filter((decision) => decision.kind === "preserve").length,
      decisions,
    });
    const plan: ToolObservationBudgetPlan = { ...unsigned, digest: digest(unsigned) };
    for (const decision of decisions) this.decisions.set(decision.callId, decision);
    this.plans.set(normalizedRoundId, plan);
    this.revision += 1;
    this.auditPlan(plan);
    return cloneValue(plan);
  }

  public planForRound(roundId: string): ToolObservationBudgetPlan | undefined {
    const plan = this.plans.get(roundId);
    return plan ? cloneValue(plan) : undefined;
  }

  public decisionFor(callId: string): ToolObservationDecision | undefined {
    const decision = this.decisions.get(callId);
    return decision ? cloneValue(decision) : undefined;
  }

  public contentFor(callId: string): ToolObservationContent {
    const record = this.records.get(callId);
    if (!record) throw new Error(`unknown tool observation ${callId}`);
    const decision = this.decisions.get(callId);
    if (decision?.kind === "replace") return decision.replacement ?? this.buildReplacement(record, decision.reason);
    return cloneValue(record.content);
  }

  public reconstructDecision(callId: string): ToolObservationDecision {
    const record = this.records.get(callId);
    if (!record) throw new Error(`unknown tool observation ${callId}`);
    const decision = this.decisions.get(callId);
    if (!decision) return this.preserveDecision(record, "within_budget");
    if (decision.originalDigest !== record.contentDigest) {
      throw new Error(`tool observation ${callId} decision no longer matches its content`);
    }
    return cloneValue(decision);
  }

  public releaseRound(roundId: string): number {
    const normalizedRoundId = assertIdentifier(roundId, "roundId");
    const callIds = this.roundRecords(normalizedRoundId).map((record) => record.callId);
    for (const callId of callIds) {
      this.records.delete(callId);
      this.decisions.delete(callId);
    }
    this.plans.delete(normalizedRoundId);
    if (callIds.length > 0) this.revision += 1;
    return callIds.length;
  }

  public reconfigure(policy: Partial<ToolObservationBudgetPolicy>): void {
    const next = normalizePolicy({ ...this.policy, ...policy });
    if (stableStringify(next) === stableStringify(this.policy)) return;
    this.policy = next;
    this.decisions.clear();
    this.plans.clear();
    this.revision += 1;
  }

  public project(): ToolObservationBudgetProjection {
    const plans = [...this.plans.values()].sort((left, right) => compareText(left.roundId, right.roundId));
    const originalChars = [...this.records.values()].reduce((total, record) => total + record.chargedChars, 0);
    const renderedChars = [...this.records.values()].reduce((total, record) => {
      const decision = this.decisions.get(record.callId);
      return total + (decision?.renderedChars ?? record.chargedChars);
    }, 0);
    return {
      revision: this.revision,
      observationCount: this.records.size,
      roundCount: new Set([...this.records.values()].map((record) => record.roundId)).size,
      replacedCount: [...this.decisions.values()].filter((decision) => decision.kind === "replace").length,
      originalChars,
      renderedChars,
      lastPlanDigest: plans.at(-1)?.digest,
    };
  }

  public snapshot(): ToolObservationBudgetSnapshot {
    const unsigned = snapshotPayload({
      version: 1,
      policy: this.currentPolicy(),
      records: [...this.records.values()].map((record) => ({
        callId: record.callId,
        roundId: record.roundId,
        toolName: record.toolName,
        content: cloneValue(record.content),
        contentDigest: record.contentDigest,
        contentChars: record.contentChars,
        chargedChars: record.chargedChars,
        preview: record.preview,
        hasImage: record.hasImage,
        artifactRefs: [...record.artifactRefs],
        isError: record.isError,
        sequence: record.sequence,
      })),
      decisions: [...this.decisions.values()].map((decision) => cloneValue(decision)),
      plans: [...this.plans.values()].map((plan) => cloneValue(plan)),
      sequence: this.sequence,
      revision: this.revision,
    });
    return { ...unsigned, checksum: digest(unsigned) };
  }

  public restore(snapshot: ToolObservationBudgetSnapshot): void {
    if (snapshot.version !== 1) throw new Error(`unsupported tool observation budget snapshot ${snapshot.version}`);
    const unsigned = snapshotPayload({
      version: snapshot.version,
      policy: snapshot.policy,
      records: snapshot.records,
      decisions: snapshot.decisions,
      plans: snapshot.plans,
      sequence: snapshot.sequence,
      revision: snapshot.revision,
    });
    const expectedChecksum = digest(unsigned);
    if (expectedChecksum !== snapshot.checksum) {
      throw new Error("tool observation budget snapshot checksum mismatch");
    }
    const restoredPolicy = normalizePolicy(snapshot.policy);
    const restoredRecords = new Map<string, StoredObservation>();
    for (const raw of [...snapshot.records].sort(
      (left, right) => left.sequence - right.sequence || compareText(left.callId, right.callId),
    )) {
      if (restoredRecords.has(raw.callId)) throw new Error(`duplicate tool observation ${raw.callId} in snapshot`);
      const content = cloneValue(raw.content);
      const actualDigest = digest(content);
      if (actualDigest !== raw.contentDigest) throw new Error(`tool observation ${raw.callId} content digest mismatch`);
      const text = contentText(content);
      if (text.length !== raw.contentChars) throw new Error(`tool observation ${raw.callId} content size mismatch`);
      const artifactRefs = uniqueSorted(raw.artifactRefs);
      const hasImage = imageLike(content);
      const chargedChars =
        text.length +
        (hasImage ? restoredPolicy.imageChargeChars : 0) +
        artifactRefs.length * restoredPolicy.artifactChargeChars;
      if (chargedChars !== raw.chargedChars || hasImage !== raw.hasImage) {
        throw new Error(`tool observation ${raw.callId} charge mismatch`);
      }
      restoredRecords.set(raw.callId, {
        callId: assertIdentifier(raw.callId, "snapshot callId"),
        roundId: assertIdentifier(raw.roundId, "snapshot roundId"),
        toolName: assertIdentifier(raw.toolName, "snapshot toolName"),
        content,
        contentDigest: raw.contentDigest,
        contentChars: raw.contentChars,
        chargedChars: raw.chargedChars,
        preview: previewText(text, restoredPolicy.replacementPreviewChars),
        hasImage,
        artifactRefs,
        isError: Boolean(raw.isError),
        sequence: nonNegativeInteger(raw.sequence, "snapshot record sequence"),
      });
    }
    const restoredDecisions = new Map<string, ToolObservationDecision>();
    for (const decision of snapshot.decisions) {
      const record = restoredRecords.get(decision.callId);
      if (!record || record.roundId !== decision.roundId) {
        throw new Error(`decision ${decision.callId} has no matching observation`);
      }
      if (record.contentDigest !== decision.originalDigest) {
        throw new Error(`decision ${decision.callId} has stale content digest`);
      }
      restoredDecisions.set(decision.callId, cloneValue(decision));
    }
    const restoredPlans = new Map<string, ToolObservationBudgetPlan>();
    for (const plan of snapshot.plans) {
      this.auditPlanValue(plan, restoredRecords);
      restoredPlans.set(plan.roundId, cloneValue(plan));
    }
    const maxSequence = Math.max(-1, ...[...restoredRecords.values()].map((record) => record.sequence));
    if (snapshot.sequence <= maxSequence) {
      throw new Error("snapshot sequence does not advance past stored observations");
    }
    this.policy = restoredPolicy;
    this.records.clear();
    this.decisions.clear();
    this.plans.clear();
    for (const [callId, record] of restoredRecords) this.records.set(callId, record);
    for (const [callId, decision] of restoredDecisions) this.decisions.set(callId, decision);
    for (const [roundId, plan] of restoredPlans) this.plans.set(roundId, plan);
    this.sequence = nonNegativeInteger(snapshot.sequence, "snapshot sequence");
    this.revision = nonNegativeInteger(snapshot.revision, "snapshot revision");
    this.audit();
  }

  public audit(): void {
    const seenSequences = new Set<number>();
    for (const record of this.records.values()) {
      if (seenSequences.has(record.sequence)) throw new Error(`duplicate observation sequence ${record.sequence}`);
      seenSequences.add(record.sequence);
      if (digest(record.content) !== record.contentDigest) {
        throw new Error(`tool observation ${record.callId} mutated after registration`);
      }
      if (record.contentChars !== contentText(record.content).length) {
        throw new Error(`tool observation ${record.callId} has invalid content size`);
      }
      const decision = this.decisions.get(record.callId);
      if (decision && decision.originalDigest !== record.contentDigest) {
        throw new Error(`tool observation ${record.callId} has a stale decision`);
      }
    }
    for (const [callId] of this.decisions) {
      if (!this.records.has(callId)) throw new Error(`decision ${callId} has no observation`);
    }
    for (const plan of this.plans.values()) this.auditPlan(plan);
  }

  private roundRecords(roundId: string): StoredObservation[] {
    const normalizedRoundId = assertIdentifier(roundId, "roundId");
    return [...this.records.values()]
      .filter((record) => record.roundId === normalizedRoundId)
      .sort(
        (left, right) => left.sequence - right.sequence || compareText(left.callId, right.callId),
      );
  }

  private toCandidate(record: StoredObservation): ToolObservationCandidate {
    return {
      callId: record.callId,
      roundId: record.roundId,
      toolName: record.toolName,
      sequence: record.sequence,
      contentDigest: record.contentDigest,
      contentChars: record.contentChars,
      chargedChars: record.chargedChars,
      preview: record.preview,
      hasImage: record.hasImage,
      artifactRefs: [...record.artifactRefs],
      isError: record.isError,
      priorDecision: this.decisions.get(record.callId)?.kind,
    };
  }

  private invalidateRound(roundId: string): void {
    this.plans.delete(roundId);
    for (const record of this.roundRecords(roundId)) {
      const prior = this.decisions.get(record.callId);
      if (prior?.kind === "preserve") this.decisions.delete(record.callId);
    }
  }

  private preserveDecision(
    record: StoredObservation,
    reason: ToolObservationDecisionReason,
  ): ToolObservationDecision {
    return {
      callId: record.callId,
      roundId: record.roundId,
      kind: "preserve",
      reason,
      originalDigest: record.contentDigest,
      originalChars: record.chargedChars,
      renderedChars: record.chargedChars,
      sequence: record.sequence,
    };
  }

  private replacementDecision(
    record: StoredObservation,
    replacement: string,
    reason: ToolObservationDecisionReason,
  ): ToolObservationDecision {
    return {
      callId: record.callId,
      roundId: record.roundId,
      kind: "replace",
      reason,
      originalDigest: record.contentDigest,
      originalChars: record.chargedChars,
      renderedChars: replacement.length,
      replacement,
      sequence: record.sequence,
    };
  }

  private buildReplacement(record: StoredObservation, reason: ToolObservationDecisionReason): string {
    const metadata = [
      `tool=${record.toolName}`,
      `call=${record.callId}`,
      `reason=${reason}`,
      `chars=${record.contentChars}`,
      `sha256=${record.contentDigest}`,
    ];
    if (record.hasImage) metadata.push("image=true");
    if (record.isError) metadata.push("error=true");
    if (record.artifactRefs.length > 0) metadata.push(`artifacts=${record.artifactRefs.join(",")}`);
    const preview = record.preview ? ` preview=${record.preview}` : "";
    return `[tool_observation_omitted ${metadata.join(" ")}]${preview}`;
  }

  private auditPlan(plan: ToolObservationBudgetPlan): void {
    this.auditPlanValue(plan, this.records);
  }

  private auditPlanValue(plan: ToolObservationBudgetPlan, records: ReadonlyMap<string, StoredObservation>): void {
    const unsigned = planPayload({
      roundId: plan.roundId,
      limitChars: plan.limitChars,
      originalChars: plan.originalChars,
      renderedChars: plan.renderedChars,
      replacementCount: plan.replacementCount,
      preservedCount: plan.preservedCount,
      decisions: plan.decisions,
    });
    if (digest(unsigned) !== plan.digest) throw new Error(`tool observation budget plan ${plan.roundId} digest mismatch`);
    const roundRecords = [...records.values()].filter((record) => record.roundId === plan.roundId);
    if (roundRecords.length !== plan.decisions.length) {
      throw new Error(`tool observation budget plan ${plan.roundId} is incomplete`);
    }
    let originalChars = 0;
    let renderedChars = 0;
    let replacementCount = 0;
    const seen = new Set<string>();
    for (const decision of plan.decisions) {
      if (seen.has(decision.callId)) throw new Error(`plan ${plan.roundId} contains duplicate ${decision.callId}`);
      seen.add(decision.callId);
      const record = records.get(decision.callId);
      if (!record || record.roundId !== plan.roundId) {
        throw new Error(`plan ${plan.roundId} references unknown ${decision.callId}`);
      }
      if (decision.originalDigest !== record.contentDigest) {
        throw new Error(`plan ${plan.roundId} references stale ${decision.callId}`);
      }
      if (decision.kind === "replace" && !decision.replacement) {
        throw new Error(`plan ${plan.roundId} replacement ${decision.callId} has no content`);
      }
      originalChars += decision.originalChars;
      renderedChars += decision.renderedChars;
      replacementCount += decision.kind === "replace" ? 1 : 0;
    }
    if (
      originalChars !== plan.originalChars ||
      renderedChars !== plan.renderedChars ||
      replacementCount !== plan.replacementCount ||
      plan.decisions.length - replacementCount !== plan.preservedCount
    ) {
      throw new Error(`tool observation budget plan ${plan.roundId} totals mismatch`);
    }
  }
}
