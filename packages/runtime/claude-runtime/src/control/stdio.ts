import { createInterface } from "node:readline";
import { resolve } from "node:path";

import { E03AgentControlCoordinator } from "../e03/coordinator.ts";
import {
  createId,
  digest,
  E03RuntimeError,
  response,
  type E03Clock,
  type E03ControlEnvelope,
  SystemE03Clock,
} from "../e03/contracts.ts";
import { FileE03PhysicalPort } from "../e03/file-port.ts";
import { ControlSchema } from "./schema.ts";

export interface StructuredControlStdioOptions {
  input?: NodeJS.ReadableStream;
  output?: NodeJS.WritableStream;
  stateRoot?: string;
  disabled?: boolean;
}

export class StructuredControlStdio {
  private readonly schema = new ControlSchema();
  private readonly coordinators = new Map<string, E03AgentControlCoordinator>();

  constructor(private readonly options: StructuredControlStdioOptions = {}) {}

  async run(): Promise<void> {
    const input = this.options.input ?? process.stdin;
    const output = this.options.output ?? process.stdout;
    const lines = createInterface({ input, crlfDelay: Infinity });
    for await (const line of lines) {
      if (!line.trim()) continue;
      let request: E03ControlEnvelope | null = null;
      try {
        request = this.schema.parse(JSON.parse(line));
        const coordinator = this.coordinator(request);
        const result = await coordinator.execute(request);
        output.write(`${JSON.stringify(result)}\n`);
      } catch (error) {
        const command = request?.command ?? "agent.status";
        const result = response({
          ok: false,
          request_id: request?.request_id ?? "invalid-request",
          command,
          phase: "rejected",
          error:
            error instanceof E03RuntimeError
              ? error.code
              : "invalid_control_json",
          state: {
            message: error instanceof Error ? error.message : String(error),
          },
        });
        output.write(`${JSON.stringify(result)}\n`);
      }
    }
  }

  private coordinator(
    envelope: E03ControlEnvelope,
  ): E03AgentControlCoordinator {
    const key = `${envelope.run_id}\0${envelope.session_id}\0${envelope.parent_task_id}`;
    let coordinator = this.coordinators.get(key);
    if (coordinator) return coordinator;
    const root = resolve(
      this.options.stateRoot ??
        process.env.ZYRA_E03_STATE_ROOT ??
        ".zyra/e03-control",
    );
    const namespace = Buffer.from(key).toString("base64url");
    coordinator = new E03AgentControlCoordinator({
      runId: envelope.run_id,
      sessionId: envelope.session_id,
      parentTaskId: envelope.parent_task_id,
      physicalPort: new FileE03PhysicalPort(root, namespace),
      disabled: this.options.disabled,
    });
    this.coordinators.set(key, coordinator);
    return coordinator;
  }
}

export async function runStructuredControlStdio(
  options: StructuredControlStdioOptions = {},
): Promise<void> {
  await new StructuredControlStdio(options).run();
}

export type ControlFrameKind =
  | "request"
  | "response"
  | "event"
  | "heartbeat"
  | "ack"
  | "error"
  | "close";

export interface ControlFrame {
  frameId: string;
  connectionId: string;
  streamId: string;
  sequence: number;
  acknowledgment: number;
  kind: ControlFrameKind;
  contentType: "application/json" | "application/octet-stream";
  encoding: "identity" | "base64";
  payload: string;
  payloadBytes: number;
  payloadDigest: string;
  compressed: boolean;
  final: boolean;
  createdAt: string;
  previousDigest: string;
  digest: string;
}

export interface ControlFrameDecodeResult {
  frames: ControlFrame[];
  remaining: Uint8Array;
  consumedBytes: number;
  rejectedBytes: number;
  digest: string;
}

function assertControlFrame(frame: ControlFrame): void {
  const { digest: checksum, ...payload } = frame;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_frame_checksum",
      `control frame ${frame.frameId} checksum mismatch`,
    );
  if (
    !frame.connectionId ||
    !frame.streamId ||
    frame.sequence < 1 ||
    frame.acknowledgment < 0 ||
    frame.payloadBytes < 0
  )
    throw new E03RuntimeError(
      "control_frame_identity",
      `control frame ${frame.frameId} identity is invalid`,
    );
  const bytes = Buffer.from(
    frame.payload,
    frame.encoding === "base64" ? "base64" : "utf8",
  );
  if (bytes.byteLength !== frame.payloadBytes)
    throw new E03RuntimeError(
      "control_frame_length",
      `control frame ${frame.frameId} payload length mismatch`,
    );
  if (digest(bytes.toString("base64")) !== frame.payloadDigest)
    throw new E03RuntimeError(
      "control_frame_payload_digest",
      `control frame ${frame.frameId} payload digest mismatch`,
    );
}

export class ControlFrameCodec {
  constructor(
    private readonly maximumFrameBytes = 16 * 1024 * 1024,
    private readonly maximumBufferedBytes = 64 * 1024 * 1024,
  ) {
    if (!Number.isSafeInteger(maximumFrameBytes) || maximumFrameBytes < 1024)
      throw new E03RuntimeError(
        "control_frame_limit_invalid",
        "control frame maximum is invalid",
      );
    if (
      !Number.isSafeInteger(maximumBufferedBytes) ||
      maximumBufferedBytes < maximumFrameBytes
    )
      throw new E03RuntimeError(
        "control_buffer_limit_invalid",
        "control buffer maximum is invalid",
      );
  }

  frame(input: {
    connectionId: string;
    streamId: string;
    sequence: number;
    acknowledgment?: number;
    kind: ControlFrameKind;
    payload?: string | Uint8Array;
    contentType?: ControlFrame["contentType"];
    encoding?: ControlFrame["encoding"];
    compressed?: boolean;
    final?: boolean;
    previousDigest?: string;
    now?: string;
  }): ControlFrame {
    const connectionId = input.connectionId.trim();
    const streamId = input.streamId.trim();
    if (!connectionId || !streamId)
      throw new E03RuntimeError(
        "control_frame_identity_missing",
        "control frame connection and stream ids are required",
      );
    if (!Number.isSafeInteger(input.sequence) || input.sequence < 1)
      throw new E03RuntimeError(
        "control_frame_sequence_invalid",
        "control frame sequence is invalid",
      );
    const encoding = input.encoding ?? "identity";
    const raw =
      typeof input.payload === "string"
        ? Buffer.from(input.payload, "utf8")
        : Buffer.from(input.payload ?? new Uint8Array());
    if (raw.byteLength > this.maximumFrameBytes)
      throw new E03RuntimeError(
        "control_frame_too_large",
        `control frame payload ${raw.byteLength} exceeds ${this.maximumFrameBytes}`,
      );
    const payload =
      encoding === "base64" ? raw.toString("base64") : raw.toString("utf8");
    const payloadBytes = raw.byteLength;
    const payloadDigest = digest(raw.toString("base64"));
    const body = {
      frameId: `control-frame-${digest({
        connectionId,
        streamId,
        sequence: input.sequence,
        kind: input.kind,
        payloadDigest,
      }).slice(0, 32)}`,
      connectionId,
      streamId,
      sequence: input.sequence,
      acknowledgment: input.acknowledgment ?? 0,
      kind: input.kind,
      contentType: input.contentType ?? "application/json",
      encoding,
      payload,
      payloadBytes,
      payloadDigest,
      compressed: input.compressed ?? false,
      final: input.final ?? false,
      createdAt: input.now ?? new Date().toISOString(),
      previousDigest: input.previousDigest ?? "",
    };
    const frame = { ...body, digest: digest(body) };
    assertControlFrame(frame);
    return frame;
  }

  encode(frame: ControlFrame): Uint8Array {
    assertControlFrame(frame);
    const body = Buffer.from(JSON.stringify(frame), "utf8");
    if (body.byteLength > this.maximumFrameBytes)
      throw new E03RuntimeError(
        "control_encoded_frame_too_large",
        `encoded control frame ${body.byteLength} exceeds ${this.maximumFrameBytes}`,
      );
    const header = Buffer.allocUnsafe(8);
    header.writeUInt32BE(0x5a595241, 0);
    header.writeUInt32BE(body.byteLength, 4);
    return Buffer.concat([header, body]);
  }

  decode(buffer: Uint8Array): ControlFrameDecodeResult {
    if (buffer.byteLength > this.maximumBufferedBytes)
      throw new E03RuntimeError(
        "control_buffer_overflow",
        `control buffer ${buffer.byteLength} exceeds ${this.maximumBufferedBytes}`,
      );
    const source = Buffer.from(buffer);
    const frames: ControlFrame[] = [];
    let offset = 0;
    let rejectedBytes = 0;
    while (source.byteLength - offset >= 8) {
      const magic = source.readUInt32BE(offset);
      if (magic !== 0x5a595241) {
        offset += 1;
        rejectedBytes += 1;
        continue;
      }
      const length = source.readUInt32BE(offset + 4);
      if (length < 2 || length > this.maximumFrameBytes)
        throw new E03RuntimeError(
          "control_frame_declared_length",
          `control frame declared length ${length} is invalid`,
        );
      if (source.byteLength - offset - 8 < length) break;
      const body = source
        .subarray(offset + 8, offset + 8 + length)
        .toString("utf8");
      let raw: unknown;
      try {
        raw = JSON.parse(body);
      } catch (error) {
        throw new E03RuntimeError(
          "control_frame_json_invalid",
          "control frame contains invalid JSON",
          { error: error instanceof Error ? error.message : String(error) },
        );
      }
      if (!raw || typeof raw !== "object" || Array.isArray(raw))
        throw new E03RuntimeError(
          "control_frame_object_required",
          "control frame JSON must be an object",
        );
      const frame = raw as ControlFrame;
      assertControlFrame(frame);
      frames.push(structuredClone(frame));
      offset += 8 + length;
    }
    const remaining = source.subarray(offset);
    const payload = {
      frames,
      remaining: new Uint8Array(remaining),
      consumedBytes: offset,
      rejectedBytes,
    };
    return {
      ...payload,
      digest: digest({
        frameDigests: frames.map((frame) => frame.digest),
        remaining: remaining.toString("base64"),
        consumedBytes: offset,
        rejectedBytes,
      }),
    };
  }

  encodeNdjson(frame: ControlFrame): string {
    assertControlFrame(frame);
    const line = JSON.stringify(frame);
    if (Buffer.byteLength(line, "utf8") > this.maximumFrameBytes)
      throw new E03RuntimeError(
        "control_ndjson_frame_too_large",
        "control NDJSON frame exceeds maximum size",
      );
    return `${line}\n`;
  }

  decodeNdjson(input: string): ControlFrame[] {
    const frames: ControlFrame[] = [];
    for (const [index, line] of input
      .replaceAll("\r", "")
      .split("\n")
      .entries()) {
      if (!line.trim()) continue;
      if (Buffer.byteLength(line, "utf8") > this.maximumFrameBytes)
        throw new E03RuntimeError(
          "control_ndjson_frame_too_large",
          `control NDJSON frame at line ${index + 1} exceeds maximum size`,
        );
      let raw: unknown;
      try {
        raw = JSON.parse(line);
      } catch (error) {
        throw new E03RuntimeError(
          "control_ndjson_invalid",
          `control NDJSON line ${index + 1} is invalid`,
          { error: error instanceof Error ? error.message : String(error) },
        );
      }
      const frame = raw as ControlFrame;
      assertControlFrame(frame);
      frames.push(structuredClone(frame));
    }
    return frames;
  }
}

export type ControlWriteState =
  | "queued"
  | "writing"
  | "flushed"
  | "acknowledged"
  | "failed"
  | "dropped";

export interface ControlWriteEntry {
  writeId: string;
  connectionId: string;
  streamId: string;
  frameId: string;
  frameSequence: number;
  frameDigest: string;
  bytes: number;
  priority: number;
  state: ControlWriteState;
  attempts: number;
  maximumAttempts: number;
  enqueuedAt: string;
  startedAt: string | null;
  flushedAt: string | null;
  acknowledgedAt: string | null;
  failedAt: string | null;
  errorCode: string;
  errorDigest: string;
  revision: number;
  digest: string;
}

export interface ControlWriteQueueProjection {
  connectionId: string;
  queuedWrites: number;
  queuedBytes: number;
  writingWrites: number;
  writingBytes: number;
  flushedWrites: number;
  unacknowledgedWrites: number;
  failedWrites: number;
  droppedWrites: number;
  saturated: boolean;
  oldestQueuedAt: string | null;
  highestAcknowledgment: number;
  projectedAt: string;
  digest: string;
}

function assertWriteEntry(entry: ControlWriteEntry): void {
  const { digest: checksum, ...payload } = entry;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_write_checksum",
      `control write ${entry.writeId} checksum mismatch`,
    );
  if (
    !entry.connectionId ||
    !entry.streamId ||
    !entry.frameId ||
    entry.frameSequence < 1 ||
    entry.bytes < 1 ||
    entry.attempts < 0 ||
    entry.maximumAttempts < 1 ||
    entry.revision < 1
  )
    throw new E03RuntimeError(
      "control_write_invalid",
      `control write ${entry.writeId} is invalid`,
    );
  if (entry.state === "writing" && !entry.startedAt)
    throw new E03RuntimeError(
      "control_write_start_time",
      `writing control entry ${entry.writeId} has no start time`,
    );
  if (["flushed", "acknowledged"].includes(entry.state) && !entry.flushedAt)
    throw new E03RuntimeError(
      "control_write_flush_time",
      `flushed control entry ${entry.writeId} has no flush time`,
    );
  if (entry.state === "acknowledged" && !entry.acknowledgedAt)
    throw new E03RuntimeError(
      "control_write_ack_time",
      `acknowledged control entry ${entry.writeId} has no ack time`,
    );
  if (entry.state === "failed" && !entry.failedAt)
    throw new E03RuntimeError(
      "control_write_failure_time",
      `failed control entry ${entry.writeId} has no failure time`,
    );
}

function resealWriteEntry(
  entry: ControlWriteEntry,
  patch: Partial<Omit<ControlWriteEntry, "writeId" | "frameId" | "digest">>,
): ControlWriteEntry {
  const { digest: _, ...prior } = entry;
  const payload = {
    ...prior,
    ...patch,
    writeId: entry.writeId,
    frameId: entry.frameId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertWriteEntry(next);
  return next;
}

export class ControlWriteQueue {
  private writes = new Map<string, ControlWriteEntry>();
  private byConnection = new Map<string, string[]>();
  private byFrame = new Map<string, string>();

  constructor(
    private readonly maximumQueuedBytes = 64 * 1024 * 1024,
    private readonly maximumQueuedWrites = 10_000,
  ) {
    if (!Number.isSafeInteger(maximumQueuedBytes) || maximumQueuedBytes < 1024)
      throw new E03RuntimeError(
        "control_write_byte_limit",
        "control write byte limit is invalid",
      );
    if (!Number.isSafeInteger(maximumQueuedWrites) || maximumQueuedWrites < 1)
      throw new E03RuntimeError(
        "control_write_count_limit",
        "control write count limit is invalid",
      );
  }

  enqueue(input: {
    frame: ControlFrame;
    encodedBytes: number;
    priority?: number;
    maximumAttempts?: number;
    now?: string;
  }): ControlWriteEntry {
    assertControlFrame(input.frame);
    if (!Number.isSafeInteger(input.encodedBytes) || input.encodedBytes < 1)
      throw new E03RuntimeError(
        "control_write_bytes_invalid",
        "control write encoded byte count is invalid",
      );
    const existingWriteId = this.byFrame.get(input.frame.frameId);
    if (existingWriteId)
      return structuredClone(this.writes.get(existingWriteId)!);
    const current = this.forConnection(input.frame.connectionId);
    const active = current.filter((entry) =>
      ["queued", "writing", "flushed"].includes(entry.state),
    );
    const queuedBytes = active.reduce((sum, entry) => sum + entry.bytes, 0);
    if (active.length >= this.maximumQueuedWrites)
      throw new E03RuntimeError(
        "control_write_queue_full",
        `control write queue ${input.frame.connectionId} is full`,
      );
    if (queuedBytes + input.encodedBytes > this.maximumQueuedBytes)
      throw new E03RuntimeError(
        "control_write_queue_bytes",
        `control write queue ${input.frame.connectionId} exceeds byte limit`,
      );
    const maximumAttempts = input.maximumAttempts ?? 3;
    if (!Number.isSafeInteger(maximumAttempts) || maximumAttempts < 1)
      throw new E03RuntimeError(
        "control_write_attempt_limit",
        "control write attempt limit is invalid",
      );
    const payload = {
      writeId: `control-write-${digest({
        connectionId: input.frame.connectionId,
        frameId: input.frame.frameId,
        frameDigest: input.frame.digest,
      }).slice(0, 32)}`,
      connectionId: input.frame.connectionId,
      streamId: input.frame.streamId,
      frameId: input.frame.frameId,
      frameSequence: input.frame.sequence,
      frameDigest: input.frame.digest,
      bytes: input.encodedBytes,
      priority: input.priority ?? 100,
      state: "queued" as const,
      attempts: 0,
      maximumAttempts,
      enqueuedAt: input.now ?? new Date().toISOString(),
      startedAt: null,
      flushedAt: null,
      acknowledgedAt: null,
      failedAt: null,
      errorCode: "",
      errorDigest: "",
      revision: 1,
    };
    const entry = { ...payload, digest: digest(payload) };
    assertWriteEntry(entry);
    this.writes.set(entry.writeId, entry);
    this.byFrame.set(entry.frameId, entry.writeId);
    const ids = this.byConnection.get(entry.connectionId) ?? [];
    ids.push(entry.writeId);
    this.byConnection.set(entry.connectionId, ids);
    return structuredClone(entry);
  }

  acquire(
    connectionId: string,
    maximumWrites: number,
    maximumBytes: number,
    now = new Date().toISOString(),
  ): ControlWriteEntry[] {
    if (!Number.isSafeInteger(maximumWrites) || maximumWrites < 1)
      throw new E03RuntimeError(
        "control_write_acquire_count",
        "control write acquire count is invalid",
      );
    if (!Number.isSafeInteger(maximumBytes) || maximumBytes < 1)
      throw new E03RuntimeError(
        "control_write_acquire_bytes",
        "control write acquire bytes is invalid",
      );
    const selected: ControlWriteEntry[] = [];
    let bytes = 0;
    for (const entry of this.forConnection(connectionId)
      .filter((value) => value.state === "queued")
      .sort(
        (left, right) =>
          left.priority - right.priority ||
          left.frameSequence - right.frameSequence ||
          left.enqueuedAt.localeCompare(right.enqueuedAt),
      )) {
      if (selected.length >= maximumWrites) break;
      if (selected.length && bytes + entry.bytes > maximumBytes) break;
      if (!selected.length && entry.bytes > maximumBytes)
        throw new E03RuntimeError(
          "control_write_head_too_large",
          `control write ${entry.writeId} exceeds acquire byte limit`,
        );
      if (entry.attempts >= entry.maximumAttempts)
        throw new E03RuntimeError(
          "control_write_attempts_exhausted",
          `control write ${entry.writeId} exhausted attempts`,
        );
      const next = resealWriteEntry(entry, {
        state: "writing",
        attempts: entry.attempts + 1,
        startedAt: now,
        failedAt: null,
        errorCode: "",
        errorDigest: "",
        revision: entry.revision + 1,
      });
      this.writes.set(next.writeId, next);
      selected.push(next);
      bytes += next.bytes;
    }
    return selected.map((entry) => structuredClone(entry));
  }

  flushed(writeId: string, now = new Date().toISOString()): ControlWriteEntry {
    const current = this.require(writeId);
    if (current.state === "flushed" || current.state === "acknowledged")
      return structuredClone(current);
    if (current.state !== "writing")
      throw new E03RuntimeError(
        "control_write_not_writing",
        `control write ${writeId} cannot flush from ${current.state}`,
      );
    const next = resealWriteEntry(current, {
      state: "flushed",
      flushedAt: now,
      revision: current.revision + 1,
    });
    this.writes.set(writeId, next);
    return structuredClone(next);
  }

  acknowledge(
    connectionId: string,
    acknowledgment: number,
    now = new Date().toISOString(),
  ): ControlWriteEntry[] {
    if (!Number.isSafeInteger(acknowledgment) || acknowledgment < 0)
      throw new E03RuntimeError(
        "control_ack_sequence_invalid",
        "control acknowledgment sequence is invalid",
      );
    const acknowledged: ControlWriteEntry[] = [];
    for (const entry of this.forConnection(connectionId)) {
      if (entry.frameSequence > acknowledgment) continue;
      if (entry.state === "acknowledged") {
        acknowledged.push(entry);
        continue;
      }
      if (entry.state !== "flushed")
        throw new E03RuntimeError(
          "control_ack_before_flush",
          `control write ${entry.writeId} acknowledged before flush`,
        );
      const next = resealWriteEntry(entry, {
        state: "acknowledged",
        acknowledgedAt: now,
        revision: entry.revision + 1,
      });
      this.writes.set(next.writeId, next);
      acknowledged.push(next);
    }
    return acknowledged.map((entry) => structuredClone(entry));
  }

  fail(input: {
    writeId: string;
    errorCode: string;
    error: string;
    retryable: boolean;
    now?: string;
  }): ControlWriteEntry {
    const current = this.require(input.writeId);
    if (current.state !== "writing")
      throw new E03RuntimeError(
        "control_write_not_writing",
        `control write ${current.writeId} cannot fail from ${current.state}`,
      );
    const exhausted = current.attempts >= current.maximumAttempts;
    const retry = input.retryable && !exhausted;
    const next = resealWriteEntry(current, {
      state: retry ? "queued" : "failed",
      startedAt: retry ? null : current.startedAt,
      failedAt: retry ? null : (input.now ?? new Date().toISOString()),
      errorCode: input.errorCode.trim() || "control_write_failed",
      errorDigest: digest(input.error),
      revision: current.revision + 1,
    });
    this.writes.set(next.writeId, next);
    return structuredClone(next);
  }

  drop(
    writeId: string,
    reason: string,
    now = new Date().toISOString(),
  ): ControlWriteEntry {
    const current = this.require(writeId);
    if (current.state === "acknowledged")
      throw new E03RuntimeError(
        "control_write_drop_acknowledged",
        `acknowledged control write ${writeId} cannot be dropped`,
      );
    const next = resealWriteEntry(current, {
      state: "dropped",
      failedAt: now,
      errorCode: reason.trim() || "control_write_dropped",
      errorDigest: digest(reason),
      revision: current.revision + 1,
    });
    this.writes.set(next.writeId, next);
    return structuredClone(next);
  }

  project(connectionId: string): ControlWriteQueueProjection {
    const entries = this.forConnection(connectionId);
    const queued = entries.filter((entry) => entry.state === "queued");
    const writing = entries.filter((entry) => entry.state === "writing");
    const flushed = entries.filter((entry) => entry.state === "flushed");
    const acknowledged = entries.filter(
      (entry) => entry.state === "acknowledged",
    );
    const failed = entries.filter((entry) => entry.state === "failed");
    const dropped = entries.filter((entry) => entry.state === "dropped");
    const payload = {
      connectionId,
      queuedWrites: queued.length,
      queuedBytes: queued.reduce((sum, entry) => sum + entry.bytes, 0),
      writingWrites: writing.length,
      writingBytes: writing.reduce((sum, entry) => sum + entry.bytes, 0),
      flushedWrites: flushed.length,
      unacknowledgedWrites: flushed.length,
      failedWrites: failed.length,
      droppedWrites: dropped.length,
      saturated:
        queued.length + writing.length + flushed.length >=
          this.maximumQueuedWrites ||
        [...queued, ...writing, ...flushed].reduce(
          (sum, entry) => sum + entry.bytes,
          0,
        ) >= this.maximumQueuedBytes,
      oldestQueuedAt:
        queued.sort((left, right) =>
          left.enqueuedAt.localeCompare(right.enqueuedAt),
        )[0]?.enqueuedAt ?? null,
      highestAcknowledgment: acknowledged.reduce(
        (maximum, entry) => Math.max(maximum, entry.frameSequence),
        0,
      ),
      projectedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(entries: readonly ControlWriteEntry[]): void {
    const writes = new Map<string, ControlWriteEntry>();
    const byConnection = new Map<string, string[]>();
    const byFrame = new Map<string, string>();
    for (const raw of entries) {
      const entry = structuredClone(raw);
      assertWriteEntry(entry);
      if (writes.has(entry.writeId) || byFrame.has(entry.frameId))
        throw new E03RuntimeError(
          "duplicate_control_write",
          `control write ${entry.writeId} repeats`,
        );
      writes.set(entry.writeId, entry);
      byFrame.set(entry.frameId, entry.writeId);
      const ids = byConnection.get(entry.connectionId) ?? [];
      ids.push(entry.writeId);
      byConnection.set(entry.connectionId, ids);
    }
    this.writes = writes;
    this.byConnection = byConnection;
    this.byFrame = byFrame;
  }

  snapshot(): ControlWriteEntry[] {
    return [...this.writes.values()]
      .sort(
        (left, right) =>
          left.connectionId.localeCompare(right.connectionId) ||
          left.frameSequence - right.frameSequence,
      )
      .map((entry) => structuredClone(entry));
  }

  private forConnection(connectionId: string): ControlWriteEntry[] {
    return (this.byConnection.get(connectionId) ?? []).map(
      (writeId) => this.writes.get(writeId)!,
    );
  }

  private require(writeId: string): ControlWriteEntry {
    const entry = this.writes.get(writeId);
    if (!entry)
      throw new E03RuntimeError(
        "control_write_missing",
        `control write ${writeId} is missing`,
      );
    assertWriteEntry(entry);
    return entry;
  }
}

export type ControlConnectionState =
  | "opening"
  | "ready"
  | "draining"
  | "closed"
  | "failed";

export interface ControlConnectionRecord {
  connectionId: string;
  peerId: string;
  state: ControlConnectionState;
  protocolVersion: string;
  maximumFrameBytes: number;
  maximumInFlight: number;
  nextOutboundSequence: number;
  highestInboundSequence: number;
  highestOutboundAcknowledged: number;
  inboundFrameCount: number;
  outboundFrameCount: number;
  duplicateInboundCount: number;
  rejectedInboundCount: number;
  heartbeatIntervalMs: number;
  heartbeatTimeoutMs: number;
  openedAt: string;
  readyAt: string | null;
  lastInboundAt: string;
  lastOutboundAt: string;
  lastHeartbeatAt: string;
  drainStartedAt: string | null;
  closedAt: string | null;
  closeReason: string;
  revision: number;
  digest: string;
}

export interface ControlInboundDecision {
  accepted: boolean;
  code: string;
  connectionId: string;
  frameId: string;
  sequence: number;
  expectedSequence: number;
  duplicate: boolean;
  gap: boolean;
  acknowledgmentAdvanced: boolean;
  connectionState: ControlConnectionState;
  decidedAt: string;
  digest: string;
}

function assertConnectionRecord(record: ControlConnectionRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_connection_checksum",
      `control connection ${record.connectionId} checksum mismatch`,
    );
  if (
    !record.connectionId ||
    !record.peerId ||
    !record.protocolVersion ||
    record.maximumFrameBytes < 1024 ||
    record.maximumInFlight < 1 ||
    record.nextOutboundSequence < 1 ||
    record.highestInboundSequence < 0 ||
    record.highestOutboundAcknowledged < 0 ||
    record.revision < 1
  )
    throw new E03RuntimeError(
      "control_connection_invalid",
      `control connection ${record.connectionId} is invalid`,
    );
  if (record.state === "ready" && !record.readyAt)
    throw new E03RuntimeError(
      "control_connection_ready_time",
      `ready control connection ${record.connectionId} has no ready time`,
    );
  if (["closed", "failed"].includes(record.state) && !record.closedAt)
    throw new E03RuntimeError(
      "control_connection_close_time",
      `closed control connection ${record.connectionId} has no close time`,
    );
}

function resealConnection(
  record: ControlConnectionRecord,
  patch: Partial<Omit<ControlConnectionRecord, "connectionId" | "digest">>,
): ControlConnectionRecord {
  const { digest: _, ...prior } = record;
  const payload = { ...prior, ...patch, connectionId: record.connectionId };
  const next = { ...payload, digest: digest(payload) };
  assertConnectionRecord(next);
  return next;
}

export class StructuredControlConnectionRuntime {
  private connections = new Map<string, ControlConnectionRecord>();
  private inboundFrames = new Map<string, Set<string>>();

  open(input: {
    peerId: string;
    protocolVersion: string;
    maximumFrameBytes: number;
    maximumInFlight: number;
    heartbeatIntervalMs: number;
    heartbeatTimeoutMs: number;
    now?: string;
  }): ControlConnectionRecord {
    if (!input.peerId.trim() || !input.protocolVersion.trim())
      throw new E03RuntimeError(
        "control_connection_peer_missing",
        "control connection peer and protocol version are required",
      );
    if (
      !Number.isSafeInteger(input.maximumFrameBytes) ||
      input.maximumFrameBytes < 1024
    )
      throw new E03RuntimeError(
        "control_connection_frame_limit",
        "control connection frame limit is invalid",
      );
    if (
      !Number.isSafeInteger(input.maximumInFlight) ||
      input.maximumInFlight < 1
    )
      throw new E03RuntimeError(
        "control_connection_flight_limit",
        "control connection in-flight limit is invalid",
      );
    if (
      !Number.isSafeInteger(input.heartbeatIntervalMs) ||
      !Number.isSafeInteger(input.heartbeatTimeoutMs) ||
      input.heartbeatIntervalMs < 1 ||
      input.heartbeatTimeoutMs <= input.heartbeatIntervalMs
    )
      throw new E03RuntimeError(
        "control_connection_heartbeat_window",
        "control connection heartbeat window is invalid",
      );
    const openedAt = input.now ?? new Date().toISOString();
    const payload = {
      connectionId: `control-connection-${digest({
        peerId: input.peerId,
        protocolVersion: input.protocolVersion,
        openedAt,
      }).slice(0, 32)}`,
      peerId: input.peerId.trim(),
      state: "opening" as const,
      protocolVersion: input.protocolVersion,
      maximumFrameBytes: input.maximumFrameBytes,
      maximumInFlight: input.maximumInFlight,
      nextOutboundSequence: 1,
      highestInboundSequence: 0,
      highestOutboundAcknowledged: 0,
      inboundFrameCount: 0,
      outboundFrameCount: 0,
      duplicateInboundCount: 0,
      rejectedInboundCount: 0,
      heartbeatIntervalMs: input.heartbeatIntervalMs,
      heartbeatTimeoutMs: input.heartbeatTimeoutMs,
      openedAt,
      readyAt: null,
      lastInboundAt: openedAt,
      lastOutboundAt: openedAt,
      lastHeartbeatAt: openedAt,
      drainStartedAt: null,
      closedAt: null,
      closeReason: "",
      revision: 1,
    };
    const record = { ...payload, digest: digest(payload) };
    assertConnectionRecord(record);
    this.connections.set(record.connectionId, record);
    this.inboundFrames.set(record.connectionId, new Set<string>());
    return structuredClone(record);
  }

  ready(
    connectionId: string,
    now = new Date().toISOString(),
  ): ControlConnectionRecord {
    const current = this.require(connectionId);
    if (current.state === "ready") return structuredClone(current);
    if (current.state !== "opening")
      throw new E03RuntimeError(
        "control_connection_not_opening",
        `control connection ${connectionId} cannot become ready from ${current.state}`,
      );
    const next = resealConnection(current, {
      state: "ready",
      readyAt: now,
      lastInboundAt: now,
      lastOutboundAt: now,
      lastHeartbeatAt: now,
      revision: current.revision + 1,
    });
    this.connections.set(connectionId, next);
    return structuredClone(next);
  }

  nextFrame(input: {
    connectionId: string;
    streamId: string;
    kind: ControlFrameKind;
    payload?: string | Uint8Array;
    contentType?: ControlFrame["contentType"];
    encoding?: ControlFrame["encoding"];
    final?: boolean;
    now?: string;
  }): ControlFrame {
    const current = this.require(input.connectionId);
    if (current.state !== "ready" && current.state !== "draining")
      throw new E03RuntimeError(
        "control_connection_not_ready",
        `control connection ${current.connectionId} is ${current.state}`,
      );
    if (current.state === "draining" && input.kind === "request")
      throw new E03RuntimeError(
        "control_connection_draining",
        `control connection ${current.connectionId} is draining`,
      );
    const codec = new ControlFrameCodec(current.maximumFrameBytes);
    const frame = codec.frame({
      ...input,
      sequence: current.nextOutboundSequence,
      acknowledgment: current.highestInboundSequence,
    });
    const next = resealConnection(current, {
      nextOutboundSequence: current.nextOutboundSequence + 1,
      outboundFrameCount: current.outboundFrameCount + 1,
      lastOutboundAt: input.now ?? new Date().toISOString(),
      revision: current.revision + 1,
    });
    this.connections.set(current.connectionId, next);
    return frame;
  }

  receive(
    frame: ControlFrame,
    now = new Date().toISOString(),
  ): ControlInboundDecision {
    assertControlFrame(frame);
    const current = this.require(frame.connectionId);
    const seen =
      this.inboundFrames.get(frame.connectionId) ?? new Set<string>();
    const duplicate =
      seen.has(frame.frameId) ||
      frame.sequence <= current.highestInboundSequence;
    const expectedSequence = current.highestInboundSequence + 1;
    const gap = frame.sequence > expectedSequence;
    const acknowledgmentAdvanced =
      frame.acknowledgment > current.highestOutboundAcknowledged;
    let code = "control_inbound_accepted";
    if (!["ready", "draining"].includes(current.state))
      code = "control_inbound_connection_not_ready";
    else if (duplicate) code = "control_inbound_duplicate";
    else if (gap) code = "control_inbound_sequence_gap";
    else if (frame.acknowledgment >= current.nextOutboundSequence)
      code = "control_inbound_ack_ahead";
    const accepted = code === "control_inbound_accepted";
    const payload = {
      accepted,
      code,
      connectionId: frame.connectionId,
      frameId: frame.frameId,
      sequence: frame.sequence,
      expectedSequence,
      duplicate,
      gap,
      acknowledgmentAdvanced,
      connectionState: current.state,
      decidedAt: now,
    };
    const decision = { ...payload, digest: digest(payload) };
    if (accepted) {
      seen.add(frame.frameId);
      this.inboundFrames.set(frame.connectionId, seen);
      const next = resealConnection(current, {
        highestInboundSequence: frame.sequence,
        highestOutboundAcknowledged: Math.max(
          current.highestOutboundAcknowledged,
          frame.acknowledgment,
        ),
        inboundFrameCount: current.inboundFrameCount + 1,
        lastInboundAt: now,
        lastHeartbeatAt:
          frame.kind === "heartbeat" ? now : current.lastHeartbeatAt,
        revision: current.revision + 1,
      });
      this.connections.set(frame.connectionId, next);
    } else if (!duplicate) {
      const next = resealConnection(current, {
        rejectedInboundCount: current.rejectedInboundCount + 1,
        revision: current.revision + 1,
      });
      this.connections.set(frame.connectionId, next);
    } else {
      const next = resealConnection(current, {
        duplicateInboundCount: current.duplicateInboundCount + 1,
        revision: current.revision + 1,
      });
      this.connections.set(frame.connectionId, next);
    }
    return decision;
  }

  heartbeatDue(
    connectionId: string,
    now = new Date().toISOString(),
  ): { due: boolean; timedOut: boolean; nextAt: string; digest: string } {
    const current = this.require(connectionId);
    const elapsed = Date.parse(now) - Date.parse(current.lastHeartbeatAt);
    const payload = {
      due: elapsed >= current.heartbeatIntervalMs,
      timedOut: elapsed >= current.heartbeatTimeoutMs,
      nextAt: new Date(
        Date.parse(current.lastHeartbeatAt) + current.heartbeatIntervalMs,
      ).toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  drain(
    connectionId: string,
    now = new Date().toISOString(),
  ): ControlConnectionRecord {
    const current = this.require(connectionId);
    if (current.state === "draining") return structuredClone(current);
    if (current.state !== "ready")
      throw new E03RuntimeError(
        "control_connection_drain_state",
        `control connection ${connectionId} cannot drain from ${current.state}`,
      );
    const next = resealConnection(current, {
      state: "draining",
      drainStartedAt: now,
      revision: current.revision + 1,
    });
    this.connections.set(connectionId, next);
    return structuredClone(next);
  }

  close(
    connectionId: string,
    reason: string,
    failed = false,
    now = new Date().toISOString(),
  ): ControlConnectionRecord {
    const current = this.require(connectionId);
    if (current.state === "closed" || current.state === "failed")
      return structuredClone(current);
    const next = resealConnection(current, {
      state: failed ? "failed" : "closed",
      closedAt: now,
      closeReason:
        reason.trim() || (failed ? "connection_failed" : "connection_closed"),
      revision: current.revision + 1,
    });
    this.connections.set(connectionId, next);
    return structuredClone(next);
  }

  restore(input: {
    connections: readonly ControlConnectionRecord[];
    inboundFrameIds?: Readonly<Record<string, readonly string[]>>;
  }): void {
    const connections = new Map<string, ControlConnectionRecord>();
    const inboundFrames = new Map<string, Set<string>>();
    for (const raw of input.connections) {
      const record = structuredClone(raw);
      assertConnectionRecord(record);
      if (connections.has(record.connectionId))
        throw new E03RuntimeError(
          "duplicate_control_connection",
          `control connection ${record.connectionId} repeats`,
        );
      const ids = new Set(input.inboundFrameIds?.[record.connectionId] ?? []);
      if (ids.size > record.inboundFrameCount)
        throw new E03RuntimeError(
          "control_connection_inbound_projection",
          `control connection ${record.connectionId} has too many restored frame ids`,
        );
      connections.set(record.connectionId, record);
      inboundFrames.set(record.connectionId, ids);
    }
    this.connections = connections;
    this.inboundFrames = inboundFrames;
  }

  snapshot(): ControlConnectionRecord[] {
    return [...this.connections.values()]
      .sort((left, right) =>
        left.connectionId.localeCompare(right.connectionId),
      )
      .map((record) => structuredClone(record));
  }

  private require(connectionId: string): ControlConnectionRecord {
    const record = this.connections.get(connectionId);
    if (!record)
      throw new E03RuntimeError(
        "control_connection_missing",
        `control connection ${connectionId} is missing`,
      );
    assertConnectionRecord(record);
    return record;
  }
}

export interface ControlFlowWindow {
  windowId: string;
  connectionId: string;
  streamId: string;
  maximumFrames: number;
  maximumBytes: number;
  availableFrames: number;
  availableBytes: number;
  consumedSequence: number;
  acknowledgedSequence: number;
  state: "open" | "paused" | "closed";
  openedAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface ControlFlowReservation {
  reservationId: string;
  windowId: string;
  connectionId: string;
  streamId: string;
  frameId: string;
  sequence: number;
  bytes: number;
  state: "reserved" | "sent" | "acknowledged" | "released" | "expired";
  reservedAt: string;
  expiresAt: string;
  sentAt: string | null;
  acknowledgedAt: string | null;
  releasedAt: string | null;
  revision: number;
  digest: string;
}

function assertControlFlowWindow(window: ControlFlowWindow): void {
  const { digest: checksum, ...payload } = window;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_flow_window_digest",
      `control flow window ${window.windowId} digest is invalid`,
    );
  if (!window.windowId || !window.connectionId || !window.streamId)
    throw new E03RuntimeError(
      "control_flow_window_identity",
      "control flow window identity is incomplete",
    );
  for (const [name, value] of [
    ["maximum frames", window.maximumFrames],
    ["maximum bytes", window.maximumBytes],
  ] as const)
    if (!Number.isSafeInteger(value) || value < 1)
      throw new E03RuntimeError(
        "control_flow_window_limit",
        `control flow window ${name} is invalid`,
      );
  if (
    !Number.isSafeInteger(window.availableFrames) ||
    window.availableFrames < 0 ||
    window.availableFrames > window.maximumFrames
  )
    throw new E03RuntimeError(
      "control_flow_window_frames",
      "control flow available frame count is invalid",
    );
  if (
    !Number.isSafeInteger(window.availableBytes) ||
    window.availableBytes < 0 ||
    window.availableBytes > window.maximumBytes
  )
    throw new E03RuntimeError(
      "control_flow_window_bytes",
      "control flow available byte count is invalid",
    );
  if (
    !Number.isSafeInteger(window.consumedSequence) ||
    !Number.isSafeInteger(window.acknowledgedSequence) ||
    window.acknowledgedSequence > window.consumedSequence
  )
    throw new E03RuntimeError(
      "control_flow_window_sequence",
      "control flow sequence is invalid",
    );
  if (!Number.isSafeInteger(window.revision) || window.revision < 1)
    throw new E03RuntimeError(
      "control_flow_window_revision",
      "control flow window revision is invalid",
    );
}

function assertControlFlowReservation(
  reservation: ControlFlowReservation,
): void {
  const { digest: checksum, ...payload } = reservation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_flow_reservation_digest",
      `control flow reservation ${reservation.reservationId} digest is invalid`,
    );
  if (
    !reservation.reservationId ||
    !reservation.windowId ||
    !reservation.connectionId ||
    !reservation.streamId ||
    !reservation.frameId
  )
    throw new E03RuntimeError(
      "control_flow_reservation_identity",
      "control flow reservation identity is incomplete",
    );
  if (!Number.isSafeInteger(reservation.sequence) || reservation.sequence < 1)
    throw new E03RuntimeError(
      "control_flow_reservation_sequence",
      "control flow reservation sequence is invalid",
    );
  if (!Number.isSafeInteger(reservation.bytes) || reservation.bytes < 1)
    throw new E03RuntimeError(
      "control_flow_reservation_bytes",
      "control flow reservation byte count is invalid",
    );
  if (!Number.isSafeInteger(reservation.revision) || reservation.revision < 1)
    throw new E03RuntimeError(
      "control_flow_reservation_revision",
      "control flow reservation revision is invalid",
    );
}

export class ControlFlowRuntime {
  private windows = new Map<string, ControlFlowWindow>();
  private reservations = new Map<string, ControlFlowReservation>();
  private byFrame = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    connectionId: string;
    streamId: string;
    maximumFrames: number;
    maximumBytes: number;
  }): ControlFlowWindow {
    if (
      [...this.windows.values()].some(
        (window) =>
          window.connectionId === input.connectionId &&
          window.streamId === input.streamId &&
          window.state !== "closed",
      )
    )
      throw new E03RuntimeError(
        "control_flow_window_duplicate",
        `control flow stream ${input.streamId} already has an open window`,
      );
    const now = this.clock.now();
    const payload = {
      windowId: createId("control-flow-window"),
      connectionId: input.connectionId.trim(),
      streamId: input.streamId.trim(),
      maximumFrames: input.maximumFrames,
      maximumBytes: input.maximumBytes,
      availableFrames: input.maximumFrames,
      availableBytes: input.maximumBytes,
      consumedSequence: 0,
      acknowledgedSequence: 0,
      state: "open" as const,
      openedAt: now,
      updatedAt: now,
      revision: 1,
    };
    const window = { ...payload, digest: digest(payload) };
    assertControlFlowWindow(window);
    this.windows.set(window.windowId, window);
    return structuredClone(window);
  }

  reserve(input: {
    windowId: string;
    frameId: string;
    bytes: number;
    expiresAt: string;
    expectedRevision: number;
  }): { window: ControlFlowWindow; reservation: ControlFlowReservation } {
    const window = this.requireWindow(input.windowId);
    this.assertWindowRevision(window, input.expectedRevision);
    if (window.state !== "open")
      throw new E03RuntimeError(
        "control_flow_window_state",
        `control flow window ${window.windowId} is ${window.state}`,
      );
    if (this.byFrame.has(input.frameId)) {
      const reservation = this.requireReservation(
        this.byFrame.get(input.frameId)!,
      );
      if (
        reservation.windowId !== window.windowId ||
        reservation.bytes !== input.bytes
      )
        throw new E03RuntimeError(
          "control_flow_frame_conflict",
          `control frame ${input.frameId} was reserved differently`,
        );
      return {
        window: structuredClone(window),
        reservation: structuredClone(reservation),
      };
    }
    if (!Number.isSafeInteger(input.bytes) || input.bytes < 1)
      throw new E03RuntimeError(
        "control_flow_frame_bytes",
        "control flow frame byte count is invalid",
      );
    if (window.availableFrames < 1 || window.availableBytes < input.bytes)
      throw new E03RuntimeError(
        "control_flow_backpressure",
        `control flow window ${window.windowId} lacks capacity`,
      );
    const now = this.clock.now();
    if (Date.parse(input.expiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "control_flow_reservation_expiry",
        "control flow reservation expiry must be in the future",
      );
    const nextWindow = this.transitionWindow(window, {
      availableFrames: window.availableFrames - 1,
      availableBytes: window.availableBytes - input.bytes,
      consumedSequence: window.consumedSequence + 1,
    });
    const payload = {
      reservationId: createId("control-flow-reservation"),
      windowId: window.windowId,
      connectionId: window.connectionId,
      streamId: window.streamId,
      frameId: input.frameId,
      sequence: nextWindow.consumedSequence,
      bytes: input.bytes,
      state: "reserved" as const,
      reservedAt: now,
      expiresAt: input.expiresAt,
      sentAt: null,
      acknowledgedAt: null,
      releasedAt: null,
      revision: 1,
    };
    const reservation = { ...payload, digest: digest(payload) };
    assertControlFlowReservation(reservation);
    this.reservations.set(reservation.reservationId, reservation);
    this.byFrame.set(reservation.frameId, reservation.reservationId);
    return {
      window: nextWindow,
      reservation: structuredClone(reservation),
    };
  }

  sent(
    reservationId: string,
    expectedRevision: number,
  ): ControlFlowReservation {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedRevision);
    if (reservation.state !== "reserved")
      throw new E03RuntimeError(
        "control_flow_sent_state",
        `control flow reservation ${reservationId} is ${reservation.state}`,
      );
    return this.transitionReservation(reservation, {
      state: "sent",
      sentAt: this.clock.now(),
    });
  }

  acknowledge(
    reservationId: string,
    expectedReservationRevision: number,
    expectedWindowRevision: number,
  ): { window: ControlFlowWindow; reservation: ControlFlowReservation } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedReservationRevision);
    const window = this.requireWindow(reservation.windowId);
    this.assertWindowRevision(window, expectedWindowRevision);
    if (reservation.state !== "sent")
      throw new E03RuntimeError(
        "control_flow_ack_state",
        `control flow reservation ${reservationId} is ${reservation.state}`,
      );
    const nextReservation = this.transitionReservation(reservation, {
      state: "acknowledged",
      acknowledgedAt: this.clock.now(),
    });
    const contiguous = this.contiguousAcknowledgedSequence(window.windowId);
    const nextWindow = this.transitionWindow(window, {
      availableFrames: Math.min(
        window.maximumFrames,
        window.availableFrames + 1,
      ),
      availableBytes: Math.min(
        window.maximumBytes,
        window.availableBytes + reservation.bytes,
      ),
      acknowledgedSequence: Math.max(window.acknowledgedSequence, contiguous),
    });
    return { window: nextWindow, reservation: nextReservation };
  }

  release(
    reservationId: string,
    expectedReservationRevision: number,
    expectedWindowRevision: number,
  ): { window: ControlFlowWindow; reservation: ControlFlowReservation } {
    const reservation = this.requireReservation(reservationId);
    this.assertReservationRevision(reservation, expectedReservationRevision);
    const window = this.requireWindow(reservation.windowId);
    this.assertWindowRevision(window, expectedWindowRevision);
    if (["acknowledged", "released", "expired"].includes(reservation.state))
      throw new E03RuntimeError(
        "control_flow_release_state",
        `control flow reservation ${reservationId} is ${reservation.state}`,
      );
    const nextReservation = this.transitionReservation(reservation, {
      state: "released",
      releasedAt: this.clock.now(),
    });
    const nextWindow = this.transitionWindow(window, {
      availableFrames: Math.min(
        window.maximumFrames,
        window.availableFrames + 1,
      ),
      availableBytes: Math.min(
        window.maximumBytes,
        window.availableBytes + reservation.bytes,
      ),
    });
    return { window: nextWindow, reservation: nextReservation };
  }

  pause(windowId: string, expectedRevision: number): ControlFlowWindow {
    const window = this.requireWindow(windowId);
    this.assertWindowRevision(window, expectedRevision);
    if (window.state !== "open")
      throw new E03RuntimeError(
        "control_flow_pause_state",
        `control flow window ${windowId} is ${window.state}`,
      );
    return this.transitionWindow(window, { state: "paused" });
  }

  resume(windowId: string, expectedRevision: number): ControlFlowWindow {
    const window = this.requireWindow(windowId);
    this.assertWindowRevision(window, expectedRevision);
    if (window.state !== "paused")
      throw new E03RuntimeError(
        "control_flow_resume_state",
        `control flow window ${windowId} is ${window.state}`,
      );
    return this.transitionWindow(window, { state: "open" });
  }

  close(windowId: string, expectedRevision: number): ControlFlowWindow {
    const window = this.requireWindow(windowId);
    this.assertWindowRevision(window, expectedRevision);
    const pending = [...this.reservations.values()].filter(
      (reservation) =>
        reservation.windowId === windowId &&
        !["acknowledged", "released", "expired"].includes(reservation.state),
    );
    if (pending.length)
      throw new E03RuntimeError(
        "control_flow_close_pending",
        `control flow window ${windowId} has ${pending.length} pending reservations`,
      );
    return this.transitionWindow(window, { state: "closed" });
  }

  sweep(now = this.clock.now()): ControlFlowReservation[] {
    const expired: ControlFlowReservation[] = [];
    for (const reservation of this.reservations.values()) {
      if (
        !["reserved", "sent"].includes(reservation.state) ||
        Date.parse(now) < Date.parse(reservation.expiresAt)
      )
        continue;
      const window = this.requireWindow(reservation.windowId);
      const nextReservation = this.transitionReservation(reservation, {
        state: "expired",
        releasedAt: now,
      });
      this.transitionWindow(window, {
        availableFrames: Math.min(
          window.maximumFrames,
          window.availableFrames + 1,
        ),
        availableBytes: Math.min(
          window.maximumBytes,
          window.availableBytes + reservation.bytes,
        ),
      });
      expired.push(nextReservation);
    }
    return expired;
  }

  snapshot(): {
    windows: ControlFlowWindow[];
    reservations: ControlFlowReservation[];
  } {
    return {
      windows: [...this.windows.values()].map((value) =>
        structuredClone(value),
      ),
      reservations: [...this.reservations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    windows: readonly ControlFlowWindow[];
    reservations: readonly ControlFlowReservation[];
  }): void {
    const windows = new Map<string, ControlFlowWindow>();
    const reservations = new Map<string, ControlFlowReservation>();
    const byFrame = new Map<string, string>();
    for (const window of input.windows) {
      assertControlFlowWindow(window);
      if (windows.has(window.windowId))
        throw new E03RuntimeError(
          "control_flow_window_restore_duplicate",
          `duplicate control flow window ${window.windowId}`,
        );
      windows.set(window.windowId, structuredClone(window));
    }
    for (const reservation of input.reservations) {
      assertControlFlowReservation(reservation);
      const window = windows.get(reservation.windowId);
      if (
        !window ||
        window.connectionId !== reservation.connectionId ||
        window.streamId !== reservation.streamId
      )
        throw new E03RuntimeError(
          "control_flow_reservation_restore_window",
          `control flow reservation ${reservation.reservationId} has invalid window custody`,
        );
      if (
        reservations.has(reservation.reservationId) ||
        byFrame.has(reservation.frameId)
      )
        throw new E03RuntimeError(
          "control_flow_reservation_restore_duplicate",
          `duplicate control flow reservation ${reservation.reservationId}`,
        );
      reservations.set(reservation.reservationId, structuredClone(reservation));
      byFrame.set(reservation.frameId, reservation.reservationId);
    }
    this.windows = windows;
    this.reservations = reservations;
    this.byFrame = byFrame;
  }

  private contiguousAcknowledgedSequence(windowId: string): number {
    const acknowledged = new Set(
      [...this.reservations.values()]
        .filter(
          (reservation) =>
            reservation.windowId === windowId &&
            reservation.state === "acknowledged",
        )
        .map((reservation) => reservation.sequence),
    );
    let sequence = 0;
    while (acknowledged.has(sequence + 1)) sequence += 1;
    return sequence;
  }

  private requireWindow(windowId: string): ControlFlowWindow {
    const window = this.windows.get(windowId);
    if (!window)
      throw new E03RuntimeError(
        "control_flow_window_missing",
        `control flow window ${windowId} does not exist`,
      );
    assertControlFlowWindow(window);
    return window;
  }

  private requireReservation(reservationId: string): ControlFlowReservation {
    const reservation = this.reservations.get(reservationId);
    if (!reservation)
      throw new E03RuntimeError(
        "control_flow_reservation_missing",
        `control flow reservation ${reservationId} does not exist`,
      );
    assertControlFlowReservation(reservation);
    return reservation;
  }

  private assertWindowRevision(
    window: ControlFlowWindow,
    expected: number,
  ): void {
    if (window.revision !== expected)
      throw new E03RuntimeError(
        "control_flow_window_stale_revision",
        `control flow window ${window.windowId} revision is stale`,
      );
  }

  private assertReservationRevision(
    reservation: ControlFlowReservation,
    expected: number,
  ): void {
    if (reservation.revision !== expected)
      throw new E03RuntimeError(
        "control_flow_reservation_stale_revision",
        `control flow reservation ${reservation.reservationId} revision is stale`,
      );
  }

  private transitionWindow(
    window: ControlFlowWindow,
    patch: Partial<Omit<ControlFlowWindow, "windowId" | "revision" | "digest">>,
  ): ControlFlowWindow {
    const { digest: _, ...prior } = window;
    const payload = {
      ...prior,
      ...patch,
      windowId: window.windowId,
      updatedAt: this.clock.now(),
      revision: window.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlFlowWindow(next);
    this.windows.set(next.windowId, next);
    return structuredClone(next);
  }

  private transitionReservation(
    reservation: ControlFlowReservation,
    patch: Partial<
      Omit<ControlFlowReservation, "reservationId" | "revision" | "digest">
    >,
  ): ControlFlowReservation {
    const { digest: _, ...prior } = reservation;
    const payload = {
      ...prior,
      ...patch,
      reservationId: reservation.reservationId,
      revision: reservation.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlFlowReservation(next);
    this.reservations.set(next.reservationId, next);
    return structuredClone(next);
  }
}

export interface ControlTransmission {
  transmissionId: string;
  connectionId: string;
  streamId: string;
  frameId: string;
  sequence: number;
  state:
    | "queued"
    | "sent"
    | "acknowledged"
    | "retry_wait"
    | "failed"
    | "discarded";
  attempt: number;
  maximumAttempts: number;
  firstQueuedAt: string;
  lastSentAt: string | null;
  nextAttemptAt: string | null;
  acknowledgedAt: string | null;
  failure: string | null;
  revision: number;
  digest: string;
}
export interface ControlTransmissionAck {
  ackId: string;
  connectionId: string;
  streamId: string;
  cumulativeSequence: number;
  selectiveSequences: number[];
  rejectedSequences: number[];
  receivedAt: string;
  previousDigest: string;
  digest: string;
}
function assertTransmission(value: ControlTransmission): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_transmission_digest",
      `control transmission ${value.transmissionId} is corrupt`,
    );
  if (
    !value.transmissionId ||
    !value.connectionId ||
    !value.streamId ||
    !value.frameId ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1 ||
    !Number.isSafeInteger(value.attempt) ||
    value.attempt < 0 ||
    !Number.isSafeInteger(value.maximumAttempts) ||
    value.maximumAttempts < 1 ||
    value.attempt > value.maximumAttempts ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_transmission",
      `control transmission ${value.transmissionId} is invalid`,
    );
  if (value.state === "acknowledged" && value.acknowledgedAt === null)
    throw new E03RuntimeError(
      "control_transmission_ack_time",
      `acknowledged transmission ${value.transmissionId} lacks time`,
    );
}
function assertTransmissionAck(value: ControlTransmissionAck): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_transmission_ack_digest",
      `control transmission ACK ${value.ackId} is corrupt`,
    );
  if (
    !value.ackId ||
    !value.connectionId ||
    !value.streamId ||
    !Number.isSafeInteger(value.cumulativeSequence) ||
    value.cumulativeSequence < 0 ||
    [...value.selectiveSequences, ...value.rejectedSequences].some(
      (sequence) => !Number.isSafeInteger(sequence) || sequence < 1,
    )
  )
    throw new E03RuntimeError(
      "control_transmission_ack",
      `control transmission ACK ${value.ackId} is invalid`,
    );
}
export class ControlRetransmissionRuntime {
  private transmissions = new Map<string, ControlTransmission>();
  private frameIndex = new Map<string, string>();
  private acknowledgements = new Map<string, ControlTransmissionAck[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  enqueue(frame: ControlFrame, maximumAttempts = 3): ControlTransmission {
    assertControlFrame(frame);
    const existingId = this.frameIndex.get(frame.frameId);
    if (existingId)
      return structuredClone(this.requireTransmission(existingId));
    if (!Number.isSafeInteger(maximumAttempts) || maximumAttempts < 1)
      throw new E03RuntimeError(
        "control_transmission_attempt_limit",
        "control transmission attempt limit is invalid",
      );
    const payload = {
      transmissionId: createId("control-transmission"),
      connectionId: frame.connectionId,
      streamId: frame.streamId,
      frameId: frame.frameId,
      sequence: frame.sequence,
      state: "queued" as const,
      attempt: 0,
      maximumAttempts,
      firstQueuedAt: this.clock.now(),
      lastSentAt: null,
      nextAttemptAt: null,
      acknowledgedAt: null,
      failure: null,
      revision: 1,
    };
    const transmission = { ...payload, digest: digest(payload) };
    assertTransmission(transmission);
    if (
      [...this.transmissions.values()].some(
        (value) =>
          value.connectionId === frame.connectionId &&
          value.streamId === frame.streamId &&
          value.sequence === frame.sequence &&
          value.state !== "discarded",
      )
    )
      throw new E03RuntimeError(
        "control_transmission_sequence_duplicate",
        `control transmission sequence ${frame.sequence} already exists`,
      );
    this.transmissions.set(transmission.transmissionId, transmission);
    this.frameIndex.set(transmission.frameId, transmission.transmissionId);
    return structuredClone(transmission);
  }
  ready(
    connectionId: string,
    maximum = 64,
    now = this.clock.now(),
  ): ControlTransmission[] {
    if (
      !Number.isSafeInteger(maximum) ||
      maximum < 1 ||
      Number.isNaN(Date.parse(now))
    )
      throw new E03RuntimeError(
        "control_transmission_ready_input",
        "control transmission ready input is invalid",
      );
    return [...this.transmissions.values()]
      .filter(
        (value) =>
          value.connectionId === connectionId &&
          (value.state === "queued" ||
            (value.state === "retry_wait" &&
              value.nextAttemptAt !== null &&
              Date.parse(value.nextAttemptAt) <= Date.parse(now))),
      )
      .sort(
        (left, right) =>
          left.streamId.localeCompare(right.streamId) ||
          left.sequence - right.sequence,
      )
      .slice(0, maximum)
      .map((value) => structuredClone(value));
  }
  markSent(
    transmissionId: string,
    expectedRevision: number,
    retryDelayMs: number,
  ): ControlTransmission {
    const transmission = this.requireTransmission(transmissionId);
    this.assertTransmissionRevision(transmission, expectedRevision);
    if (transmission.state !== "queued" && transmission.state !== "retry_wait")
      throw new E03RuntimeError(
        "control_transmission_send_state",
        `control transmission ${transmissionId} is ${transmission.state}`,
      );
    if (transmission.attempt >= transmission.maximumAttempts)
      throw new E03RuntimeError(
        "control_transmission_attempts_exhausted",
        `control transmission ${transmissionId} exhausted attempts`,
      );
    if (!Number.isSafeInteger(retryDelayMs) || retryDelayMs < 1)
      throw new E03RuntimeError(
        "control_transmission_retry_delay",
        "control transmission retry delay is invalid",
      );
    const attempt = transmission.attempt + 1;
    return this.transitionTransmission(transmission, {
      state: "sent",
      attempt,
      lastSentAt: this.clock.now(),
      nextAttemptAt: new Date(
        Date.parse(this.clock.now()) + retryDelayMs * 2 ** (attempt - 1),
      ).toISOString(),
      failure: null,
    });
  }
  timeout(
    transmissionId: string,
    expectedRevision: number,
  ): ControlTransmission {
    const transmission = this.requireTransmission(transmissionId);
    this.assertTransmissionRevision(transmission, expectedRevision);
    if (transmission.state !== "sent")
      throw new E03RuntimeError(
        "control_transmission_timeout_state",
        `control transmission ${transmissionId} is ${transmission.state}`,
      );
    if (transmission.attempt >= transmission.maximumAttempts)
      return this.transitionTransmission(transmission, {
        state: "failed",
        failure: "acknowledgement timeout",
      });
    return this.transitionTransmission(transmission, {
      state: "retry_wait",
      failure: "acknowledgement timeout",
    });
  }
  acknowledge(input: {
    connectionId: string;
    streamId: string;
    cumulativeSequence: number;
    selectiveSequences?: readonly number[];
    rejectedSequences?: readonly number[];
  }): {
    ack: ControlTransmissionAck;
    acknowledged: ControlTransmission[];
    retried: ControlTransmission[];
  } {
    const selectiveSequences = [
      ...new Set(input.selectiveSequences ?? []),
    ].sort((a, b) => a - b);
    const rejectedSequences = [...new Set(input.rejectedSequences ?? [])].sort(
      (a, b) => a - b,
    );
    const overlap = selectiveSequences.find((sequence) =>
      rejectedSequences.includes(sequence),
    );
    if (overlap !== undefined)
      throw new E03RuntimeError(
        "control_transmission_ack_overlap",
        `control transmission sequence ${overlap} is both accepted and rejected`,
      );
    const entries = this.acknowledgements.get(input.connectionId) ?? [];
    const payload = {
      ackId: createId("control-transmission-ack"),
      connectionId: input.connectionId,
      streamId: input.streamId,
      cumulativeSequence: input.cumulativeSequence,
      selectiveSequences,
      rejectedSequences,
      receivedAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const ack = { ...payload, digest: digest(payload) };
    assertTransmissionAck(ack);
    entries.push(ack);
    this.acknowledgements.set(input.connectionId, entries);
    const acknowledged: ControlTransmission[] = [];
    const retried: ControlTransmission[] = [];
    for (const transmission of this.transmissions.values()) {
      if (
        transmission.connectionId !== input.connectionId ||
        transmission.streamId !== input.streamId ||
        transmission.state === "acknowledged" ||
        transmission.state === "discarded"
      )
        continue;
      if (
        transmission.sequence <= input.cumulativeSequence ||
        selectiveSequences.includes(transmission.sequence)
      )
        acknowledged.push(
          this.transitionTransmission(transmission, {
            state: "acknowledged",
            acknowledgedAt: this.clock.now(),
            nextAttemptAt: null,
            failure: null,
          }),
        );
      else if (rejectedSequences.includes(transmission.sequence)) {
        if (transmission.attempt >= transmission.maximumAttempts)
          retried.push(
            this.transitionTransmission(transmission, {
              state: "failed",
              failure: "peer rejected frame",
            }),
          );
        else
          retried.push(
            this.transitionTransmission(transmission, {
              state: "retry_wait",
              failure: "peer rejected frame",
            }),
          );
      }
    }
    return { ack: structuredClone(ack), acknowledged, retried };
  }
  discard(
    transmissionId: string,
    expectedRevision: number,
    reason: string,
  ): ControlTransmission {
    const transmission = this.requireTransmission(transmissionId);
    this.assertTransmissionRevision(transmission, expectedRevision);
    if (
      transmission.state === "acknowledged" ||
      transmission.state === "discarded"
    )
      return structuredClone(transmission);
    if (!reason.trim())
      throw new E03RuntimeError(
        "control_transmission_discard_reason",
        "control transmission discard reason is required",
      );
    return this.transitionTransmission(transmission, {
      state: "discarded",
      failure: reason.trim(),
      nextAttemptAt: null,
    });
  }
  verify(): void {
    for (const [connectionId, values] of this.acknowledgements) {
      let previousDigest = "root";
      for (const ack of values) {
        assertTransmissionAck(ack);
        if (
          ack.connectionId !== connectionId ||
          ack.previousDigest !== previousDigest
        )
          throw new E03RuntimeError(
            "control_transmission_ack_chain",
            `control transmission ACK ${ack.ackId} breaks chain`,
          );
        previousDigest = ack.digest;
      }
    }
    const sequences = new Set<string>();
    for (const transmission of this.transmissions.values()) {
      assertTransmission(transmission);
      const key = `${transmission.connectionId}:${transmission.streamId}:${transmission.sequence}`;
      if (sequences.has(key) && transmission.state !== "discarded")
        throw new E03RuntimeError(
          "control_transmission_sequence_duplicate",
          `control transmission sequence ${key} is duplicated`,
        );
      sequences.add(key);
    }
  }
  snapshot(): {
    transmissions: ControlTransmission[];
    acknowledgements: ControlTransmissionAck[];
  } {
    this.verify();
    return {
      transmissions: [...this.transmissions.values()].map((value) =>
        structuredClone(value),
      ),
      acknowledgements: [...this.acknowledgements.values()]
        .flat()
        .map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    transmissions: readonly ControlTransmission[];
    acknowledgements: readonly ControlTransmissionAck[];
  }): void {
    const transmissions = new Map<string, ControlTransmission>();
    const frameIndex = new Map<string, string>();
    const acknowledgements = new Map<string, ControlTransmissionAck[]>();
    for (const value of snapshot.transmissions) {
      assertTransmission(value);
      if (
        transmissions.has(value.transmissionId) ||
        frameIndex.has(value.frameId)
      )
        throw new E03RuntimeError(
          "control_transmission_restore_duplicate",
          `duplicate control transmission ${value.transmissionId}`,
        );
      transmissions.set(value.transmissionId, structuredClone(value));
      frameIndex.set(value.frameId, value.transmissionId);
    }
    for (const value of snapshot.acknowledgements) {
      assertTransmissionAck(value);
      const entries = acknowledgements.get(value.connectionId) ?? [];
      if (entries.some((entry) => entry.ackId === value.ackId))
        throw new E03RuntimeError(
          "control_transmission_ack_restore_duplicate",
          `duplicate control transmission ACK ${value.ackId}`,
        );
      entries.push(structuredClone(value));
      acknowledgements.set(value.connectionId, entries);
    }
    this.transmissions = transmissions;
    this.frameIndex = frameIndex;
    this.acknowledgements = acknowledgements;
    this.verify();
  }
  private requireTransmission(id: string): ControlTransmission {
    const value = this.transmissions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transmission_missing",
        `control transmission ${id} does not exist`,
      );
    assertTransmission(value);
    return value;
  }
  private assertTransmissionRevision(
    value: ControlTransmission,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transmission_stale_revision",
        `control transmission ${value.transmissionId} revision is stale`,
      );
  }
  private transitionTransmission(
    value: ControlTransmission,
    patch: Partial<
      Omit<ControlTransmission, "transmissionId" | "revision" | "digest">
    >,
  ): ControlTransmission {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transmissionId: value.transmissionId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertTransmission(next);
    this.transmissions.set(next.transmissionId, next);
    return structuredClone(next);
  }
}

export interface ControlFrameAssembly {
  assemblyId: string;
  connectionId: string;
  streamId: string;
  startSequence: number;
  endSequence: number | null;
  frameIds: string[];
  state: "assembling" | "complete" | "rejected" | "consumed";
  contentType: ControlFrame["contentType"];
  encoding: ControlFrame["encoding"];
  payloadBytes: number;
  payloadDigest: string | null;
  rejectionReason: string | null;
  createdAt: string;
  completedAt: string | null;
  revision: number;
  digest: string;
}
function assertFrameAssembly(value: ControlFrameAssembly): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_frame_assembly_digest",
      `control frame assembly ${value.assemblyId} is corrupt`,
    );
  if (
    !value.assemblyId ||
    !value.connectionId ||
    !value.streamId ||
    !Number.isSafeInteger(value.startSequence) ||
    value.startSequence < 1 ||
    (value.endSequence !== null &&
      (!Number.isSafeInteger(value.endSequence) ||
        value.endSequence < value.startSequence)) ||
    !Number.isSafeInteger(value.payloadBytes) ||
    value.payloadBytes < 0 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_frame_assembly",
      `control frame assembly ${value.assemblyId} is invalid`,
    );
}
export interface ControlTransportSession {
  transportSessionId: string;
  connectionId: string;
  peerId: string;
  protocolVersion: string;
  negotiatedCapabilities: string[];
  state:
    | "negotiating"
    | "established"
    | "rekeying"
    | "draining"
    | "closed"
    | "failed";
  activeKeyId: string;
  sendSequence: number;
  receiveSequence: number;
  maximumFrameBytes: number;
  heartbeatIntervalMs: number;
  establishedAt: string;
  lastActivityAt: string;
  closedAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface ControlTransportKey {
  keyId: string;
  transportSessionId: string;
  generation: number;
  publicKeyDigest: string;
  proofDigest: string;
  state: "proposed" | "verified" | "active" | "retired" | "revoked";
  proposedAt: string;
  verifiedAt: string;
  activatedAt: string;
  retiredAt: string;
  expiresAt: string;
  revision: number;
  digest: string;
}

export interface ControlTransportHandshake {
  handshakeId: string;
  transportSessionId: string;
  direction: "offer" | "answer" | "confirmation";
  protocolVersions: string[];
  capabilities: string[];
  nonce: string;
  keyId: string;
  transcriptDigest: string;
  previousDigest: string;
  accepted: boolean;
  errorCode: string;
  createdAt: string;
  digest: string;
}

export interface ControlTransportSessionSnapshot {
  sessions: ControlTransportSession[];
  keys: ControlTransportKey[];
  handshakes: ControlTransportHandshake[];
  activeSessionByConnection: [string, string][];
  keyBySessionGeneration: [string, string][];
}

function assertControlTransportSession(value: ControlTransportSession): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.transportSessionId ||
    !value.connectionId ||
    !value.peerId ||
    value.sendSequence < 0 ||
    value.receiveSequence < 0 ||
    value.maximumFrameBytes < 1 ||
    value.heartbeatIntervalMs < 1 ||
    new Set(value.negotiatedCapabilities).size !==
      value.negotiatedCapabilities.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_session_corrupt",
      `control transport session ${value.transportSessionId || "<empty>"} is corrupt`,
    );
}

function assertControlTransportKey(value: ControlTransportKey): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.keyId ||
    !value.transportSessionId ||
    value.generation < 1 ||
    !value.publicKeyDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_key_corrupt",
      `control transport key ${value.keyId || "<empty>"} is corrupt`,
    );
}

function assertControlTransportHandshake(
  value: ControlTransportHandshake,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.handshakeId ||
    !value.transportSessionId ||
    !value.nonce ||
    new Set(value.protocolVersions).size !== value.protocolVersions.length ||
    new Set(value.capabilities).size !== value.capabilities.length ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_handshake_corrupt",
      `control transport handshake ${value.handshakeId || "<empty>"} is corrupt`,
    );
}

export class ControlTransportSessionRuntime {
  private sessions = new Map<string, ControlTransportSession>();
  private keys = new Map<string, ControlTransportKey[]>();
  private handshakes = new Map<string, ControlTransportHandshake[]>();
  private activeSessionByConnection = new Map<string, string>();
  private keyBySessionGeneration = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    transportSessionId?: string;
    connectionId: string;
    peerId: string;
    maximumFrameBytes: number;
    heartbeatIntervalMs: number;
  }): ControlTransportSession {
    const activeId = this.activeSessionByConnection.get(input.connectionId);
    if (activeId) return structuredClone(this.requireSession(activeId));
    if (input.maximumFrameBytes < 1 || input.heartbeatIntervalMs < 1)
      throw new E03RuntimeError(
        "control_transport_session_limits",
        "control transport session limits must be positive",
      );
    const transportSessionId =
      input.transportSessionId ?? createId("control-transport-session");
    const now = this.clock.now();
    const payload = {
      transportSessionId,
      connectionId: input.connectionId,
      peerId: input.peerId,
      protocolVersion: "",
      negotiatedCapabilities: [] as string[],
      state: "negotiating" as const,
      activeKeyId: "",
      sendSequence: 0,
      receiveSequence: 0,
      maximumFrameBytes: input.maximumFrameBytes,
      heartbeatIntervalMs: input.heartbeatIntervalMs,
      establishedAt: "",
      lastActivityAt: now,
      closedAt: "",
      errorCode: "",
      revision: 1,
    };
    const session = { ...payload, digest: digest(payload) };
    assertControlTransportSession(session);
    this.sessions.set(transportSessionId, session);
    this.keys.set(transportSessionId, []);
    this.handshakes.set(transportSessionId, []);
    this.activeSessionByConnection.set(input.connectionId, transportSessionId);
    return structuredClone(session);
  }

  proposeKey(input: {
    keyId?: string;
    transportSessionId: string;
    publicKeyDigest: string;
    proofDigest: string;
    expiresAt: string;
  }): ControlTransportKey {
    const session = this.requireSession(input.transportSessionId);
    if (!["negotiating", "established", "rekeying"].includes(session.state))
      throw new E03RuntimeError(
        "control_transport_key_session_state",
        `control transport session ${session.transportSessionId} is ${session.state}`,
      );
    const entries = this.keyEntries(session.transportSessionId);
    const generation = entries.length + 1;
    const keyId = input.keyId ?? createId("control-transport-key");
    const payload = {
      keyId,
      transportSessionId: session.transportSessionId,
      generation,
      publicKeyDigest: input.publicKeyDigest,
      proofDigest: input.proofDigest,
      state: "proposed" as const,
      proposedAt: this.clock.now(),
      verifiedAt: "",
      activatedAt: "",
      retiredAt: "",
      expiresAt: input.expiresAt,
      revision: 1,
    };
    const key = { ...payload, digest: digest(payload) };
    assertControlTransportKey(key);
    entries.push(key);
    this.keys.set(session.transportSessionId, entries);
    this.keyBySessionGeneration.set(
      this.keyGenerationKey(session.transportSessionId, generation),
      keyId,
    );
    if (session.state === "established")
      this.transitionSession(session, { state: "rekeying" });
    return structuredClone(key);
  }

  verifyKey(
    keyId: string,
    expectedRevision: number,
    proofDigest: string,
  ): ControlTransportKey {
    const key = this.requireKey(keyId);
    this.assertKeyRevision(key, expectedRevision);
    if (key.state !== "proposed" || key.proofDigest !== proofDigest)
      throw new E03RuntimeError(
        "control_transport_key_proof",
        `control transport key ${keyId} proof is invalid`,
      );
    return this.transitionKey(key, {
      state: "verified",
      verifiedAt: this.clock.now(),
    });
  }

  recordHandshake(input: {
    handshakeId?: string;
    transportSessionId: string;
    direction: ControlTransportHandshake["direction"];
    protocolVersions: readonly string[];
    capabilities: readonly string[];
    nonce: string;
    keyId: string;
    accepted: boolean;
    errorCode?: string;
  }): ControlTransportHandshake {
    const session = this.requireSession(input.transportSessionId);
    const entries = this.handshakeEntries(session.transportSessionId);
    const previousDigest = entries.at(-1)?.digest ?? "";
    const payload = {
      handshakeId: input.handshakeId ?? createId("control-transport-handshake"),
      transportSessionId: session.transportSessionId,
      direction: input.direction,
      protocolVersions: [...new Set(input.protocolVersions)].sort(),
      capabilities: [...new Set(input.capabilities)].sort(),
      nonce: input.nonce,
      keyId: input.keyId,
      transcriptDigest: digest({
        previousDigest,
        direction: input.direction,
        protocolVersions: [...new Set(input.protocolVersions)].sort(),
        capabilities: [...new Set(input.capabilities)].sort(),
        nonce: input.nonce,
        keyId: input.keyId,
      }),
      previousDigest,
      accepted: input.accepted,
      errorCode: input.errorCode ?? "",
      createdAt: this.clock.now(),
    };
    const handshake = { ...payload, digest: digest(payload) };
    assertControlTransportHandshake(handshake);
    entries.push(handshake);
    this.handshakes.set(session.transportSessionId, entries);
    if (!input.accepted)
      this.transitionSession(session, {
        state: "failed",
        errorCode: input.errorCode ?? "handshake_rejected",
      });
    return structuredClone(handshake);
  }

  establish(input: {
    transportSessionId: string;
    expectedRevision: number;
    protocolVersion: string;
    capabilities: readonly string[];
    keyId: string;
  }): ControlTransportSession {
    const session = this.requireSession(input.transportSessionId);
    this.assertSessionRevision(session, input.expectedRevision);
    if (!["negotiating", "rekeying"].includes(session.state))
      throw new E03RuntimeError(
        "control_transport_establish_state",
        `control transport session ${session.transportSessionId} is ${session.state}`,
      );
    const key = this.requireKey(input.keyId);
    if (
      key.transportSessionId !== session.transportSessionId ||
      key.state !== "verified"
    )
      throw new E03RuntimeError(
        "control_transport_establish_key",
        `control transport key ${key.keyId} is not verified for session`,
      );
    const handshakes = this.handshakeEntries(session.transportSessionId);
    if (
      !handshakes.some(
        (value) => value.direction === "confirmation" && value.accepted,
      )
    )
      throw new E03RuntimeError(
        "control_transport_confirmation_missing",
        `control transport session ${session.transportSessionId} lacks confirmation`,
      );
    if (session.activeKeyId) {
      const prior = this.requireKey(session.activeKeyId);
      this.transitionKey(prior, {
        state: "retired",
        retiredAt: this.clock.now(),
      });
    }
    this.transitionKey(key, { state: "active", activatedAt: this.clock.now() });
    return this.transitionSession(session, {
      state: "established",
      protocolVersion: input.protocolVersion,
      negotiatedCapabilities: [...new Set(input.capabilities)].sort(),
      activeKeyId: key.keyId,
      establishedAt: session.establishedAt || this.clock.now(),
      errorCode: "",
    });
  }

  send(
    transportSessionId: string,
    expectedRevision: number,
    byteLength: number,
  ): ControlTransportSession {
    const session = this.requireSession(transportSessionId);
    this.assertSessionRevision(session, expectedRevision);
    this.assertEstablishedAndSized(session, byteLength);
    return this.transitionSession(session, {
      sendSequence: session.sendSequence + 1,
      lastActivityAt: this.clock.now(),
    });
  }

  receive(
    transportSessionId: string,
    expectedRevision: number,
    sequence: number,
    byteLength: number,
  ): ControlTransportSession {
    const session = this.requireSession(transportSessionId);
    this.assertSessionRevision(session, expectedRevision);
    this.assertEstablishedAndSized(session, byteLength);
    if (sequence !== session.receiveSequence + 1)
      throw new E03RuntimeError(
        "control_transport_receive_sequence",
        `control transport session expected sequence ${session.receiveSequence + 1}`,
      );
    return this.transitionSession(session, {
      receiveSequence: sequence,
      lastActivityAt: this.clock.now(),
    });
  }

  close(
    transportSessionId: string,
    expectedRevision: number,
    errorCode = "",
  ): ControlTransportSession {
    const session = this.requireSession(transportSessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (["closed", "failed"].includes(session.state))
      throw new E03RuntimeError(
        "control_transport_close_terminal",
        `control transport session ${transportSessionId} is terminal`,
      );
    const next = this.transitionSession(session, {
      state: errorCode ? "failed" : "closed",
      closedAt: this.clock.now(),
      errorCode,
    });
    this.activeSessionByConnection.delete(session.connectionId);
    return next;
  }

  snapshot(): ControlTransportSessionSnapshot {
    return {
      sessions: [...this.sessions.values()].map((value) =>
        structuredClone(value),
      ),
      keys: [...this.keys.values()]
        .flat()
        .map((value) => structuredClone(value)),
      handshakes: [...this.handshakes.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeSessionByConnection: [...this.activeSessionByConnection.entries()],
      keyBySessionGeneration: [...this.keyBySessionGeneration.entries()],
    };
  }

  restore(snapshot: ControlTransportSessionSnapshot): void {
    const sessions = new Map<string, ControlTransportSession>();
    const keys = new Map<string, ControlTransportKey[]>();
    const handshakes = new Map<string, ControlTransportHandshake[]>();
    for (const value of snapshot.sessions) {
      assertControlTransportSession(value);
      if (sessions.has(value.transportSessionId))
        throw new E03RuntimeError(
          "control_transport_restore_duplicate",
          `session ${value.transportSessionId} duplicate`,
        );
      sessions.set(value.transportSessionId, structuredClone(value));
      keys.set(value.transportSessionId, []);
      handshakes.set(value.transportSessionId, []);
    }
    for (const value of snapshot.keys) {
      assertControlTransportKey(value);
      const entries = keys.get(value.transportSessionId);
      if (!entries || value.generation !== entries.length + 1)
        throw new E03RuntimeError(
          "control_transport_key_restore_order",
          `key ${value.keyId} generation invalid`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.handshakes) {
      assertControlTransportHandshake(value);
      const entries = handshakes.get(value.transportSessionId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "control_transport_handshake_restore_chain",
          `handshake ${value.handshakeId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const activeSessionByConnection = new Map(
      snapshot.activeSessionByConnection,
    );
    const keyBySessionGeneration = new Map(snapshot.keyBySessionGeneration);
    if (
      activeSessionByConnection.size !==
        snapshot.activeSessionByConnection.length ||
      keyBySessionGeneration.size !== snapshot.keyBySessionGeneration.length
    )
      throw new E03RuntimeError(
        "control_transport_restore_index_duplicate",
        "transport indexes duplicate",
      );
    for (const [connectionId, sessionId] of activeSessionByConnection) {
      const value = sessions.get(sessionId);
      if (
        !value ||
        value.connectionId !== connectionId ||
        ["closed", "failed"].includes(value.state)
      )
        throw new E03RuntimeError(
          "control_transport_restore_active",
          `connection ${connectionId} index invalid`,
        );
    }
    for (const [index, keyId] of keyBySessionGeneration) {
      const value = [...keys.values()]
        .flat()
        .find((entry) => entry.keyId === keyId);
      if (
        !value ||
        index !==
          this.keyGenerationKey(value.transportSessionId, value.generation)
      )
        throw new E03RuntimeError(
          "control_transport_restore_key_index",
          `key index ${index} invalid`,
        );
    }
    this.sessions = sessions;
    this.keys = keys;
    this.handshakes = handshakes;
    this.activeSessionByConnection = activeSessionByConnection;
    this.keyBySessionGeneration = keyBySessionGeneration;
  }

  private keyGenerationKey(sessionId: string, generation: number): string {
    return `${sessionId}\u0000${generation}`;
  }

  private keyEntries(sessionId: string): ControlTransportKey[] {
    return this.keys.get(sessionId) ?? [];
  }

  private handshakeEntries(sessionId: string): ControlTransportHandshake[] {
    return this.handshakes.get(sessionId) ?? [];
  }

  private requireSession(id: string): ControlTransportSession {
    const value = this.sessions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transport_session_missing",
        `session ${id} missing`,
      );
    assertControlTransportSession(value);
    return value;
  }

  private requireKey(id: string): ControlTransportKey {
    const value = [...this.keys.values()]
      .flat()
      .find((entry) => entry.keyId === id);
    if (!value)
      throw new E03RuntimeError(
        "control_transport_key_missing",
        `key ${id} missing`,
      );
    assertControlTransportKey(value);
    return value;
  }

  private assertSessionRevision(
    value: ControlTransportSession,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transport_session_stale_revision",
        `session ${value.transportSessionId} stale`,
      );
  }

  private assertKeyRevision(
    value: ControlTransportKey,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transport_key_stale_revision",
        `key ${value.keyId} stale`,
      );
  }

  private assertEstablishedAndSized(
    value: ControlTransportSession,
    byteLength: number,
  ): void {
    if (value.state !== "established")
      throw new E03RuntimeError(
        "control_transport_session_not_established",
        `session ${value.transportSessionId} not established`,
      );
    if (byteLength < 0 || byteLength > value.maximumFrameBytes)
      throw new E03RuntimeError(
        "control_transport_frame_size",
        `frame length ${byteLength} invalid`,
      );
    const key = this.requireKey(value.activeKeyId);
    if (
      key.state !== "active" ||
      Date.parse(key.expiresAt) <= Date.parse(this.clock.now())
    )
      throw new E03RuntimeError(
        "control_transport_active_key_invalid",
        `session ${value.transportSessionId} key invalid`,
      );
  }

  private transitionSession(
    value: ControlTransportSession,
    patch: Partial<
      Omit<
        ControlTransportSession,
        "transportSessionId" | "revision" | "digest"
      >
    >,
  ): ControlTransportSession {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transportSessionId: value.transportSessionId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransportSession(next);
    this.sessions.set(next.transportSessionId, next);
    return structuredClone(next);
  }

  private transitionKey(
    value: ControlTransportKey,
    patch: Partial<Omit<ControlTransportKey, "keyId" | "revision" | "digest">>,
  ): ControlTransportKey {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      keyId: value.keyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransportKey(next);
    const entries = this.keyEntries(value.transportSessionId);
    const index = entries.findIndex((entry) => entry.keyId === value.keyId);
    entries[index] = next;
    this.keys.set(value.transportSessionId, entries);
    return structuredClone(next);
  }
}

export class ControlFrameAssemblyRuntime {
  private assemblies = new Map<string, ControlFrameAssembly>();
  private frames = new Map<string, ControlFrame>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  begin(frame: ControlFrame): ControlFrameAssembly {
    assertControlFrame(frame);
    if (frame.final)
      throw new E03RuntimeError(
        "control_frame_assembly_single",
        `final frame ${frame.frameId} does not require assembly`,
      );
    if (
      [...this.assemblies.values()].some(
        (value) =>
          value.connectionId === frame.connectionId &&
          value.streamId === frame.streamId &&
          value.state === "assembling",
      )
    )
      throw new E03RuntimeError(
        "control_frame_assembly_active",
        `stream ${frame.streamId} already has an assembly`,
      );
    this.frames.set(frame.frameId, structuredClone(frame));
    const payload = {
      assemblyId: createId("control-frame-assembly"),
      connectionId: frame.connectionId,
      streamId: frame.streamId,
      startSequence: frame.sequence,
      endSequence: null,
      frameIds: [frame.frameId],
      state: "assembling" as const,
      contentType: frame.contentType,
      encoding: frame.encoding,
      payloadBytes: frame.payloadBytes,
      payloadDigest: null,
      rejectionReason: null,
      createdAt: this.clock.now(),
      completedAt: null,
      revision: 1,
    };
    const assembly = { ...payload, digest: digest(payload) };
    assertFrameAssembly(assembly);
    this.assemblies.set(assembly.assemblyId, assembly);
    return structuredClone(assembly);
  }
  append(
    assemblyId: string,
    expectedRevision: number,
    frame: ControlFrame,
  ): ControlFrameAssembly {
    const assembly = this.requireAssembly(assemblyId);
    this.assertAssemblyRevision(assembly, expectedRevision);
    assertControlFrame(frame);
    if (assembly.state !== "assembling")
      throw new E03RuntimeError(
        "control_frame_assembly_append_state",
        `control frame assembly ${assemblyId} is ${assembly.state}`,
      );
    const prior = this.frames.get(
      assembly.frameIds[assembly.frameIds.length - 1]!,
    );
    if (
      !prior ||
      frame.connectionId !== assembly.connectionId ||
      frame.streamId !== assembly.streamId ||
      frame.sequence !== prior.sequence + 1 ||
      frame.previousDigest !== prior.digest ||
      frame.contentType !== assembly.contentType ||
      frame.encoding !== assembly.encoding
    )
      return this.transitionAssembly(assembly, {
        state: "rejected",
        rejectionReason: "frame continuity mismatch",
      });
    this.frames.set(frame.frameId, structuredClone(frame));
    const frameIds = [...assembly.frameIds, frame.frameId];
    const completed = frame.final;
    const payloadDigest = completed
      ? digest(frameIds.map((id) => this.frames.get(id)!.payloadDigest))
      : null;
    return this.transitionAssembly(assembly, {
      frameIds,
      payloadBytes: assembly.payloadBytes + frame.payloadBytes,
      state: completed ? "complete" : "assembling",
      endSequence: completed ? frame.sequence : null,
      payloadDigest,
      completedAt: completed ? this.clock.now() : null,
    });
  }
  materialize(
    assemblyId: string,
    expectedRevision: number,
  ): { assembly: ControlFrameAssembly; payload: Uint8Array } {
    const assembly = this.requireAssembly(assemblyId);
    this.assertAssemblyRevision(assembly, expectedRevision);
    if (assembly.state !== "complete")
      throw new E03RuntimeError(
        "control_frame_assembly_materialize_state",
        `control frame assembly ${assemblyId} is ${assembly.state}`,
      );
    const chunks = assembly.frameIds.map((id) => {
      const frame = this.frames.get(id);
      if (!frame)
        throw new E03RuntimeError(
          "control_frame_assembly_frame_missing",
          `control frame ${id} does not exist`,
        );
      assertControlFrame(frame);
      return frame.encoding === "base64"
        ? Buffer.from(frame.payload, "base64")
        : Buffer.from(frame.payload, "utf8");
    });
    const payload = Buffer.concat(chunks);
    if (payload.byteLength !== assembly.payloadBytes)
      throw new E03RuntimeError(
        "control_frame_assembly_size",
        `control frame assembly ${assemblyId} size mismatch`,
      );
    const next = this.transitionAssembly(assembly, { state: "consumed" });
    return { assembly: next, payload: new Uint8Array(payload) };
  }
  snapshot(): { assemblies: ControlFrameAssembly[]; frames: ControlFrame[] } {
    return {
      assemblies: [...this.assemblies.values()].map((value) =>
        structuredClone(value),
      ),
      frames: [...this.frames.values()].map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    assemblies: readonly ControlFrameAssembly[];
    frames: readonly ControlFrame[];
  }): void {
    const assemblies = new Map<string, ControlFrameAssembly>();
    const frames = new Map<string, ControlFrame>();
    for (const frame of snapshot.frames) {
      assertControlFrame(frame);
      if (frames.has(frame.frameId))
        throw new E03RuntimeError(
          "control_frame_assembly_frame_duplicate",
          `duplicate control frame ${frame.frameId}`,
        );
      frames.set(frame.frameId, structuredClone(frame));
    }
    for (const value of snapshot.assemblies) {
      assertFrameAssembly(value);
      if (
        assemblies.has(value.assemblyId) ||
        value.frameIds.some((id) => !frames.has(id))
      )
        throw new E03RuntimeError(
          "control_frame_assembly_restore",
          `control frame assembly ${value.assemblyId} is invalid`,
        );
      assemblies.set(value.assemblyId, structuredClone(value));
    }
    this.assemblies = assemblies;
    this.frames = frames;
  }
  private requireAssembly(id: string): ControlFrameAssembly {
    const value = this.assemblies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_frame_assembly_missing",
        `control frame assembly ${id} does not exist`,
      );
    assertFrameAssembly(value);
    return value;
  }
  private assertAssemblyRevision(
    value: ControlFrameAssembly,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_frame_assembly_stale_revision",
        `control frame assembly ${value.assemblyId} revision is stale`,
      );
  }
  private transitionAssembly(
    value: ControlFrameAssembly,
    patch: Partial<
      Omit<ControlFrameAssembly, "assemblyId" | "revision" | "digest">
    >,
  ): ControlFrameAssembly {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      assemblyId: value.assemblyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertFrameAssembly(next);
    this.assemblies.set(next.assemblyId, next);
    return structuredClone(next);
  }
}

export interface ControlTransportCheckpoint {
  checkpointId: string;
  connectionId: string;
  streamId: string;
  generation: number;
  lastReceivedSequence: number;
  lastAcknowledgedSequence: number;
  receiveHeadDigest: string;
  acknowledgedHeadDigest: string;
  pendingFrameIds: string[];
  state: "active" | "sealed" | "superseded";
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface ControlTransportFrameReceipt {
  receiptId: string;
  checkpointId: string;
  frameId: string;
  sequence: number;
  frameDigest: string;
  outcome: "received" | "acknowledged" | "rejected";
  errorCode: string;
  previousReceiptDigest: string;
  receivedAt: string;
  revision: number;
  digest: string;
}

export interface ControlTransportReplayPlan {
  planId: string;
  checkpointId: string;
  generation: number;
  fromSequence: number;
  toSequence: number;
  frameIds: string[];
  frameDigests: string[];
  state: "planned" | "claimed" | "completed" | "failed";
  claimerId: string;
  resultDigest: string;
  errorCode: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface ControlTransportCheckpointSnapshot {
  checkpoints: ControlTransportCheckpoint[];
  frames: ControlFrame[];
  receipts: ControlTransportFrameReceipt[];
  replayPlans: ControlTransportReplayPlan[];
  activeCheckpointIdByStream: Array<[string, string]>;
  receiptIdByFrameOutcome: Array<[string, string]>;
  digest: string;
}

function assertControlTransportCheckpoint(
  value: ControlTransportCheckpoint,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.checkpointId ||
    !value.connectionId ||
    !value.streamId ||
    value.generation < 1 ||
    value.lastReceivedSequence < 0 ||
    value.lastAcknowledgedSequence < 0 ||
    value.lastAcknowledgedSequence > value.lastReceivedSequence ||
    !value.receiveHeadDigest ||
    !value.acknowledgedHeadDigest ||
    new Set(value.pendingFrameIds).size !== value.pendingFrameIds.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_checkpoint_corrupt",
      `control transport checkpoint ${value.checkpointId || "<empty>"} is corrupt`,
    );
}

function assertControlTransportFrameReceipt(
  value: ControlTransportFrameReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.checkpointId ||
    !value.frameId ||
    value.sequence < 1 ||
    !value.frameDigest ||
    !value.previousReceiptDigest ||
    (value.outcome === "rejected" && !value.errorCode) ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_frame_receipt_corrupt",
      `control transport receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

function assertControlTransportReplayPlan(
  value: ControlTransportReplayPlan,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.planId ||
    !value.checkpointId ||
    value.generation < 1 ||
    value.fromSequence < 1 ||
    value.toSequence < value.fromSequence ||
    value.frameIds.length !== value.frameDigests.length ||
    new Set(value.frameIds).size !== value.frameIds.length ||
    value.revision < 1 ||
    (value.state === "completed" && !value.resultDigest) ||
    (value.state === "failed" && !value.errorCode) ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_transport_replay_plan_corrupt",
      `control transport replay plan ${value.planId || "<empty>"} is corrupt`,
    );
}

export class ControlTransportCheckpointRuntime {
  private checkpoints = new Map<string, ControlTransportCheckpoint>();
  private frames = new Map<string, ControlFrame>();
  private receipts = new Map<string, ControlTransportFrameReceipt[]>();
  private replayPlans = new Map<string, ControlTransportReplayPlan>();
  private activeCheckpointIdByStream = new Map<string, string>();
  private receiptIdByFrameOutcome = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    connectionId: string;
    streamId: string;
    generation: number;
  }): ControlTransportCheckpoint {
    const streamKey = this.streamKey(input.connectionId, input.streamId);
    const activeId = this.activeCheckpointIdByStream.get(streamKey);
    const active = activeId ? this.checkpoints.get(activeId) : undefined;
    if (active) {
      if (active.generation === input.generation)
        return structuredClone(active);
      if (active.generation > input.generation)
        throw new E03RuntimeError(
          "control_transport_checkpoint_generation_stale",
          "control transport checkpoint generation is stale",
        );
      this.transitionCheckpoint(active, {
        state: "superseded",
        updatedAt: this.clock.now(),
      });
    }
    if (!input.connectionId || !input.streamId || input.generation < 1)
      throw new E03RuntimeError(
        "control_transport_checkpoint_input_invalid",
        "control transport checkpoint input is invalid",
      );
    const now = this.clock.now();
    const root = digest(`control-transport:${streamKey}:${input.generation}`);
    const payload = {
      checkpointId: createId("control-transport-checkpoint"),
      connectionId: input.connectionId,
      streamId: input.streamId,
      generation: input.generation,
      lastReceivedSequence: 0,
      lastAcknowledgedSequence: 0,
      receiveHeadDigest: root,
      acknowledgedHeadDigest: root,
      pendingFrameIds: [] as string[],
      state: "active" as const,
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const checkpoint = { ...payload, digest: digest(payload) };
    assertControlTransportCheckpoint(checkpoint);
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    this.receipts.set(checkpoint.checkpointId, []);
    this.activeCheckpointIdByStream.set(streamKey, checkpoint.checkpointId);
    return structuredClone(checkpoint);
  }

  receive(input: {
    checkpointId: string;
    expectedRevision: number;
    generation: number;
    frame: ControlFrame;
  }): {
    checkpoint: ControlTransportCheckpoint;
    receipt: ControlTransportFrameReceipt;
  } {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    this.assertCheckpointRevision(checkpoint, input.expectedRevision);
    assertControlFrame(input.frame);
    if (
      checkpoint.state !== "active" ||
      checkpoint.generation !== input.generation
    )
      throw new E03RuntimeError(
        "control_transport_receive_checkpoint_inactive",
        "control transport checkpoint is inactive or stale",
      );
    if (
      input.frame.connectionId !== checkpoint.connectionId ||
      input.frame.streamId !== checkpoint.streamId
    )
      throw new E03RuntimeError(
        "control_transport_receive_stream_mismatch",
        "control frame does not belong to checkpoint stream",
      );
    const existing = this.frames.get(input.frame.frameId);
    if (existing) {
      if (existing.digest !== input.frame.digest)
        throw new E03RuntimeError(
          "control_transport_receive_frame_conflict",
          `control frame ${input.frame.frameId} conflicts`,
        );
      const receipt = this.findReceipt(
        checkpoint.checkpointId,
        input.frame.frameId,
        "received",
      );
      if (!receipt)
        throw new E03RuntimeError(
          "control_transport_receive_receipt_missing",
          "received control frame lacks receipt",
        );
      return { checkpoint: structuredClone(checkpoint), receipt };
    }
    if (
      input.frame.sequence !== checkpoint.lastReceivedSequence + 1 ||
      input.frame.previousDigest !== checkpoint.receiveHeadDigest
    )
      throw new E03RuntimeError(
        "control_transport_receive_sequence_gap",
        "control frame sequence or digest chain has a gap",
      );
    this.frames.set(input.frame.frameId, structuredClone(input.frame));
    const receipt = this.recordReceipt(checkpoint, input.frame, "received", "");
    const next = this.transitionCheckpoint(checkpoint, {
      lastReceivedSequence: input.frame.sequence,
      receiveHeadDigest: input.frame.digest,
      pendingFrameIds: [...checkpoint.pendingFrameIds, input.frame.frameId],
      updatedAt: this.clock.now(),
    });
    return { checkpoint: next, receipt };
  }

  acknowledge(input: {
    checkpointId: string;
    expectedRevision: number;
    frameId: string;
    sequence: number;
    frameDigest: string;
  }): {
    checkpoint: ControlTransportCheckpoint;
    receipt: ControlTransportFrameReceipt;
  } {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    this.assertCheckpointRevision(checkpoint, input.expectedRevision);
    const frame = this.frames.get(input.frameId);
    if (
      !frame ||
      frame.sequence !== input.sequence ||
      frame.digest !== input.frameDigest
    )
      throw new E03RuntimeError(
        "control_transport_ack_frame_mismatch",
        "control transport acknowledgement frame does not match",
      );
    if (!checkpoint.pendingFrameIds.includes(frame.frameId)) {
      const receipt = this.findReceipt(
        checkpoint.checkpointId,
        frame.frameId,
        "acknowledged",
      );
      if (!receipt)
        throw new E03RuntimeError(
          "control_transport_ack_receipt_missing",
          "acknowledged control frame lacks receipt",
        );
      return { checkpoint: structuredClone(checkpoint), receipt };
    }
    if (frame.sequence !== checkpoint.lastAcknowledgedSequence + 1)
      throw new E03RuntimeError(
        "control_transport_ack_sequence_gap",
        "control transport acknowledgements must be contiguous",
      );
    const receipt = this.recordReceipt(checkpoint, frame, "acknowledged", "");
    const next = this.transitionCheckpoint(checkpoint, {
      lastAcknowledgedSequence: frame.sequence,
      acknowledgedHeadDigest: frame.digest,
      pendingFrameIds: checkpoint.pendingFrameIds.filter(
        (id) => id !== frame.frameId,
      ),
      updatedAt: this.clock.now(),
    });
    return { checkpoint: next, receipt };
  }

  planReplay(input: {
    checkpointId: string;
    expectedRevision: number;
    generation: number;
  }): ControlTransportReplayPlan {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    this.assertCheckpointRevision(checkpoint, input.expectedRevision);
    if (
      checkpoint.state !== "active" ||
      checkpoint.generation !== input.generation
    )
      throw new E03RuntimeError(
        "control_transport_replay_checkpoint_stale",
        "control transport replay checkpoint is stale",
      );
    const frames = checkpoint.pendingFrameIds
      .map((id) => this.frames.get(id))
      .filter((value): value is ControlFrame => Boolean(value))
      .sort((left, right) => left.sequence - right.sequence);
    if (!frames.length)
      throw new E03RuntimeError(
        "control_transport_replay_empty",
        "control transport checkpoint has no pending frames",
      );
    const payload = {
      planId: createId("control-transport-replay"),
      checkpointId: checkpoint.checkpointId,
      generation: checkpoint.generation,
      fromSequence: frames[0]!.sequence,
      toSequence: frames.at(-1)!.sequence,
      frameIds: frames.map((value) => value.frameId),
      frameDigests: frames.map((value) => value.digest),
      state: "planned" as const,
      claimerId: "",
      resultDigest: "",
      errorCode: "",
      createdAt: this.clock.now(),
      updatedAt: this.clock.now(),
      revision: 1,
    };
    const plan = { ...payload, digest: digest(payload) };
    assertControlTransportReplayPlan(plan);
    this.replayPlans.set(plan.planId, plan);
    return structuredClone(plan);
  }

  claimReplay(input: {
    planId: string;
    expectedRevision: number;
    claimerId: string;
  }): ControlTransportReplayPlan {
    const plan = this.requireReplayPlan(input.planId);
    this.assertReplayPlanRevision(plan, input.expectedRevision);
    if (plan.state !== "planned" && plan.state !== "failed")
      throw new E03RuntimeError(
        "control_transport_replay_claim_invalid_state",
        `cannot claim control transport replay from ${plan.state}`,
      );
    if (!input.claimerId)
      throw new E03RuntimeError(
        "control_transport_replay_claimer_required",
        "control transport replay claimer is required",
      );
    return this.transitionReplayPlan(plan, {
      state: "claimed",
      claimerId: input.claimerId,
      errorCode: "",
      updatedAt: this.clock.now(),
    });
  }

  settleReplay(input: {
    planId: string;
    expectedRevision: number;
    claimerId: string;
    accepted: boolean;
    resultDigest?: string;
    errorCode?: string;
  }): ControlTransportReplayPlan {
    const plan = this.requireReplayPlan(input.planId);
    this.assertReplayPlanRevision(plan, input.expectedRevision);
    if (plan.state !== "claimed" || plan.claimerId !== input.claimerId)
      throw new E03RuntimeError(
        "control_transport_replay_settle_not_claimed",
        "control transport replay is not claimed by executor",
      );
    return this.transitionReplayPlan(plan, {
      state: input.accepted ? "completed" : "failed",
      resultDigest: input.resultDigest ?? "",
      errorCode: input.errorCode ?? "",
      updatedAt: this.clock.now(),
    });
  }

  seal(input: {
    checkpointId: string;
    expectedRevision: number;
  }): ControlTransportCheckpoint {
    const checkpoint = this.requireCheckpoint(input.checkpointId);
    this.assertCheckpointRevision(checkpoint, input.expectedRevision);
    if (checkpoint.pendingFrameIds.length)
      throw new E03RuntimeError(
        "control_transport_checkpoint_pending_frames",
        "control transport checkpoint cannot seal with pending frames",
      );
    const next = this.transitionCheckpoint(checkpoint, {
      state: "sealed",
      updatedAt: this.clock.now(),
    });
    this.activeCheckpointIdByStream.delete(
      this.streamKey(checkpoint.connectionId, checkpoint.streamId),
    );
    return next;
  }

  snapshot(): ControlTransportCheckpointSnapshot {
    const payload = {
      checkpoints: [...this.checkpoints.values()].map((value) =>
        structuredClone(value),
      ),
      frames: [...this.frames.values()].map((value) => structuredClone(value)),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      replayPlans: [...this.replayPlans.values()].map((value) =>
        structuredClone(value),
      ),
      activeCheckpointIdByStream: [
        ...this.activeCheckpointIdByStream.entries(),
      ],
      receiptIdByFrameOutcome: [...this.receiptIdByFrameOutcome.entries()],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: ControlTransportCheckpointSnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "control_transport_checkpoint_snapshot_corrupt",
        "control transport checkpoint snapshot digest mismatch",
      );
    const checkpoints = new Map<string, ControlTransportCheckpoint>();
    const frames = new Map<string, ControlFrame>();
    const receipts = new Map<string, ControlTransportFrameReceipt[]>();
    const replayPlans = new Map<string, ControlTransportReplayPlan>();
    for (const value of payload.checkpoints) {
      assertControlTransportCheckpoint(value);
      if (checkpoints.has(value.checkpointId))
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_duplicate",
          `duplicate control transport checkpoint ${value.checkpointId}`,
        );
      checkpoints.set(value.checkpointId, structuredClone(value));
      receipts.set(value.checkpointId, []);
    }
    for (const value of payload.frames) {
      assertControlFrame(value);
      frames.set(value.frameId, structuredClone(value));
    }
    for (const value of payload.receipts) {
      assertControlTransportFrameReceipt(value);
      const list = receipts.get(value.checkpointId);
      if (!list)
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_receipt_orphaned",
          `control transport receipt ${value.receiptId} is orphaned`,
        );
      if (
        list.length > 0 &&
        value.previousReceiptDigest !== list.at(-1)!.digest
      )
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_receipt_chain_corrupt",
          `control transport receipt ${value.receiptId} breaks chain`,
        );
      list.push(structuredClone(value));
    }
    for (const value of payload.replayPlans) {
      assertControlTransportReplayPlan(value);
      if (!checkpoints.has(value.checkpointId))
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_replay_orphaned",
          `control transport replay ${value.planId} is orphaned`,
        );
      replayPlans.set(value.planId, structuredClone(value));
    }
    const activeIndex = new Map(payload.activeCheckpointIdByStream);
    for (const [streamKey, checkpointId] of activeIndex) {
      const value = checkpoints.get(checkpointId);
      if (
        !value ||
        this.streamKey(value.connectionId, value.streamId) !== streamKey ||
        value.state !== "active"
      )
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_active_index_corrupt",
          `control transport active checkpoint ${checkpointId} is invalid`,
        );
    }
    const receiptIndex = new Map(payload.receiptIdByFrameOutcome);
    const allReceiptIds = new Set(
      [...receipts.values()].flat().map((value) => value.receiptId),
    );
    for (const receiptId of receiptIndex.values())
      if (!allReceiptIds.has(receiptId))
        throw new E03RuntimeError(
          "control_transport_checkpoint_snapshot_receipt_index_corrupt",
          `control transport receipt index references ${receiptId}`,
        );
    this.checkpoints = checkpoints;
    this.frames = frames;
    this.receipts = receipts;
    this.replayPlans = replayPlans;
    this.activeCheckpointIdByStream = activeIndex;
    this.receiptIdByFrameOutcome = receiptIndex;
  }

  private recordReceipt(
    checkpoint: ControlTransportCheckpoint,
    frame: ControlFrame,
    outcome: ControlTransportFrameReceipt["outcome"],
    errorCode: string,
  ): ControlTransportFrameReceipt {
    const list = this.receipts.get(checkpoint.checkpointId) ?? [];
    const payload = {
      receiptId: createId("control-transport-frame-receipt"),
      checkpointId: checkpoint.checkpointId,
      frameId: frame.frameId,
      sequence: frame.sequence,
      frameDigest: frame.digest,
      outcome,
      errorCode,
      previousReceiptDigest:
        list.at(-1)?.digest ?? digest("control-transport-frame-receipt-root"),
      receivedAt: this.clock.now(),
      revision: list.length + 1,
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertControlTransportFrameReceipt(receipt);
    this.receipts.set(checkpoint.checkpointId, [...list, receipt]);
    this.receiptIdByFrameOutcome.set(
      `${checkpoint.checkpointId}:${frame.frameId}:${outcome}`,
      receipt.receiptId,
    );
    return structuredClone(receipt);
  }

  private findReceipt(
    checkpointId: string,
    frameId: string,
    outcome: ControlTransportFrameReceipt["outcome"],
  ): ControlTransportFrameReceipt | undefined {
    const receiptId = this.receiptIdByFrameOutcome.get(
      `${checkpointId}:${frameId}:${outcome}`,
    );
    const receipt = (this.receipts.get(checkpointId) ?? []).find(
      (value) => value.receiptId === receiptId,
    );
    return receipt ? structuredClone(receipt) : undefined;
  }

  private streamKey(connectionId: string, streamId: string): string {
    return `${connectionId}:${streamId}`;
  }

  private requireCheckpoint(id: string): ControlTransportCheckpoint {
    const value = this.checkpoints.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transport_checkpoint_missing",
        `control transport checkpoint ${id} does not exist`,
      );
    assertControlTransportCheckpoint(value);
    return value;
  }

  private requireReplayPlan(id: string): ControlTransportReplayPlan {
    const value = this.replayPlans.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_transport_replay_plan_missing",
        `control transport replay plan ${id} does not exist`,
      );
    assertControlTransportReplayPlan(value);
    return value;
  }

  private assertCheckpointRevision(
    value: ControlTransportCheckpoint,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transport_checkpoint_stale_revision",
        `control transport checkpoint ${value.checkpointId} revision is stale`,
      );
  }

  private assertReplayPlanRevision(
    value: ControlTransportReplayPlan,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_transport_replay_plan_stale_revision",
        `control transport replay plan ${value.planId} revision is stale`,
      );
  }

  private transitionCheckpoint(
    value: ControlTransportCheckpoint,
    patch: Partial<
      Omit<ControlTransportCheckpoint, "checkpointId" | "revision" | "digest">
    >,
  ): ControlTransportCheckpoint {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      checkpointId: value.checkpointId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransportCheckpoint(next);
    this.checkpoints.set(next.checkpointId, next);
    return structuredClone(next);
  }

  private transitionReplayPlan(
    value: ControlTransportReplayPlan,
    patch: Partial<
      Omit<ControlTransportReplayPlan, "planId" | "revision" | "digest">
    >,
  ): ControlTransportReplayPlan {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      planId: value.planId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlTransportReplayPlan(next);
    this.replayPlans.set(next.planId, next);
    return structuredClone(next);
  }
}
