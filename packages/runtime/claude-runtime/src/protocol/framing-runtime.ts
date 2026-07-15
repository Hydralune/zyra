import { createHash, randomUUID } from "node:crypto";

import { asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const FRAMING_SNAPSHOT_VERSION = "zyra.runtime-framing/v2";
export const FRAMING_PROTOCOL_VERSION = "zyra.runtime-jsonl/v2";

export type FrameDirection = "inbound" | "outbound";
export type FrameKind = "hello" | "start" | "event" | "result" | "ack" | "nack" | "cancel" | "heartbeat";

export interface RuntimeFrame {
  protocol: typeof FRAMING_PROTOCOL_VERSION;
  frameId: string;
  kind: FrameKind;
  sequence: number;
  ackSequence: number;
  runId: string;
  sessionId: string;
  correlationId: string;
  causationId: string | null;
  payload: JsonValue;
  payloadDigest: string;
  createdAt: string;
  checksum: string;
}

export interface FrameReceipt {
  receiptId: string;
  frameId: string;
  direction: FrameDirection;
  sequence: number;
  accepted: boolean;
  duplicate: boolean;
  reason: string;
  receivedAt: string;
}

export interface FramingSnapshot {
  version: typeof FRAMING_SNAPSHOT_VERSION;
  runId: string;
  sessionId: string;
  revision: number;
  inboundSequence: number;
  outboundSequence: number;
  acknowledgedOutboundSequence: number;
  handshakeComplete: boolean;
  inboundFrames: RuntimeFrame[];
  outboundFrames: RuntimeFrame[];
  receipts: FrameReceipt[];
  checksum: string;
}

export class ProtocolFramingRuntime {
  private readonly runId: string;
  private readonly sessionId: string;
  private readonly maximumFrameBytes: number;
  private readonly maximumRetainedFrames: number;
  private readonly inboundFrames = new Map<string, RuntimeFrame>();
  private readonly outboundFrames = new Map<string, RuntimeFrame>();
  private readonly receipts: FrameReceipt[] = [];
  private inboundSequence = 0;
  private outboundSequence = 0;
  private acknowledgedOutboundSequence = 0;
  private handshakeComplete = false;
  private revision = 0;
  private buffer = "";

  constructor(runId: string, sessionId: string, options: { maximumFrameBytes?: number; maximumRetainedFrames?: number } = {}) {
    this.runId = required(runId, "run id");
    this.sessionId = required(sessionId, "session id");
    this.maximumFrameBytes = bounded(options.maximumFrameBytes ?? 4 * 1024 * 1024, 1_024, 64 * 1024 * 1024);
    this.maximumRetainedFrames = bounded(options.maximumRetainedFrames ?? 10_000, 100, 1_000_000);
  }

  framing_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "encode") {
      const frame = this.createOutbound(
        frameKind(asString(value.kind, "event")),
        value.payload ?? null,
        asString(value.correlation_id, randomUUID()),
        asString(value.causation_id) || null,
      );
      return { frame: frameToJson(frame), jsonl: this.encode(frame) };
    }
    if (action === "feed") return { receipts: this.feed(asString(value.chunk)).map(receiptToJson) };
    if (action === "ack") {
      this.ackOutbound(integer(value.sequence, 0));
      return this.project();
    }
    return this.project();
  }

  createOutbound(kind: FrameKind, payload: JsonValue, correlationId: string, causationId: string | null): RuntimeFrame {
    if (!this.handshakeComplete && kind !== "hello") throw new Error("framing handshake is not complete");
    this.outboundSequence += 1;
    const unsigned: Omit<RuntimeFrame, "checksum"> = {
      protocol: FRAMING_PROTOCOL_VERSION,
      frameId: randomUUID(),
      kind,
      sequence: this.outboundSequence,
      ackSequence: this.inboundSequence,
      runId: this.runId,
      sessionId: this.sessionId,
      correlationId: required(correlationId, "frame correlation id"),
      causationId: causationId?.trim() || null,
      payload: structuredClone(payload),
      payloadDigest: digest(payload),
      createdAt: new Date().toISOString(),
    };
    const frame: RuntimeFrame = { ...unsigned, checksum: digest(unsigned) };
    this.assertFrameSize(frame);
    this.outboundFrames.set(frame.frameId, frame);
    this.trimFrames();
    this.revision += 1;
    return structuredClone(frame);
  }

  acceptInbound(value: JsonObject): FrameReceipt {
    const frame = parseFrame(value);
    if (frame.runId !== this.runId || frame.sessionId !== this.sessionId) return this.receipt(frame, false, false, "identity_mismatch");
    const existing = this.inboundFrames.get(frame.frameId);
    if (existing) {
      if (existing.checksum !== frame.checksum) throw new Error(`inbound frame id conflict: ${frame.frameId}`);
      return this.receipt(frame, true, true, "duplicate_frame");
    }
    if (frame.sequence !== this.inboundSequence + 1) return this.receipt(frame, false, false, `sequence_gap_expected_${this.inboundSequence + 1}`);
    if (frame.ackSequence > this.outboundSequence) return this.receipt(frame, false, false, "ack_sequence_ahead_of_outbound");
    if (!this.handshakeComplete && frame.kind !== "hello") return this.receipt(frame, false, false, "handshake_required");
    this.assertFrameSize(frame);
    this.inboundSequence = frame.sequence;
    this.inboundFrames.set(frame.frameId, frame);
    this.ackOutbound(frame.ackSequence);
    if (frame.kind === "hello") this.handshakeComplete = true;
    this.trimFrames();
    this.revision += 1;
    return this.receipt(frame, true, false, "accepted");
  }

  feed(chunk: string): FrameReceipt[] {
    if (Buffer.byteLength(this.buffer) + Buffer.byteLength(chunk) > this.maximumFrameBytes * 2) {
      this.buffer = "";
      throw new Error("framing input buffer exceeded limit");
    }
    this.buffer += chunk;
    const receipts: FrameReceipt[] = [];
    while (true) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) break;
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) continue;
      if (Buffer.byteLength(line) > this.maximumFrameBytes) throw new Error("runtime frame exceeds maximum bytes");
      let value: JsonObject;
      try {
        value = asObject(JSON.parse(line));
      } catch {
        throw new Error("runtime frame is not valid JSON");
      }
      receipts.push(this.acceptInbound(value));
    }
    return receipts;
  }

  encode(frame: RuntimeFrame): string {
    const registered = this.outboundFrames.get(frame.frameId);
    if (!registered || registered.checksum !== frame.checksum) throw new Error("cannot encode unregistered outbound frame");
    const line = canonicalJson(frame);
    if (Buffer.byteLength(line) > this.maximumFrameBytes) throw new Error("runtime frame exceeds maximum bytes");
    return `${line}\n`;
  }

  ackOutbound(sequence: number): void {
    if (sequence < this.acknowledgedOutboundSequence) return;
    if (sequence > this.outboundSequence) throw new Error("outbound ACK sequence is ahead of producer");
    this.acknowledgedOutboundSequence = sequence;
    this.revision += 1;
  }

  unacknowledged(): RuntimeFrame[] {
    return [...this.outboundFrames.values()]
      .filter((frame) => frame.sequence > this.acknowledgedOutboundSequence)
      .sort((left, right) => left.sequence - right.sequence)
      .map((frame) => structuredClone(frame));
  }

  project(): JsonObject {
    return {
      protocol: FRAMING_PROTOCOL_VERSION,
      run_id: this.runId,
      session_id: this.sessionId,
      revision: this.revision,
      inbound_sequence: this.inboundSequence,
      outbound_sequence: this.outboundSequence,
      acknowledged_outbound_sequence: this.acknowledgedOutboundSequence,
      handshake_complete: this.handshakeComplete,
      inbound_frame_count: this.inboundFrames.size,
      outbound_frame_count: this.outboundFrames.size,
      unacknowledged_count: this.unacknowledged().length,
      buffered_chars: this.buffer.length,
    };
  }

  snapshot(): FramingSnapshot {
    if (this.buffer.length > 0) throw new Error("cannot snapshot framing runtime with partial input line");
    const unsigned: Omit<FramingSnapshot, "checksum"> = {
      version: FRAMING_SNAPSHOT_VERSION,
      runId: this.runId,
      sessionId: this.sessionId,
      revision: this.revision,
      inboundSequence: this.inboundSequence,
      outboundSequence: this.outboundSequence,
      acknowledgedOutboundSequence: this.acknowledgedOutboundSequence,
      handshakeComplete: this.handshakeComplete,
      inboundFrames: structuredClone([...this.inboundFrames.values()]),
      outboundFrames: structuredClone([...this.outboundFrames.values()]),
      receipts: structuredClone(this.receipts),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(
    snapshot: FramingSnapshot,
    options: { allowRunRebind?: boolean } = {},
  ): void {
    if (snapshot.version !== FRAMING_SNAPSHOT_VERSION) throw new Error("unsupported framing snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("framing snapshot checksum mismatch");
    const runChanged = snapshot.runId !== this.runId;
    if (
      snapshot.sessionId !== this.sessionId ||
      (runChanged && options.allowRunRebind !== true)
    ) {
      throw new Error("framing snapshot identity mismatch");
    }
    validateFrameSequence(snapshot.inboundFrames, snapshot.inboundSequence, "inbound");
    validateFrameSequence(snapshot.outboundFrames, snapshot.outboundSequence, "outbound");
    if (snapshot.acknowledgedOutboundSequence > snapshot.outboundSequence) throw new Error("framing snapshot ACK exceeds outbound sequence");
    if (runChanged) {
      this.inboundFrames.clear();
      this.outboundFrames.clear();
      this.receipts.splice(0, this.receipts.length, ...structuredClone(snapshot.receipts));
      this.inboundSequence = 0;
      this.outboundSequence = 0;
      this.acknowledgedOutboundSequence = 0;
      this.handshakeComplete = false;
      this.revision = snapshot.revision + 1;
      this.buffer = "";
      return;
    }
    this.inboundFrames.clear();
    for (const frame of snapshot.inboundFrames) this.inboundFrames.set(frame.frameId, structuredClone(frame));
    this.outboundFrames.clear();
    for (const frame of snapshot.outboundFrames) this.outboundFrames.set(frame.frameId, structuredClone(frame));
    this.receipts.splice(0, this.receipts.length, ...structuredClone(snapshot.receipts));
    this.inboundSequence = snapshot.inboundSequence;
    this.outboundSequence = snapshot.outboundSequence;
    this.acknowledgedOutboundSequence = snapshot.acknowledgedOutboundSequence;
    this.handshakeComplete = snapshot.handshakeComplete;
    this.revision = snapshot.revision;
    this.buffer = "";
  }

  private receipt(frame: RuntimeFrame, accepted: boolean, duplicate: boolean, reason: string): FrameReceipt {
    const receipt: FrameReceipt = {
      receiptId: randomUUID(),
      frameId: frame.frameId,
      direction: "inbound",
      sequence: frame.sequence,
      accepted,
      duplicate,
      reason,
      receivedAt: new Date().toISOString(),
    };
    this.receipts.push(receipt);
    if (this.receipts.length > this.maximumRetainedFrames) this.receipts.splice(0, this.receipts.length - this.maximumRetainedFrames);
    return structuredClone(receipt);
  }

  private assertFrameSize(frame: RuntimeFrame): void {
    if (Buffer.byteLength(canonicalJson(frame)) > this.maximumFrameBytes) throw new Error("runtime frame exceeds maximum bytes");
  }

  private trimFrames(): void {
    trimMap(this.inboundFrames, this.maximumRetainedFrames, (frame) => frame.sequence);
    trimMap(this.outboundFrames, this.maximumRetainedFrames, (frame) => frame.sequence);
  }
}

function parseFrame(value: JsonObject): RuntimeFrame {
  const frame: RuntimeFrame = {
    protocol: asString(value.protocol) as typeof FRAMING_PROTOCOL_VERSION,
    frameId: asString(value.frame_id, asString(value.frameId)),
    kind: frameKind(asString(value.kind)),
    sequence: integer(value.sequence, -1),
    ackSequence: integer(value.ack_sequence ?? value.ackSequence, 0),
    runId: asString(value.run_id, asString(value.runId)),
    sessionId: asString(value.session_id, asString(value.sessionId)),
    correlationId: asString(value.correlation_id, asString(value.correlationId)),
    causationId: asString(value.causation_id, asString(value.causationId)) || null,
    payload: value.payload ?? null,
    payloadDigest: asString(value.payload_digest, asString(value.payloadDigest)),
    createdAt: asString(value.created_at, asString(value.createdAt)),
    checksum: asString(value.checksum),
  };
  if (frame.protocol !== FRAMING_PROTOCOL_VERSION) throw new Error("unsupported runtime frame protocol");
  if (!frame.frameId || frame.sequence < 1 || !frame.runId || !frame.sessionId || !frame.correlationId) throw new Error("runtime frame missing required identity");
  if (digest(frame.payload) !== frame.payloadDigest) throw new Error("runtime frame payload digest mismatch");
  const { checksum, ...unsigned } = frame;
  if (digest(unsigned) !== checksum) throw new Error("runtime frame checksum mismatch");
  normalizeTimestamp(frame.createdAt);
  return frame;
}

function validateFrameSequence(frames: readonly RuntimeFrame[], head: number, direction: string): void {
  const sorted = [...frames].sort((left, right) => left.sequence - right.sequence);
  const ids = new Set<string>();
  let previous = Math.max(0, head - sorted.length);
  for (const frame of sorted) {
    if (ids.has(frame.frameId)) throw new Error(`duplicate ${direction} frame id`);
    if (frame.sequence <= previous) throw new Error(`${direction} frame sequence is not monotonic`);
    ids.add(frame.frameId);
    previous = frame.sequence;
  }
  if (sorted.length > 0 && sorted.at(-1)!.sequence !== head) throw new Error(`${direction} frame head mismatch`);
}

function trimMap<T>(value: Map<string, T>, maximum: number, sequence: (item: T) => number): void {
  const remove = value.size - maximum;
  if (remove <= 0) return;
  const selected = [...value.entries()].sort((left, right) => sequence(left[1]) - sequence(right[1])).slice(0, remove);
  for (const [key] of selected) value.delete(key);
}

function frameToJson(value: RuntimeFrame): JsonObject {
  return {
    protocol: value.protocol,
    frame_id: value.frameId,
    kind: value.kind,
    sequence: value.sequence,
    ack_sequence: value.ackSequence,
    run_id: value.runId,
    session_id: value.sessionId,
    correlation_id: value.correlationId,
    causation_id: value.causationId,
    payload: value.payload,
    payload_digest: value.payloadDigest,
    created_at: value.createdAt,
    checksum: value.checksum,
  };
}

function receiptToJson(value: FrameReceipt): JsonObject {
  return {
    receipt_id: value.receiptId,
    frame_id: value.frameId,
    direction: value.direction,
    sequence: value.sequence,
    accepted: value.accepted,
    duplicate: value.duplicate,
    reason: value.reason,
    received_at: value.receivedAt,
  };
}

function frameKind(value: string): FrameKind {
  if (value === "hello" || value === "start" || value === "result" || value === "ack" || value === "nack" || value === "cancel" || value === "heartbeat") return value;
  return "event";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.floor(value) : fallback;
}

function bounded(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, Math.floor(value)));
}

function normalizeTimestamp(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid frame timestamp: ${value}`);
  return new Date(timestamp).toISOString();
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
