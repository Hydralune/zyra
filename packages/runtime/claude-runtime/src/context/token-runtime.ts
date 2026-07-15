import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
  thinkingTokens: number;
}

export interface TokenBudget {
  maximum: number;
  reservedForOutput: number;
  reservedForTools: number;
  warningBuffer: number;
  hardBuffer: number;
}

export interface BudgetTracker {
  initial: number;
  remaining: number;
  consumed: number;
  completionRatio: number;
  consecutiveDiminishingTurns: number;
  lastTurnTokens: number;
  revision: number;
}

export interface TokenBudgetDecision {
  kind: "continue" | "warn" | "compact" | "stop";
  reason: string;
  remaining: number;
  consumed: number;
  completionRatio: number;
  suggestedOutputTokens: number;
  shouldCompact: boolean;
  shouldStop: boolean;
  revision: number;
}

export interface TokenEstimate {
  characters: number;
  words: number;
  codeTokens: number;
  punctuationTokens: number;
  imageTokens: number;
  estimatedTokens: number;
  digest: string;
}

export interface ContextWindowSegment {
  id: string;
  kind: "system" | "user" | "assistant" | "tool_use" | "tool_result" | "attachment";
  content: string;
  pinned: boolean;
  toolPairId: string | null;
  priority: number;
  createdSequence: number;
  metadata: JsonObject;
}

export interface ContextWindowPlan {
  selected: ContextWindowSegment[];
  dropped: ContextWindowSegment[];
  selectedTokens: number;
  droppedTokens: number;
  budgetTokens: number;
  pairInvariantOk: boolean;
  digest: string;
}

const COMPLETION_THRESHOLD = 0.9;
const DIMINISHING_THRESHOLD = 500;

function positive(value: number, fallback = 0): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return "{" + Object.keys(record).sort().map((key) => {
      return JSON.stringify(key) + ":" + canonical(record[key]);
    }).join(",") + "}";
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return "sha256:" + createHash("sha256").update(canonical(value)).digest("hex");
}

function unicodeWordCount(value: string): number {
  const matches = value.match(/[\p{L}\p{N}_]+/gu);
  return matches?.length ?? 0;
}

function codeTokenCount(value: string): number {
  const operators = value.match(/=>|===|!==|==|!=|<=|>=|\+\+|--|&&|\|\||\?\?|[{}()[\].,;:+*/%<>=!?&|^-]/g);
  const identifiers = value.match(/[A-Za-z_$][A-Za-z0-9_$]*/g);
  const strings = value.match(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g);
  return (operators?.length ?? 0) + (identifiers?.length ?? 0) + (strings?.length ?? 0);
}

function punctuationCount(value: string): number {
  return value.match(/[^\p{L}\p{N}\s_]/gu)?.length ?? 0;
}

function imageTokenEstimate(value: JsonValue): number {
  if (value === null || typeof value !== "object") return 0;
  if (Array.isArray(value)) {
    return value.reduce<number>((sum, item) => sum + imageTokenEstimate(item), 0);
  }
  const record = value as JsonObject;
  const kind = String(record.type ?? record.kind ?? "").toLowerCase();
  if (kind === "image" || kind === "image_url" || kind === "screenshot") {
    const width = typeof record.width === "number" ? record.width : 1024;
    const height = typeof record.height === "number" ? record.height : 768;
    const tiles = Math.max(1, Math.ceil(width / 512) * Math.ceil(height / 512));
    return 85 + tiles * 170;
  }
  return Object.values(record).reduce<number>((sum, item) => sum + imageTokenEstimate(item), 0);
}

export class ContextTokenRuntime {
  private tracker: BudgetTracker;
  private estimates = new Map<string, TokenEstimate>();

  constructor(initialBudget = 200000) {
    const initial = positive(initialBudget, 200000);
    this.tracker = {
      initial,
      remaining: initial,
      consumed: 0,
      completionRatio: 0,
      consecutiveDiminishingTurns: 0,
      lastTurnTokens: 0,
      revision: 0,
    };
  }

  tokenBudget_module(input: {
    action: "estimate" | "consume" | "decide" | "plan";
    content?: JsonValue;
    usage?: Partial<TokenUsage>;
    budget?: Partial<TokenBudget>;
    segments?: ContextWindowSegment[];
  }): TokenEstimate | BudgetTracker | TokenBudgetDecision | ContextWindowPlan {
    if (input.action === "estimate") return this.estimate(input.content ?? null);
    if (input.action === "consume") return this.consume(input.usage ?? {});
    if (input.action === "plan") {
      return this.planWindow(input.segments ?? [], input.budget?.maximum ?? this.tracker.remaining);
    }
    return this.decide(input.budget ?? {});
  }

  estimate(value: JsonValue): TokenEstimate {
    const serialized = typeof value === "string" ? value : canonical(value);
    const existing = this.estimates.get(serialized);
    if (existing) return structuredClone(existing);
    const words = unicodeWordCount(serialized);
    const codeTokens = codeTokenCount(serialized);
    const punctuationTokens = punctuationCount(serialized);
    const imageTokens = imageTokenEstimate(value);
    const characterBaseline = Math.ceil(serialized.length / 4);
    const languageBaseline = Math.ceil(words * 1.18);
    const syntaxBaseline = Math.ceil(codeTokens * 0.72 + punctuationTokens * 0.18);
    const estimatedTokens = Math.max(1, characterBaseline, languageBaseline + syntaxBaseline) + imageTokens;
    const result: TokenEstimate = {
      characters: serialized.length,
      words,
      codeTokens,
      punctuationTokens,
      imageTokens,
      estimatedTokens,
      digest: digest(serialized),
    };
    if (this.estimates.size >= 2048) {
      const first = this.estimates.keys().next().value;
      if (typeof first === "string") this.estimates.delete(first);
    }
    this.estimates.set(serialized, result);
    return structuredClone(result);
  }

  consume(value: Partial<TokenUsage>): BudgetTracker {
    const turnTokens = positive(value.inputTokens ?? 0)
      + positive(value.outputTokens ?? 0)
      + positive(value.cacheCreationTokens ?? 0)
      + positive(value.thinkingTokens ?? 0);
    const effective = Math.max(0, turnTokens - positive(value.cacheReadTokens ?? 0));
    this.tracker.consumed = Math.min(this.tracker.initial, this.tracker.consumed + effective);
    this.tracker.remaining = Math.max(0, this.tracker.initial - this.tracker.consumed);
    this.tracker.completionRatio = this.tracker.initial === 0
      ? 1
      : this.tracker.consumed / this.tracker.initial;
    if (effective <= DIMINISHING_THRESHOLD && this.tracker.lastTurnTokens <= DIMINISHING_THRESHOLD) {
      this.tracker.consecutiveDiminishingTurns += 1;
    } else {
      this.tracker.consecutiveDiminishingTurns = 0;
    }
    this.tracker.lastTurnTokens = effective;
    this.tracker.revision += 1;
    return this.snapshot();
  }

  decide(value: Partial<TokenBudget>): TokenBudgetDecision {
    const maximum = positive(value.maximum ?? this.tracker.initial, this.tracker.initial);
    const output = positive(value.reservedForOutput ?? 16000, 16000);
    const tools = positive(value.reservedForTools ?? 8000, 8000);
    const warning = positive(value.warningBuffer ?? 20000, 20000);
    const hard = positive(value.hardBuffer ?? 13000, 13000);
    const available = Math.min(this.tracker.remaining, maximum) - output - tools;
    let kind: TokenBudgetDecision["kind"] = "continue";
    let reason = "budget_available";
    if (available <= 0 || this.tracker.remaining <= hard) {
      kind = "stop";
      reason = "hard_context_limit";
    } else if (this.tracker.completionRatio >= COMPLETION_THRESHOLD) {
      kind = "compact";
      reason = "completion_threshold";
    } else if (this.tracker.remaining <= warning + output + tools) {
      kind = "compact";
      reason = "warning_buffer";
    } else if (this.tracker.consecutiveDiminishingTurns >= 2) {
      kind = "warn";
      reason = "diminishing_progress";
    }
    return {
      kind,
      reason,
      remaining: this.tracker.remaining,
      consumed: this.tracker.consumed,
      completionRatio: this.tracker.completionRatio,
      suggestedOutputTokens: Math.max(0, Math.min(output, available)),
      shouldCompact: kind === "compact",
      shouldStop: kind === "stop",
      revision: this.tracker.revision,
    };
  }

  planWindow(segments: ContextWindowSegment[], budgetTokens: number): ContextWindowPlan {
    const normalized = segments.map((segment, index) => ({
      ...structuredClone(segment),
      id: segment.id || "segment-" + String(index + 1),
      priority: Number.isFinite(segment.priority) ? segment.priority : 0,
      createdSequence: Number.isFinite(segment.createdSequence) ? segment.createdSequence : index + 1,
    }));
    const estimateById = new Map(normalized.map((segment) => [
      segment.id,
      this.estimate(segment.content).estimatedTokens,
    ]));
    const pairMembers = new Map<string, ContextWindowSegment[]>();
    for (const segment of normalized) {
      if (!segment.toolPairId) continue;
      pairMembers.set(segment.toolPairId, (pairMembers.get(segment.toolPairId) ?? []).concat(segment));
    }
    const selectedIds = new Set<string>();
    let selectedTokens = 0;
    const select = (segment: ContextWindowSegment): boolean => {
      if (selectedIds.has(segment.id)) return true;
      const cost = estimateById.get(segment.id) ?? 0;
      if (selectedTokens + cost > budgetTokens) return false;
      selectedIds.add(segment.id);
      selectedTokens += cost;
      return true;
    };
    for (const segment of normalized.filter((item) => item.pinned)) {
      if (!select(segment)) throw new Error("pinned_context_exceeds_budget");
    }
    const groups: Array<{ key: string; members: ContextWindowSegment[]; priority: number; sequence: number; cost: number }> = [];
    const grouped = new Set<string>();
    for (const segment of normalized) {
      if (selectedIds.has(segment.id)) continue;
      if (segment.toolPairId) {
        if (grouped.has(segment.toolPairId)) continue;
        grouped.add(segment.toolPairId);
        const members = pairMembers.get(segment.toolPairId) ?? [segment];
        groups.push({
          key: "pair:" + segment.toolPairId,
          members,
          priority: Math.max(...members.map((item) => item.priority)),
          sequence: Math.max(...members.map((item) => item.createdSequence)),
          cost: members.reduce((sum, item) => sum + (estimateById.get(item.id) ?? 0), 0),
        });
      } else {
        groups.push({
          key: "single:" + segment.id,
          members: [segment],
          priority: segment.priority,
          sequence: segment.createdSequence,
          cost: estimateById.get(segment.id) ?? 0,
        });
      }
    }
    groups.sort((left, right) => {
      return right.priority - left.priority || right.sequence - left.sequence || left.key.localeCompare(right.key);
    });
    for (const group of groups) {
      if (selectedTokens + group.cost > budgetTokens) continue;
      for (const member of group.members) select(member);
    }
    const selected = normalized.filter((item) => selectedIds.has(item.id))
      .sort((left, right) => left.createdSequence - right.createdSequence);
    const dropped = normalized.filter((item) => !selectedIds.has(item.id));
    let pairInvariantOk = true;
    for (const members of pairMembers.values()) {
      const included = members.filter((item) => selectedIds.has(item.id)).length;
      if (included !== 0 && included !== members.length) pairInvariantOk = false;
    }
    if (!pairInvariantOk) throw new Error("tool_pair_context_invariant");
    const droppedTokens = dropped.reduce((sum, item) => sum + (estimateById.get(item.id) ?? 0), 0);
    return {
      selected,
      dropped,
      selectedTokens,
      droppedTokens,
      budgetTokens,
      pairInvariantOk,
      digest: digest({
        selected: selected.map((item) => item.id),
        dropped: dropped.map((item) => item.id),
        budgetTokens,
      }),
    };
  }

  restore(value: BudgetTracker): void {
    if (!Number.isInteger(value.revision) || value.revision < 0) throw new Error("token_budget_revision_invalid");
    if (value.initial <= 0 || value.remaining < 0 || value.consumed < 0) throw new Error("token_budget_snapshot_invalid");
    if (value.remaining + value.consumed !== value.initial) throw new Error("token_budget_conservation");
    this.tracker = structuredClone(value);
  }

  snapshot(): BudgetTracker {
    return structuredClone(this.tracker);
  }
}
