import assert from "node:assert/strict";
import { test } from "node:test";
import {
  digest,
  E03RuntimeError,
  type ControlCommand,
  type E03ControlEnvelope,
} from "../../src/e03/contracts.ts";
import type { JsonObject } from "../../src/contracts.ts";
import { runtimeContract } from "../../src/stdio.ts";
import {
  ControlBodySchemaRegistry,
  ControlCommandCatalog,
  ControlProtocolNegotiator,
  ControlSchema,
  type ControlBodySchema,
  type ControlFieldConstraint,
  type ControlProtocolCapability,
  type ControlProtocolPeer,
} from "../../src/control/schema.ts";
import {
  ControlFrameCodec,
  type ControlFrame,
} from "../../src/control/stdio.ts";

function assertRuntimeCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.length > 0);
  return true;
}

test("e03 built health contract exposes canonical control readiness", () => {
  const health = runtimeContract("health");
  const readiness = health.e03AgentControlRuntime;
  assert.ok(
    readiness && typeof readiness === "object" && !Array.isArray(readiness),
  );
  const projection = readiness as JsonObject;
  assert.equal(projection.implementationReady, true);
  assert.equal(
    projection.canonicalEntrypoint,
    "E03AgentControlCoordinator.execute",
  );
  assert.equal(
    projection.defaultTaskEntrypoint,
    "CodeWorkerApplication.runTaskRuntime",
  );
  assert.equal(projection.stateJournalOwner, "DurableTaskRegistry");
  assert.equal(projection.pythonLogicalOwner, false);
  assert.equal(projection.pythonLogicalFallback, false);
});

function envelope(
  command: ControlCommand,
  body: JsonObject,
  overrides: Partial<E03ControlEnvelope> = {},
): E03ControlEnvelope {
  return {
    schema_version: "3.0",
    request_id: overrides.request_id ?? `request-${command}`,
    idempotency_key: overrides.idempotency_key ?? `key-${command}`,
    run_id: overrides.run_id ?? "run-control-e03",
    session_id: overrides.session_id ?? "session-control-e03",
    parent_task_id: overrides.parent_task_id ?? "parent-control-e03",
    expected_revision: overrides.expected_revision ?? 4,
    command,
    body,
    ...(overrides.simulate_lost_ack === undefined
      ? {}
      : { simulate_lost_ack: overrides.simulate_lost_ack }),
  };
}

function field(
  name: string,
  type: ControlFieldConstraint["type"],
  overrides: Partial<
    Omit<ControlFieldConstraint, "field" | "type" | "digest">
  > = {},
): ControlFieldConstraint {
  const payload = {
    field: name,
    type,
    required: overrides.required ?? false,
    nullable: overrides.nullable ?? false,
    minimum: overrides.minimum ?? null,
    maximum: overrides.maximum ?? null,
    minimumLength: overrides.minimumLength ?? null,
    maximumLength: overrides.maximumLength ?? null,
    pattern: overrides.pattern ?? null,
    enumValues: overrides.enumValues ?? [],
    itemType: overrides.itemType ?? null,
    uniqueItems: overrides.uniqueItems ?? false,
    sensitive: overrides.sensitive ?? false,
    mutable: overrides.mutable ?? true,
    defaultValue: overrides.defaultValue,
    description: overrides.description ?? `${name} contract`,
  };
  return { ...payload, digest: digest(payload) };
}

function customBodyRegistry(
  label: string,
  fields: ControlFieldConstraint[],
  options: {
    allowUnknown?: boolean;
    maximumProperties?: number;
    maximumDepth?: number;
  } = {},
): { runtime: ControlBodySchemaRegistry; schema: ControlBodySchema } {
  const runtime = new ControlBodySchemaRegistry();
  const schema = runtime.register({
    schemaId: `schema-${label}`,
    command: "agent.steer",
    version: `4.${label.length}`,
    fields,
    allowUnknown: options.allowUnknown ?? false,
    maximumProperties:
      options.maximumProperties ?? Math.max(8, fields.length + 2),
    maximumDepth: options.maximumDepth ?? 8,
    mutation: "logical-write",
    owner: "typescript.E03AgentControlCoordinator",
    createdAt: "2026-07-18T16:00:00.000Z",
  });
  return { runtime, schema };
}

function capability(
  name: string,
  options: { minimum?: string; maximum?: string; required?: boolean } = {},
): ControlProtocolCapability {
  const payload = {
    capability: name,
    minimumVersion: options.minimum ?? "3.0",
    maximumVersion: options.maximum ?? "3.2",
    required: options.required ?? true,
  };
  return { ...payload, digest: digest(payload) };
}

function peerInput(
  peerId: string,
  overrides: Partial<
    Omit<
      ControlProtocolPeer,
      "registeredAt" | "expiresAt" | "revision" | "digest"
    >
  > & { ttlMs?: number; now?: string } = {},
) {
  return {
    peerId,
    runtime: overrides.runtime ?? "zyra-control",
    runtimeVersion: overrides.runtimeVersion ?? "1.0.0",
    protocolVersions: overrides.protocolVersions ?? ["3.0", "3.1"],
    commands: overrides.commands ?? [
      "agent.create",
      "agent.status",
      "agent.cancel",
    ],
    capabilities: overrides.capabilities ?? [
      capability("revision-fence"),
      capability("physical-receipt"),
      capability("lost-ack-recovery", { required: false }),
    ],
    maximumFrameBytes: overrides.maximumFrameBytes ?? 1024 * 1024,
    supportsCompression: overrides.supportsCompression ?? true,
    supportsStreaming: overrides.supportsStreaming ?? true,
    supportsLostAckRecovery: overrides.supportsLostAckRecovery ?? true,
    supportsRevisionFence: overrides.supportsRevisionFence ?? true,
    supportsPhysicalReceipt: overrides.supportsPhysicalReceipt ?? true,
    ttlMs: overrides.ttlMs ?? 60_000,
    now: overrides.now ?? "2026-07-18T17:00:00.000Z",
  };
}

function negotiatedPair(
  label: string,
  localOverrides: Parameters<typeof peerInput>[1] = {},
  remoteOverrides: Parameters<typeof peerInput>[1] = {},
) {
  const runtime = new ControlProtocolNegotiator();
  const local = runtime.register(peerInput(`${label}-local`, localOverrides));
  const remote = runtime.register(
    peerInput(`${label}-remote`, remoteOverrides),
  );
  return { runtime, local, remote };
}

function framed(
  label: string,
  overrides: Partial<Parameters<ControlFrameCodec["frame"]>[0]> = {},
) {
  const codec = new ControlFrameCodec(16 * 1024, 64 * 1024);
  const frame = codec.frame({
    connectionId: `connection-${label}`,
    streamId: `stream-${label}`,
    sequence: 1,
    kind: "request",
    payload: JSON.stringify({ request: label }),
    now: "2026-07-18T18:00:00.000Z",
    ...overrides,
  });
  return { codec, frame };
}

test("e03 control schema parses an agent create envelope", () => {
  const schema = new ControlSchema();
  const parsed = schema.parse(
    envelope("agent.create", {
      task_id: "child-control",
      agent: "reviewer",
      prompt: "Review the implementation",
      execution_mode: "background",
      isolation: "worktree",
    }),
  );
  assert.equal(parsed.schema_version, "3.0");
  assert.equal(parsed.command, "agent.create");
  assert.equal(parsed.request_id, "request-agent.create");
  assert.equal(parsed.idempotency_key, "key-agent.create");
  assert.equal(parsed.expected_revision, 4);
  assert.equal(parsed.body.task_id, "child-control");
  assert.equal(parsed.body.agent, "reviewer");
  assert.equal(parsed.body.execution_mode, "background");
});

test("e03 control schema preserves the lost-ack simulation flag", () => {
  const schema = new ControlSchema();
  const parsed = schema.parse(
    envelope(
      "agent.status",
      { task_id: "status-task" },
      { simulate_lost_ack: true },
    ),
  );
  assert.equal(parsed.command, "agent.status");
  assert.equal(parsed.simulate_lost_ack, true);
  assert.equal(parsed.body.task_id, "status-task");
  assert.equal(parsed.expected_revision, 4);
});

test("e03 control schema descriptor separates E03 mutations and reads", () => {
  const schema = new ControlSchema();
  const create = schema.descriptor("agent.create");
  const status = schema.descriptor("agent.status");
  const fanout = schema.descriptor("team.fanout");
  assert.equal(create.owner, "E03");
  assert.equal(create.mutation, true);
  assert.deepEqual(create.required, ["task_id", "agent", "prompt"]);
  assert.ok(create.optional.includes("isolation"));
  assert.equal(status.owner, "E03");
  assert.equal(status.mutation, false);
  assert.deepEqual(status.required, ["task_id"]);
  assert.equal(fanout.owner, "E03");
  assert.equal(fanout.mutation, true);
  assert.deepEqual(fanout.required, ["parent_task_id", "targets"]);
});

test("e03 control schema descriptor retains predecessor owners", () => {
  const schema = new ControlSchema();
  const compact = schema.descriptor("context.compact");
  const permission = schema.descriptor("permission.inspect");
  const mcp = schema.descriptor("mcp.inspect");
  assert.equal(compact.owner, "E01");
  assert.equal(compact.mutation, true);
  assert.deepEqual(compact.required, []);
  assert.equal(permission.owner, "E02");
  assert.equal(permission.mutation, false);
  assert.equal(mcp.owner, "E02");
  assert.equal(mcp.mutation, false);
});

test("e03 control schema failure rejects unsupported protocol version", () => {
  const schema = new ControlSchema();
  assert.throws(
    () =>
      schema.parse({
        ...envelope("agent.status", { task_id: "version-task" }),
        schema_version: "2.0",
      }),
    (error) => assertRuntimeCode(error, "unsupported_control_schema"),
  );
});

test("e03 control schema failure rejects unsupported command", () => {
  const schema = new ControlSchema();
  assert.throws(
    () =>
      schema.parse({
        ...envelope("agent.status", { task_id: "unsupported-task" }),
        command: "agent.teleport",
      }),
    (error) => assertRuntimeCode(error, "unsupported_control_command"),
  );
});

test("e03 control schema failure rejects missing required body field", () => {
  const schema = new ControlSchema();
  assert.throws(
    () => schema.parse(envelope("agent.create", { task_id: "missing-agent" })),
    (error) => assertRuntimeCode(error, "missing_control_field"),
  );
});

test("e03 control schema failure rejects unknown body field", () => {
  const schema = new ControlSchema();
  assert.throws(
    () =>
      schema.parse(
        envelope("agent.status", {
          task_id: "unknown-field-task",
          unexpected: true,
        }),
      ),
    (error) => assertRuntimeCode(error, "unknown_control_field"),
  );
});

test("e03 control schema failure rejects negative expected revision", () => {
  const schema = new ControlSchema();
  assert.throws(
    () =>
      schema.parse(
        envelope(
          "agent.status",
          { task_id: "negative-revision" },
          { expected_revision: -1 },
        ),
      ),
    (error) => assertRuntimeCode(error, "invalid_field"),
  );
});

test("e03 control catalog resolves canonical agent command custody", () => {
  const catalog = new ControlCommandCatalog();
  const create = catalog.resolve("agent.create");
  assert.equal(create.domain, "agent");
  assert.equal(create.mutation, "physical-write");
  assert.equal(create.requiresTask, false);
  assert.equal(create.requiresExpectedRevision, false);
  assert.equal(create.requiresPhysicalEffect, true);
  assert.equal(create.idempotent, true);
  assert.equal(create.permissionBinding, "agent.spawn");
  assert.equal(create.canonicalOwner, "typescript.E03AgentControlCoordinator");
  assert.deepEqual(create.requiredBody, ["task_id", "agent", "prompt"]);
  assert.ok(create.digest.length === 64);
});

test("e03 control catalog resolves delegated predecessor custody", () => {
  const catalog = new ControlCommandCatalog();
  const compact = catalog.resolve("context.compact");
  const permission = catalog.resolve("permission.inspect");
  assert.equal(compact.domain, "e01-session");
  assert.equal(compact.mutation, "delegated-write");
  assert.equal(compact.canonicalOwner, "typescript.QueryEngine");
  assert.equal(compact.permissionBinding, "session.compact");
  assert.equal(permission.domain, "e02-capability");
  assert.equal(permission.mutation, "read");
  assert.equal(permission.canonicalOwner, "typescript.PermissionRuntime");
  assert.equal(permission.requiresPhysicalEffect, false);
});

test("e03 control catalog snapshots commands in deterministic order", () => {
  const catalog = new ControlCommandCatalog();
  const snapshot = catalog.snapshot();
  const commands = snapshot.map((value) => value.command);
  assert.ok(snapshot.length >= 20);
  assert.deepEqual(commands, [...commands].sort());
  assert.equal(new Set(commands).size, commands.length);
  assert.ok(snapshot.every((value) => value.digest.length === 64));
  assert.ok(
    snapshot.every((value) => value.canonicalOwner.startsWith("typescript.")),
  );
});

test("e03 control catalog groups command descriptors by domain", () => {
  const catalog = new ControlCommandCatalog();
  const agents = catalog.byDomain("agent");
  const team = catalog.byDomain("team");
  const worktree = catalog.byDomain("worktree");
  assert.equal(agents.length, 9);
  assert.deepEqual(
    team.map((value) => value.command),
    ["team.collect", "team.fanout", "team.send"],
  );
  assert.deepEqual(
    worktree.map((value) => value.command),
    ["worktree.cleanup", "worktree.merge", "worktree.prepare"],
  );
  assert.ok(agents.every((value) => value.domain === "agent"));
});

test("e03 control catalog validates an owned mutation envelope", () => {
  const catalog = new ControlCommandCatalog();
  const value = envelope("agent.cancel", {
    task_id: "cancel-catalog-task",
    reason: "operator request",
  });
  const descriptor = catalog.validate(value);
  assert.equal(descriptor.command, "agent.cancel");
  assert.equal(descriptor.mutation, "physical-write");
  assert.equal(descriptor.permissionBinding, "agent.cancel");
  assert.equal(descriptor.requiresExpectedRevision, true);
  assert.equal(descriptor.requiresTask, true);
});

test("e03 control catalog permits agent extension fields for migration", () => {
  const catalog = new ControlCommandCatalog();
  const descriptor = catalog.validate(
    envelope("agent.create", {
      task_id: "extended-create",
      agent: "reviewer",
      prompt: "review",
      extension_trace: "migration-compatible",
    }),
  );
  assert.equal(descriptor.command, "agent.create");
  assert.equal(descriptor.domain, "agent");
  assert.equal(descriptor.mutation, "physical-write");
});

test("e03 control catalog permission binding contains run session and task", () => {
  const catalog = new ControlCommandCatalog();
  const binding = catalog.permissionBinding(
    envelope(
      "team.send",
      {
        sender_task_id: "sender-task",
        recipient_task_id: "recipient-task",
        message: "inspect",
      },
      {
        run_id: "run-binding",
        session_id: "session-binding",
      },
    ),
  );
  assert.equal(binding.permission, "team.message");
  assert.equal(binding.resource, "run-binding/session-binding/recipient-task");
  assert.equal(binding.mutation, "logical-write");
  assert.equal(binding.owner, "typescript.E03AgentControlCoordinator");
});

test("e03 control catalog failure rejects missing mutation idempotency", () => {
  const catalog = new ControlCommandCatalog();
  assert.throws(
    () =>
      catalog.validate(
        envelope(
          "agent.cancel",
          { task_id: "missing-idempotency" },
          { idempotency_key: "   " },
        ),
      ),
    (error) => assertRuntimeCode(error, "missing_control_idempotency"),
  );
});

test("e03 control catalog failure rejects missing required body value", () => {
  const catalog = new ControlCommandCatalog();
  assert.throws(
    () =>
      catalog.validate(
        envelope("agent.steer", {
          task_id: "steer-missing-message",
          message: "   ",
        }),
      ),
    (error) => assertRuntimeCode(error, "missing_control_body"),
  );
});

test("e03 control catalog failure rejects stale revision shape", () => {
  const catalog = new ControlCommandCatalog();
  assert.throws(
    () =>
      catalog.validate(
        envelope(
          "agent.resume",
          { task_id: "resume-invalid-revision" },
          { expected_revision: -1 },
        ),
      ),
    (error) => assertRuntimeCode(error, "invalid_expected_revision"),
  );
});

test("e03 control catalog failure rejects unknown fields outside agent and team", () => {
  const catalog = new ControlCommandCatalog();
  assert.throws(
    () =>
      catalog.validate(
        envelope("worktree.cleanup", {
          task_id: "cleanup-task",
          cleanup_receipt: "receipt",
          opaque_extension: true,
        }),
      ),
    (error) => assertRuntimeCode(error, "unknown_control_body"),
  );
});

test("e03 control catalog failure detects descriptor digest corruption", () => {
  const source = new ControlCommandCatalog().snapshot();
  const corrupted = source.map((value) =>
    value.command === "agent.status"
      ? { ...value, mutation: "logical-write" as const }
      : value,
  );
  assert.throws(
    () => new ControlCommandCatalog(corrupted),
    (error) => assertRuntimeCode(error, "control_descriptor_digest"),
  );
});

test("e03 control catalog failure detects conflicting descriptor registration", () => {
  const catalog = new ControlCommandCatalog();
  const prior = catalog.resolve("agent.status");
  const { digest: _discarded, ...raw } = prior;
  const changedPayload = { ...raw, permissionBinding: "agent.read.changed" };
  const changed = { ...changedPayload, digest: digest(changedPayload) };
  assert.throws(
    () => catalog.register(changed),
    (error) => assertRuntimeCode(error, "control_descriptor_conflict"),
  );
});

test("e03 control body registry validates default agent steer shape", () => {
  const runtime = new ControlBodySchemaRegistry();
  const report = runtime.validate("agent.steer", {
    task_id: "steer-body-task",
    message: "Please revisit the failing branch",
  });
  assert.equal(report.accepted, true);
  assert.equal(report.command, "agent.steer");
  assert.equal(report.issues.length, 0);
  assert.deepEqual(report.normalizedBody, {
    task_id: "steer-body-task",
    message: "Please revisit the failing branch",
  });
  assert.deepEqual(report.redactedBody, report.normalizedBody);
  assert.deepEqual(report.unknownFields, []);
  assert.deepEqual(report.defaultedFields, []);
  assert.equal(report.digest.length, 64);
});

test("e03 control body registry reports required and unknown fields", () => {
  const runtime = new ControlBodySchemaRegistry();
  const report = runtime.validate("agent.steer", {
    task_id: "steer-issues-task",
    unexpected: "field",
  });
  assert.equal(report.accepted, false);
  assert.deepEqual(report.unknownFields, ["unexpected"]);
  assert.ok(
    report.issues.some((issue) => issue.code === "control_unknown_field"),
  );
  assert.ok(
    report.issues.some((issue) => issue.code === "control_required_field"),
  );
  assert.ok(report.issues.every((issue) => issue.digest.length === 64));
  assert.equal(report.normalizedBody.task_id, "steer-issues-task");
  assert.equal(report.normalizedBody.message, undefined);
});

test("e03 control body failure assert closes invalid input", () => {
  const runtime = new ControlBodySchemaRegistry();
  assert.throws(
    () => runtime.assert("agent.cancel", { task_id: "" }),
    (error) => assertRuntimeCode(error, "control_body_invalid"),
  );
});

test("e03 control body custom schema applies defaults and sensitive redaction", () => {
  const { runtime } = customBodyRegistry("defaults", [
    field("task_id", "string", { required: true, minimumLength: 1 }),
    field("secret_token", "string", {
      required: true,
      minimumLength: 8,
      sensitive: true,
    }),
    field("retry_count", "integer", {
      minimum: 0,
      maximum: 5,
      defaultValue: 2,
    }),
  ]);
  const report = runtime.validate("agent.steer", {
    task_id: "custom-task",
    secret_token: "topsecret",
  });
  assert.equal(report.accepted, true);
  assert.equal(report.normalizedBody.secret_token, "topsecret");
  assert.equal(report.redactedBody.secret_token, "[redacted]");
  assert.equal(report.normalizedBody.retry_count, 2);
  assert.equal(report.redactedBody.retry_count, 2);
  assert.deepEqual(report.defaultedFields, ["retry_count"]);
});

test("e03 control body custom schema accepts nullable value", () => {
  const { runtime } = customBodyRegistry("nullable", [
    field("task_id", "string", { required: true }),
    field("optional_note", "string", { nullable: true }),
  ]);
  const report = runtime.validate("agent.steer", {
    task_id: "nullable-task",
    optional_note: null,
  });
  assert.equal(report.accepted, true);
  assert.equal(report.normalizedBody.optional_note, null);
  assert.equal(report.redactedBody.optional_note, null);
  assert.equal(report.issues.length, 0);
});

test("e03 control body custom schema enforces numeric bounds", () => {
  const { runtime } = customBodyRegistry("numeric", [
    field("task_id", "string", { required: true }),
    field("priority", "integer", { required: true, minimum: 1, maximum: 5 }),
  ]);
  const below = runtime.validate("agent.steer", {
    task_id: "numeric-below",
    priority: 0,
  });
  const above = runtime.validate("agent.steer", {
    task_id: "numeric-above",
    priority: 6,
  });
  assert.equal(below.accepted, false);
  assert.equal(above.accepted, false);
  assert.equal(below.issues[0]?.code, "control_field_minimum");
  assert.equal(above.issues[0]?.code, "control_field_maximum");
  assert.equal(below.issues[0]?.path, "body.priority");
  assert.equal(above.issues[0]?.path, "body.priority");
});

test("e03 control body custom schema enforces string and array lengths", () => {
  const { runtime } = customBodyRegistry("lengths", [
    field("task_id", "string", {
      required: true,
      minimumLength: 3,
      maximumLength: 8,
    }),
    field("tags", "array", {
      required: true,
      minimumLength: 1,
      maximumLength: 2,
    }),
  ]);
  const short = runtime.validate("agent.steer", { task_id: "x", tags: [] });
  const long = runtime.validate("agent.steer", {
    task_id: "too-long-task-id",
    tags: ["one", "two", "three"],
  });
  assert.equal(short.accepted, false);
  assert.equal(long.accepted, false);
  assert.deepEqual(
    short.issues.map((value) => value.code),
    ["control_field_minimum_length", "control_field_minimum_length"],
  );
  assert.deepEqual(
    long.issues.map((value) => value.code),
    ["control_field_maximum_length", "control_field_maximum_length"],
  );
});

test("e03 control body custom schema validates pattern enum timestamp and digest", () => {
  const { runtime } = customBodyRegistry("special", [
    field("task_id", "string", { required: true, pattern: "^task-[a-z]+$" }),
    field("mode", "enum", {
      required: true,
      enumValues: ["safe", "sealed"],
    }),
    field("deadline", "timestamp", { required: true }),
    field("artifact_digest", "digest", { required: true }),
  ]);
  const accepted = runtime.validate("agent.steer", {
    task_id: "task-alpha",
    mode: "sealed",
    deadline: "2026-07-18T20:00:00.000Z",
    artifact_digest: digest("artifact"),
  });
  assert.equal(accepted.accepted, true);
  assert.equal(accepted.issues.length, 0);
  const rejected = runtime.validate("agent.steer", {
    task_id: "INVALID",
    mode: "unsafe",
    deadline: "tomorrow",
    artifact_digest: "not-a-digest",
  });
  assert.equal(rejected.accepted, false);
  assert.deepEqual(
    rejected.issues.map((value) => value.code),
    [
      "control_field_pattern",
      "control_field_enum",
      "control_field_timestamp",
      "control_field_digest",
    ],
  );
});

test("e03 control body custom schema rejects duplicate array items", () => {
  const { runtime } = customBodyRegistry("unique", [
    field("task_id", "string", { required: true }),
    field("tools", "array", { required: true, uniqueItems: true }),
  ]);
  const report = runtime.validate("agent.steer", {
    task_id: "unique-task",
    tools: ["read", "read", "write"],
  });
  assert.equal(report.accepted, false);
  assert.equal(report.issues.length, 1);
  assert.equal(report.issues[0]?.code, "control_field_unique_items");
  assert.equal(report.issues[0]?.expected, "unique values");
  assert.equal(report.issues[0]?.actual, "3");
});

test("e03 control body custom schema preserves allowed unknown fields", () => {
  const { runtime } = customBodyRegistry(
    "unknowns",
    [field("task_id", "string", { required: true })],
    { allowUnknown: true },
  );
  const report = runtime.validate("agent.steer", {
    task_id: "unknown-preserved",
    extension: { enabled: true },
  });
  assert.equal(report.accepted, true);
  assert.deepEqual(report.unknownFields, ["extension"]);
  assert.deepEqual(report.normalizedBody.extension, { enabled: true });
  assert.deepEqual(report.redactedBody.extension, { enabled: true });
});

test("e03 control body custom schema enforces property and depth limits", () => {
  const { runtime } = customBodyRegistry(
    "limits",
    [field("task_id", "string", { required: true })],
    { allowUnknown: true, maximumProperties: 2, maximumDepth: 2 },
  );
  const report = runtime.validate("agent.steer", {
    task_id: "limited-task",
    first: true,
    second: { nested: { too: "deep" } },
  });
  assert.equal(report.accepted, false);
  assert.ok(
    report.issues.some((issue) => issue.code === "control_property_limit"),
  );
  assert.ok(report.issues.some((issue) => issue.code === "control_body_depth"));
  assert.equal(report.normalizedBody.first, true);
  assert.deepEqual(report.normalizedBody.second, { nested: { too: "deep" } });
});

test("e03 control body snapshot restores schemas deterministically", () => {
  const runtime = new ControlBodySchemaRegistry();
  const snapshot = runtime.snapshot();
  const restored = new ControlBodySchemaRegistry();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.deepEqual(
    snapshot.map((value) => value.command),
    snapshot.map((value) => value.command).sort(),
  );
  const report = restored.assert("agent.status", {
    task_id: "restored-status",
  });
  assert.equal(report.accepted, true);
  assert.equal(report.command, "agent.status");
});

test("e03 control body failure rejects corrupt restored schema", () => {
  const runtime = new ControlBodySchemaRegistry();
  const snapshot = runtime.snapshot();
  const corrupted = snapshot.map((value) =>
    value.command === "agent.status"
      ? { ...value, maximumDepth: value.maximumDepth + 1 }
      : value,
  );
  assert.throws(
    () => runtime.restore(corrupted),
    (error) => assertRuntimeCode(error, "control_body_schema_checksum"),
  );
});

test("e03 control body failure rejects duplicate restored command schema", () => {
  const runtime = new ControlBodySchemaRegistry();
  const selected = runtime
    .snapshot()
    .find((value) => value.command === "agent.status")!;
  assert.throws(
    () => runtime.restore([selected, structuredClone(selected)]),
    (error) => assertRuntimeCode(error, "duplicate_control_body_schema"),
  );
});

test("e03 control body failure rejects invalid field bounds", () => {
  const invalid = field("priority", "integer", { minimum: 10, maximum: 1 });
  const runtime = new ControlBodySchemaRegistry();
  assert.throws(
    () =>
      runtime.register({
        command: "agent.steer",
        version: "invalid-bounds",
        fields: [invalid],
        allowUnknown: false,
        maximumProperties: 4,
        maximumDepth: 4,
        mutation: "logical-write",
        owner: "typescript.E03AgentControlCoordinator",
      }),
    (error) => assertRuntimeCode(error, "control_field_numeric_bounds"),
  );
});

test("e03 control protocol registers normalized peer capabilities", () => {
  const runtime = new ControlProtocolNegotiator();
  const peer = runtime.register(
    peerInput("normalized-peer", {
      protocolVersions: ["3.0", "3.1", "3.0"],
      commands: ["agent.status", "agent.create", "agent.status"],
      capabilities: [
        capability("zeta", { required: false }),
        capability("alpha"),
      ],
    }),
  );
  assert.equal(peer.peerId, "normalized-peer");
  assert.deepEqual(peer.protocolVersions, ["3.1", "3.0"]);
  assert.deepEqual(peer.commands, ["agent.create", "agent.status"]);
  assert.deepEqual(
    peer.capabilities.map((value) => value.capability),
    ["alpha", "zeta"],
  );
  assert.equal(peer.revision, 1);
  assert.equal(peer.registeredAt, "2026-07-18T17:00:00.000Z");
  assert.equal(peer.expiresAt, "2026-07-18T17:01:00.000Z");
  assert.equal(peer.digest.length, 64);
});

test("e03 control protocol registration advances revision without changing origin", () => {
  const runtime = new ControlProtocolNegotiator();
  const first = runtime.register(peerInput("revision-peer"));
  const second = runtime.register(
    peerInput("revision-peer", {
      now: "2026-07-18T17:00:30.000Z",
      maximumFrameBytes: 2048,
    }),
  );
  assert.equal(first.revision, 1);
  assert.equal(second.revision, 2);
  assert.equal(second.registeredAt, first.registeredAt);
  assert.equal(second.expiresAt, "2026-07-18T17:01:30.000Z");
  assert.equal(second.maximumFrameBytes, 2048);
  assert.notEqual(second.digest, first.digest);
});

test("e03 control protocol negotiates shared version commands and features", () => {
  const { runtime, local, remote } = negotiatedPair(
    "accepted",
    {},
    {
      protocolVersions: ["3.1", "3.2"],
      commands: ["agent.status", "agent.cancel", "team.collect"],
      maximumFrameBytes: 64 * 1024,
      supportsCompression: false,
    },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:10.000Z",
  );
  assert.equal(result.accepted, true);
  assert.equal(result.code, "control_protocol_accepted");
  assert.equal(result.protocolVersion, "3.1");
  assert.deepEqual(result.commands, ["agent.cancel", "agent.status"]);
  assert.deepEqual(result.capabilities, [
    "lost-ack-recovery",
    "physical-receipt",
    "revision-fence",
  ]);
  assert.equal(result.maximumFrameBytes, 64 * 1024);
  assert.equal(result.compression, false);
  assert.equal(result.streaming, true);
  assert.equal(result.lostAckRecovery, true);
  assert.equal(result.revisionFence, true);
  assert.equal(result.physicalReceipt, true);
  assert.equal(result.digest.length, 64);
});

test("e03 control protocol assert returns an accepted negotiation", () => {
  const { runtime, local, remote } = negotiatedPair("assert-accepted");
  const result = runtime.assert(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, true);
  assert.equal(result.localPeerId, local.peerId);
  assert.equal(result.remotePeerId, remote.peerId);
  assert.equal(result.protocolVersion, "3.1");
  assert.match(result.negotiationId, /^control-negotiation-/);
});

test("e03 control protocol failure reports version mismatch", () => {
  const { runtime, local, remote } = negotiatedPair(
    "version-mismatch",
    { protocolVersions: ["3.0"] },
    { protocolVersions: ["4.0"] },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, false);
  assert.equal(result.code, "control_protocol_version_mismatch");
  assert.equal(result.protocolVersion, null);
  assert.deepEqual(result.capabilities, []);
  assert.throws(
    () =>
      runtime.assert(local.peerId, remote.peerId, "2026-07-18T17:00:20.000Z"),
    (error) => assertRuntimeCode(error, "control_protocol_version_mismatch"),
  );
});

test("e03 control protocol failure reports missing local capability", () => {
  const { runtime, local, remote } = negotiatedPair(
    "missing-local",
    { capabilities: [capability("revision-fence")] },
    {
      capabilities: [
        capability("revision-fence"),
        capability("remote-required"),
      ],
    },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, false);
  assert.equal(result.code, "control_protocol_local_capability_missing");
  assert.deepEqual(result.missingLocalCapabilities, ["remote-required"]);
  assert.deepEqual(result.missingRemoteCapabilities, []);
});

test("e03 control protocol failure reports missing remote capability", () => {
  const { runtime, local, remote } = negotiatedPair(
    "missing-remote",
    {
      capabilities: [
        capability("revision-fence"),
        capability("local-required"),
      ],
    },
    { capabilities: [capability("revision-fence")] },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, false);
  assert.equal(result.code, "control_protocol_remote_capability_missing");
  assert.deepEqual(result.missingLocalCapabilities, []);
  assert.deepEqual(result.missingRemoteCapabilities, ["local-required"]);
});

test("e03 control protocol failure requires revision fencing", () => {
  const { runtime, local, remote } = negotiatedPair(
    "revision-fence",
    { supportsRevisionFence: true },
    { supportsRevisionFence: false },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, false);
  assert.equal(result.code, "control_protocol_revision_fence_required");
  assert.equal(result.revisionFence, false);
  assert.equal(result.physicalReceipt, true);
});

test("e03 control protocol failure requires physical receipts", () => {
  const { runtime, local, remote } = negotiatedPair(
    "physical-receipt",
    { supportsPhysicalReceipt: true },
    { supportsPhysicalReceipt: false },
  );
  const result = runtime.negotiate(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, false);
  assert.equal(result.code, "control_protocol_physical_receipt_required");
  assert.equal(result.revisionFence, true);
  assert.equal(result.physicalReceipt, false);
});

test("e03 control protocol failure rejects expired peer registration", () => {
  const { runtime, local, remote } = negotiatedPair(
    "expired-peer",
    { ttlMs: 1_000 },
    { ttlMs: 1_000 },
  );
  assert.throws(
    () =>
      runtime.negotiate(
        local.peerId,
        remote.peerId,
        "2026-07-18T17:00:01.000Z",
      ),
    (error) => assertRuntimeCode(error, "control_peer_expired"),
  );
});

test("e03 control protocol failure rejects invalid TTL", () => {
  const runtime = new ControlProtocolNegotiator();
  assert.throws(
    () => runtime.register(peerInput("invalid-ttl", { ttlMs: 0 })),
    (error) => assertRuntimeCode(error, "control_peer_ttl_invalid"),
  );
});

test("e03 control protocol failure rejects invalid protocol version", () => {
  const runtime = new ControlProtocolNegotiator();
  assert.throws(
    () =>
      runtime.register(
        peerInput("invalid-version", { protocolVersions: ["three"] }),
      ),
    (error) => assertRuntimeCode(error, "control_protocol_version_invalid"),
  );
});

test("e03 control protocol snapshot restores peers and revisions", () => {
  const { runtime, local, remote } = negotiatedPair("snapshot-peers");
  const snapshot = runtime.snapshot();
  const restored = new ControlProtocolNegotiator();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.deepEqual(
    snapshot.map((value) => value.peerId),
    [local.peerId, remote.peerId],
  );
  const result = restored.assert(
    local.peerId,
    remote.peerId,
    "2026-07-18T17:00:20.000Z",
  );
  assert.equal(result.accepted, true);
  assert.equal(result.protocolVersion, "3.1");
});

test("e03 control protocol failure rejects duplicate restored peer", () => {
  const { runtime, local } = negotiatedPair("duplicate-peer");
  assert.throws(
    () => runtime.restore([local, structuredClone(local)]),
    (error) => assertRuntimeCode(error, "duplicate_control_peer"),
  );
});

test("e03 control protocol failure rejects corrupt restored peer", () => {
  const { runtime, local } = negotiatedPair("corrupt-peer");
  const corrupted = {
    ...local,
    maximumFrameBytes: local.maximumFrameBytes + 1,
  };
  assert.throws(
    () => runtime.restore([corrupted]),
    (error) => assertRuntimeCode(error, "control_peer_checksum"),
  );
});

test("e03 control frame codec creates a deterministic request frame", () => {
  const { frame } = framed("request");
  assert.match(frame.frameId, /^control-frame-/);
  assert.equal(frame.connectionId, "connection-request");
  assert.equal(frame.streamId, "stream-request");
  assert.equal(frame.sequence, 1);
  assert.equal(frame.acknowledgment, 0);
  assert.equal(frame.kind, "request");
  assert.equal(frame.contentType, "application/json");
  assert.equal(frame.encoding, "identity");
  assert.equal(frame.payload, JSON.stringify({ request: "request" }));
  assert.equal(frame.payloadBytes, Buffer.byteLength(frame.payload));
  assert.equal(frame.compressed, false);
  assert.equal(frame.final, false);
  assert.equal(frame.previousDigest, "");
  assert.equal(frame.digest.length, 64);
});

test("e03 control frame codec supports binary base64 payload", () => {
  const raw = Uint8Array.from([0, 1, 2, 127, 128, 255]);
  const { frame } = framed("binary", {
    payload: raw,
    contentType: "application/octet-stream",
    encoding: "base64",
    kind: "event",
    final: true,
  });
  assert.equal(frame.contentType, "application/octet-stream");
  assert.equal(frame.encoding, "base64");
  assert.equal(frame.kind, "event");
  assert.equal(frame.payload, Buffer.from(raw).toString("base64"));
  assert.equal(frame.payloadBytes, raw.byteLength);
  assert.equal(frame.final, true);
  assert.equal(
    frame.payloadDigest,
    digest(Buffer.from(raw).toString("base64")),
  );
});

test("e03 control frame binary encode and decode round trip", () => {
  const { codec, frame } = framed("roundtrip", {
    acknowledgment: 7,
    sequence: 8,
    kind: "response",
    final: true,
  });
  const bytes = codec.encode(frame);
  const decoded = codec.decode(bytes);
  assert.equal(decoded.frames.length, 1);
  assert.deepEqual(decoded.frames[0], frame);
  assert.equal(decoded.remaining.byteLength, 0);
  assert.equal(decoded.consumedBytes, bytes.byteLength);
  assert.equal(decoded.rejectedBytes, 0);
  assert.equal(decoded.digest.length, 64);
});

test("e03 control frame decoder preserves incomplete frame bytes", () => {
  const { codec, frame } = framed("partial-buffer");
  const bytes = codec.encode(frame);
  const prefix = bytes.subarray(0, bytes.byteLength - 5);
  const decoded = codec.decode(prefix);
  assert.equal(decoded.frames.length, 0);
  assert.equal(decoded.consumedBytes, 0);
  assert.equal(decoded.rejectedBytes, 0);
  assert.deepEqual(decoded.remaining, prefix);
  const complete = codec.decode(
    Buffer.concat([decoded.remaining, bytes.subarray(bytes.byteLength - 5)]),
  );
  assert.equal(complete.frames.length, 1);
  assert.equal(complete.frames[0]?.digest, frame.digest);
});

test("e03 control frame decoder skips garbage before magic header", () => {
  const { codec, frame } = framed("garbage-prefix");
  const bytes = codec.encode(frame);
  const garbage = Buffer.from([1, 2, 3, 4, 5]);
  const decoded = codec.decode(Buffer.concat([garbage, bytes]));
  assert.equal(decoded.frames.length, 1);
  assert.equal(decoded.frames[0]?.digest, frame.digest);
  assert.equal(decoded.rejectedBytes, garbage.byteLength);
  assert.equal(decoded.remaining.byteLength, 0);
  assert.equal(decoded.consumedBytes, garbage.byteLength + bytes.byteLength);
});

test("e03 control frame NDJSON round trip preserves ordered frames", () => {
  const { codec, frame: first } = framed("ndjson-first", { sequence: 1 });
  const second = codec.frame({
    connectionId: first.connectionId,
    streamId: first.streamId,
    sequence: 2,
    acknowledgment: 1,
    kind: "response",
    payload: JSON.stringify({ ok: true }),
    previousDigest: first.digest,
    final: true,
    now: "2026-07-18T18:00:01.000Z",
  });
  const input = `${codec.encodeNdjson(first)}${codec.encodeNdjson(second)}`;
  const decoded = codec.decodeNdjson(input);
  assert.equal(decoded.length, 2);
  assert.deepEqual(decoded[0], first);
  assert.deepEqual(decoded[1], second);
  assert.equal(decoded[1]?.previousDigest, first.digest);
  assert.equal(decoded[1]?.acknowledgment, 1);
  assert.equal(decoded[1]?.final, true);
});

test("e03 control frame failure rejects missing connection identity", () => {
  const codec = new ControlFrameCodec();
  assert.throws(
    () =>
      codec.frame({
        connectionId: " ",
        streamId: "stream",
        sequence: 1,
        kind: "request",
      }),
    (error) => assertRuntimeCode(error, "control_frame_identity_missing"),
  );
});

test("e03 control frame failure rejects invalid sequence", () => {
  const codec = new ControlFrameCodec();
  assert.throws(
    () =>
      codec.frame({
        connectionId: "connection",
        streamId: "stream",
        sequence: 0,
        kind: "request",
      }),
    (error) => assertRuntimeCode(error, "control_frame_sequence_invalid"),
  );
});

test("e03 control frame failure rejects oversized payload", () => {
  const codec = new ControlFrameCodec(1024, 4096);
  assert.throws(
    () =>
      codec.frame({
        connectionId: "connection-large",
        streamId: "stream-large",
        sequence: 1,
        kind: "request",
        payload: "x".repeat(1025),
      }),
    (error) => assertRuntimeCode(error, "control_frame_too_large"),
  );
});

test("e03 control frame failure rejects corrupt frame checksum", () => {
  const { codec, frame } = framed("corrupt-checksum");
  const corrupted: ControlFrame = { ...frame, acknowledgment: 1 };
  assert.throws(
    () => codec.encode(corrupted),
    (error) => assertRuntimeCode(error, "control_frame_checksum"),
  );
  assert.throws(
    () => codec.encodeNdjson(corrupted),
    (error) => assertRuntimeCode(error, "control_frame_checksum"),
  );
});

test("e03 control frame failure rejects corrupt payload digest", () => {
  const { codec, frame } = framed("corrupt-payload");
  const payload = {
    ...frame,
    payload: `${frame.payload}!`,
    payloadBytes: frame.payloadBytes + 1,
  };
  const { digest: _discarded, ...raw } = payload;
  const corrupted = { ...raw, digest: digest(raw) };
  assert.throws(
    () => codec.encode(corrupted),
    (error) => assertRuntimeCode(error, "control_frame_payload_digest"),
  );
});

test("e03 control frame failure rejects invalid declared binary length", () => {
  const codec = new ControlFrameCodec(1024, 4096);
  const header = Buffer.alloc(8);
  header.writeUInt32BE(0x5a595241, 0);
  header.writeUInt32BE(1, 4);
  assert.throws(
    () => codec.decode(header),
    (error) => assertRuntimeCode(error, "control_frame_declared_length"),
  );
});

test("e03 control frame failure rejects invalid framed JSON", () => {
  const codec = new ControlFrameCodec(1024, 4096);
  const body = Buffer.from("{broken", "utf8");
  const header = Buffer.alloc(8);
  header.writeUInt32BE(0x5a595241, 0);
  header.writeUInt32BE(body.byteLength, 4);
  assert.throws(
    () => codec.decode(Buffer.concat([header, body])),
    (error) => assertRuntimeCode(error, "control_frame_json_invalid"),
  );
});

test("e03 control frame failure rejects non-object framed JSON", () => {
  const codec = new ControlFrameCodec(1024, 4096);
  const body = Buffer.from(JSON.stringify(["not", "object"]), "utf8");
  const header = Buffer.alloc(8);
  header.writeUInt32BE(0x5a595241, 0);
  header.writeUInt32BE(body.byteLength, 4);
  assert.throws(
    () => codec.decode(Buffer.concat([header, body])),
    (error) => assertRuntimeCode(error, "control_frame_object_required"),
  );
});

test("e03 control frame failure rejects buffer overflow", () => {
  const codec = new ControlFrameCodec(1024, 2048);
  assert.throws(
    () => codec.decode(Buffer.alloc(2049)),
    (error) => assertRuntimeCode(error, "control_buffer_overflow"),
  );
});

test("e03 control frame failure rejects invalid NDJSON", () => {
  const codec = new ControlFrameCodec(1024, 4096);
  assert.throws(
    () => codec.decodeNdjson("{invalid}\n"),
    (error) => assertRuntimeCode(error, "control_ndjson_invalid"),
  );
});

test("e03 control frame failure rejects invalid codec limits", () => {
  assert.throws(
    () => new ControlFrameCodec(100, 4096),
    (error) => assertRuntimeCode(error, "control_frame_limit_invalid"),
  );
  assert.throws(
    () => new ControlFrameCodec(4096, 2048),
    (error) => assertRuntimeCode(error, "control_buffer_limit_invalid"),
  );
});
