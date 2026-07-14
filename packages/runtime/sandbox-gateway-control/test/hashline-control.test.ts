import assert from "node:assert/strict";
import test from "node:test";

import {
  CallbackCredentialResolver,
  CommandDeadline,
  CredentialEnvelopeRelay,
  GatewayProtocolError,
  HashlinePatcher,
  JsonLineDecoder,
  SessionActorQueue,
  createGatewayControlRouter,
  createRpcRequest,
  redactSecrets,
} from "../src/index.ts";

test("Hashline edit is snapshot-bound and rejects stale content", () => {
  const patcher = new HashlinePatcher();
  const content = "alpha\nbeta\ngamma\n";
  const snapshot = patcher.snapshot(content);
  const edit = patcher.createEdit(
    snapshot,
    patcher.range(snapshot, 2, 2),
    "changed",
  );
  const result = patcher.apply(content, edit);

  assert.equal(result.content, "alpha\nchanged\ngamma\n");
  assert.equal(result.changed, true);
  assert.throws(
    () => patcher.apply("alpha\nconcurrent\ngamma\n", edit),
    (error: unknown) =>
      error instanceof GatewayProtocolError &&
      error.code === "hashline_snapshot_mismatch",
  );
});

test("Hashline preflights all ranges and rejects overlap", () => {
  const patcher = new HashlinePatcher();
  const content = "one\ntwo\nthree\nfour\n";
  const snapshot = patcher.snapshot(content);
  const first = patcher.createEdit(
    snapshot,
    patcher.range(snapshot, 1, 2),
    "first",
  );
  const overlap = patcher.createEdit(
    snapshot,
    patcher.range(snapshot, 2, 3),
    "overlap",
  );

  assert.throws(
    () => patcher.prepareMany(content, [first, overlap]),
    (error: unknown) =>
      error instanceof GatewayProtocolError &&
      error.code === "hashline_overlap",
  );
});

test("credential envelope is audience-bound, secret-free, and one-use", async () => {
  const secret = "credential-secret-value";
  const relay = new CredentialEnvelopeRelay(
    new CallbackCredentialResolver(() => secret),
  );
  const envelope = await relay.issue({
    requestId: "credential-request",
    sessionId: "session-control",
    commandId: "command-control",
    provider: "test",
    credentialName: "api",
    audience: "network-client",
    scope: ["read"],
    ttlMilliseconds: 60_000,
  });

  assert.doesNotMatch(JSON.stringify(envelope), /credential-secret-value/u);
  assert.throws(
    () =>
      relay.consume(envelope.envelopeId, {
        sessionId: "session-control",
        commandId: "command-control",
        audience: "wrong-audience",
      }),
    GatewayProtocolError,
  );
  assert.equal(
    relay.consume(envelope.envelopeId, {
      sessionId: "session-control",
      commandId: "command-control",
      audience: "network-client",
      requiredScope: "read",
    }),
    secret,
  );
  assert.throws(
    () =>
      relay.consume(envelope.envelopeId, {
        sessionId: "session-control",
        commandId: "command-control",
        audience: "network-client",
      }),
    GatewayProtocolError,
  );
});

test("redaction removes nested secret fields and values", () => {
  const secret = "sk-live-abcdefghijklmnopqrstuvwxyz";
  const projected = redactSecrets(
    {
      authorization: "Bearer " + secret,
      nested: { message: "token=" + secret, apiToken: secret },
    },
    [secret],
  );

  assert.doesNotMatch(JSON.stringify(projected), new RegExp(secret, "u"));
  assert.match(JSON.stringify(projected), /\[REDACTED\]/u);
});

test("session queue serializes one session while another can progress", async () => {
  const queue = new SessionActorQueue();
  const order: string[] = [];
  let release: () => void = () => undefined;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  const first = queue.run("same", async () => {
    order.push("first-start");
    await blocked;
    order.push("first-end");
  });
  const second = queue.run("same", () => {
    order.push("second");
  });
  await queue.run("other", () => {
    order.push("other");
  });
  release();
  await Promise.all([first, second]);

  assert.ok(order.indexOf("other") < order.indexOf("first-end"));
  assert.ok(order.indexOf("second") > order.indexOf("first-end"));
});

test("deadline propagates cancellation and cleans parent listener", async () => {
  const parent = new AbortController();
  const deadline = new CommandDeadline({
    timeoutMilliseconds: 60_000,
    parentSignal: parent.signal,
  });
  parent.abort(new Error("cancel test"));

  assert.equal(deadline.aborted, true);
  await assert.rejects(
    () => deadline.race(new Promise(() => undefined)),
    /cancel test/u,
  );
  deadline.dispose();
});

test("typed RPC reaches policy and Hashline without dynamic imports", async () => {
  const router = createGatewayControlRouter();
  const describe = await router.handle(
    createRpcRequest("control.describe", {}),
    { metadata: {} },
  );
  const snapshot = await router.handle(
    createRpcRequest("hashline.snapshot", { content: "one\ntwo\n" }),
    { metadata: {} },
  );

  assert.equal(describe.ok, true);
  assert.equal(snapshot.ok, true);
  assert.equal(
    (router.descriptor() as Record<string, unknown>).dynamicImports,
    false,
  );
  const decoder = new JsonLineDecoder();
  assert.deepEqual(decoder.push('{"one":1}\n{"two":2}\n'), [
    '{"one":1}',
    '{"two":2}',
  ]);
});
