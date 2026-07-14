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
