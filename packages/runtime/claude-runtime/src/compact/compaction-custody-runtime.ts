export interface CustodyCompactionMessage {
  role: string;
  content: unknown;
  id?: string;
  createdAtMs?: number;
  [key: string]: unknown;
}

export interface MicrocompactResult {
  messages: CustodyCompactionMessage[];
  editedToolResultIds: string[];
  removedCharacters: number;
  cachePath: string;
  performed: boolean;
  reason: "interval" | "threshold" | "not-main-thread" | "not-due";
}

export interface CompactBoundary {
  boundaryId: string;
  firstKeptIndex: number;
  preservedSegment: CustodyCompactionMessage[];
  summary: string;
  source: "stream" | "session-memory" | "partial";
}

export interface SessionMemoryCompactionInput {
  sessionId: string;
  summary: string;
  messages: readonly CustodyCompactionMessage[];
  attachments?: readonly unknown[];
  keepLastMessages: number;
  originalTokenCount: number;
  summaryTokenCount: number;
}

export interface SessionMemoryCompactionResult {
  boundary: CompactBoundary;
  messages: CustodyCompactionMessage[];
  attachments: unknown[];
  originalTokenCount: number;
  compactedTokenCount: number;
  savedTokenCount: number;
}

export type SummaryStreamEvent =
  | { type: "delta"; text: string }
  | { type: "keepalive" }
  | { type: "complete"; text?: string }
  | { type: "error"; error: unknown };

type MutableRecord = Record<string, unknown>;

const recordOf = (value: unknown): MutableRecord | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as MutableRecord
    : null;

const cloneValue = <T>(value: T): T => {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  const record = recordOf(value);
  if (!record) return value;
  return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, cloneValue(item)])) as T;
};

const stableId = (value: string): string => {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
};

const messageBlocks = (message: CustodyCompactionMessage): MutableRecord[] => {
  if (!Array.isArray(message.content)) return [];
  return message.content.map(recordOf).filter((item): item is MutableRecord => item !== null);
};

export class CompactionSourceCustodyRuntime {
  private readonly lastMicrocompactAt = new Map<string, number>();
  private readonly pendingEdits = new Map<string, string[]>();

  pendingCacheEdits(sessionId: string): readonly string[] {
    return [...(this.pendingEdits.get(sessionId) ?? [])];
  }

  consumePendingCacheEdits(sessionId: string): string[] {
    const edits = [...(this.pendingEdits.get(sessionId) ?? [])];
    this.pendingEdits.delete(sessionId);
    return edits;
  }

  isMainThreadSource(source: string): boolean {
    return !/subagent|background|fork|worker/i.test(source);
  }

  cachedMicrocompactPath(cacheRoot: string, sessionId: string): string {
    const root = cacheRoot.replaceAll("\\", "/").replace(/\/$/, "");
    const safeSession = sessionId.replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 96) || "unknown";
    return `${root}/${safeSession}/microcompact-${stableId(sessionId)}.json`;
  }

  microcompactMessages(
    messages: readonly CustodyCompactionMessage[],
    options: { keepLastMessages: number; replacementLimit: number },
  ): { messages: CustodyCompactionMessage[]; editedToolResultIds: string[]; removedCharacters: number } {
    const cloned = cloneValue(messages) as CustodyCompactionMessage[];
    const cutoff = Math.max(0, cloned.length - Math.max(1, options.keepLastMessages));
    const editedToolResultIds: string[] = [];
    let removedCharacters = 0;
    let replacements = 0;
    for (let messageIndex = 0; messageIndex < cutoff; messageIndex += 1) {
      for (const block of messageBlocks(cloned[messageIndex]!)) {
        if (String(block.type ?? "") !== "tool_result") continue;
        if (replacements >= Math.max(0, options.replacementLimit)) break;
        const content = typeof block.content === "string" ? block.content : JSON.stringify(block.content ?? "");
        if (content.length < 96) continue;
        const id = String(block.tool_use_id ?? block.toolUseId ?? `message-${messageIndex}`);
        const replacement = `[tool result ${id} compacted; ${content.length} characters removed]`;
        block.content = replacement;
        block.compacted = true;
        block.originalCharacterCount = content.length;
        editedToolResultIds.push(id);
        removedCharacters += Math.max(0, content.length - replacement.length);
        replacements += 1;
      }
    }
    return { messages: cloned, editedToolResultIds, removedCharacters };
  }

  maybeTimeBasedMicrocompact(input: {
    sessionId: string;
    source: string;
    nowMs: number;
    intervalMs: number;
    cacheRoot: string;
    messages: readonly CustodyCompactionMessage[];
    keepLastMessages?: number;
    replacementLimit?: number;
  }): MicrocompactResult {
    const cachePath = this.cachedMicrocompactPath(input.cacheRoot, input.sessionId);
    if (!this.isMainThreadSource(input.source)) {
      return { messages: [...cloneValue(input.messages)], editedToolResultIds: [], removedCharacters: 0, cachePath, performed: false, reason: "not-main-thread" };
    }
    const lastAt = this.lastMicrocompactAt.get(input.sessionId) ?? 0;
    if (input.nowMs - lastAt < Math.max(1, input.intervalMs)) {
      return { messages: [...cloneValue(input.messages)], editedToolResultIds: [], removedCharacters: 0, cachePath, performed: false, reason: "not-due" };
    }
    const compacted = this.microcompactMessages(input.messages, {
      keepLastMessages: input.keepLastMessages ?? 4,
      replacementLimit: input.replacementLimit ?? 8,
    });
    this.lastMicrocompactAt.set(input.sessionId, input.nowMs);
    if (compacted.editedToolResultIds.length > 0) {
      this.pendingEdits.set(input.sessionId, compacted.editedToolResultIds);
    }
    return {
      ...compacted,
      cachePath,
      performed: compacted.editedToolResultIds.length > 0,
      reason: "interval",
    };
  }

  shouldAutoCompact(input: {
    currentTokens: number;
    contextWindow: number;
    reservedTokens: number;
    minimumFreeTokens: number;
  }): boolean {
    const usable = Math.max(1, input.contextWindow - Math.max(0, input.reservedTokens));
    const free = usable - Math.max(0, input.currentTokens);
    return free <= Math.max(0, input.minimumFreeTokens);
  }

  autoCompactIfNeeded(input: {
    currentTokens: number;
    contextWindow: number;
    reservedTokens: number;
    minimumFreeTokens: number;
    messages: readonly CustodyCompactionMessage[];
    keepLastMessages: number;
  }): { required: boolean; firstKeptIndex: number; preserved: CustodyCompactionMessage[] } {
    const required = this.shouldAutoCompact(input);
    const firstKeptIndex = required
      ? Math.max(0, input.messages.length - Math.max(1, input.keepLastMessages))
      : 0;
    return {
      required,
      firstKeptIndex,
      preserved: cloneValue(input.messages.slice(firstKeptIndex)),
    };
  }

  annotateBoundaryWithPreservedSegment(
    boundary: Omit<CompactBoundary, "preservedSegment">,
    messages: readonly CustodyCompactionMessage[],
  ): CompactBoundary {
    return {
      ...cloneValue(boundary),
      preservedSegment: cloneValue(messages.slice(Math.max(0, boundary.firstKeptIndex))),
    };
  }

  partialCompactConversation(input: {
    sessionId: string;
    messages: readonly CustodyCompactionMessage[];
    summary: string;
    keepLastMessages: number;
  }): CompactBoundary {
    const firstKeptIndex = Math.max(0, input.messages.length - Math.max(1, input.keepLastMessages));
    return this.annotateBoundaryWithPreservedSegment({
      boundaryId: `partial-${stableId(`${input.sessionId}:${firstKeptIndex}:${input.summary}`)}`,
      firstKeptIndex,
      summary: input.summary,
      source: "partial",
    }, input.messages);
  }

  async streamCompactSummary(input: {
    stream: (attempt: number) => AsyncIterable<SummaryStreamEvent>;
    maxAttempts: number;
    onKeepalive?: (attempt: number) => void;
  }): Promise<{ summary: string; attempts: number; keepalives: number }> {
    let lastError: unknown = new Error("summary stream did not complete");
    for (let attempt = 1; attempt <= Math.max(1, input.maxAttempts); attempt += 1) {
      let summary = "";
      let keepalives = 0;
      let completed = false;
      try {
        for await (const event of input.stream(attempt)) {
          if (event.type === "delta") summary += event.text;
          else if (event.type === "keepalive") {
            keepalives += 1;
            input.onKeepalive?.(attempt);
          } else if (event.type === "complete") {
            summary += event.text ?? "";
            completed = true;
          } else if (event.type === "error") {
            throw event.error;
          }
        }
        if (!completed || summary.trim().length === 0) throw new Error("incomplete compact summary response");
        return { summary: summary.trim(), attempts: attempt, keepalives };
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError;
  }

  shouldUseSessionMemoryCompaction(input: {
    enabled: boolean;
    summary: string | null | undefined;
    summaryTokenCount: number;
    currentTokenCount: number;
  }): boolean {
    if (!input.enabled || !input.summary?.trim()) return false;
    if (input.summaryTokenCount <= 0) return false;
    return input.summaryTokenCount < input.currentTokenCount;
  }

  createCompactionResultFromSessionMemory(input: SessionMemoryCompactionInput): SessionMemoryCompactionResult {
    const firstKeptIndex = Math.max(0, input.messages.length - Math.max(1, input.keepLastMessages));
    const boundary = this.annotateBoundaryWithPreservedSegment({
      boundaryId: `memory-${stableId(`${input.sessionId}:${input.summary}`)}`,
      firstKeptIndex,
      summary: input.summary.trim(),
      source: "session-memory",
    }, input.messages);
    return {
      boundary,
      messages: [
        { role: "system", content: input.summary.trim(), compactedSessionMemory: true },
        ...cloneValue(input.messages.slice(firstKeptIndex)),
      ],
      attachments: [...cloneValue(input.attachments ?? [])],
      originalTokenCount: input.originalTokenCount,
      compactedTokenCount: input.summaryTokenCount,
      savedTokenCount: Math.max(0, input.originalTokenCount - input.summaryTokenCount),
    };
  }

  trySessionMemoryCompaction(input: SessionMemoryCompactionInput & { enabled: boolean }): SessionMemoryCompactionResult | null {
    if (!this.shouldUseSessionMemoryCompaction({
      enabled: input.enabled,
      summary: input.summary,
      summaryTokenCount: input.summaryTokenCount,
      currentTokenCount: input.originalTokenCount,
    })) return null;
    return this.createCompactionResultFromSessionMemory(input);
  }

  applyCompactionCustody(input: unknown): MicrocompactResult | null {
    const record = recordOf(input);
    if (!record || !Array.isArray(record.messages)) return null;
    const result = this.maybeTimeBasedMicrocompact({
      sessionId: String(record.sessionId ?? "default"),
      source: String(record.source ?? record.querySource ?? "runtime"),
      nowMs: typeof record.nowMs === "number" ? record.nowMs : Date.now(),
      intervalMs: typeof record.microcompactIntervalMs === "number" ? record.microcompactIntervalMs : 5 * 60_000,
      cacheRoot: String(record.cacheRoot ?? ".cache/compact"),
      messages: record.messages as CustodyCompactionMessage[],
      keepLastMessages: typeof record.keepLastMessages === "number" ? record.keepLastMessages : 4,
      replacementLimit: typeof record.replacementLimit === "number" ? record.replacementLimit : 8,
    });
    if (result.performed) record.messages = result.messages;
    record.sourceCustodyCompaction = result;
    return result;
  }

  snapshot(): { lastMicrocompactAt: Record<string, number>; pendingEdits: Record<string, string[]> } {
    return {
      lastMicrocompactAt: Object.fromEntries(this.lastMicrocompactAt),
      pendingEdits: Object.fromEntries([...this.pendingEdits].map(([key, value]) => [key, [...value]])),
    };
  }

  restore(snapshot: { lastMicrocompactAt: Record<string, number>; pendingEdits: Record<string, string[]> }): void {
    this.lastMicrocompactAt.clear();
    for (const [key, value] of Object.entries(snapshot.lastMicrocompactAt)) this.lastMicrocompactAt.set(key, value);
    this.pendingEdits.clear();
    for (const [key, value] of Object.entries(snapshot.pendingEdits)) this.pendingEdits.set(key, [...value]);
  }
}

export const compactionSourceCustodyRuntime = new CompactionSourceCustodyRuntime();
