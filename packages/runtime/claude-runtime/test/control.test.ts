import assert from "node:assert/strict";
import test from "node:test";

import { TypeScriptControlRuntime } from "../src/index.ts";

test("control runtime applies idempotent revisioned mutations", () => {
  const runtime = new TypeScriptControlRuntime("model-a");
  const compact = runtime.apply({
    name: "compact",
    request_id: "control-1",
    expected_revision: 0,
  });
  assert.equal(compact.accepted, true);
  assert.equal(compact.changed, true);
  assert.equal(compact.revisionAfter, 1);
  assert.deepEqual(runtime.apply({
    name: "compact",
    request_id: "control-1",
    expected_revision: 0,
  }), compact);
  const conflict = runtime.apply({
    name: "model",
    model: "model-b",
    request_id: "control-2",
    expected_revision: 0,
  });
  assert.equal(conflict.status, "conflict");
  assert.equal(runtime.modelName, "model-a");
});

test("control runtime mutates model, compact and cancel state", () => {
  const runtime = new TypeScriptControlRuntime("model-a");
  assert.equal(runtime.apply({ name: "model", model: "model-b" }).accepted, true);
  assert.equal(runtime.modelName, "model-b");
  assert.equal(runtime.apply({ name: "compact" }).accepted, true);
  assert.equal(runtime.compactRequested, true);
  assert.equal(runtime.apply({ name: "cancel" }).accepted, true);
  assert.equal(runtime.cancelled, true);
  assert.equal(runtime.snapshot().canonical_owner, "typescript");
  assert.equal(runtime.snapshot().python_control_fallback, false);
});

test("control snapshot rejects tampering before exact restore", () => {
  const runtime = new TypeScriptControlRuntime("model-a");
  runtime.apply({ name: "model", model: "model-b", request_id: "model-change" });
  const snapshot = runtime.snapshot();
  assert.equal(typeof snapshot.checksum, "string");
  assert.throws(
    () => new TypeScriptControlRuntime("model-a", { ...snapshot, model_name: "tampered" }),
    /checksum mismatch/,
  );
  const restored = new TypeScriptControlRuntime("model-a", snapshot);
  assert.equal(restored.modelName, "model-b");
});
