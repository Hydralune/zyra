import {
  CompactionSourceCustodyRuntime,
  type SummaryStreamEvent,
} from "./compaction-custody-runtime.js";
import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";
import { ContextTokenRuntime, type TokenEstimate } from "../context/token-runtime.ts";

export const CONTEXT_COMPACTION_SNAPSHOT_VERSION = "zyra.context-compaction/v1";
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000;
export const WARNING_THRESHOLD_BUFFER_TOKENS = 20_000;
export const ERROR_THRESHOLD_BUFFER_TOKENS = 20_000;
export const MANUAL_COMPACT_BUFFER_TOKENS = 3_000;
export const POST_COMPACT_MAX_FILES_TO_RESTORE = 5;
export const POST_COMPACT_TOKEN_BUDGET = 50_000;
export const POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000;
export const POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000;
export const POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000;
export const TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]";

export type CompactMessageRole = "system" | "user" | "assistant" | "tool";
export type CompactBlockType =
  | "text"
  | "image"
  | "document"
  | "tool_use"
  | "tool_result"
  | "thinking"
  | "attachment";
export type CompactTrigger =
  | "manual"
  | "auto_threshold"
  | "prompt_too_long"
  | "session_memory"
  | "microcompact"
  | "resume_repair";
export type CompactWarningLevel = "none" | "warning" | "error";

export interface CompactTextBlock {
  type: "text";
  text: string;
}

export interface CompactImageBlock {
  type: "image";
  mediaType: string;
  data: string;
  width?: number;
  height?: number;
}

export interface CompactDocumentBlock {
  type: "document";
  mediaType: string;
  data: string;
  title?: string;
}

export interface CompactToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: JsonObject;
}

export interface CompactToolResultBlock {
  type: "tool_result";
  toolUseId: string;
  content: string | JsonValue[];
  isError: boolean;
  createdAt: string;
  compacted: boolean;
}

export interface CompactThinkingBlock {
  type: "thinking";
  thinking: string;
  signature: string | null;
}

export interface CompactAttachmentBlock {
  type: "attachment";
  attachmentKind: "file" | "plan" | "skill" | "agent" | "memory";
  path: string | null;
  name: string;
  content: string;
  sourceDigest: string;
  truncated: boolean;
}

export type CompactContentBlock =
  | CompactTextBlock
  | CompactImageBlock
  | CompactDocumentBlock
  | CompactToolUseBlock
  | CompactToolResultBlock
  | CompactThinkingBlock
  | CompactAttachmentBlock;

export interface CompactMessage {
  id: string;
  role: CompactMessageRole;
  content: CompactContentBlock[];
  createdAt: string;
  turnIndex: number | null;
  apiRound: number;
  synthetic: boolean;
  metadata: JsonObject;
}

export interface CompactBoundary {
  boundaryId: string;
  trigger: CompactTrigger;
  summary: string;
  sourceMessageIds: string[];
  preservedMessageIds: string[];
  attachmentIds: string[];
  sourceTokenEstimate: number;
  resultTokenEstimate: number;
  removedMessageCount: number;
  removedToolResultCount: number;
  compactGeneration: number;
  parentBoundaryId: string | null;
  createdAt: string;
  checksum: string;
}

export interface CompactionResult {
  messages: CompactMessage[];
  boundary: CompactBoundary;
  summaryMessage: CompactMessage;
  attachments: CompactAttachmentBlock[];
  changed: boolean;
  reason: string;
}

export interface MicrocompactResult {
  messages: CompactMessage[];
  compactedToolUseIds: string[];
  removedTokens: number;
  preservedTokens: number;
  changed: boolean;
  trigger: "size" | "age" | "count" | "none";
}

export interface TokenWarningState {
  level: CompactWarningLevel;
  estimatedTokens: number;
  contextWindow: number;
  remainingTokens: number;
  autoCompactThreshold: number;
  warningThreshold: number;
  errorThreshold: number;
  shouldAutoCompact: boolean;
  outputReservation: number;
}

export interface SessionMemoryState {
  enabled: boolean;
  minimumMessages: number;
  minimumTokens: number;
  targetTokensToKeep: number;
  lastSummarizedMessageId: string | null;
  summary: string;
  discoveredTools: string[];
  generation: number;
}

export interface CleanupState {
  generation: number;
  classifierApprovalsCleared: number;
  speculativeChecksCleared: number;
  systemPromptSectionsCleared: number;
  sessionMessageCacheCleared: number;
  memoryFileCacheCleared: number;
  betaTracingCleared: number;
  microcompactStateCleared: number;
  lastQuerySource: string | null;
  cleanedAt: string | null;
}

export interface AttachmentCandidate {
  kind: CompactAttachmentBlock["attachmentKind"];
  path?: string;
  name: string;
  content: string;
  lastReadAt?: string;
  priority?: number;
}

export interface CompactOptions {
  trigger: CompactTrigger;
  model: string;
  contextWindow: number;
  maxOutputTokens: number;
  targetTokens: number;
  preserveRecentMessages: number;
  preserveApiRounds: number;
  systemPrompt: string;
  customInstructions: string;
  attachments: readonly AttachmentCandidate[];
  querySource: string;
  sessionId?: string;
  microcompactIntervalMs?: number;
  cacheRoot?: string;
  summaryMaxAttempts?: number;
  now?: string;
}

export interface SummaryRequest {
  trigger: CompactTrigger;
  messages: readonly CompactMessage[];
  previousSummary: string;
  systemPrompt: string;
  customInstructions: string;
  tokenBudget: number;
}

export type SummaryProvider = (request: SummaryRequest) => Promise<string>;

export interface ContextCompactionSnapshot {
  version: typeof CONTEXT_COMPACTION_SNAPSHOT_VERSION;
  revision: number;
  compactGeneration: number;
  boundaries: CompactBoundary[];
  sessionMemory: SessionMemoryState;
  cleanup: CleanupState;
  microcompactPinnedToolIds: string[];
  microcompactSentToolIds: string[];
  tokenRuntime: ReturnType<ContextTokenRuntime["snapshot"]>;
  sourceCustody?: ReturnType<CompactionSourceCustodyRuntime["snapshot"]>;
  checksum: string;
}

interface ApiRound {
  index: number;
  messages: CompactMessage[];
  toolUseIds: Set<string>;
  toolResultIds: Set<string>;
  tokenEstimate: number;
}

interface SelectionPlan {
  compact: CompactMessage[];
  preserve: CompactMessage[];
  compactTokens: number;
  preserveTokens: number;
  splitIndex: number;
}

export class ContextCompactionRuntime {
  private readonly tokens: ContextTokenRuntime;
  private readonly sourceCustody = new CompactionSourceCustodyRuntime();
  private readonly boundaries: CompactBoundary[] = [];
  private readonly microcompactPinnedToolIds = new Set<string>();
  private readonly microcompactSentToolIds = new Set<string>();
  private sessionMemory: SessionMemoryState;
  private cleanup: CleanupState;
  private revision = 0;
  private compactGeneration = 0;

  constructor(contextWindow = 200_000) {
    this.tokens = new ContextTokenRuntime(contextWindow);
    this.sessionMemory = defaultSessionMemory();
    this.cleanup = defaultCleanupState();
  }

  private applySourceCustody(
    messages: readonly CompactMessage[],
    options: CompactOptions,
    summary = "",
  ): { messages: CompactMessage[]; envelope: Record<string, unknown> } {
    const effectiveContextWindow = this.getEffectiveContextWindowSize(options.model, options.contextWindow);
    const envelope: Record<string, unknown> = {
      sessionId: options.sessionId ?? "default",
      source: options.querySource,
      querySource: options.querySource,
      nowMs: Date.parse(normalizeTimestamp(options.now)),
      microcompactIntervalMs: options.microcompactIntervalMs ?? 5 * 60_000,
      cacheRoot: options.cacheRoot ?? ".cache/compact",
      keepLastMessages: Math.max(1, options.preserveRecentMessages),
      messages: structuredClone(messages),
      currentTokens: estimateMessages(this.tokens, messages),
      contextWindow: effectiveContextWindow,
      reservedTokens: options.maxOutputTokens,
      minimumFreeTokens: AUTOCOMPACT_BUFFER_TOKENS,
      summary,
      summaryTokenCount: summary ? this.tokens.estimate(summary).estimatedTokens : 0,
      sessionMemoryEnabled: this.sessionMemory.enabled,
      attachments: structuredClone(options.attachments),
    };
    this.sourceCustody.applyCompactionCustody(envelope);
    const custodyMessages = Array.isArray(envelope.messages)
      ? envelope.messages as CompactMessage[]
      : [...messages];
    return { messages: normalizeMessages(custodyMessages), envelope };
  }

  microCompact_module(value: JsonObject): JsonObject {
    const messages = messagesFromJson(value.messages);
    const result = this.microcompact(messages, {
      maximumToolResults: integer(value.maximum_tool_results, 30),
      preserveRecentToolResults: integer(value.preserve_recent_tool_results, 6),
      minimumResultTokens: integer(value.minimum_result_tokens, 2_000),
      maximumAgeMs: integer(value.maximum_age_ms, 30 * 60 * 1_000),
      now: asString(value.now, new Date().toISOString()),
    });
    return microcompactToJson(result);
  }

  autoCompact_module(value: JsonObject): JsonObject {
    const messages = messagesFromJson(value.messages);
    const state = this.calculateTokenWarningState(
      messages,
      integer(value.context_window, 200_000),
      integer(value.max_output_tokens, 8_192),
    );
    return warningToJson(state);
  }

  compact_module(value: JsonObject): JsonObject {
    const messages = messagesFromJson(value.messages);
    const action = asString(value.action, "plan");
    const options = compactOptionsFromJson(value);
    if (action === "strip_images") return { messages: this.stripImagesFromMessages(messages).map(messageToJson) };
    if (action === "strip_attachments") return { messages: this.stripReinjectedAttachments(messages).map(messageToJson) };
    const plan = this.planCompaction(messages, options);
    return {
      compact_message_ids: plan.compact.map((item) => item.id),
      preserved_message_ids: plan.preserve.map((item) => item.id),
      compact_tokens: plan.compactTokens,
      preserve_tokens: plan.preserveTokens,
      split_index: plan.splitIndex,
    };
  }

  sessionMemoryCompact_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "configure") {
      this.configureSessionMemory({
        enabled: value.enabled === undefined ? this.sessionMemory.enabled : asBoolean(value.enabled),
        minimumMessages: integer(value.minimum_messages, this.sessionMemory.minimumMessages),
        minimumTokens: integer(value.minimum_tokens, this.sessionMemory.minimumTokens),
        targetTokensToKeep: integer(value.target_tokens_to_keep, this.sessionMemory.targetTokensToKeep),
      });
    }
    return sessionMemoryToJson(this.sessionMemory);
  }

  postCompactCleanup_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "run");
    if (action === "run") this.runPostCompactCleanup(asString(value.query_source, "runtime"));
    return cleanupToJson(this.cleanup);
  }

  getEffectiveContextWindowSize(model: string, configured = 200_000): number {
    const name = model.toLowerCase();
    let effective = Math.max(8_192, Math.floor(configured));
    if (name.includes("haiku")) effective = Math.min(effective, 200_000);
    if (name.includes("local")) effective = Math.min(effective, 128_000);
    const configuredCap = Number(process.env.ZYRA_CONTEXT_WINDOW_CAP ?? "");
    if (Number.isSafeInteger(configuredCap) && configuredCap >= 8_192) {
      effective = Math.min(effective, configuredCap);
    }
    return effective;
  }

  getAutoCompactThreshold(contextWindow: number, maxOutputTokens: number): number {
    const reserved = Math.max(AUTOCOMPACT_BUFFER_TOKENS, maxOutputTokens);
    return Math.max(1_024, contextWindow - reserved);
  }

  calculateTokenWarningState(
    messages: readonly CompactMessage[],
    contextWindow: number,
    maxOutputTokens: number,
  ): TokenWarningState {
    const estimatedTokens = estimateMessages(this.tokens, messages);
    const outputReservation = Math.max(1_024, maxOutputTokens);
    const autoCompactThreshold = this.getAutoCompactThreshold(contextWindow, outputReservation);
    const warningThreshold = Math.max(1_024, contextWindow - WARNING_THRESHOLD_BUFFER_TOKENS);
    const errorThreshold = Math.max(1_024, contextWindow - ERROR_THRESHOLD_BUFFER_TOKENS + outputReservation);
    const remainingTokens = contextWindow - estimatedTokens;
    let level: CompactWarningLevel = "none";
    if (estimatedTokens >= errorThreshold) level = "error";
    else if (estimatedTokens >= warningThreshold) level = "warning";
    return {
      level,
      estimatedTokens,
      contextWindow,
      remainingTokens,
      autoCompactThreshold,
      warningThreshold,
      errorThreshold,
      shouldAutoCompact: estimatedTokens >= autoCompactThreshold,
      outputReservation,
    };
  }

  shouldAutoCompact(
    messages: readonly CompactMessage[],
    contextWindow: number,
    maxOutputTokens: number,
    enabled = true,
  ): boolean {
    if (!enabled) return false;
    if (messages.length < 4) return false;
    return this.calculateTokenWarningState(messages, contextWindow, maxOutputTokens).shouldAutoCompact;
  }

  async autoCompactIfNeeded(
    messages: readonly CompactMessage[],
    options: CompactOptions,
    summarize: SummaryProvider,
  ): Promise<CompactionResult | null> {
    const custody = this.applySourceCustody(messages, options);
    const contextWindow = this.getEffectiveContextWindowSize(options.model, options.contextWindow);
    if (!this.shouldAutoCompact(custody.messages, contextWindow, options.maxOutputTokens)) return null;
    return this.compactConversation(custody.messages, { ...options, contextWindow, trigger: "auto_threshold" }, summarize);
  }

  stripImagesFromMessages(messages: readonly CompactMessage[]): CompactMessage[] {
    return messages.map((message) => ({
      ...structuredClone(message),
      content: message.content.flatMap((block): CompactContentBlock[] => {
        if (block.type !== "image") return [structuredClone(block)];
        return [{
          type: "text",
          text: `[Image removed during compaction: ${block.mediaType}, ${block.width ?? "?"}x${block.height ?? "?"}]`,
        }];
      }),
      metadata: { ...message.metadata, images_stripped: true },
    }));
  }

  stripReinjectedAttachments(messages: readonly CompactMessage[]): CompactMessage[] {
    return messages
      .map((message) => ({
        ...structuredClone(message),
        content: message.content.filter((block) => block.type !== "attachment"),
        metadata: { ...message.metadata, reinjected_attachments_stripped: true },
      }))
      .filter((message) => message.content.length > 0 || message.role === "system");
  }

  truncateHeadForPromptTooLong(
    messages: readonly CompactMessage[],
    targetTokens: number,
  ): CompactMessage[] {
    const rounds = groupMessagesByApiRound(messages, this.tokens);
    const kept: ApiRound[] = [];
    let tokens = 0;
    for (let index = rounds.length - 1; index >= 0; index -= 1) {
      const round = rounds[index];
      if (kept.length > 0 && tokens + round.tokenEstimate > targetTokens) break;
      kept.unshift(round);
      tokens += round.tokenEstimate;
    }
    const result = kept.flatMap((round) => round.messages.map((message) => structuredClone(message)));
    validateMessageInvariants(result);
    return result;
  }

  buildPostCompactMessages(result: CompactionResult): CompactMessage[] {
    const boundaryMessage: CompactMessage = {
      id: `compact-boundary-${result.boundary.boundaryId}`,
      role: "system",
      content: [{
        type: "text",
        text: `Compaction boundary ${result.boundary.boundaryId}\n${result.boundary.summary}`,
      }],
      createdAt: result.boundary.createdAt,
      turnIndex: null,
      apiRound: 0,
      synthetic: true,
      metadata: {
        compact_boundary: boundaryToJson(result.boundary),
        canonical_owner: "typescript",
      },
    };
    const attachments = result.attachments.length === 0
      ? []
      : [{
        id: `compact-attachments-${result.boundary.boundaryId}`,
        role: "user" as const,
        content: result.attachments.map((item) => structuredClone(item)),
        createdAt: result.boundary.createdAt,
        turnIndex: null,
        apiRound: 0,
        synthetic: true,
        metadata: { post_compact_attachments: true },
      }];
    const preserved = result.messages.filter((message) => result.boundary.preservedMessageIds.includes(message.id));
    const messages = [boundaryMessage, result.summaryMessage, ...attachments, ...preserved]
      .map((message, index) => ({ ...message, apiRound: index }));
    validateMessageInvariants(messages);
    return messages;
  }

  annotateBoundaryWithPreservedSegment(
    boundary: CompactBoundary,
    preserved: readonly CompactMessage[],
  ): CompactBoundary {
    const result = structuredClone(boundary);
    result.preservedMessageIds = preserved.map((item) => item.id);
    result.checksum = boundaryChecksum(result);
    return result;
  }

  mergeHookInstructions(summary: string, instructions: readonly string[]): string {
    const unique = [...new Set(instructions.map((item) => item.trim()).filter(Boolean))];
    if (unique.length === 0) return summary.trim();
    return `${summary.trim()}\n\nPost-compaction instructions:\n${unique.map((item) => `- ${item}`).join("\n")}`;
  }

  planCompaction(
    messages: readonly CompactMessage[],
    options: CompactOptions,
  ): SelectionPlan {
    const normalized = normalizeMessages(messages);
    validateMessageInvariants(normalized);
    const rounds = groupMessagesByApiRound(normalized, this.tokens);
    const preserveRoundCount = Math.max(1, options.preserveApiRounds);
    let splitRound = Math.max(1, rounds.length - preserveRoundCount);
    let preserve = rounds.slice(splitRound).flatMap((round) => round.messages);
    let preserveTokens = estimateMessages(this.tokens, preserve);
    while (splitRound > 1 && preserve.length < options.preserveRecentMessages) {
      splitRound -= 1;
      preserve = rounds.slice(splitRound).flatMap((round) => round.messages);
      preserveTokens = estimateMessages(this.tokens, preserve);
    }
    while (splitRound < rounds.length - 1 && preserveTokens > options.targetTokens) {
      splitRound += 1;
      preserve = rounds.slice(splitRound).flatMap((round) => round.messages);
      preserveTokens = estimateMessages(this.tokens, preserve);
    }
    let compact = rounds.slice(0, splitRound).flatMap((round) => round.messages);
    if (compact.length === 0 && normalized.length > 2) {
      const fallback = adjustIndexToPreserveApiInvariants(normalized, Math.floor(normalized.length / 2));
      compact = normalized.slice(0, fallback);
      preserve = normalized.slice(fallback);
      preserveTokens = estimateMessages(this.tokens, preserve);
    }
    const splitIndex = compact.length;
    return {
      compact: compact.map((item) => structuredClone(item)),
      preserve: preserve.map((item) => structuredClone(item)),
      compactTokens: estimateMessages(this.tokens, compact),
      preserveTokens,
      splitIndex,
    };
  }

  async compactConversation(
    messages: readonly CompactMessage[],
    options: CompactOptions,
    summarize: SummaryProvider,
  ): Promise<CompactionResult> {
    const custody = this.applySourceCustody(messages, options);
    if (custody.messages.length < 3) throw new Error("not enough messages to compact");
    const stripped = this.stripImagesFromMessages(this.stripReinjectedAttachments(custody.messages));
    const plan = this.planCompaction(stripped, options);
    if (plan.compact.length === 0) throw new Error("compaction did not select source messages");
    const previousSummary = this.sessionMemory.summary;
    const summaryRequest: SummaryRequest = {
      trigger: options.trigger,
      messages: plan.compact,
      previousSummary,
      systemPrompt: options.systemPrompt,
      customInstructions: options.customInstructions,
      tokenBudget: Math.max(1_024, options.targetTokens - plan.preserveTokens),
    };
    const streamedSummary = await this.sourceCustody.streamCompactSummary({
      maxAttempts: options.summaryMaxAttempts ?? 2,
      stream: async function* (): AsyncIterable<SummaryStreamEvent> {
        try {
          const output = (await summarize(summaryRequest)).trim();
          if (!output) throw new Error("compaction summary provider returned empty output");
          yield { type: "delta", text: output };
          yield { type: "complete" };
        } catch (error) {
          yield { type: "error", error };
        }
      },
    });
    let summary = streamedSummary.summary;
    summary = this.mergeHookInstructions(summary, [options.customInstructions]);
    const completedCustody = this.applySourceCustody(stripped, options, summary);
    const custodyBoundary = completedCustody.envelope.sourceCustodyPartialBoundary;
    const custodyBoundaryId = custodyBoundary !== null && typeof custodyBoundary === "object"
      ? asString((custodyBoundary as JsonObject).boundaryId)
      : "";
    const now = normalizeTimestamp(options.now);
    const attachments = this.createPostCompactAttachments(options.attachments, stripped);
    this.compactGeneration += 1;
    const summaryMessage: CompactMessage = {
      id: randomUUID(),
      role: "user",
      content: [{ type: "text", text: summary }],
      createdAt: now,
      turnIndex: null,
      apiRound: 0,
      synthetic: true,
      metadata: {
        compact_summary: true,
        compact_generation: this.compactGeneration,
        source_message_count: plan.compact.length,
      },
    };
    const boundary: CompactBoundary = {
      boundaryId: custodyBoundaryId
        ? `${custodyBoundaryId}-${this.compactGeneration}`
        : randomUUID(),
      trigger: options.trigger,
      summary,
      sourceMessageIds: plan.compact.map((item) => item.id),
      preservedMessageIds: plan.preserve.map((item) => item.id),
      attachmentIds: attachments.map((item) => item.sourceDigest),
      sourceTokenEstimate: plan.compactTokens,
      resultTokenEstimate: this.tokens.estimate(summary).estimatedTokens + plan.preserveTokens + estimateAttachments(this.tokens, attachments),
      removedMessageCount: plan.compact.length,
      removedToolResultCount: countBlocks(plan.compact, "tool_result"),
      compactGeneration: this.compactGeneration,
      parentBoundaryId: this.boundaries.at(-1)?.boundaryId ?? null,
      createdAt: now,
      checksum: "",
    };
    boundary.checksum = boundaryChecksum(boundary);
    this.boundaries.push(boundary);
    this.sessionMemory.summary = summary;
    this.sessionMemory.lastSummarizedMessageId = plan.compact.at(-1)?.id ?? null;
    this.sessionMemory.discoveredTools = extractDiscoveredToolNames(plan.compact);
    this.sessionMemory.generation += 1;
    this.runPostCompactCleanup(options.querySource);
    this.revision += 1;
    const preliminary: CompactionResult = {
      messages: [...plan.preserve.map((item) => structuredClone(item))],
      boundary: structuredClone(boundary),
      summaryMessage,
      attachments,
      changed: true,
      reason: options.trigger,
    };
    preliminary.messages = this.buildPostCompactMessages(preliminary);
    return preliminary;
  }

  async partialCompactConversation(
    messages: readonly CompactMessage[],
    selectedMessageIds: ReadonlySet<string>,
    options: CompactOptions,
    summarize: SummaryProvider,
  ): Promise<CompactionResult> {
    if (selectedMessageIds.size === 0) throw new Error("partial compaction selection is empty");
    const first = messages.findIndex((item) => selectedMessageIds.has(item.id));
    const last = findLastIndex(messages, (item) => selectedMessageIds.has(item.id));
    if (first < 0 || last < first) throw new Error("partial compaction selection does not exist");
    const adjustedFirst = adjustIndexToPreserveApiInvariants(messages, first);
    const adjustedLast = adjustEndIndexToPreserveApiInvariants(messages, last + 1);
    const selected = messages.slice(adjustedFirst, adjustedLast);
    const before = messages.slice(0, adjustedFirst);
    const after = messages.slice(adjustedLast);
    const result = await this.compactConversation(
      [...selected, ...after],
      { ...options, preserveRecentMessages: after.length, preserveApiRounds: Math.max(1, options.preserveApiRounds) },
      summarize,
    );
    result.messages = [...before.map((item) => structuredClone(item)), ...result.messages];
    result.boundary.sourceMessageIds = selected.map((item) => item.id);
    result.boundary.preservedMessageIds = [...before, ...after].map((item) => item.id);
    result.boundary.checksum = boundaryChecksum(result.boundary);
    validateMessageInvariants(result.messages);
    return result;
  }

  microcompact(
    messages: readonly CompactMessage[],
    options: {
      maximumToolResults: number;
      preserveRecentToolResults: number;
      minimumResultTokens: number;
      maximumAgeMs: number;
      now?: string;
    },
  ): MicrocompactResult {
    const normalized = normalizeMessages(messages);
    const candidates = collectToolResults(normalized);
    const now = Date.parse(normalizeTimestamp(options.now));
    const preserveIds = new Set(
      candidates.slice(-Math.max(0, options.preserveRecentToolResults)).map((item) => item.block.toolUseId),
    );
    for (const id of this.microcompactPinnedToolIds) preserveIds.add(id);
    let trigger: MicrocompactResult["trigger"] = "none";
    if (candidates.length > options.maximumToolResults) trigger = "count";
    const selected = new Set<string>();
    for (const candidate of candidates) {
      if (preserveIds.has(candidate.block.toolUseId)) continue;
      if (candidate.block.compacted) continue;
      const estimate = estimateToolResult(this.tokens, candidate.block);
      const age = Math.max(0, now - Date.parse(candidate.block.createdAt));
      if (estimate >= options.minimumResultTokens) {
        selected.add(candidate.block.toolUseId);
        if (trigger === "none") trigger = "size";
      } else if (age >= options.maximumAgeMs) {
        selected.add(candidate.block.toolUseId);
        if (trigger === "none") trigger = "age";
      } else if (candidates.length - selected.size > options.maximumToolResults) {
        selected.add(candidate.block.toolUseId);
        trigger = "count";
      }
    }
    if (selected.size === 0) {
      return {
        messages: normalized,
        compactedToolUseIds: [],
        removedTokens: 0,
        preservedTokens: estimateMessages(this.tokens, normalized),
        changed: false,
        trigger: "none",
      };
    }
    let removedTokens = 0;
    const compacted = normalized.map((message) => ({
      ...structuredClone(message),
      content: message.content.map((block): CompactContentBlock => {
        if (block.type !== "tool_result" || !selected.has(block.toolUseId)) return structuredClone(block);
        const previous = estimateToolResult(this.tokens, block);
        const replacement: CompactToolResultBlock = {
          ...structuredClone(block),
          content: TIME_BASED_MC_CLEARED_MESSAGE,
          compacted: true,
        };
        removedTokens += Math.max(0, previous - estimateToolResult(this.tokens, replacement));
        this.microcompactSentToolIds.add(block.toolUseId);
        return replacement;
      }),
      metadata: selected.size > 0 ? { ...message.metadata, microcompacted: true } : message.metadata,
    }));
    this.revision += 1;
    return {
      messages: compacted,
      compactedToolUseIds: [...selected].sort(),
      removedTokens,
      preservedTokens: estimateMessages(this.tokens, compacted),
      changed: true,
      trigger,
    };
  }

  pinCacheEdits(toolUseIds: readonly string[]): void {
    for (const id of toolUseIds) if (id.trim()) this.microcompactPinnedToolIds.add(id.trim());
    this.revision += 1;
  }

  consumePendingCacheEdits(): string[] {
    const values = [...this.microcompactSentToolIds].sort();
    this.microcompactSentToolIds.clear();
    this.revision += 1;
    return values;
  }

  resetMicrocompactState(): void {
    this.microcompactPinnedToolIds.clear();
    this.microcompactSentToolIds.clear();
    this.cleanup.microcompactStateCleared += 1;
    this.revision += 1;
  }

  configureSessionMemory(value: Partial<SessionMemoryState>): SessionMemoryState {
    this.sessionMemory = {
      ...this.sessionMemory,
      enabled: value.enabled ?? this.sessionMemory.enabled,
      minimumMessages: boundedInteger(value.minimumMessages, 2, 100_000, this.sessionMemory.minimumMessages),
      minimumTokens: boundedInteger(value.minimumTokens, 1_000, 1_000_000, this.sessionMemory.minimumTokens),
      targetTokensToKeep: boundedInteger(value.targetTokensToKeep, 1_000, 500_000, this.sessionMemory.targetTokensToKeep),
    };
    this.revision += 1;
    return structuredClone(this.sessionMemory);
  }

  shouldUseSessionMemoryCompaction(messages: readonly CompactMessage[]): boolean {
    if (!this.sessionMemory.enabled) return false;
    if (messages.length < this.sessionMemory.minimumMessages) return false;
    return estimateMessages(this.tokens, messages) >= this.sessionMemory.minimumTokens;
  }

  calculateMessagesToKeepIndex(messages: readonly CompactMessage[]): number {
    let tokens = 0;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      tokens += estimateMessage(this.tokens, messages[index]).estimatedTokens;
      if (tokens >= this.sessionMemory.targetTokensToKeep) {
        return adjustIndexToPreserveApiInvariants(messages, index);
      }
    }
    return 0;
  }

  async trySessionMemoryCompaction(
    messages: readonly CompactMessage[],
    options: CompactOptions,
    summarize: SummaryProvider,
  ): Promise<CompactionResult | null> {
    if (!this.shouldUseSessionMemoryCompaction(messages)) return null;
    const keepIndex = this.calculateMessagesToKeepIndex(messages);
    if (keepIndex <= 0) return null;
    const selected = new Set(messages.slice(0, keepIndex).map((item) => item.id));
    return this.partialCompactConversation(
      messages,
      selected,
      { ...options, trigger: "session_memory" },
      summarize,
    );
  }

  collectReadToolFilePaths(messages: readonly CompactMessage[]): string[] {
    const reads = new Map<string, string>();
    const unchanged = new Set<string>();
    for (const message of messages) {
      for (const block of message.content) {
        if (block.type === "tool_use" && /^(?:read|read_file|readfile)$/i.test(block.name)) {
          const path = asString(block.input.path ?? block.input.file_path ?? block.input.filePath).trim();
          if (path) reads.set(block.id, path.replaceAll("\\", "/"));
        }
        if (block.type === "tool_result") {
          const content = typeof block.content === "string" ? block.content : JSON.stringify(block.content);
          if (/unchanged|not modified|already current/i.test(content)) unchanged.add(block.toolUseId);
        }
      }
    }
    return [...reads]
      .filter(([toolUseId]) => !unchanged.has(toolUseId))
      .map(([, path]) => path)
      .filter((path, index, values) => values.indexOf(path) === index)
      .sort();
  }

  truncateToTokens(content: string, maximumTokens: number): { content: string; truncated: boolean } {
    return truncateToTokens(this.tokens, content, maximumTokens);
  }

  shouldExcludeFromPostCompactRestore(
    path: string,
    messages: readonly CompactMessage[] = [],
  ): boolean {
    const normalized = path.trim().replaceAll("\\", "/");
    if (!normalized) return true;
    if (/(?:^|\/)(?:\.git|node_modules|\.cache|dist)(?:\/|$)/i.test(normalized)) return true;
    if (/\.(?:tmp|temp|lock)$/i.test(normalized)) return true;
    const readPaths = this.collectReadToolFilePaths(messages);
    return readPaths.length > 0 && !readPaths.includes(normalized);
  }

  createPostCompactAttachments(
    candidates: readonly AttachmentCandidate[],
    messages: readonly CompactMessage[] = [],
  ): CompactAttachmentBlock[] {
    const files = candidates
      .filter((item) => item.kind === "file")
      .filter((item) => !this.shouldExcludeFromPostCompactRestore(item.path ?? "", messages))
      .sort(candidateOrder)
      .slice(0, POST_COMPACT_MAX_FILES_TO_RESTORE);
    const skills = candidates
      .filter((item) => item.kind === "skill")
      .sort(candidateOrder);
    const special = candidates
      .filter((item) => item.kind !== "file" && item.kind !== "skill")
      .sort(candidateOrder);
    const result: CompactAttachmentBlock[] = [];
    let totalTokens = 0;
    let skillTokens = 0;
    for (const candidate of [...special, ...files, ...skills]) {
      const perItemLimit = candidate.kind === "skill"
        ? POST_COMPACT_MAX_TOKENS_PER_SKILL
        : candidate.kind === "file"
        ? POST_COMPACT_MAX_TOKENS_PER_FILE
        : 10_000;
      const truncated = this.truncateToTokens(candidate.content, perItemLimit);
      const tokenCount = this.tokens.estimate(truncated.content).estimatedTokens;
      if (totalTokens + tokenCount > POST_COMPACT_TOKEN_BUDGET) continue;
      if (candidate.kind === "skill" && skillTokens + tokenCount > POST_COMPACT_SKILLS_TOKEN_BUDGET) continue;
      const attachment: CompactAttachmentBlock = {
        type: "attachment",
        attachmentKind: candidate.kind,
        path: candidate.path ?? null,
        name: candidate.name,
        content: truncated.content,
        sourceDigest: digest({ kind: candidate.kind, path: candidate.path ?? null, content: candidate.content }),
        truncated: truncated.truncated,
      };
      result.push(attachment);
      totalTokens += tokenCount;
      if (candidate.kind === "skill") skillTokens += tokenCount;
    }
    return result;
  }

  createPlanAttachmentIfNeeded(plan: string, path: string | null = null): CompactAttachmentBlock | null {
    if (!plan.trim()) return null;
    return this.createPostCompactAttachments([{ kind: "plan", path: path ?? undefined, name: "Active plan", content: plan }])[0] ?? null;
  }

  createSkillAttachmentIfNeeded(name: string, content: string): CompactAttachmentBlock | null {
    if (!name.trim() || !content.trim()) return null;
    return this.createPostCompactAttachments([{ kind: "skill", name, content }])[0] ?? null;
  }

  createAsyncAgentAttachments(candidates: readonly AttachmentCandidate[]): CompactAttachmentBlock[] {
    return this.createPostCompactAttachments(candidates.filter((item) => item.kind === "agent"));
  }

  runPostCompactCleanup(querySource: string): CleanupState {
    this.cleanup = {
      generation: this.cleanup.generation + 1,
      classifierApprovalsCleared: this.cleanup.classifierApprovalsCleared + 1,
      speculativeChecksCleared: this.cleanup.speculativeChecksCleared + 1,
      systemPromptSectionsCleared: this.cleanup.systemPromptSectionsCleared + 1,
      sessionMessageCacheCleared: this.cleanup.sessionMessageCacheCleared + 1,
      memoryFileCacheCleared: this.cleanup.memoryFileCacheCleared + 1,
      betaTracingCleared: this.cleanup.betaTracingCleared + 1,
      microcompactStateCleared: this.cleanup.microcompactStateCleared + 1,
      lastQuerySource: querySource,
      cleanedAt: new Date().toISOString(),
    };
    this.microcompactPinnedToolIds.clear();
    this.microcompactSentToolIds.clear();
    this.revision += 1;
    return structuredClone(this.cleanup);
  }

  snapshot(): ContextCompactionSnapshot {
    const unsigned: Omit<ContextCompactionSnapshot, "checksum"> = {
      version: CONTEXT_COMPACTION_SNAPSHOT_VERSION,
      revision: this.revision,
      compactGeneration: this.compactGeneration,
      boundaries: structuredClone(this.boundaries),
      sessionMemory: structuredClone(this.sessionMemory),
      cleanup: structuredClone(this.cleanup),
      microcompactPinnedToolIds: [...this.microcompactPinnedToolIds].sort(),
      microcompactSentToolIds: [...this.microcompactSentToolIds].sort(),
      tokenRuntime: this.tokens.snapshot(),
      sourceCustody: this.sourceCustody.snapshot(),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ContextCompactionSnapshot): void {
    if (snapshot.version !== CONTEXT_COMPACTION_SNAPSHOT_VERSION) {
      throw new Error("unsupported context compaction snapshot version");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("context compaction snapshot checksum mismatch");
    for (const boundary of snapshot.boundaries) {
      if (boundaryChecksum(boundary) !== boundary.checksum) {
        throw new Error(`compaction boundary checksum mismatch: ${boundary.boundaryId}`);
      }
    }
    this.boundaries.splice(0, this.boundaries.length, ...structuredClone(snapshot.boundaries));
    this.sessionMemory = structuredClone(snapshot.sessionMemory);
    this.cleanup = structuredClone(snapshot.cleanup);
    this.microcompactPinnedToolIds.clear();
    for (const id of snapshot.microcompactPinnedToolIds) this.microcompactPinnedToolIds.add(id);
    this.microcompactSentToolIds.clear();
    for (const id of snapshot.microcompactSentToolIds) this.microcompactSentToolIds.add(id);
    this.tokens.restore(snapshot.tokenRuntime);
    this.sourceCustody.restore(snapshot.sourceCustody ?? { lastMicrocompactAt: {}, pendingEdits: {} });
    this.compactGeneration = snapshot.compactGeneration;
    this.revision = snapshot.revision;
  }
}

export function adjustIndexToPreserveApiInvariants(
  messages: readonly CompactMessage[],
  proposedIndex: number,
): number {
  let index = Math.max(0, Math.min(messages.length, proposedIndex));
  const useBefore = new Set<string>();
  for (let cursor = 0; cursor < index; cursor += 1) {
    for (const block of messages[cursor].content) if (block.type === "tool_use") useBefore.add(block.id);
  }
  while (index > 0 && index < messages.length) {
    const message = messages[index];
    const orphan = message.content.some((block) => block.type === "tool_result" && useBefore.has(block.toolUseId));
    if (!orphan) break;
    index -= 1;
    for (const block of messages[index].content) if (block.type === "tool_use") useBefore.delete(block.id);
  }
  const role = messages[index]?.role;
  if (role === "tool") {
    while (index > 0 && messages[index - 1].role === "tool") index -= 1;
  }
  return index;
}

function adjustEndIndexToPreserveApiInvariants(
  messages: readonly CompactMessage[],
  proposedIndex: number,
): number {
  let index = Math.max(0, Math.min(messages.length, proposedIndex));
  const uses = new Set<string>();
  for (let cursor = 0; cursor < index; cursor += 1) {
    for (const block of messages[cursor].content) if (block.type === "tool_use") uses.add(block.id);
    for (const block of messages[cursor].content) if (block.type === "tool_result") uses.delete(block.toolUseId);
  }
  while (uses.size > 0 && index < messages.length) {
    for (const block of messages[index].content) if (block.type === "tool_result") uses.delete(block.toolUseId);
    index += 1;
  }
  return index;
}

function groupMessagesByApiRound(
  messages: readonly CompactMessage[],
  tokens: ContextTokenRuntime,
): ApiRound[] {
  const rounds = new Map<number, CompactMessage[]>();
  for (const message of messages) {
    const list = rounds.get(message.apiRound) ?? [];
    list.push(structuredClone(message));
    rounds.set(message.apiRound, list);
  }
  return [...rounds.entries()]
    .sort(([left], [right]) => left - right)
    .map(([index, values]) => {
      const toolUseIds = new Set<string>();
      const toolResultIds = new Set<string>();
      for (const message of values) {
        for (const block of message.content) {
          if (block.type === "tool_use") toolUseIds.add(block.id);
          if (block.type === "tool_result") toolResultIds.add(block.toolUseId);
        }
      }
      return {
        index,
        messages: values,
        toolUseIds,
        toolResultIds,
        tokenEstimate: estimateMessages(tokens, values),
      };
    });
}

function validateMessageInvariants(messages: readonly CompactMessage[]): void {
  const uses = new Map<string, { name: string; index: number }>();
  const results = new Set<string>();
  let previousRound = -1;
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.apiRound < previousRound) throw new Error("message API rounds are not monotonic");
    previousRound = message.apiRound;
    for (const block of message.content) {
      if (block.type === "tool_use") {
        if (uses.has(block.id)) throw new Error(`duplicate tool use id: ${block.id}`);
        uses.set(block.id, { name: block.name, index });
      }
      if (block.type === "tool_result") {
        const use = uses.get(block.toolUseId);
        if (!use) throw new Error(`orphan tool result: ${block.toolUseId}`);
        if (use.index > index) throw new Error(`tool result precedes tool use: ${block.toolUseId}`);
        if (results.has(block.toolUseId)) throw new Error(`duplicate tool result: ${block.toolUseId}`);
        results.add(block.toolUseId);
      }
    }
  }
}

function collectToolResults(messages: readonly CompactMessage[]): Array<{
  message: CompactMessage;
  block: CompactToolResultBlock;
}> {
  const result: Array<{ message: CompactMessage; block: CompactToolResultBlock }> = [];
  for (const message of messages) {
    for (const block of message.content) {
      if (block.type === "tool_result") result.push({ message, block });
    }
  }
  return result.sort((left, right) => Date.parse(left.block.createdAt) - Date.parse(right.block.createdAt));
}

function normalizeMessages(messages: readonly CompactMessage[]): CompactMessage[] {
  return messages.map((message, index) => ({
    id: message.id || randomUUID(),
    role: compactRole(message.role),
    content: message.content.map(normalizeBlock),
    createdAt: normalizeTimestamp(message.createdAt),
    turnIndex: message.turnIndex === null ? null : Math.max(0, Math.floor(message.turnIndex)),
    apiRound: Math.max(0, Math.floor(message.apiRound ?? index)),
    synthetic: Boolean(message.synthetic),
    metadata: asObject(message.metadata),
  }));
}

function normalizeBlock(block: CompactContentBlock): CompactContentBlock {
  if (block.type === "text") return { type: "text", text: block.text };
  if (block.type === "image") return { ...structuredClone(block), data: block.data.replace(/^data:[^;]+;base64,/, "") };
  if (block.type === "document") return { ...structuredClone(block), data: block.data.replace(/^data:[^;]+;base64,/, "") };
  if (block.type === "tool_use") {
    if (!block.id || !block.name) throw new Error("tool use requires id and name");
    return { ...structuredClone(block), input: asObject(block.input) };
  }
  if (block.type === "tool_result") {
    if (!block.toolUseId) throw new Error("tool result requires tool use id");
    return { ...structuredClone(block), createdAt: normalizeTimestamp(block.createdAt) };
  }
  if (block.type === "thinking") return structuredClone(block);
  if (block.type === "attachment") return structuredClone(block);
  throw new Error("unsupported compact content block");
}

function estimateMessages(tokens: ContextTokenRuntime, messages: readonly CompactMessage[]): number {
  return messages.reduce((sum, message) => sum + estimateMessage(tokens, message).estimatedTokens, 0);
}

function estimateMessage(tokens: ContextTokenRuntime, message: CompactMessage): TokenEstimate {
  return tokens.estimate(messageToJson(message));
}

function estimateToolResult(tokens: ContextTokenRuntime, block: CompactToolResultBlock): number {
  return tokens.estimate(blockToJson(block)).estimatedTokens;
}

function estimateAttachments(
  tokens: ContextTokenRuntime,
  attachments: readonly CompactAttachmentBlock[],
): number {
  return attachments.reduce((sum, item) => sum + tokens.estimate(item.content).estimatedTokens, 0);
}

function countBlocks(messages: readonly CompactMessage[], type: CompactBlockType): number {
  return messages.reduce((sum, message) => sum + message.content.filter((block) => block.type === type).length, 0);
}

function extractDiscoveredToolNames(messages: readonly CompactMessage[]): string[] {
  const names = new Set<string>();
  for (const message of messages) {
    for (const block of message.content) if (block.type === "tool_use") names.add(block.name);
  }
  return [...names].sort();
}

function truncateToTokens(
  tokens: ContextTokenRuntime,
  content: string,
  maximumTokens: number,
): { content: string; truncated: boolean } {
  if (tokens.estimate(content).estimatedTokens <= maximumTokens) return { content, truncated: false };
  let low = 0;
  let high = content.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (tokens.estimate(content.slice(0, middle)).estimatedTokens <= maximumTokens) low = middle;
    else high = middle - 1;
  }
  return { content: `${content.slice(0, low)}\n...[truncated after ${maximumTokens} tokens]`, truncated: true };
}

function candidateOrder(left: AttachmentCandidate, right: AttachmentCandidate): number {
  const priority = (right.priority ?? 0) - (left.priority ?? 0);
  if (priority !== 0) return priority;
  const recency = Date.parse(right.lastReadAt ?? "1970-01-01") - Date.parse(left.lastReadAt ?? "1970-01-01");
  if (recency !== 0) return recency;
  return left.name.localeCompare(right.name);
}

function compactOptionsFromJson(value: JsonObject): CompactOptions {
  const attachments = Array.isArray(value.attachments) ? value.attachments : [];
  return {
    trigger: compactTrigger(asString(value.trigger, "manual")),
    model: asString(value.model, "unknown"),
    contextWindow: integer(value.context_window, 200_000),
    maxOutputTokens: integer(value.max_output_tokens, 8_192),
    targetTokens: integer(value.target_tokens, 50_000),
    preserveRecentMessages: integer(value.preserve_recent_messages, 8),
    preserveApiRounds: integer(value.preserve_api_rounds, 3),
    systemPrompt: asString(value.system_prompt),
    customInstructions: asString(value.custom_instructions),
    attachments: attachments.map((item) => attachmentCandidateFromJson(asObject(item))),
    querySource: asString(value.query_source, "runtime"),
    now: asString(value.now, new Date().toISOString()),
  };
}

function attachmentCandidateFromJson(value: JsonObject): AttachmentCandidate {
  const kind = asString(value.kind, "file");
  return {
    kind: kind === "plan" || kind === "skill" || kind === "agent" || kind === "memory" ? kind : "file",
    path: asString(value.path) || undefined,
    name: asString(value.name, "attachment"),
    content: asString(value.content),
    lastReadAt: asString(value.last_read_at) || undefined,
    priority: integer(value.priority, 0),
  };
}

function messagesFromJson(value: JsonValue | undefined): CompactMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => messageFromJson(asObject(item), index));
}

function messageFromJson(value: JsonObject, index: number): CompactMessage {
  const content = Array.isArray(value.content)
    ? value.content.map((item) => blockFromJson(asObject(item)))
    : [{ type: "text" as const, text: asString(value.content) }];
  return {
    id: asString(value.id, randomUUID()),
    role: compactRole(asString(value.role, "user")),
    content,
    createdAt: asString(value.created_at, new Date().toISOString()),
    turnIndex: value.turn_index === null || value.turn_index === undefined ? null : integer(value.turn_index, index),
    apiRound: integer(value.api_round, index),
    synthetic: asBoolean(value.synthetic, false),
    metadata: asObject(value.metadata),
  };
}

function blockFromJson(value: JsonObject): CompactContentBlock {
  const type = asString(value.type, "text");
  if (type === "tool_use") return { type, id: asString(value.id), name: asString(value.name), input: asObject(value.input) };
  if (type === "tool_result") {
    return {
      type,
      toolUseId: asString(value.tool_use_id),
      content: Array.isArray(value.content) ? value.content : asString(value.content),
      isError: asBoolean(value.is_error),
      createdAt: asString(value.created_at, new Date().toISOString()),
      compacted: asBoolean(value.compacted),
    };
  }
  if (type === "thinking") return { type, thinking: asString(value.thinking), signature: asString(value.signature) || null };
  if (type === "image") {
    return {
      type,
      mediaType: asString(value.media_type, "image/png"),
      data: asString(value.data),
      width: integer(value.width, 0) || undefined,
      height: integer(value.height, 0) || undefined,
    };
  }
  if (type === "document") {
    return {
      type,
      mediaType: asString(value.media_type, "application/pdf"),
      data: asString(value.data),
      title: asString(value.title) || undefined,
    };
  }
  if (type === "attachment") {
    const kind = asString(value.attachment_kind, "file");
    return {
      type,
      attachmentKind: kind === "plan" || kind === "skill" || kind === "agent" || kind === "memory" ? kind : "file",
      path: asString(value.path) || null,
      name: asString(value.name),
      content: asString(value.content),
      sourceDigest: asString(value.source_digest, digest(value)),
      truncated: asBoolean(value.truncated),
    };
  }
  return { type: "text", text: asString(value.text) };
}

function messageToJson(value: CompactMessage): JsonObject {
  return {
    id: value.id,
    role: value.role,
    content: value.content.map(blockToJson),
    created_at: value.createdAt,
    turn_index: value.turnIndex,
    api_round: value.apiRound,
    synthetic: value.synthetic,
    metadata: value.metadata,
  };
}

function blockToJson(value: CompactContentBlock): JsonObject {
  if (value.type === "text") return { type: value.type, text: value.text };
  if (value.type === "image") {
    return { type: value.type, media_type: value.mediaType, data: value.data, width: value.width ?? null, height: value.height ?? null };
  }
  if (value.type === "document") {
    return { type: value.type, media_type: value.mediaType, data: value.data, title: value.title ?? null };
  }
  if (value.type === "tool_use") return { type: value.type, id: value.id, name: value.name, input: value.input };
  if (value.type === "tool_result") {
    return {
      type: value.type,
      tool_use_id: value.toolUseId,
      content: value.content,
      is_error: value.isError,
      created_at: value.createdAt,
      compacted: value.compacted,
    };
  }
  if (value.type === "thinking") return { type: value.type, thinking: value.thinking, signature: value.signature };
  return {
    type: value.type,
    attachment_kind: value.attachmentKind,
    path: value.path,
    name: value.name,
    content: value.content,
    source_digest: value.sourceDigest,
    truncated: value.truncated,
  };
}

function boundaryToJson(value: CompactBoundary): JsonObject {
  return {
    boundary_id: value.boundaryId,
    trigger: value.trigger,
    summary: value.summary,
    source_message_ids: value.sourceMessageIds,
    preserved_message_ids: value.preservedMessageIds,
    attachment_ids: value.attachmentIds,
    source_token_estimate: value.sourceTokenEstimate,
    result_token_estimate: value.resultTokenEstimate,
    removed_message_count: value.removedMessageCount,
    removed_tool_result_count: value.removedToolResultCount,
    compact_generation: value.compactGeneration,
    parent_boundary_id: value.parentBoundaryId,
    created_at: value.createdAt,
    checksum: value.checksum,
  };
}

function microcompactToJson(value: MicrocompactResult): JsonObject {
  return {
    messages: value.messages.map(messageToJson),
    compacted_tool_use_ids: value.compactedToolUseIds,
    removed_tokens: value.removedTokens,
    preserved_tokens: value.preservedTokens,
    changed: value.changed,
    trigger: value.trigger,
  };
}

function warningToJson(value: TokenWarningState): JsonObject {
  return {
    level: value.level,
    estimated_tokens: value.estimatedTokens,
    context_window: value.contextWindow,
    remaining_tokens: value.remainingTokens,
    auto_compact_threshold: value.autoCompactThreshold,
    warning_threshold: value.warningThreshold,
    error_threshold: value.errorThreshold,
    should_auto_compact: value.shouldAutoCompact,
    output_reservation: value.outputReservation,
  };
}

function sessionMemoryToJson(value: SessionMemoryState): JsonObject {
  return {
    enabled: value.enabled,
    minimum_messages: value.minimumMessages,
    minimum_tokens: value.minimumTokens,
    target_tokens_to_keep: value.targetTokensToKeep,
    last_summarized_message_id: value.lastSummarizedMessageId,
    summary: value.summary,
    discovered_tools: value.discoveredTools,
    generation: value.generation,
  };
}

function cleanupToJson(value: CleanupState): JsonObject {
  return {
    generation: value.generation,
    classifier_approvals_cleared: value.classifierApprovalsCleared,
    speculative_checks_cleared: value.speculativeChecksCleared,
    system_prompt_sections_cleared: value.systemPromptSectionsCleared,
    session_message_cache_cleared: value.sessionMessageCacheCleared,
    memory_file_cache_cleared: value.memoryFileCacheCleared,
    beta_tracing_cleared: value.betaTracingCleared,
    microcompact_state_cleared: value.microcompactStateCleared,
    last_query_source: value.lastQuerySource,
    cleaned_at: value.cleanedAt,
  };
}

function defaultSessionMemory(): SessionMemoryState {
  return {
    enabled: true,
    minimumMessages: 20,
    minimumTokens: 80_000,
    targetTokensToKeep: 30_000,
    lastSummarizedMessageId: null,
    summary: "",
    discoveredTools: [],
    generation: 0,
  };
}

function defaultCleanupState(): CleanupState {
  return {
    generation: 0,
    classifierApprovalsCleared: 0,
    speculativeChecksCleared: 0,
    systemPromptSectionsCleared: 0,
    sessionMessageCacheCleared: 0,
    memoryFileCacheCleared: 0,
    betaTracingCleared: 0,
    microcompactStateCleared: 0,
    lastQuerySource: null,
    cleanedAt: null,
  };
}

function boundaryChecksum(value: CompactBoundary): string {
  const selected = { ...value, checksum: "" };
  return digest(selected);
}

function compactRole(value: string): CompactMessageRole {
  if (value === "system" || value === "assistant" || value === "tool") return value;
  return "user";
}

function compactTrigger(value: string): CompactTrigger {
  if (value === "auto_threshold" || value === "prompt_too_long" || value === "session_memory" || value === "microcompact" || value === "resume_repair") return value;
  return "manual";
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function boundedInteger(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined || !Number.isFinite(value)) return fallback;
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : fallback;
}

function findLastIndex<T>(values: readonly T[], predicate: (value: T) => boolean): number {
  for (let index = values.length - 1; index >= 0; index -= 1) if (predicate(values[index])) return index;
  return -1;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
