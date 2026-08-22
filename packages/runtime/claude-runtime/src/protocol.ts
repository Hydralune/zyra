import {
  asObject,
  asString,
  type JsonObject,
  type JsonValue,
} from "./contracts.ts";

export const RUNTIME_PROTOCOL_VERSION = "zyra.claude-runtime.v1";

export type RuntimeFrameKind =
  | "run.start"
  | "run.accepted"
  | "runtime.event"
  | "runtime.checkpoint"
  | "runtime.checkpoint.result"
  | "completion.check.request"
  | "completion.check.result"
  | "tool.request"
  | "tool.result"
  | "tool.batch.request"
  | "tool.batch.result"
  | "tool.settle"
  | "tool.settle.result"
  | "agent.mutate"
  | "agent.mutate.result"
  | "artifact.request"
  | "artifact.result"
  | "run.result"
  | "run.result.ack"
  | "run.closed"
  | "runtime.error";

export interface RuntimeFrame {
  protocol: typeof RUNTIME_PROTOCOL_VERSION;
  run_id: string;
  sequence: number;
  kind: RuntimeFrameKind;
  correlation_id: string;
  payload: JsonObject;
}

export class RuntimeProtocolError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "RuntimeProtocolError";
    this.code = code;
  }
}

const FRAME_KINDS = new Set<RuntimeFrameKind>([
  "run.start",
  "run.accepted",
  "runtime.event",
  "runtime.checkpoint",
  "runtime.checkpoint.result",
  "completion.check.request",
  "completion.check.result",
  "tool.request",
  "tool.result",
  "tool.batch.request",
  "tool.batch.result",
  "tool.settle",
  "tool.settle.result",
  "agent.mutate",
  "agent.mutate.result",
  "artifact.request",
  "artifact.result",
  "run.result",
  "run.result.ack",
  "run.closed",
  "runtime.error",
]);

export class FrameSequence {
  private nextValue = 1;

  next(): number {
    const selected = this.nextValue;
    this.nextValue += 1;
    return selected;
  }

  accept(sequence: number): void {
    if (!Number.isInteger(sequence) || sequence < 1) {
      throw new RuntimeProtocolError("invalid_sequence", "frame sequence must be a positive integer");
    }
    if (sequence < this.nextValue) {
      throw new RuntimeProtocolError(
        "duplicate_frame",
        "received duplicate frame sequence " + String(sequence),
      );
    }
    if (sequence > this.nextValue) {
      throw new RuntimeProtocolError(
        "out_of_order_frame",
        "expected frame sequence " + String(this.nextValue) + " but received " + String(sequence),
      );
    }
    this.nextValue += 1;
  }
}

export function createFrame(
  sequence: number,
  runId: string,
  kind: RuntimeFrameKind,
  payload: JsonObject,
  correlationId = "",
): RuntimeFrame {
  if (!runId) {
    throw new RuntimeProtocolError("missing_run_id", "runtime frame requires run_id");
  }
  return {
    protocol: RUNTIME_PROTOCOL_VERSION,
    run_id: runId,
    sequence,
    kind,
    correlation_id: correlationId,
    payload,
  };
}

export function decodeFrame(line: string, expectedRunId?: string): RuntimeFrame {
  let decoded: JsonValue;
  try {
    decoded = JSON.parse(line) as JsonValue;
  } catch (error) {
    throw new RuntimeProtocolError(
      "invalid_json",
      "runtime frame is not valid JSON: " + String(error),
    );
  }
  const record = asObject(decoded);
  if (record.protocol !== RUNTIME_PROTOCOL_VERSION) {
    throw new RuntimeProtocolError(
      "unsupported_protocol",
      "expected protocol " + RUNTIME_PROTOCOL_VERSION,
    );
  }
  const runId = asString(record.run_id);
  if (!runId) {
    throw new RuntimeProtocolError("missing_run_id", "runtime frame requires run_id");
  }
  if (expectedRunId && runId !== expectedRunId) {
    throw new RuntimeProtocolError("run_id_mismatch", "runtime frame belongs to another run");
  }
  const sequence = record.sequence;
  if (typeof sequence !== "number" || !Number.isInteger(sequence)) {
    throw new RuntimeProtocolError("invalid_sequence", "runtime frame sequence is invalid");
  }
  const kind = asString(record.kind) as RuntimeFrameKind;
  if (!FRAME_KINDS.has(kind)) {
    throw new RuntimeProtocolError("unknown_frame_kind", "unknown runtime frame kind " + kind);
  }
  return {
    protocol: RUNTIME_PROTOCOL_VERSION,
    run_id: runId,
    sequence,
    kind,
    correlation_id: asString(record.correlation_id),
    payload: asObject(record.payload),
  };
}
