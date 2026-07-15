import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const QUERY_STREAM_SNAPSHOT_VERSION = "zyra.query-stream/v1";

export type QueryStreamKind = "text" | "reasoning" | "tool_input" | "tool_result" | "status";
export type QueryStreamState = "opened" | "streaming" | "ended" | "failed" | "cancelled";

export interface QueryStreamChannel {
  streamId: string;
  turnId: string;
  kind: QueryStreamKind;
  state: QueryStreamState;
  sequence: number;
  aggregateSequence: number;
  content: string;
  contentDigest: string;
  toolCallId: string | null;
  maximumChars: number;
  withheldChars: number;
  startedAt: string;
  endedAt: string | null;
  error: string | null;
  revision: number;
}

export interface QueryStreamDelta {
  deltaId: string;
  streamId: string;
  turnId: string;
  sequence: number;
  aggregateSequence: number;
  kind: QueryStreamKind;
  delta: string;
  deltaDigest: string;
  effective: boolean;
  withheld: boolean;
  createdAt: string;
}

export interface QueryStreamSnapshot {
  version: typeof QUERY_STREAM_SNAPSHOT_VERSION;
  revision: number;
  aggregateSequence: number;
  restartEpoch: number;
  channels: QueryStreamChannel[];
  deltas: QueryStreamDelta[];
  checksum: string;
}

export class QueryStreamRuntime {
  private readonly channels = new Map<string, QueryStreamChannel>();
  private readonly deltas = new Map<string, QueryStreamDelta[]>();
  private readonly maximumRetainedDeltas: number;
  private revision = 0;
  private aggregateSequence = 0;
  private restartEpoch = 0;

  constructor(maximumRetainedDeltas = 10_000) {
    this.maximumRetainedDeltas = boundedInteger(maximumRetainedDeltas, 100, 1_000_000);
  }

  stream_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "open") return channelToJson(this.open({
      streamId: asString(value.stream_id) || undefined,
      turnId: asString(value.turn_id),
      kind: streamKind(asString(value.kind, "text")),
      toolCallId: asString(value.tool_call_id) || null,
      maximumChars: positive(value.maximum_chars, 1_000_000),
    }));
    if (action === "append") return deltaToJson(this.append(asString(value.stream_id), {
      sequence: integer(value.sequence, -1),
      delta: asString(value.delta),
      effective: asBoolean(value.effective, false),
    }));
    if (action === "end") return channelToJson(this.end(asString(value.stream_id), asString(value.error) || null));
    if (action === "cancel") return channelToJson(this.cancel(asString(value.stream_id), asString(value.reason, "cancelled")));
    return channelToJson(this.requireChannel(asString(value.stream_id)));
  }

  open(input: {
    streamId?: string;
    turnId: string;
    kind: QueryStreamKind;
    toolCallId: string | null;
    maximumChars: number;
  }): QueryStreamChannel {
    const streamId = input.streamId?.trim() || randomUUID();
    const existing = this.channels.get(streamId);
    if (existing) {
      if (existing.turnId !== input.turnId || existing.kind !== input.kind) throw new Error(`query stream identity conflict: ${streamId}`);
      return structuredClone(existing);
    }
    const channel: QueryStreamChannel = {
      streamId,
      turnId: required(input.turnId, "turn id"),
      kind: input.kind,
      state: "opened",
      sequence: 0,
      aggregateSequence: this.aggregateSequence,
      content: "",
      contentDigest: digest(""),
      toolCallId: input.toolCallId,
      maximumChars: positive(input.maximumChars, 1_000_000),
      withheldChars: 0,
      startedAt: new Date().toISOString(),
      endedAt: null,
      error: null,
      revision: 1,
    };
    this.channels.set(streamId, channel);
    this.deltas.set(streamId, []);
    this.revision += 1;
    return structuredClone(channel);
  }

  append(streamId: string, input: { sequence: number; delta: string; effective: boolean }): QueryStreamDelta {
    const channel = this.requireChannel(streamId);
    if (channel.state !== "opened" && channel.state !== "streaming") throw new Error(`query stream cannot append from ${channel.state}`);
    const expected = channel.sequence + 1;
    const sequence = input.sequence < 0 ? expected : input.sequence;
    if (sequence !== expected) throw new Error(`query stream sequence gap: expected ${expected}, received ${sequence}`);
    this.aggregateSequence += 1;
    const remaining = Math.max(0, channel.maximumChars - channel.content.length);
    const retained = input.delta.slice(0, remaining);
    const withheld = retained.length < input.delta.length;
    channel.content += retained;
    channel.withheldChars += input.delta.length - retained.length;
    channel.contentDigest = digest(channel.content);
    channel.sequence = sequence;
    channel.aggregateSequence = this.aggregateSequence;
    channel.state = "streaming";
    channel.revision += 1;
    const delta: QueryStreamDelta = {
      deltaId: randomUUID(),
      streamId,
      turnId: channel.turnId,
      sequence,
      aggregateSequence: this.aggregateSequence,
      kind: channel.kind,
      delta: retained,
      deltaDigest: digest(input.delta),
      effective: input.effective,
      withheld,
      createdAt: new Date().toISOString(),
    };
    const values = this.deltas.get(streamId)!;
    values.push(delta);
    trimDeltas(this.deltas, this.maximumRetainedDeltas);
    this.revision += 1;
    return structuredClone(delta);
  }

  end(streamId: string, error: string | null = null): QueryStreamChannel {
    const channel = this.requireChannel(streamId);
    if (channel.state === "ended" || channel.state === "failed") return structuredClone(channel);
    if (channel.state === "cancelled") throw new Error("cancelled query stream cannot end");
    channel.state = error ? "failed" : "ended";
    channel.error = error?.trim().slice(0, 8_192) || null;
    channel.endedAt = new Date().toISOString();
    channel.revision += 1;
    this.aggregateSequence += 1;
    channel.aggregateSequence = this.aggregateSequence;
    this.revision += 1;
    return structuredClone(channel);
  }

  cancel(streamId: string, reason: string): QueryStreamChannel {
    const channel = this.requireChannel(streamId);
    if (channel.state === "ended" || channel.state === "failed" || channel.state === "cancelled") return structuredClone(channel);
    channel.state = "cancelled";
    channel.error = reason.trim().slice(0, 8_192);
    channel.endedAt = new Date().toISOString();
    channel.revision += 1;
    this.aggregateSequence += 1;
    channel.aggregateSequence = this.aggregateSequence;
    this.revision += 1;
    return structuredClone(channel);
  }

  cleanupForModel(): JsonObject[] {
    const completed = [...this.channels.values()]
      .filter((channel) => channel.state === "ended" || channel.state === "failed")
      .sort((left, right) => left.aggregateSequence - right.aggregateSequence);
    return completed.map((channel) => ({
      type: channel.kind,
      stream_id: channel.streamId,
      turn_id: channel.turnId,
      tool_call_id: channel.toolCallId,
      content: channel.content,
      content_digest: channel.contentDigest,
      withheld_chars: channel.withheldChars,
      error: channel.error,
    }));
  }

  replay(afterAggregateSequence: number, maximum = 1_000): QueryStreamDelta[] {
    return [...this.deltas.values()]
      .flat()
      .filter((delta) => delta.aggregateSequence > afterAggregateSequence)
      .sort((left, right) => left.aggregateSequence - right.aggregateSequence)
      .slice(0, Math.max(0, Math.floor(maximum)))
      .map((delta) => structuredClone(delta));
  }

  activeForTurn(turnId: string): QueryStreamChannel[] {
    return [...this.channels.values()]
      .filter((channel) => channel.turnId === turnId && (channel.state === "opened" || channel.state === "streaming"))
      .map((channel) => structuredClone(channel));
  }

  closeTurn(turnId: string, error: string | null = null): QueryStreamChannel[] {
    const result: QueryStreamChannel[] = [];
    for (const channel of this.activeForTurn(turnId)) result.push(this.end(channel.streamId, error));
    return result;
  }

  snapshot(): QueryStreamSnapshot {
    const unsigned: Omit<QueryStreamSnapshot, "checksum"> = {
      version: QUERY_STREAM_SNAPSHOT_VERSION,
      revision: this.revision,
      aggregateSequence: this.aggregateSequence,
      restartEpoch: this.restartEpoch,
      channels: structuredClone([...this.channels.values()]),
      deltas: structuredClone([...this.deltas.values()].flat()),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: QueryStreamSnapshot): void {
    if (snapshot.version !== QUERY_STREAM_SNAPSHOT_VERSION) throw new Error("unsupported query stream snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("query stream snapshot checksum mismatch");
    validateSnapshot(snapshot);
    this.channels.clear();
    for (const channel of snapshot.channels) {
      const restored = structuredClone(channel);
      if (restored.state === "opened" || restored.state === "streaming") {
        restored.state = "failed";
        restored.error = "stream_interrupted_by_restart";
        restored.endedAt = new Date().toISOString();
        restored.revision += 1;
      }
      this.channels.set(restored.streamId, restored);
    }
    this.deltas.clear();
    for (const delta of snapshot.deltas) {
      const values = this.deltas.get(delta.streamId) ?? [];
      values.push(structuredClone(delta));
      this.deltas.set(delta.streamId, values);
    }
    this.revision = snapshot.revision;
    this.aggregateSequence = snapshot.aggregateSequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
  }

  private requireChannel(streamId: string): QueryStreamChannel {
    const channel = this.channels.get(streamId);
    if (!channel) throw new Error(`query stream not found: ${streamId}`);
    return channel;
  }
}

function validateSnapshot(snapshot: QueryStreamSnapshot): void {
  const channels = new Map(snapshot.channels.map((channel) => [channel.streamId, channel]));
  const sequences = new Map<string, number>();
  let aggregate = 0;
  for (const delta of [...snapshot.deltas].sort((left, right) => left.aggregateSequence - right.aggregateSequence)) {
    if (!channels.has(delta.streamId)) throw new Error(`query stream delta references missing channel: ${delta.streamId}`);
    const expected = (sequences.get(delta.streamId) ?? 0) + 1;
    if (delta.sequence !== expected) throw new Error(`query stream snapshot sequence gap for ${delta.streamId}`);
    if (delta.aggregateSequence <= aggregate) throw new Error("query stream aggregate sequence is not monotonic");
    sequences.set(delta.streamId, delta.sequence);
    aggregate = delta.aggregateSequence;
  }
  for (const channel of snapshot.channels) {
    if (channel.sequence !== (sequences.get(channel.streamId) ?? 0)) throw new Error(`query stream channel sequence mismatch: ${channel.streamId}`);
    const content = snapshot.deltas.filter((delta) => delta.streamId === channel.streamId).map((delta) => delta.delta).join("");
    if (!channel.content.startsWith(content) && !content.startsWith(channel.content)) throw new Error(`query stream content projection mismatch: ${channel.streamId}`);
  }
}

function trimDeltas(values: Map<string, QueryStreamDelta[]>, maximum: number): void {
  const all = [...values.values()].flat().sort((left, right) => left.aggregateSequence - right.aggregateSequence);
  const remove = all.length - maximum;
  if (remove <= 0) return;
  const ids = new Set(all.slice(0, remove).map((item) => item.deltaId));
  for (const [streamId, deltas] of values) values.set(streamId, deltas.filter((delta) => !ids.has(delta.deltaId)));
}

function channelToJson(value: QueryStreamChannel): JsonObject {
  return {
    stream_id: value.streamId,
    turn_id: value.turnId,
    kind: value.kind,
    state: value.state,
    sequence: value.sequence,
    aggregate_sequence: value.aggregateSequence,
    content: value.content,
    content_digest: value.contentDigest,
    tool_call_id: value.toolCallId,
    maximum_chars: value.maximumChars,
    withheld_chars: value.withheldChars,
    started_at: value.startedAt,
    ended_at: value.endedAt,
    error: value.error,
    revision: value.revision,
  };
}

function deltaToJson(value: QueryStreamDelta): JsonObject {
  return {
    delta_id: value.deltaId,
    stream_id: value.streamId,
    turn_id: value.turnId,
    sequence: value.sequence,
    aggregate_sequence: value.aggregateSequence,
    kind: value.kind,
    delta: value.delta,
    delta_digest: value.deltaDigest,
    effective: value.effective,
    withheld: value.withheld,
    created_at: value.createdAt,
  };
}

function streamKind(value: string): QueryStreamKind {
  if (value === "reasoning" || value === "tool_input" || value === "tool_result" || value === "status") return value;
  return "text";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.floor(value) : fallback;
}

function boundedInteger(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
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
