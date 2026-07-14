import assert from "node:assert/strict";
import test from "node:test";

import {
  COMMAND_SCHEMA,
  GatewayProtocolError,
  assertCommandEnvelope,
  canonicalJson,
  canonicalLogicalPath,
  commandDigest,
  createCommandEnvelope,
  requestFingerprint,
} from "../src/index.ts";

test("command envelope is canonical and stable", () => {
  const first = createCommandEnvelope({
    sessionId: "session-contract",
    runId: "run-contract",
    taskId: "task-contract",
    workerId: "CodeWorkerRuntime",
    toolUseId: "tool-contract",
    executable: "rg",
    argv: ["needle", "."],
  });
  const second = createCommandEnvelope({
    sessionId: "session-contract",
    runId: "run-contract",
    taskId: "task-contract",
    workerId: "CodeWorkerRuntime",
    toolUseId: "tool-contract",
    executable: "rg",
    argv: ["needle", "."],
  });

  assert.equal(first.schema, COMMAND_SCHEMA);
  assert.equal(first.commandId, second.commandId);
  assert.equal(commandDigest(first), commandDigest(second));
  assert.equal(requestFingerprint(first), requestFingerprint(second));
  assert.equal(assertCommandEnvelope(first), first);
});

test("canonical JSON sorts keys and rejects non-finite values", () => {
  assert.equal(canonicalJson({ z: 1, a: 2 }), '{"a":2,"z":1}');
  assert.throws(
    () => canonicalJson({ value: Number.NaN }),
    GatewayProtocolError,
  );
});

test("logical paths reject traversal, UNC, drive, and device names", () => {
  for (const value of [
    "../outside.txt",
    "\\\\server\\share\\payload",
    "C:\\outside.txt",
    "nested/NUL",
  ]) {
    assert.throws(
      () => canonicalLogicalPath(value),
      GatewayProtocolError,
      value,
    );
  }
  assert.equal(
    canonicalLogicalPath("nested\\safe.txt"),
    "nested/safe.txt",
  );
});
