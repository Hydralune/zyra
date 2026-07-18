import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  boundedString,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";
import {
  jsonChars,
  runtimeId,
  type ArtifactReceipt,
  type RuntimeHost,
  type ToolExecutionResponse,
} from "../contracts.ts";

export type ToolResultBlockKind =
  | "text"
  | "json"
  | "image"
  | "file"
  | "diagnostic"
  | "error";

export type ToolResultStatus = "open" | "sealed" | "cancelled" | "late";
export type ToolResultSensitivity = "public" | "internal" | "sensitive" | "secret";

export interface ToolResultBlock {
  blockId: string;
  kind: ToolResultBlockKind;
  sequence: number;
  text: string | null;
  json: JsonValue | null;
  mediaType: string | null;
  artifactId: string | null;
  byteLength: number;
  estimatedTokens: number;
  sensitivity: ToolResultSensitivity;
  digest: string;
  metadata: JsonRecord;
}

export interface ToolResultChunk {
  chunkId: string;
  toolCallId: string;
  sequence: number;
  emittedAt: number;
  blocks: ToolResultBlock[];
  final: boolean;
  digest: string;
}

export interface ToolResultBudget {
  maximumCharacters: number;
  maximumTokens: number;
  maximumBlocks: number;
  maximumChunks: number;
  maximumInlineBytes: number;
  preserveHeadCharacters: number;
  preserveTailCharacters: number;
  includeDiagnostics: boolean;
  allowedSensitivity: ToolResultSensitivity;
}

export interface ToolResultAccumulator {
  resultId: string;
  toolCallId: string;
  toolName: string;
  sessionId: string;
  runId: string;
  status: ToolResultStatus;
  expectedSequence: number;
  startedAt: number;
  sealedAt: number | null;
  success: boolean | null;
  errorCode: string | null;
  chunks: ToolResultChunk[];
  revision: number;
  metadata: JsonRecord;
}

export interface ToolResultDelivery {
  deliveryId: string;
  resultId: string;
  toolCallId: string;
  success: boolean;
  content: ToolResultBlock[];
  omittedBlockIds: string[];
  originalCharacters: number;
  deliveredCharacters: number;
  originalTokens: number;
  deliveredTokens: number;
  truncated: boolean;
  redacted: boolean;
  late: boolean;
  createdAt: number;
  sourceDigest: string;
  deliveryDigest: string;
}

export interface ToolResultRedactionRule {
  ruleId: string;
  pattern: string;
  flags: string;
  replacement: string;
  minimumSensitivity: ToolResultSensitivity;
  enabled: boolean;
}

export interface ToolResultRuntimeSnapshot {
  version: "zyra.tool-result/v1";
  accumulators: ToolResultAccumulator[];
  deliveries: ToolResultDelivery[];
  redactionRules: ToolResultRedactionRule[];
  budgetReplacements?: ToolResultBudgetReplacement[];
  checksum: string;
}

export interface ToolResultBudgetReplacement {
  toolCallId: string;
  sourceDigest: string;
  maximumCharacters: number;
  originalCharacters: number;
  artifact: ArtifactReceipt;
  result: ToolExecutionResponse;
  createdAt: number;
}

export interface BudgetedToolResult {
  result: ToolExecutionResponse;
  artifact: ArtifactReceipt | null;
  originalChars: number;
  applied: boolean;
  reapplied: boolean;
}

export interface ToolResultRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  estimateTokens?: (text: string) => number;
  maximumRetainedDeliveries?: number;
  redactionRules?: ToolResultRedactionRule[];
}

const SENSITIVITY_RANK: Record<ToolResultSensitivity, number> = {
  public: 0,
  internal: 1,
  sensitive: 2,
  secret: 3,
};

export class ToolResultRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly estimateTokens: (text: string) => number;
  private readonly maximumRetainedDeliveries: number;
  private readonly accumulators = new Map<string, ToolResultAccumulator>();
  private readonly byToolCallId = new Map<string, string>();
  private readonly deliveries = new Map<string, ToolResultDelivery>();
  private readonly redactionRules = new Map<string, ToolResultRedactionRule>();
  private readonly budgetReplacements = new Map<string, ToolResultBudgetReplacement>();
  private readonly budgetInFlight = new Map<string, {
    sourceDigest: string;
    operation: Promise<BudgetedToolResult>;
  }>();

  constructor(options: ToolResultRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.estimateTokens = options.estimateTokens ?? defaultTokenEstimate;
    this.maximumRetainedDeliveries = options.maximumRetainedDeliveries ?? 5_000;
    assertNonNegativeInteger(
      this.maximumRetainedDeliveries,
      "maximumRetainedDeliveries",
    );
    for (const rule of options.redactionRules ?? defaultRedactionRules()) {
      this.registerRedactionRule(rule);
    }
  }

  /**
   * Adapted from claude-code-best enforceToolResultBudget. Prior decisions are
   * reapplied without I/O; fresh oversized results are persisted before the
   * seen/replacement pair becomes observable.
   */
  async enforceToolResultBudget(
    host: Pick<RuntimeHost, "externalize">,
    result: ToolExecutionResponse,
    maxChars: number,
  ): Promise<BudgetedToolResult> {
    if (process.env.ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME === "1") {
      throw new Error("e04_tool_source_runtime_disabled");
    }
    const sourceDigest = digestJson(result as unknown as JsonValue);
    const existing = this.budgetReplacements.get(result.tool_call_id);
    if (existing) {
      if (existing.sourceDigest !== sourceDigest) {
        throw new RuntimeInvariantError("tool_result_budget_identity_conflict", {
          toolCallId: result.tool_call_id,
        });
      }
      return {
        result: deepClone(existing.result),
        artifact: deepClone(existing.artifact),
        originalChars: existing.originalCharacters,
        applied: true,
        reapplied: true,
      };
    }
    const pending = this.budgetInFlight.get(result.tool_call_id);
    if (pending) {
      if (pending.sourceDigest !== sourceDigest) {
        throw new RuntimeInvariantError("tool_result_budget_identity_conflict", {
          toolCallId: result.tool_call_id,
        });
      }
      return pending.operation;
    }
    const operation = this.enforceFreshToolResultBudget(
      host,
      result,
      Math.max(1, Math.floor(maxChars)),
      sourceDigest,
    );
    this.budgetInFlight.set(result.tool_call_id, { sourceDigest, operation });
    try {
      return await operation;
    } finally {
      this.budgetInFlight.delete(result.tool_call_id);
    }
  }

  private async enforceFreshToolResultBudget(
    host: Pick<RuntimeHost, "externalize">,
    result: ToolExecutionResponse,
    maxChars: number,
    sourceDigest: string,
  ): Promise<BudgetedToolResult> {
    const originalChars = jsonChars(result.output);
    if (originalChars <= maxChars) {
      return { result, artifact: null, originalChars, applied: false, reapplied: false };
    }
    const serialized = JSON.stringify(result.output);
    const artifact = await host.externalize({
      requestId: runtimeId("artifact_request"),
      title: "CodeWorker tool result " + result.tool_call_id,
      kind: "structured_data",
      extension: ".json",
      content: serialized,
      metadata: {
        source: "typescript_tool_result_budget",
        tool_call_id: result.tool_call_id,
        original_chars: originalChars,
        budget_chars: maxChars,
      },
    });
    const previewChars = Math.max(0, Math.min(maxChars, serialized.length));
    const replacement: ToolExecutionResponse = {
      ...result,
      output: {
        content_preview: serialized.slice(0, previewChars),
        truncated: true,
        original_chars: originalChars,
        artifact_id: artifact.artifact_id,
      },
      artifacts: [...result.artifacts, artifact],
      metadata: {
        ...result.metadata,
        tool_result_budget_applied: "true",
        tool_result_budget_chars: String(maxChars),
        tool_result_original_chars: String(originalChars),
        tool_result_artifact_id: artifact.artifact_id,
      },
    };
    // Commit the pair only after the physical artifact effect has succeeded.
    this.budgetReplacements.set(result.tool_call_id, {
      toolCallId: result.tool_call_id,
      sourceDigest,
      maximumCharacters: maxChars,
      originalCharacters: originalChars,
      artifact: deepClone(artifact),
      result: deepClone(replacement),
      createdAt: this.clock.now(),
    });
    return {
      result: replacement,
      artifact,
      originalChars,
      applied: true,
      reapplied: false,
    };
  }

  begin(input: {
    resultId?: string;
    toolCallId: string;
    toolName: string;
    sessionId: string;
    runId: string;
    metadata?: JsonRecord;
  }): ToolResultAccumulator {
    validateBegin(input);
    const existingId = this.byToolCallId.get(input.toolCallId);
    if (existingId !== undefined) {
      const existing = this.requireAccumulator(existingId);
      if (
        existing.toolName !== input.toolName ||
        existing.sessionId !== input.sessionId ||
        existing.runId !== input.runId
      ) {
        throw new RuntimeInvariantError("tool_result_begin_conflict", {
          toolCallId: input.toolCallId,
        });
      }
      return deepClone(existing);
    }
    const resultId = input.resultId ?? this.ids.next("tool-result");
    if (this.accumulators.has(resultId)) {
      throw new RuntimeInvariantError("tool_result_already_exists", { resultId });
    }
    const accumulator: ToolResultAccumulator = {
      resultId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      sessionId: input.sessionId,
      runId: input.runId,
      status: "open",
      expectedSequence: 1,
      startedAt: this.clock.now(),
      sealedAt: null,
      success: null,
      errorCode: null,
      chunks: [],
      revision: 1,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.accumulators.set(resultId, accumulator);
    this.byToolCallId.set(input.toolCallId, resultId);
    return deepClone(accumulator);
  }

  append(input: {
    resultId: string;
    chunkId?: string;
    sequence: number;
    blocks: Array<
      Omit<
        ToolResultBlock,
        "blockId" | "sequence" | "byteLength" | "estimatedTokens" | "digest"
      > & { blockId?: string }
    >;
    final?: boolean;
  }): ToolResultChunk {
    const accumulator = this.requireAccumulator(input.resultId);
    this.assertOpen(accumulator);
    assertNonNegativeInteger(input.sequence, "sequence");
    const chunkId = input.chunkId ?? this.ids.next("tool-result-chunk");
    const existing = accumulator.chunks.find((chunk) => chunk.chunkId === chunkId);
    const normalizedBlocks = input.blocks.map((block, index) =>
      this.normalizeBlock(block, index),
    );
    const digest = digestJson({
      toolCallId: accumulator.toolCallId,
      sequence: input.sequence,
      blocks: normalizedBlocks,
      final: input.final ?? false,
    });
    if (existing !== undefined) {
      if (existing.digest !== digest) {
        throw new RuntimeInvariantError("tool_result_chunk_conflict", {
          chunkId,
        });
      }
      return deepClone(existing);
    }
    if (input.sequence !== accumulator.expectedSequence) {
      throw new RuntimeInvariantError("tool_result_chunk_sequence_gap", {
        resultId: input.resultId,
        expected: accumulator.expectedSequence,
        actual: input.sequence,
      });
    }
    const chunk: ToolResultChunk = {
      chunkId,
      toolCallId: accumulator.toolCallId,
      sequence: input.sequence,
      emittedAt: this.clock.now(),
      blocks: normalizedBlocks,
      final: input.final ?? false,
      digest,
    };
    accumulator.chunks.push(chunk);
    accumulator.expectedSequence += 1;
    accumulator.revision += 1;
    if (chunk.final) {
      accumulator.status = "sealed";
      accumulator.sealedAt = this.clock.now();
      accumulator.success = true;
    }
    return deepClone(chunk);
  }

  seal(input: {
    resultId: string;
    success: boolean;
    errorCode?: string | null;
    metadata?: JsonRecord;
  }): ToolResultAccumulator {
    const accumulator = this.requireAccumulator(input.resultId);
    if (accumulator.status === "sealed" || accumulator.status === "late") {
      if (
        accumulator.success !== input.success ||
        accumulator.errorCode !== (input.errorCode ?? null)
      ) {
        throw new RuntimeInvariantError("tool_result_seal_conflict", {
          resultId: input.resultId,
        });
      }
      return deepClone(accumulator);
    }
    this.assertOpen(accumulator);
    if (!input.success && (input.errorCode ?? "").length === 0) {
      throw new RuntimeInvariantError("tool_result_error_code_required", {
        resultId: input.resultId,
      });
    }
    accumulator.status = "sealed";
    accumulator.sealedAt = this.clock.now();
    accumulator.success = input.success;
    accumulator.errorCode = input.errorCode ?? null;
    accumulator.metadata = {
      ...accumulator.metadata,
      ...deepClone(input.metadata ?? {}),
    };
    accumulator.revision += 1;
    return deepClone(accumulator);
  }

  cancel(resultId: string, reason: string): ToolResultAccumulator {
    const accumulator = this.requireAccumulator(resultId);
    assertNonEmpty(reason, "reason");
    if (accumulator.status === "sealed" || accumulator.status === "late") {
      throw new RuntimeInvariantError("sealed_tool_result_not_cancellable", {
        resultId,
      });
    }
    accumulator.status = "cancelled";
    accumulator.sealedAt = this.clock.now();
    accumulator.success = false;
    accumulator.errorCode = "cancelled";
    accumulator.metadata = { ...accumulator.metadata, cancellationReason: reason };
    accumulator.revision += 1;
    return deepClone(accumulator);
  }

  markLate(resultId: string, reason: string): ToolResultAccumulator {
    const accumulator = this.requireAccumulator(resultId);
    assertNonEmpty(reason, "reason");
    if (accumulator.status === "cancelled") {
      throw new RuntimeInvariantError("cancelled_tool_result_not_late", {
        resultId,
      });
    }
    accumulator.status = "late";
    accumulator.metadata = { ...accumulator.metadata, lateReason: reason };
    accumulator.revision += 1;
    return deepClone(accumulator);
  }

  deliver(resultId: string, budget: ToolResultBudget): ToolResultDelivery {
    validateBudget(budget);
    const accumulator = this.requireAccumulator(resultId);
    if (accumulator.status === "open") {
      throw new RuntimeInvariantError("open_tool_result_not_deliverable", {
        resultId,
      });
    }
    const sourceBlocks = accumulator.chunks.flatMap((chunk) => chunk.blocks);
    const originalCharacters = sourceBlocks.reduce(
      (sum, block) => sum + blockCharacters(block),
      0,
    );
    const originalTokens = sourceBlocks.reduce(
      (sum, block) => sum + block.estimatedTokens,
      0,
    );
    const eligibleChunks = accumulator.chunks.slice(0, budget.maximumChunks);
    const normalized = eligibleChunks.flatMap((chunk) => chunk.blocks).map((block) =>
      this.enforceSensitivityAndRedaction(block, budget.allowedSensitivity),
    );
    const selected: ToolResultBlock[] = [];
    const omittedBlockIds: string[] = accumulator.chunks
      .slice(budget.maximumChunks)
      .flatMap((chunk) => chunk.blocks.map((block) => block.blockId));
    let characters = 0;
    let tokens = 0;
    let inlineBytes = 0;
    let redacted = false;
    for (const block of normalized) {
      if (selected.length >= budget.maximumBlocks) {
        omittedBlockIds.push(block.blockId);
        continue;
      }
      if (!budget.includeDiagnostics && block.kind === "diagnostic") {
        omittedBlockIds.push(block.blockId);
        continue;
      }
      const projectedCharacters = characters + blockCharacters(block);
      const projectedTokens = tokens + block.estimatedTokens;
      const projectedBytes =
        inlineBytes + (block.artifactId === null ? block.byteLength : 0);
      const overBudget =
        projectedCharacters > budget.maximumCharacters ||
        projectedTokens > budget.maximumTokens ||
        projectedBytes > budget.maximumInlineBytes;
      if (overBudget) {
        const truncated = this.truncateBlock(block, budget, {
          remainingCharacters: Math.max(0, budget.maximumCharacters - characters),
          remainingTokens: Math.max(0, budget.maximumTokens - tokens),
          remainingInlineBytes: Math.max(
            0,
            budget.maximumInlineBytes - inlineBytes,
          ),
        });
        if (truncated === null) {
          omittedBlockIds.push(block.blockId);
          continue;
        }
        selected.push(truncated);
        characters += blockCharacters(truncated);
        tokens += truncated.estimatedTokens;
        inlineBytes += truncated.artifactId === null ? truncated.byteLength : 0;
      } else {
        selected.push(block);
        characters = projectedCharacters;
        tokens = projectedTokens;
        inlineBytes = projectedBytes;
      }
      redacted ||= block.digest !== sourceBlocks.find(
        (source) => source.blockId === block.blockId,
      )?.digest;
    }
    const sourceDigest = digestJson(sourceBlocks);
    const body = {
      deliveryId: this.ids.next("tool-result-delivery"),
      resultId,
      toolCallId: accumulator.toolCallId,
      success: accumulator.success ?? false,
      content: selected,
      omittedBlockIds,
      originalCharacters,
      deliveredCharacters: characters,
      originalTokens,
      deliveredTokens: tokens,
      truncated:
        omittedBlockIds.length > 0 ||
        selected.some((block) => block.metadata.truncated === true),
      redacted,
      late: accumulator.status === "late",
      createdAt: this.clock.now(),
      sourceDigest,
    };
    const delivery: ToolResultDelivery = {
      ...body,
      deliveryDigest: digestJson(body),
    };
    this.deliveries.set(delivery.deliveryId, deepClone(delivery));
    this.trimDeliveries();
    return deepClone(delivery);
  }

  registerRedactionRule(
    rule: ToolResultRedactionRule,
  ): ToolResultRedactionRule {
    validateRedactionRule(rule);
    new RegExp(rule.pattern, rule.flags);
    this.redactionRules.set(rule.ruleId, deepClone(rule));
    return deepClone(rule);
  }

  removeRedactionRule(ruleId: string): void {
    this.redactionRules.delete(ruleId);
  }

  getResult(resultId: string): ToolResultAccumulator {
    return deepClone(this.requireAccumulator(resultId));
  }

  findByToolCallId(toolCallId: string): ToolResultAccumulator | null {
    const resultId = this.byToolCallId.get(toolCallId);
    return resultId === undefined ? null : this.getResult(resultId);
  }

  getDelivery(deliveryId: string): ToolResultDelivery {
    const delivery = this.deliveries.get(deliveryId);
    if (delivery === undefined) {
      throw new RuntimeInvariantError("unknown_tool_result_delivery", {
        deliveryId,
      });
    }
    const { deliveryDigest, ...body } = delivery;
    if (digestJson(body) !== deliveryDigest) {
      throw new RuntimeInvariantError("tool_result_delivery_digest_mismatch", {
        deliveryId,
      });
    }
    return deepClone(delivery);
  }

  snapshot(): ToolResultRuntimeSnapshot {
    const body = {
      version: "zyra.tool-result/v1" as const,
      accumulators: [...this.accumulators.values()]
        .sort((left, right) => compareStrings(left.resultId, right.resultId))
        .map((result) => deepClone(result)),
      deliveries: [...this.deliveries.values()]
        .sort((left, right) =>
          compareNumbers(left.createdAt, right.createdAt) ||
          compareStrings(left.deliveryId, right.deliveryId),
        )
        .map((delivery) => deepClone(delivery)),
      redactionRules: [...this.redactionRules.values()]
        .sort((left, right) => compareStrings(left.ruleId, right.ruleId))
        .map((rule) => deepClone(rule)),
      budgetReplacements: [...this.budgetReplacements.values()]
        .sort((left, right) => compareStrings(left.toolCallId, right.toolCallId))
        .map((replacement) => deepClone(replacement)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ToolResultRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.tool-result/v1") {
      throw new RuntimeInvariantError("unsupported_tool_result_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("tool_result_snapshot_checksum_mismatch");
    }
    this.accumulators.clear();
    this.byToolCallId.clear();
    this.deliveries.clear();
    this.redactionRules.clear();
    this.budgetReplacements.clear();
    for (const accumulator of snapshot.accumulators) {
      validateAccumulator(accumulator);
      if (this.byToolCallId.has(accumulator.toolCallId)) {
        throw new RuntimeInvariantError("duplicate_tool_call_result_snapshot", {
          toolCallId: accumulator.toolCallId,
        });
      }
      this.accumulators.set(accumulator.resultId, deepClone(accumulator));
      this.byToolCallId.set(accumulator.toolCallId, accumulator.resultId);
    }
    for (const delivery of snapshot.deliveries) {
      const { deliveryDigest, ...deliveryBody } = delivery;
      if (digestJson(deliveryBody) !== deliveryDigest) {
        throw new RuntimeInvariantError("tool_result_delivery_digest_mismatch", {
          deliveryId: delivery.deliveryId,
        });
      }
      this.deliveries.set(delivery.deliveryId, deepClone(delivery));
    }
    for (const rule of snapshot.redactionRules) {
      this.registerRedactionRule(rule);
    }
    for (const replacement of snapshot.budgetReplacements ?? []) {
      if (digestJson(replacement.result as unknown as JsonValue) === replacement.sourceDigest) {
        throw new RuntimeInvariantError("tool_result_budget_replacement_is_not_transformed", {
          toolCallId: replacement.toolCallId,
        });
      }
      this.budgetReplacements.set(replacement.toolCallId, deepClone(replacement));
    }
    this.trimDeliveries();
  }

  private normalizeBlock(
    input: Omit<
      ToolResultBlock,
      "blockId" | "sequence" | "byteLength" | "estimatedTokens" | "digest"
    > & { blockId?: string },
    sequence: number,
  ): ToolResultBlock {
    validateBlockInput(input);
    const serialized =
      input.text ?? (input.json === null ? "" : JSON.stringify(input.json));
    const byteLength = Buffer.byteLength(serialized, "utf8");
    const body = {
      blockId: input.blockId ?? this.ids.next("tool-result-block"),
      kind: input.kind,
      sequence,
      text: input.text,
      json: deepClone(input.json),
      mediaType: input.mediaType,
      artifactId: input.artifactId,
      byteLength,
      estimatedTokens: this.estimateTokens(serialized),
      sensitivity: input.sensitivity,
      metadata: deepClone(input.metadata),
    };
    return { ...body, digest: digestJson(body) };
  }

  private enforceSensitivityAndRedaction(
    block: ToolResultBlock,
    maximumSensitivity: ToolResultSensitivity,
  ): ToolResultBlock {
    if (
      SENSITIVITY_RANK[block.sensitivity] >
      SENSITIVITY_RANK[maximumSensitivity]
    ) {
      return this.rebuildTextBlock(block, "[content withheld by sensitivity policy]", {
        ...block.metadata,
        redacted: true,
        originalDigest: block.digest,
        redactionReason: "sensitivity_policy",
      });
    }
    if (block.text === null) {
      return deepClone(block);
    }
    let text = block.text;
    const matchedRules: string[] = [];
    for (const rule of this.redactionRules.values()) {
      if (
        !rule.enabled ||
        SENSITIVITY_RANK[block.sensitivity] <
          SENSITIVITY_RANK[rule.minimumSensitivity]
      ) {
        continue;
      }
      const regex = new RegExp(rule.pattern, rule.flags);
      const next = text.replace(regex, rule.replacement);
      if (next !== text) {
        matchedRules.push(rule.ruleId);
        text = next;
      }
    }
    return matchedRules.length === 0
      ? deepClone(block)
      : this.rebuildTextBlock(block, text, {
          ...block.metadata,
          redacted: true,
          originalDigest: block.digest,
          redactionRuleIds: matchedRules,
        });
  }

  private truncateBlock(
    block: ToolResultBlock,
    budget: ToolResultBudget,
    remaining: {
      remainingCharacters: number;
      remainingTokens: number;
      remainingInlineBytes: number;
    },
  ): ToolResultBlock | null {
    if (block.text === null || remaining.remainingCharacters === 0) {
      return null;
    }
    const tokenLimitedCharacters = remaining.remainingTokens * 4;
    const byteLimitedCharacters = remaining.remainingInlineBytes;
    const maximumCharacters = Math.min(
      remaining.remainingCharacters,
      tokenLimitedCharacters,
      byteLimitedCharacters,
    );
    if (maximumCharacters <= 0) {
      return null;
    }
    const head = Math.min(
      budget.preserveHeadCharacters,
      Math.ceil(maximumCharacters / 2),
    );
    const tail = Math.min(
      budget.preserveTailCharacters,
      Math.max(0, maximumCharacters - head - 30),
    );
    const marker = `\n...[${block.text.length - head - tail} chars omitted]...\n`;
    const text =
      tail === 0
        ? boundedString(block.text, maximumCharacters, "...")
        : `${block.text.slice(0, head)}${marker}${block.text.slice(-tail)}`.slice(
            0,
            maximumCharacters,
          );
    return this.rebuildTextBlock(block, text, {
      ...block.metadata,
      truncated: true,
      originalDigest: block.digest,
      originalCharacters: block.text.length,
    });
  }

  private rebuildTextBlock(
    source: ToolResultBlock,
    text: string,
    metadata: JsonRecord,
  ): ToolResultBlock {
    const body = {
      ...source,
      text,
      json: null,
      byteLength: Buffer.byteLength(text, "utf8"),
      estimatedTokens: this.estimateTokens(text),
      metadata,
    };
    const { digest: _digest, ...withoutDigest } = body;
    return { ...withoutDigest, digest: digestJson(withoutDigest) };
  }

  private requireAccumulator(resultId: string): ToolResultAccumulator {
    const accumulator = this.accumulators.get(resultId);
    if (accumulator === undefined) {
      throw new RuntimeInvariantError("unknown_tool_result", { resultId });
    }
    return accumulator;
  }

  private assertOpen(accumulator: ToolResultAccumulator): void {
    if (accumulator.status !== "open") {
      throw new RuntimeInvariantError("tool_result_not_open", {
        resultId: accumulator.resultId,
        status: accumulator.status,
      });
    }
  }

  private trimDeliveries(): void {
    if (this.deliveries.size <= this.maximumRetainedDeliveries) {
      return;
    }
    const oldest = [...this.deliveries.values()].sort(
      (left, right) =>
        compareNumbers(left.createdAt, right.createdAt) ||
        compareStrings(left.deliveryId, right.deliveryId),
    );
    for (const delivery of oldest.slice(
      0,
      this.deliveries.size - this.maximumRetainedDeliveries,
    )) {
      this.deliveries.delete(delivery.deliveryId);
    }
  }
}

function validateBegin(input: {
  toolCallId: string;
  toolName: string;
  sessionId: string;
  runId: string;
}): void {
  assertNonEmpty(input.toolCallId, "toolCallId");
  assertNonEmpty(input.toolName, "toolName");
  assertNonEmpty(input.sessionId, "sessionId");
  assertNonEmpty(input.runId, "runId");
}

function validateBlockInput(input: {
  kind: ToolResultBlockKind;
  text: string | null;
  json: JsonValue | null;
  mediaType: string | null;
  artifactId: string | null;
  sensitivity: ToolResultSensitivity;
}): void {
  if (input.text === null && input.json === null && input.artifactId === null) {
    throw new RuntimeInvariantError("tool_result_block_empty", {
      kind: input.kind,
    });
  }
  if (input.text !== null && input.json !== null) {
    throw new RuntimeInvariantError("tool_result_block_multiple_payloads", {
      kind: input.kind,
    });
  }
  if (
    (input.kind === "image" || input.kind === "file") &&
    input.artifactId === null
  ) {
    throw new RuntimeInvariantError("tool_result_artifact_required", {
      kind: input.kind,
    });
  }
}

function validateBudget(budget: ToolResultBudget): void {
  for (const [name, value] of Object.entries(budget)) {
    if (typeof value === "number") {
      assertNonNegativeInteger(value, `budget.${name}`);
    }
  }
  if (budget.preserveHeadCharacters + budget.preserveTailCharacters > budget.maximumCharacters) {
    throw new RuntimeInvariantError("tool_result_preservation_exceeds_budget", {
      maximumCharacters: budget.maximumCharacters,
      preserveHeadCharacters: budget.preserveHeadCharacters,
      preserveTailCharacters: budget.preserveTailCharacters,
    });
  }
}

function validateRedactionRule(rule: ToolResultRedactionRule): void {
  assertNonEmpty(rule.ruleId, "ruleId");
  assertNonEmpty(rule.pattern, "pattern");
  if (rule.replacement.length > 1_000) {
    throw new RuntimeInvariantError("redaction_replacement_too_large", {
      ruleId: rule.ruleId,
    });
  }
}

function validateAccumulator(accumulator: ToolResultAccumulator): void {
  assertNonEmpty(accumulator.resultId, "resultId");
  assertNonEmpty(accumulator.toolCallId, "toolCallId");
  assertNonNegativeInteger(accumulator.expectedSequence, "expectedSequence");
  const sequences = accumulator.chunks.map((chunk) => chunk.sequence);
  for (let index = 0; index < sequences.length; index += 1) {
    if (sequences[index] !== index + 1) {
      throw new RuntimeInvariantError("tool_result_snapshot_sequence_gap", {
        resultId: accumulator.resultId,
        index,
        actual: sequences[index] ?? null,
      });
    }
  }
}

function blockCharacters(block: ToolResultBlock): number {
  return block.text?.length ?? (block.json === null ? 0 : JSON.stringify(block.json).length);
}

function defaultTokenEstimate(text: string): number {
  if (text.length === 0) {
    return 0;
  }
  const ascii = [...text].filter((character) => character.codePointAt(0)! <= 0x7f).length;
  const nonAscii = text.length - ascii;
  return Math.ceil(ascii / 4 + nonAscii / 1.7);
}

function defaultRedactionRules(): ToolResultRedactionRule[] {
  return [
    {
      ruleId: "bearer-token",
      pattern: "Bearer\\s+[A-Za-z0-9._~+\\/-]+=*",
      flags: "gi",
      replacement: "Bearer [REDACTED]",
      minimumSensitivity: "internal",
      enabled: true,
    },
    {
      ruleId: "private-key",
      pattern: "-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----[\\s\\S]*?-----END(?: [A-Z]+)? PRIVATE KEY-----",
      flags: "g",
      replacement: "[PRIVATE KEY REDACTED]",
      minimumSensitivity: "internal",
      enabled: true,
    },
  ];
}
