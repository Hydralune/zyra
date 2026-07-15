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
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";

export type ResponseBlockKind =
  | "text"
  | "reasoning"
  | "tool_use"
  | "tool_result"
  | "media";

export type ResponseEventKind =
  | "message_start"
  | "block_start"
  | "block_delta"
  | "block_stop"
  | "usage"
  | "message_stop"
  | "error";

export type NormalizedStopReason =
  | "end_turn"
  | "tool_use"
  | "maximum_tokens"
  | "stop_sequence"
  | "content_filter"
  | "cancelled"
  | "error"
  | "unknown";

export interface ResponseUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  serviceTier: string | null;
  providerFields: JsonRecord;
}

export interface ResponseEvent {
  eventId: string;
  responseId: string;
  sequence: number;
  kind: ResponseEventKind;
  blockIndex: number | null;
  payload: JsonRecord;
  receivedAt: number;
  digest: string;
}

export interface ResponseBlock {
  blockId: string;
  responseId: string;
  index: number;
  kind: ResponseBlockKind;
  status: "open" | "complete" | "failed";
  text: string | null;
  reasoning: string | null;
  toolUseId: string | null;
  toolName: string | null;
  toolInput: JsonRecord | null;
  toolResult: JsonValue | null;
  artifactId: string | null;
  mediaType: string | null;
  openedAt: number;
  closedAt: number | null;
  revision: number;
  metadata: JsonRecord;
}

export interface ResponseBuilder {
  responseId: string;
  requestId: string;
  attemptId: string;
  providerId: string;
  modelId: string;
  providerMessageId: string | null;
  status: "open" | "complete" | "failed";
  expectedSequence: number;
  eventIds: string[];
  blockIds: string[];
  usage: ResponseUsage;
  stopReason: NormalizedStopReason | null;
  stopSequence: string | null;
  openedAt: number;
  firstTokenAt: number | null;
  closedAt: number | null;
  errorCode: string | null;
  errorMessage: string | null;
  revision: number;
}

export interface NormalizedResponse {
  responseId: string;
  requestId: string;
  attemptId: string;
  providerId: string;
  modelId: string;
  providerMessageId: string | null;
  blocks: ResponseBlock[];
  text: string;
  reasoning: string;
  toolUses: Array<{
    toolUseId: string;
    toolName: string;
    input: JsonRecord;
    blockIndex: number;
  }>;
  usage: ResponseUsage;
  stopReason: NormalizedStopReason;
  stopSequence: string | null;
  openedAt: number;
  firstTokenAt: number | null;
  closedAt: number;
  eventDigest: string;
  responseDigest: string;
}

export interface ResponseRuntimeSnapshot {
  version: "zyra.provider-response/v1";
  builders: ResponseBuilder[];
  blocks: ResponseBlock[];
  events: ResponseEvent[];
  responses: NormalizedResponse[];
  checksum: string;
}

export interface ResponseRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumEvents?: number;
  maximumBlocks?: number;
  maximumCharacters?: number;
}

export class ProviderResponseRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumEvents: number;
  private readonly maximumBlocks: number;
  private readonly maximumCharacters: number;
  private readonly builders = new Map<string, ResponseBuilder>();
  private readonly blocks = new Map<string, ResponseBlock>();
  private readonly events = new Map<string, ResponseEvent>();
  private readonly responses = new Map<string, NormalizedResponse>();

  constructor(options: ResponseRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumEvents = options.maximumEvents ?? 100_000;
    this.maximumBlocks = options.maximumBlocks ?? 1_000;
    this.maximumCharacters = options.maximumCharacters ?? 16_000_000;
    assertNonNegativeInteger(this.maximumEvents, "maximumEvents");
    assertNonNegativeInteger(this.maximumBlocks, "maximumBlocks");
    assertNonNegativeInteger(this.maximumCharacters, "maximumCharacters");
  }

  begin(input: {
    responseId?: string;
    requestId: string;
    attemptId: string;
    providerId: string;
    modelId: string;
  }): ResponseBuilder {
    for (const [name, value] of Object.entries(input)) {
      if (name !== "responseId") {
        assertNonEmpty(value, name);
      }
    }
    const prior = [...this.builders.values()].find(
      (builder) => builder.attemptId === input.attemptId,
    );
    if (prior !== undefined) {
      if (
        prior.requestId !== input.requestId ||
        prior.providerId !== input.providerId ||
        prior.modelId !== input.modelId
      ) {
        throw new RuntimeInvariantError("response_attempt_conflict", {
          attemptId: input.attemptId,
        });
      }
      return deepClone(prior);
    }
    const responseId = input.responseId ?? this.ids.next("provider-response");
    if (this.builders.has(responseId)) {
      throw new RuntimeInvariantError("response_id_conflict", { responseId });
    }
    const builder: ResponseBuilder = {
      responseId,
      requestId: input.requestId,
      attemptId: input.attemptId,
      providerId: input.providerId,
      modelId: input.modelId,
      providerMessageId: null,
      status: "open",
      expectedSequence: 1,
      eventIds: [],
      blockIds: [],
      usage: emptyUsage(),
      stopReason: null,
      stopSequence: null,
      openedAt: this.clock.now(),
      firstTokenAt: null,
      closedAt: null,
      errorCode: null,
      errorMessage: null,
      revision: 1,
    };
    this.builders.set(responseId, builder);
    return deepClone(builder);
  }

  apply(input: {
    responseId: string;
    eventId?: string;
    sequence: number;
    kind: ResponseEventKind;
    blockIndex?: number | null;
    payload: JsonRecord;
  }): ResponseEvent {
    const builder = this.requireBuilder(input.responseId);
    this.assertOpen(builder);
    assertNonNegativeInteger(input.sequence, "sequence");
    const eventId = input.eventId ?? this.ids.next("response-event");
    const digest = eventDigest({
      responseId: input.responseId,
      sequence: input.sequence,
      kind: input.kind,
      blockIndex: input.blockIndex ?? null,
      payload: input.payload,
    });
    const existing = this.events.get(eventId);
    if (existing !== undefined) {
      if (existing.digest !== digest) {
        throw new RuntimeInvariantError("response_event_conflict", { eventId });
      }
      return deepClone(existing);
    }
    if (input.sequence !== builder.expectedSequence) {
      throw new RuntimeInvariantError("response_event_sequence_gap", {
        expected: builder.expectedSequence,
        actual: input.sequence,
      });
    }
    if (builder.eventIds.length >= this.maximumEvents) {
      throw new RuntimeInvariantError("response_event_limit", {
        responseId: builder.responseId,
      });
    }
    const event: ResponseEvent = {
      eventId,
      responseId: input.responseId,
      sequence: input.sequence,
      kind: input.kind,
      blockIndex: input.blockIndex ?? null,
      payload: deepClone(input.payload),
      receivedAt: this.clock.now(),
      digest,
    };
    this.reduce(builder, event);
    this.events.set(eventId, event);
    builder.eventIds.push(eventId);
    builder.expectedSequence += 1;
    builder.revision += 1;
    return deepClone(event);
  }

  finalize(responseId: string): NormalizedResponse {
    const prior = this.responses.get(responseId);
    if (prior !== undefined) {
      return deepClone(prior);
    }
    const builder = this.requireBuilder(responseId);
    if (builder.status !== "complete") {
      throw new RuntimeInvariantError("response_not_complete", {
        responseId,
        status: builder.status,
      });
    }
    const blocks = builder.blockIds
      .map((blockId) => this.requireBlock(blockId))
      .sort((left, right) => compareNumbers(left.index, right.index));
    const openBlockIds = blocks
      .filter((block) => block.status === "open")
      .map((block) => block.blockId);
    if (openBlockIds.length > 0) {
      throw new RuntimeInvariantError("response_blocks_open", { openBlockIds });
    }
    const toolUseIds = new Set<string>();
    const toolUses = blocks
      .filter((block) => block.kind === "tool_use")
      .map((block) => {
        if (
          block.toolUseId === null ||
          block.toolName === null ||
          block.toolInput === null
        ) {
          throw new RuntimeInvariantError("response_tool_use_incomplete", {
            blockId: block.blockId,
          });
        }
        if (toolUseIds.has(block.toolUseId)) {
          throw new RuntimeInvariantError("response_tool_use_duplicate", {
            toolUseId: block.toolUseId,
          });
        }
        toolUseIds.add(block.toolUseId);
        return {
          toolUseId: block.toolUseId,
          toolName: block.toolName,
          input: deepClone(block.toolInput),
          blockIndex: block.index,
        };
      });
    const closedAt = builder.closedAt ?? this.clock.now();
    const body = {
      responseId,
      requestId: builder.requestId,
      attemptId: builder.attemptId,
      providerId: builder.providerId,
      modelId: builder.modelId,
      providerMessageId: builder.providerMessageId,
      blocks: blocks.map((block) => deepClone(block)),
      text: blocks
        .filter((block) => block.kind === "text")
        .map((block) => block.text ?? "")
        .join(""),
      reasoning: blocks
        .filter((block) => block.kind === "reasoning")
        .map((block) => block.reasoning ?? "")
        .join(""),
      toolUses,
      usage: deepClone(builder.usage),
      stopReason: builder.stopReason ?? "unknown",
      stopSequence: builder.stopSequence,
      openedAt: builder.openedAt,
      firstTokenAt: builder.firstTokenAt,
      closedAt,
      eventDigest: digestJson(
        builder.eventIds.map((eventId) => this.requireEvent(eventId).digest),
      ),
    };
    const response: NormalizedResponse = {
      ...body,
      responseDigest: digestJson(body),
    };
    this.responses.set(responseId, response);
    return deepClone(response);
  }

  fail(responseId: string, code: string, message: string): ResponseBuilder {
    const builder = this.requireBuilder(responseId);
    this.assertOpen(builder);
    assertNonEmpty(code, "code");
    assertNonEmpty(message, "message");
    builder.status = "failed";
    builder.stopReason = "error";
    builder.errorCode = code;
    builder.errorMessage = message;
    builder.closedAt = this.clock.now();
    builder.revision += 1;
    for (const blockId of builder.blockIds) {
      const block = this.requireBlock(blockId);
      if (block.status === "open") {
        block.status = "failed";
        block.closedAt = this.clock.now();
        block.revision += 1;
      }
    }
    return deepClone(builder);
  }

  replay(responseId: string, afterSequence = 0): ResponseEvent[] {
    const builder = this.requireBuilder(responseId);
    assertNonNegativeInteger(afterSequence, "afterSequence");
    return builder.eventIds
      .map((eventId) => this.requireEvent(eventId))
      .filter((event) => event.sequence > afterSequence)
      .sort((left, right) => compareNumbers(left.sequence, right.sequence))
      .map((event) => deepClone(event));
  }

  getResponse(responseId: string): NormalizedResponse {
    const response = this.responses.get(responseId);
    if (response === undefined) {
      throw new RuntimeInvariantError("unknown_normalized_response", { responseId });
    }
    const { responseDigest, ...body } = response;
    if (digestJson(body) !== responseDigest) {
      throw new RuntimeInvariantError("response_digest_mismatch", { responseId });
    }
    return deepClone(response);
  }

  snapshot(): ResponseRuntimeSnapshot {
    const body = {
      version: "zyra.provider-response/v1" as const,
      builders: sorted(this.builders, (value) => value.responseId),
      blocks: sorted(this.blocks, (value) => `${value.responseId}:${value.index}`),
      events: sorted(
        this.events,
        (value) => `${value.responseId}:${String(value.sequence).padStart(12, "0")}`,
      ),
      responses: sorted(this.responses, (value) => value.responseId),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ResponseRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.provider-response/v1") {
      throw new RuntimeInvariantError("unsupported_response_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("response_snapshot_checksum_mismatch");
    }
    this.builders.clear();
    this.blocks.clear();
    this.events.clear();
    this.responses.clear();
    for (const block of snapshot.blocks) {
      this.blocks.set(block.blockId, deepClone(block));
    }
    for (const event of snapshot.events) {
      if (
        event.digest !==
        eventDigest({
          responseId: event.responseId,
          sequence: event.sequence,
          kind: event.kind,
          blockIndex: event.blockIndex,
          payload: event.payload,
        })
      ) {
        throw new RuntimeInvariantError("response_event_digest_mismatch", {
          eventId: event.eventId,
        });
      }
      this.events.set(event.eventId, deepClone(event));
    }
    for (const builder of snapshot.builders) {
      if (
        builder.blockIds.some((blockId) => !this.blocks.has(blockId)) ||
        builder.eventIds.some((eventId) => !this.events.has(eventId))
      ) {
        throw new RuntimeInvariantError("response_builder_reference_missing", {
          responseId: builder.responseId,
        });
      }
      this.builders.set(builder.responseId, deepClone(builder));
    }
    for (const response of snapshot.responses) {
      const { responseDigest, ...responseBody } = response;
      if (digestJson(responseBody) !== responseDigest) {
        throw new RuntimeInvariantError("response_digest_mismatch", {
          responseId: response.responseId,
        });
      }
      this.responses.set(response.responseId, deepClone(response));
    }
  }

  private reduce(builder: ResponseBuilder, event: ResponseEvent): void {
    if (event.kind === "message_start") {
      builder.providerMessageId = textField(event.payload, "messageId", false);
    } else if (event.kind === "block_start") {
      this.startBlock(builder, event);
    } else if (event.kind === "block_delta") {
      this.applyDelta(builder, event);
    } else if (event.kind === "block_stop") {
      const block = this.requireOpenBlock(builder, requireIndex(event));
      block.status = "complete";
      block.closedAt = this.clock.now();
      block.revision += 1;
    } else if (event.kind === "usage") {
      builder.usage = mergeUsage(builder.usage, event.payload);
    } else if (event.kind === "error") {
      builder.status = "failed";
      builder.stopReason = "error";
      builder.errorCode = textField(event.payload, "code", false) ?? "provider_error";
      builder.errorMessage =
        textField(event.payload, "message", false) ?? "provider response failed";
      builder.closedAt = this.clock.now();
    } else {
      builder.stopReason = normalizeStop(
        textField(event.payload, "stopReason", false) ?? "unknown",
      );
      builder.stopSequence = textField(event.payload, "stopSequence", false);
      builder.status = "complete";
      builder.closedAt = this.clock.now();
    }
  }

  private startBlock(builder: ResponseBuilder, event: ResponseEvent): void {
    const index = requireIndex(event);
    if (builder.blockIds.length >= this.maximumBlocks) {
      throw new RuntimeInvariantError("response_block_limit", {
        responseId: builder.responseId,
      });
    }
    if (this.findBlock(builder, index) !== null) {
      throw new RuntimeInvariantError("response_block_duplicate", { index });
    }
    const kind = normalizeKind(textField(event.payload, "kind", true) ?? "text");
    const block: ResponseBlock = {
      blockId: this.ids.next("response-block"),
      responseId: builder.responseId,
      index,
      kind,
      status: "open",
      text: kind === "text" ? textField(event.payload, "text", false) ?? "" : null,
      reasoning:
        kind === "reasoning"
          ? textField(event.payload, "reasoning", false) ?? ""
          : null,
      toolUseId:
        kind === "tool_use" ? textField(event.payload, "toolUseId", true) : null,
      toolName:
        kind === "tool_use" ? textField(event.payload, "toolName", true) : null,
      toolInput: kind === "tool_use" ? objectField(event.payload, "input") ?? {} : null,
      toolResult: kind === "tool_result" ? event.payload.result ?? null : null,
      artifactId: textField(event.payload, "artifactId", false),
      mediaType: textField(event.payload, "mediaType", false),
      openedAt: this.clock.now(),
      closedAt: null,
      revision: 1,
      metadata: objectField(event.payload, "metadata") ?? {},
    };
    this.blocks.set(block.blockId, block);
    builder.blockIds.push(block.blockId);
  }

  private applyDelta(builder: ResponseBuilder, event: ResponseEvent): void {
    const block = this.requireOpenBlock(builder, requireIndex(event));
    if (builder.firstTokenAt === null) {
      builder.firstTokenAt = this.clock.now();
    }
    if (block.kind === "text") {
      block.text = appendLimited(
        block.text ?? "",
        textField(event.payload, "text", false) ?? "",
        this.maximumCharacters,
      );
    } else if (block.kind === "reasoning") {
      block.reasoning = appendLimited(
        block.reasoning ?? "",
        textField(event.payload, "reasoning", false) ?? "",
        this.maximumCharacters,
      );
    } else if (block.kind === "tool_use") {
      block.toolInput = {
        ...(block.toolInput ?? {}),
        ...(objectField(event.payload, "input") ?? {}),
      };
    } else if (block.kind === "tool_result" && "result" in event.payload) {
      block.toolResult = deepClone(event.payload.result ?? null);
    }
    block.revision += 1;
  }

  private findBlock(builder: ResponseBuilder, index: number): ResponseBlock | null {
    for (const blockId of builder.blockIds) {
      const block = this.requireBlock(blockId);
      if (block.index === index) {
        return block;
      }
    }
    return null;
  }

  private requireOpenBlock(builder: ResponseBuilder, index: number): ResponseBlock {
    const block = this.findBlock(builder, index);
    if (block === null || block.status !== "open") {
      throw new RuntimeInvariantError("response_block_not_open", {
        responseId: builder.responseId,
        index,
      });
    }
    return block;
  }

  private assertOpen(builder: ResponseBuilder): void {
    if (builder.status !== "open") {
      throw new RuntimeInvariantError("response_not_open", {
        responseId: builder.responseId,
        status: builder.status,
      });
    }
  }

  private requireBuilder(responseId: string): ResponseBuilder {
    const builder = this.builders.get(responseId);
    if (builder === undefined) {
      throw new RuntimeInvariantError("unknown_response_builder", { responseId });
    }
    return builder;
  }

  private requireBlock(blockId: string): ResponseBlock {
    const block = this.blocks.get(blockId);
    if (block === undefined) {
      throw new RuntimeInvariantError("unknown_response_block", { blockId });
    }
    return block;
  }

  private requireEvent(eventId: string): ResponseEvent {
    const event = this.events.get(eventId);
    if (event === undefined) {
      throw new RuntimeInvariantError("unknown_response_event", { eventId });
    }
    return event;
  }
}

function emptyUsage(): ResponseUsage {
  return {
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    serviceTier: null,
    providerFields: {},
  };
}

function mergeUsage(current: ResponseUsage, payload: JsonRecord): ResponseUsage {
  const next: ResponseUsage = {
    inputTokens: numericField(payload, "inputTokens") ?? current.inputTokens,
    outputTokens: numericField(payload, "outputTokens") ?? current.outputTokens,
    cacheReadTokens:
      numericField(payload, "cacheReadTokens") ?? current.cacheReadTokens,
    cacheWriteTokens:
      numericField(payload, "cacheWriteTokens") ?? current.cacheWriteTokens,
    serviceTier: textField(payload, "serviceTier", false) ?? current.serviceTier,
    providerFields: {
      ...current.providerFields,
      ...(objectField(payload, "providerFields") ?? {}),
    },
  };
  for (const [name, value] of Object.entries(next)) {
    if (typeof value === "number") {
      assertNonNegativeInteger(value, `usage.${name}`);
    }
  }
  return next;
}

function normalizeKind(value: string): ResponseBlockKind {
  const normalized = value.toLowerCase().replace(/-/g, "_");
  const kinds: ResponseBlockKind[] = [
    "text",
    "reasoning",
    "tool_use",
    "tool_result",
    "media",
  ];
  if (!kinds.includes(normalized as ResponseBlockKind)) {
    throw new RuntimeInvariantError("unsupported_response_block_kind", { value });
  }
  return normalized as ResponseBlockKind;
}

function normalizeStop(value: string): NormalizedStopReason {
  const aliases: Record<string, NormalizedStopReason> = {
    end_turn: "end_turn",
    stop: "end_turn",
    tool_use: "tool_use",
    tool_calls: "tool_use",
    max_tokens: "maximum_tokens",
    maximum_tokens: "maximum_tokens",
    length: "maximum_tokens",
    stop_sequence: "stop_sequence",
    content_filter: "content_filter",
    safety: "content_filter",
    cancelled: "cancelled",
    error: "error",
    unknown: "unknown",
  };
  return aliases[value.toLowerCase().replace(/-/g, "_")] ?? "unknown";
}

function textField(
  record: JsonRecord,
  name: string,
  required: boolean,
): string | null {
  const value = record[name];
  if (typeof value === "string") {
    return value;
  }
  if (required) {
    throw new RuntimeInvariantError("response_string_field_required", { name });
  }
  return null;
}

function numericField(record: JsonRecord, name: string): number | null {
  const value = record[name];
  return typeof value === "number" ? value : null;
}

function objectField(record: JsonRecord, name: string): JsonRecord | null {
  const value = record[name];
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? deepClone(value)
    : null;
}

function requireIndex(event: ResponseEvent): number {
  if (event.blockIndex === null) {
    throw new RuntimeInvariantError("response_block_index_required", {
      eventId: event.eventId,
    });
  }
  return event.blockIndex;
}

function appendLimited(current: string, delta: string, maximum: number): string {
  if (current.length + delta.length > maximum) {
    throw new RuntimeInvariantError("response_character_limit", {
      currentCharacters: current.length,
      deltaCharacters: delta.length,
      maximum,
    });
  }
  return current + delta;
}

function eventDigest(input: {
  responseId: string;
  sequence: number;
  kind: ResponseEventKind;
  blockIndex: number | null;
  payload: JsonRecord;
}): string {
  return digestJson(input);
}

function sorted<T>(
  values: ReadonlyMap<string, T>,
  key: (value: T) => string,
): T[] {
  return [...values.values()]
    .sort((left, right) => compareStrings(key(left), key(right)))
    .map((value) => deepClone(value));
}
