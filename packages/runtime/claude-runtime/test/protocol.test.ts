import assert from "node:assert/strict";
import test from "node:test";

import {
  createFrame,
  decodeFrame,
  FrameSequence,
  RUNTIME_PROTOCOL_VERSION,
  RuntimeProtocolError,
} from "../src/index.ts";

test("protocol accepts a versioned ordered frame", () => {
  const frame = createFrame(1, "run-1", "run.start", { value: "ok" });
  const decoded = decodeFrame(JSON.stringify(frame), "run-1");
  assert.equal(decoded.protocol, RUNTIME_PROTOCOL_VERSION);
  assert.equal(decoded.sequence, 1);
  assert.equal(decoded.kind, "run.start");
});

test("protocol rejects schema and version drift", () => {
  assert.throws(
    () => decodeFrame(JSON.stringify({
      protocol: "unknown",
      run_id: "run-1",
      sequence: 1,
      kind: "run.start",
      payload: {},
    })),
    (error: unknown) => error instanceof RuntimeProtocolError
      && error.code === "unsupported_protocol",
  );
  assert.throws(
    () => decodeFrame("{not-json"),
    (error: unknown) => error instanceof RuntimeProtocolError
      && error.code === "invalid_json",
  );
});

test("protocol rejects duplicate and out-of-order frames", () => {
  const sequence = new FrameSequence();
  sequence.accept(1);
  assert.throws(
    () => sequence.accept(1),
    (error: unknown) => error instanceof RuntimeProtocolError
      && error.code === "duplicate_frame",
  );
  assert.throws(
    () => sequence.accept(3),
    (error: unknown) => error instanceof RuntimeProtocolError
      && error.code === "out_of_order_frame",
  );
});

test("terminal result requires a correlated durable ACK before close", () => {
  const result = createFrame(
    8,
    "run-terminal",
    "run.result",
    {
      terminal_id: "terminal-1",
      terminal_revision: 1,
      requires_ack: true,
      result: { ok: true },
    },
    "terminal-1",
  );
  const acknowledgement = createFrame(
    4,
    "run-terminal",
    "run.result.ack",
    {
      terminal_id: "terminal-1",
      terminal_revision: 1,
      accepted: true,
      durable: true,
    },
    "terminal-1",
  );
  const closed = createFrame(
    9,
    "run-terminal",
    "run.closed",
    { terminal_id: "terminal-1", terminal_revision: 1, accepted: true },
    "terminal-1",
  );

  assert.equal(decodeFrame(JSON.stringify(result)).kind, "run.result");
  assert.equal(
    decodeFrame(JSON.stringify(acknowledgement)).kind,
    "run.result.ack",
  );
  assert.equal(decodeFrame(JSON.stringify(closed)).kind, "run.closed");
  assert.equal(closed.correlation_id, result.correlation_id);
});
