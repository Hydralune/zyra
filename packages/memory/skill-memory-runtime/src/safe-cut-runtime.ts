import {
  cloneJson,
  contractError,
  digest,
  estimateTokens,
  nowIso,
  stableId,
  uniqueStrings,
  type CompactMessageBlock,
  type JsonObject,
  type SafeCutPlan,
} from "./contracts.ts";

export interface SafeCutRequest {
  triggerId: string;
  compactGeneration: number;
  blocks: CompactMessageBlock[];
  targetTokens: number;
  minimumRecentTurns: number;
  preserveMessageIds?: string[];
  preserveToolPairIds?: string[];
  metadata?: JsonObject;
}

export interface SafeCutAudit {
  ok: boolean;
  planId: string;
  missingToolCallPairIds: string[];
  missingToolResultPairIds: string[];
  splitPairIds: string[];
  duplicateBlockIds: string[];
  duplicateMessageIds: string[];
  nonMonotonicTurnIds: string[];
  sourceTokensConsistent: boolean;
  planDigestValid: boolean;
  failures: string[];
}

export class SafeCutRuntime {
  private readonly plans = new Map<string, SafeCutPlan>();
  private readonly now: () => Date;

  constructor(now: () => Date = () => new Date()) {
    this.now = now;
  }

  plan(requestValue: SafeCutRequest): SafeCutPlan {
    const request = normalizeRequest(requestValue);
    const normalized = request.blocks.map(normalizeBlock).sort(blockOrder);
    const sourceMessageIds = uniqueStrings(normalized.map((block) => block.messageId));
    const sourceTokens = normalized.reduce((total, block) => total + block.tokenEstimate, 0);
    const explicitMessages = new Set(request.preserveMessageIds);
    const explicitPairs = new Set(request.preserveToolPairIds);
    const turnIndexes = uniqueTurns(normalized);
    const recentTurns = new Set(turnIndexes.slice(-request.minimumRecentTurns));
    const pairState = toolPairState(normalized);
    const unresolvedToolPairIds = pairState
      .filter((pair) => pair.calls.length !== pair.results.length || pair.calls.length === 0 || pair.results.length === 0)
      .map((pair) => pair.pairId);
    const preservePairIds = new Set([
      ...explicitPairs,
      ...unresolvedToolPairIds,
    ]);
    for (const pair of pairState) {
      if (pair.blocks.some((block) => explicitMessages.has(block.messageId))) preservePairIds.add(pair.pairId);
      if (pair.blocks.some((block) => block.turnIndex !== null && recentTurns.has(block.turnIndex))) preservePairIds.add(pair.pairId);
    }
    const forcedPreserved = new Set<string>();
    for (const block of normalized) {
      if (explicitMessages.has(block.messageId)) forcedPreserved.add(block.messageId);
      if (block.turnIndex !== null && recentTurns.has(block.turnIndex)) forcedPreserved.add(block.messageId);
      if (block.toolPairId && preservePairIds.has(block.toolPairId)) forcedPreserved.add(block.messageId);
      if (block.role === "system") forcedPreserved.add(block.messageId);
      if (block.metadata.required === true || block.metadata.pinned === true) forcedPreserved.add(block.messageId);
    }
    const candidateMessages = sourceMessageIds.filter((id) => !forcedPreserved.has(id));
    const tokensByMessage = messageTokenMap(normalized);
    const desiredSummaryBudget = Math.max(0, sourceTokens - request.targetTokens);
    const summarized = new Set<string>();
    let summarizedTokens = 0;
    for (const messageId of candidateMessages) {
      if (summarizedTokens >= desiredSummaryBudget) break;
      summarized.add(messageId);
      summarizedTokens += tokensByMessage.get(messageId) ?? 0;
    }
    closeToolPairs(normalized, summarized, forcedPreserved);
    summarizedTokens = [...summarized].reduce((total, id) => total + (tokensByMessage.get(id) ?? 0), 0);
    const preservedMessageIds = sourceMessageIds.filter((id) => !summarized.has(id));
    const summarizedMessageIds = sourceMessageIds.filter((id) => summarized.has(id));
    const preservedTokens = sourceTokens - summarizedTokens;
    const summarySpan = contiguousSummarySpan(sourceMessageIds, summarized);
    const cutAfterIndex = summarySpan.endIndex;
    const nonContiguous = !summarySpan.contiguous;
    const valid = summarizedMessageIds.length > 0
      && unresolvedToolPairIds.length === 0
      && !nonContiguous
      && !splitPairIds(normalized, summarized).length;
    const reason = summarizedMessageIds.length === 0
      ? "no_safe_messages_to_compact"
      : unresolvedToolPairIds.length > 0
        ? "unresolved_tool_pair"
        : nonContiguous
          ? "safe_cut_not_contiguous"
          : splitPairIds(normalized, summarized).length > 0
            ? "tool_pair_would_split"
            : summarizedTokens < desiredSummaryBudget
              ? "safe_cut_below_target_but_valid"
              : "safe_cut_ready";
    const createdAt = nowIso(this.now);
    const unsigned = {
      planId: stableId("safe-cut-plan", request.triggerId, request.compactGeneration, sourceMessageIds, summarizedMessageIds),
      triggerId: request.triggerId,
      compactGeneration: request.compactGeneration,
      sourceMessageIds,
      summarizedMessageIds,
      preservedMessageIds,
      preservedToolPairIds: [...preservePairIds].sort(),
      unresolvedToolPairIds,
      cutAfterIndex,
      sourceTokens,
      summarizedTokens,
      preservedTokens,
      targetTokens: request.targetTokens,
      minimumRecentTurns: request.minimumRecentTurns,
      valid,
      reason,
      createdAt,
      metadata: {
        ...request.metadata,
        source: "OMP tool-call/result-safe cut adapted to Zyra compact boundary",
        block_count: normalized.length,
        message_count: sourceMessageIds.length,
        recent_turns: [...recentTurns],
        summary_start_index: summarySpan.startIndex,
        explicit_preserve_message_ids: request.preserveMessageIds,
        explicit_preserve_tool_pair_ids: request.preserveToolPairIds,
      },
    } satisfies Omit<SafeCutPlan, "planDigest">;
    const plan: SafeCutPlan = { ...unsigned, planDigest: digest(unsigned) };
    const existing = this.plans.get(plan.planId);
    if (existing) {
      if (existing.planDigest !== plan.planDigest) throw contractError("safe_cut_id_conflict", `safe cut plan id conflict: ${plan.planId}`);
      return cloneJson(existing);
    }
    this.plans.set(plan.planId, plan);
    return cloneJson(plan);
  }

  audit(planValue: SafeCutPlan, blocksValue: CompactMessageBlock[]): SafeCutAudit {
    const plan = cloneJson(planValue);
    const blocks = blocksValue.map(normalizeBlock).sort(blockOrder);
    const pairState = toolPairState(blocks);
    const missingToolCallPairIds = pairState.filter((pair) => pair.calls.length === 0).map((pair) => pair.pairId);
    const missingToolResultPairIds = pairState.filter((pair) => pair.results.length === 0).map((pair) => pair.pairId);
    const summarized = new Set(plan.summarizedMessageIds);
    const split = splitPairIds(blocks, summarized);
    const duplicateBlockIds = duplicates(blocks.map((block) => block.blockId));
    const duplicateMessageIds = duplicates(plan.sourceMessageIds);
    const nonMonotonicTurnIds = nonMonotonicTurns(blocks);
    const sourceTokens = blocks.reduce((total, block) => total + block.tokenEstimate, 0);
    const sourceTokensConsistent = sourceTokens === plan.sourceTokens;
    const { planDigest, ...unsigned } = plan;
    const planDigestValid = digest(unsigned) === planDigest;
    const failures: string[] = [];
    if (missingToolCallPairIds.length > 0) failures.push("tool_result_without_call");
    if (missingToolResultPairIds.length > 0) failures.push("tool_call_without_result");
    if (split.length > 0) failures.push("tool_pair_split");
    if (duplicateBlockIds.length > 0) failures.push("duplicate_block_id");
    if (duplicateMessageIds.length > 0) failures.push("duplicate_message_id");
    if (nonMonotonicTurnIds.length > 0) failures.push("non_monotonic_turn");
    if (!sourceTokensConsistent) failures.push("source_token_mismatch");
    if (!planDigestValid) failures.push("plan_digest_mismatch");
    if (!plan.valid) failures.push(`plan_invalid:${plan.reason}`);
    return {
      ok: failures.length === 0,
      planId: plan.planId,
      missingToolCallPairIds,
      missingToolResultPairIds,
      splitPairIds: split,
      duplicateBlockIds,
      duplicateMessageIds,
      nonMonotonicTurnIds,
      sourceTokensConsistent,
      planDigestValid,
      failures,
    };
  }

  get(planId: string): SafeCutPlan | null {
    const plan = this.plans.get(planId.trim());
    return plan ? cloneJson(plan) : null;
  }

  list(limit = 1_000): SafeCutPlan[] {
    return [...this.plans.values()]
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .slice(-Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  restore(plans: SafeCutPlan[]): void {
    this.plans.clear();
    for (const plan of plans) {
      const { planDigest, ...unsigned } = plan;
      if (digest(unsigned) !== planDigest) throw contractError("safe_cut_restore_digest", `safe cut plan digest mismatch: ${plan.planId}`);
      if (this.plans.has(plan.planId)) throw contractError("safe_cut_restore_duplicate", `duplicate safe cut plan id ${plan.planId}`);
      this.plans.set(plan.planId, cloneJson(plan));
    }
  }
}

function normalizeRequest(value: SafeCutRequest): Required<SafeCutRequest> {
  if (!value.triggerId?.trim()) throw contractError("safe_cut_trigger_required", "safe cut trigger id is required");
  if (!Number.isSafeInteger(value.compactGeneration) || value.compactGeneration < 0) throw contractError("safe_cut_generation", "safe cut compact generation must be non-negative");
  if (!Number.isSafeInteger(value.targetTokens) || value.targetTokens < 1) throw contractError("safe_cut_target", "safe cut target tokens must be positive");
  if (!Number.isSafeInteger(value.minimumRecentTurns) || value.minimumRecentTurns < 1) throw contractError("safe_cut_recent_turns", "safe cut recent turn count must be positive");
  if (!Array.isArray(value.blocks) || value.blocks.length === 0) throw contractError("safe_cut_blocks_required", "safe cut requires message blocks");
  return {
    triggerId: value.triggerId.trim(),
    compactGeneration: value.compactGeneration,
    blocks: value.blocks,
    targetTokens: value.targetTokens,
    minimumRecentTurns: value.minimumRecentTurns,
    preserveMessageIds: uniqueStrings(value.preserveMessageIds),
    preserveToolPairIds: uniqueStrings(value.preserveToolPairIds),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function normalizeBlock(value: CompactMessageBlock): CompactMessageBlock {
  const blockId = value.blockId?.trim();
  const messageId = value.messageId?.trim();
  if (!blockId || !messageId) throw contractError("safe_cut_block_identity", "compact message block requires block and message ids");
  if (!["system", "user", "assistant", "tool"].includes(value.role)) throw contractError("safe_cut_role", `unsupported compact role ${value.role}`);
  if (!["text", "tool_call", "tool_result", "attachment", "notice"].includes(value.kind)) throw contractError("safe_cut_kind", `unsupported compact block kind ${value.kind}`);
  if ((value.kind === "tool_call" || value.kind === "tool_result") && !value.toolPairId?.trim()) {
    throw contractError("safe_cut_tool_pair_required", `${value.kind} block requires a tool pair id`);
  }
  const turnIndex = value.turnIndex === null ? null : Number(value.turnIndex);
  if (turnIndex !== null && (!Number.isSafeInteger(turnIndex) || turnIndex < 0)) throw contractError("safe_cut_turn_index", "compact turn index must be non-negative");
  const apiRound = value.apiRound === null ? null : Number(value.apiRound);
  if (apiRound !== null && (!Number.isSafeInteger(apiRound) || apiRound < 0)) throw contractError("safe_cut_api_round", "compact api round must be non-negative");
  const text = String(value.text ?? "");
  const sourceDigest = value.sourceDigest?.replace(/^sha256:/, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(sourceDigest)) throw contractError("safe_cut_source_digest", "compact block source digest must be sha256");
  return {
    blockId,
    messageId,
    role: value.role,
    kind: value.kind,
    text,
    toolPairId: value.toolPairId?.trim() || null,
    turnIndex,
    apiRound,
    tokenEstimate: Number.isSafeInteger(value.tokenEstimate) && value.tokenEstimate >= 0 ? value.tokenEstimate : estimateTokens(text),
    createdAt: value.createdAt,
    sourceDigest,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

interface ToolPairState {
  pairId: string;
  calls: CompactMessageBlock[];
  results: CompactMessageBlock[];
  blocks: CompactMessageBlock[];
}

function toolPairState(blocks: CompactMessageBlock[]): ToolPairState[] {
  const values = new Map<string, ToolPairState>();
  for (const block of blocks) {
    if (!block.toolPairId) continue;
    const pair = values.get(block.toolPairId) ?? { pairId: block.toolPairId, calls: [], results: [], blocks: [] };
    pair.blocks.push(block);
    if (block.kind === "tool_call") pair.calls.push(block);
    if (block.kind === "tool_result") pair.results.push(block);
    values.set(pair.pairId, pair);
  }
  return [...values.values()].sort((left, right) => left.pairId.localeCompare(right.pairId));
}

function closeToolPairs(blocks: CompactMessageBlock[], summarized: Set<string>, preserved: Set<string>): void {
  for (const pair of toolPairState(blocks)) {
    const messageIds = uniqueStrings(pair.blocks.map((block) => block.messageId));
    const summarizedCount = messageIds.filter((id) => summarized.has(id)).length;
    if (summarizedCount === 0 || summarizedCount === messageIds.length) continue;
    if (messageIds.some((id) => preserved.has(id))) {
      for (const id of messageIds) summarized.delete(id);
    } else {
      for (const id of messageIds) summarized.add(id);
    }
  }
}

function splitPairIds(blocks: CompactMessageBlock[], summarized: Set<string>): string[] {
  return toolPairState(blocks)
    .filter((pair) => {
      const messageIds = uniqueStrings(pair.blocks.map((block) => block.messageId));
      const count = messageIds.filter((id) => summarized.has(id)).length;
      return count > 0 && count < messageIds.length;
    })
    .map((pair) => pair.pairId);
}

function messageTokenMap(blocks: CompactMessageBlock[]): Map<string, number> {
  const values = new Map<string, number>();
  for (const block of blocks) values.set(block.messageId, (values.get(block.messageId) ?? 0) + block.tokenEstimate);
  return values;
}

function uniqueTurns(blocks: CompactMessageBlock[]): number[] {
  return [...new Set(blocks.map((block) => block.turnIndex).filter((value): value is number => value !== null))].sort((left, right) => left - right);
}

function blockOrder(left: CompactMessageBlock, right: CompactMessageBlock): number {
  const turnLeft = left.turnIndex ?? -1;
  const turnRight = right.turnIndex ?? -1;
  if (turnLeft !== turnRight) return turnLeft - turnRight;
  const roundLeft = left.apiRound ?? -1;
  const roundRight = right.apiRound ?? -1;
  if (roundLeft !== roundRight) return roundLeft - roundRight;
  return left.blockId.localeCompare(right.blockId);
}

function contiguousSummarySpan(
  messageIds: string[],
  summarized: Set<string>,
): { startIndex: number; endIndex: number; contiguous: boolean } {
  const indexes = messageIds
    .map((messageId, index) => summarized.has(messageId) ? index : -1)
    .filter((index) => index >= 0);
  if (indexes.length === 0) {
    return { startIndex: -1, endIndex: -1, contiguous: true };
  }
  const startIndex = indexes[0]!;
  const endIndex = indexes[indexes.length - 1]!;
  const contiguous = indexes.length === endIndex - startIndex + 1
    && messageIds
      .slice(startIndex, endIndex + 1)
      .every((messageId) => summarized.has(messageId));
  return { startIndex, endIndex, contiguous };
}

function duplicates(values: string[]): string[] {
  const seen = new Set<string>();
  const duplicate = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) duplicate.add(value);
    seen.add(value);
  }
  return [...duplicate].sort();
}

function nonMonotonicTurns(blocks: CompactMessageBlock[]): string[] {
  const failures: string[] = [];
  let last = -1;
  for (const block of blocks) {
    if (block.turnIndex === null) continue;
    if (block.turnIndex < last) failures.push(block.blockId);
    last = Math.max(last, block.turnIndex);
  }
  return failures;
}
