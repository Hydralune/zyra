import { createHmac, timingSafeEqual } from "node:crypto";

import {
  createId,
  digest,
  type E03Clock,
  SystemE03Clock,
} from "../e03/contracts.ts";
import {
  E03RuntimeError,
  requireInteger,
  requireObject,
  requireString,
  type ControlCommand,
  type E03ControlEnvelope,
} from "../e03/contracts.ts";

const COMMANDS = new Set<ControlCommand>([
  "agent.create",
  "agent.steer",
  "agent.cancel",
  "agent.kill",
  "agent.wait",
  "agent.result",
  "agent.status",
  "agent.resume",
  "agent.list",
  "team.send",
  "team.fanout",
  "team.collect",
  "worktree.prepare",
  "worktree.merge",
  "worktree.cleanup",
  "context.compact",
  "context.clear",
  "session.model",
  "permission.inspect",
  "mcp.inspect",
  "skills.inspect",
]);

export class ControlSchema {
  parse(value: unknown): E03ControlEnvelope {
    const root = requireObject(value, "control");
    if (root.schema_version !== "3.0")
      throw new E03RuntimeError(
        "unsupported_control_schema",
        `expected schema 3.0, got ${String(root.schema_version)}`,
      );
    const command = requireString(
      root.command,
      "control.command",
      1,
      128,
    ) as ControlCommand;
    if (!COMMANDS.has(command))
      throw new E03RuntimeError(
        "unsupported_control_command",
        `unsupported control command ${command}`,
      );
    const envelope: E03ControlEnvelope = {
      schema_version: "3.0",
      request_id: requireString(root.request_id, "control.request_id", 1, 512),
      idempotency_key: requireString(
        root.idempotency_key,
        "control.idempotency_key",
        1,
        512,
      ),
      run_id: requireString(root.run_id, "control.run_id", 1, 512),
      session_id: requireString(root.session_id, "control.session_id", 1, 512),
      parent_task_id: requireString(
        root.parent_task_id,
        "control.parent_task_id",
        1,
        512,
      ),
      expected_revision: requireInteger(
        root.expected_revision,
        "control.expected_revision",
        0,
      ),
      command,
      body: requireObject(root.body ?? {}, "control.body"),
    };
    if (root.simulate_lost_ack === true) envelope.simulate_lost_ack = true;
    this.validateBody(envelope);
    return envelope;
  }

  descriptor(command: ControlCommand): {
    command: ControlCommand;
    mutation: boolean;
    owner: "E01" | "E02" | "E03";
    required: string[];
    optional: string[];
  } {
    const descriptors: Record<
      ControlCommand,
      {
        mutation: boolean;
        owner: "E01" | "E02" | "E03";
        required: string[];
        optional: string[];
      }
    > = {
      "agent.create": {
        mutation: true,
        owner: "E03",
        required: ["task_id", "agent", "prompt"],
        optional: [
          "execution_mode",
          "isolation",
          "workspace_root",
          "base_revision",
        ],
      },
      "agent.steer": {
        mutation: true,
        owner: "E03",
        required: ["task_id", "message"],
        optional: [],
      },
      "agent.cancel": {
        mutation: true,
        owner: "E03",
        required: ["task_id"],
        optional: ["reason"],
      },
      "agent.kill": {
        mutation: true,
        owner: "E03",
        required: ["task_id"],
        optional: ["reason"],
      },
      "agent.wait": {
        mutation: false,
        owner: "E03",
        required: ["task_id"],
        optional: ["timeout_ms"],
      },
      "agent.result": {
        mutation: false,
        owner: "E03",
        required: ["task_id"],
        optional: [],
      },
      "agent.status": {
        mutation: false,
        owner: "E03",
        required: ["task_id"],
        optional: [],
      },
      "agent.resume": {
        mutation: true,
        owner: "E03",
        required: ["task_id"],
        optional: ["prompt"],
      },
      "agent.list": {
        mutation: false,
        owner: "E03",
        required: [],
        optional: ["parent_task_id"],
      },
      "team.send": {
        mutation: true,
        owner: "E03",
        required: ["sender_task_id", "recipient_task_id", "message"],
        optional: [],
      },
      "team.fanout": {
        mutation: true,
        owner: "E03",
        required: ["parent_task_id", "targets"],
        optional: ["maximum_concurrency", "failure_mode"],
      },
      "team.collect": {
        mutation: false,
        owner: "E03",
        required: ["parent_task_id"],
        optional: [],
      },
      "worktree.prepare": {
        mutation: true,
        owner: "E03",
        required: ["task_id", "workspace_root", "base_revision"],
        optional: [
          "expected_artifacts",
          "allow_dirty_baseline",
          "allow_nested_repository",
        ],
      },
      "worktree.merge": {
        mutation: true,
        owner: "E03",
        required: ["task_id", "merge_receipt"],
        optional: [],
      },
      "worktree.cleanup": {
        mutation: true,
        owner: "E03",
        required: ["task_id", "cleanup_receipt"],
        optional: [],
      },
      "context.compact": {
        mutation: true,
        owner: "E01",
        required: [],
        optional: [],
      },
      "context.clear": {
        mutation: true,
        owner: "E01",
        required: [],
        optional: [],
      },
      "session.model": {
        mutation: true,
        owner: "E01",
        required: ["model"],
        optional: [],
      },
      "permission.inspect": {
        mutation: false,
        owner: "E02",
        required: [],
        optional: [],
      },
      "mcp.inspect": {
        mutation: false,
        owner: "E02",
        required: [],
        optional: [],
      },
      "skills.inspect": {
        mutation: false,
        owner: "E02",
        required: [],
        optional: [],
      },
    };
    return { command, ...descriptors[command] };
  }

  private validateBody(envelope: E03ControlEnvelope): void {
    const descriptor = this.descriptor(envelope.command);
    for (const field of descriptor.required) {
      if (!(field in envelope.body))
        throw new E03RuntimeError(
          "missing_control_field",
          `${envelope.command} requires body.${field}`,
          { field },
        );
    }
    const allowed = new Set([...descriptor.required, ...descriptor.optional]);
    const unknown = Object.keys(envelope.body).filter(
      (field) => !allowed.has(field),
    );
    if (unknown.length)
      throw new E03RuntimeError(
        "unknown_control_field",
        `${envelope.command} has unknown fields ${unknown.join(", ")}`,
        { unknown },
      );
  }
}

export type CommandDomain =
  | "agent"
  | "team"
  | "worktree"
  | "e01-session"
  | "e02-capability";
export type CommandMutation =
  | "read"
  | "logical-write"
  | "physical-write"
  | "delegated-write";

export interface ControlCommandDescriptor {
  command: ControlCommand;
  domain: CommandDomain;
  mutation: CommandMutation;
  requiredBody: string[];
  optionalBody: string[];
  requiresTask: boolean;
  requiresExpectedRevision: boolean;
  requiresPhysicalEffect: boolean;
  idempotent: boolean;
  permissionBinding: string;
  canonicalOwner: string;
  digest: string;
}

const DEFINITIONS: ReadonlyArray<Omit<ControlCommandDescriptor, "digest">> = [
  descriptor(
    "agent.create",
    "agent",
    "physical-write",
    ["task_id", "agent", "prompt"],
    [
      "execution_mode",
      "tools",
      "skills",
      "mcp_servers",
      "isolation",
      "workspace_root",
      "base_revision",
      "start_immediately",
    ],
    false,
    false,
    true,
    "agent.spawn",
  ),
  descriptor(
    "agent.steer",
    "agent",
    "logical-write",
    ["task_id", "message"],
    [],
    true,
    true,
    true,
    "agent.message",
  ),
  descriptor(
    "agent.cancel",
    "agent",
    "physical-write",
    ["task_id"],
    ["reason"],
    true,
    true,
    true,
    "agent.cancel",
  ),
  descriptor(
    "agent.kill",
    "agent",
    "physical-write",
    ["task_id"],
    ["reason"],
    true,
    true,
    true,
    "agent.kill",
  ),
  descriptor(
    "agent.wait",
    "agent",
    "read",
    ["task_id"],
    ["timeout_ms"],
    true,
    false,
    false,
    "agent.read",
  ),
  descriptor(
    "agent.result",
    "agent",
    "read",
    ["task_id"],
    [],
    true,
    false,
    false,
    "agent.read",
  ),
  descriptor(
    "agent.status",
    "agent",
    "read",
    ["task_id"],
    [],
    true,
    false,
    false,
    "agent.read",
  ),
  descriptor(
    "agent.resume",
    "agent",
    "physical-write",
    ["task_id"],
    ["prompt"],
    true,
    true,
    true,
    "agent.resume",
  ),
  descriptor(
    "agent.list",
    "agent",
    "read",
    [],
    ["parent_task_id"],
    false,
    false,
    false,
    "agent.read",
  ),
  descriptor(
    "team.send",
    "team",
    "logical-write",
    ["sender_task_id", "recipient_task_id", "message"],
    [],
    true,
    true,
    true,
    "team.message",
  ),
  descriptor(
    "team.fanout",
    "team",
    "physical-write",
    ["parent_task_id", "targets"],
    ["maximum_concurrency", "failure_mode"],
    true,
    true,
    true,
    "team.fanout",
  ),
  descriptor(
    "team.collect",
    "team",
    "read",
    ["parent_task_id"],
    [],
    true,
    false,
    false,
    "team.read",
  ),
  descriptor(
    "worktree.prepare",
    "worktree",
    "physical-write",
    ["task_id", "workspace_root", "base_revision"],
    ["expected_artifacts", "allow_dirty_baseline", "allow_nested_repository"],
    true,
    true,
    true,
    "workspace.prepare",
  ),
  descriptor(
    "worktree.merge",
    "worktree",
    "physical-write",
    ["task_id", "merge_receipt"],
    [],
    true,
    true,
    true,
    "workspace.merge",
  ),
  descriptor(
    "worktree.cleanup",
    "worktree",
    "physical-write",
    ["task_id", "cleanup_receipt"],
    [],
    true,
    true,
    true,
    "workspace.cleanup",
  ),
  descriptor(
    "context.compact",
    "e01-session",
    "delegated-write",
    [],
    [],
    false,
    true,
    true,
    "session.compact",
    "typescript.QueryEngine",
  ),
  descriptor(
    "context.clear",
    "e01-session",
    "delegated-write",
    [],
    [],
    false,
    true,
    true,
    "session.clear",
    "typescript.QueryEngine",
  ),
  descriptor(
    "session.model",
    "e01-session",
    "delegated-write",
    ["model"],
    [],
    false,
    true,
    true,
    "session.model",
    "typescript.QueryEngine",
  ),
  descriptor(
    "permission.inspect",
    "e02-capability",
    "read",
    [],
    [],
    false,
    false,
    false,
    "permission.read",
    "typescript.PermissionRuntime",
  ),
  descriptor(
    "mcp.inspect",
    "e02-capability",
    "read",
    [],
    [],
    false,
    false,
    false,
    "mcp.read",
    "typescript.McpRuntime",
  ),
  descriptor(
    "skills.inspect",
    "e02-capability",
    "read",
    [],
    [],
    false,
    false,
    false,
    "skills.read",
    "typescript.SkillRuntime",
  ),
];

export class ControlCommandCatalog {
  private readonly descriptors = new Map<
    ControlCommand,
    ControlCommandDescriptor
  >();

  constructor(
    values: readonly ControlCommandDescriptor[] = DEFINITIONS.map(
      sealDescriptor,
    ),
  ) {
    for (const value of values) this.register(value);
    for (const definition of DEFINITIONS)
      if (!this.descriptors.has(definition.command))
        throw new E03RuntimeError(
          "missing_control_descriptor",
          `missing descriptor for ${definition.command}`,
        );
  }

  register(value: ControlCommandDescriptor): void {
    this.assertDescriptor(value);
    const prior = this.descriptors.get(value.command);
    if (prior && prior.digest !== value.digest)
      throw new E03RuntimeError(
        "control_descriptor_conflict",
        `descriptor ${value.command} is already registered differently`,
      );
    this.descriptors.set(value.command, structuredClone(value));
  }

  resolve(command: ControlCommand): ControlCommandDescriptor {
    const descriptor = this.descriptors.get(command);
    if (!descriptor)
      throw new E03RuntimeError(
        "unknown_control_command",
        `unknown control command ${command}`,
      );
    this.assertDescriptor(descriptor);
    return structuredClone(descriptor);
  }

  validate(envelope: E03ControlEnvelope): ControlCommandDescriptor {
    const descriptor = this.resolve(envelope.command);
    const missing = descriptor.requiredBody.filter(
      (key) => !present(envelope.body[key]),
    );
    if (missing.length)
      throw new E03RuntimeError(
        "missing_control_body",
        `${envelope.command} requires ${missing.join(", ")}`,
        { missing },
      );
    if (
      descriptor.requiresExpectedRevision &&
      (!Number.isSafeInteger(envelope.expected_revision) ||
        envelope.expected_revision < 0)
    )
      throw new E03RuntimeError(
        "invalid_expected_revision",
        `${envelope.command} requires a non-negative expected revision`,
      );
    if (
      descriptor.requiresTask &&
      !present(
        envelope.body.task_id ??
          envelope.body.parent_task_id ??
          envelope.body.recipient_task_id,
      )
    )
      throw new E03RuntimeError(
        "missing_control_task",
        `${envelope.command} requires task custody`,
      );
    if (descriptor.mutation !== "read" && !envelope.idempotency_key.trim())
      throw new E03RuntimeError(
        "missing_control_idempotency",
        `${envelope.command} mutation requires an idempotency key`,
      );
    const allowed = new Set([
      ...descriptor.requiredBody,
      ...descriptor.optionalBody,
    ]);
    const protocol = new Set(["expected_revision"]);
    const unknown = Object.keys(envelope.body).filter(
      (key) => !allowed.has(key) && !protocol.has(key),
    );
    if (
      unknown.length &&
      descriptor.domain !== "agent" &&
      descriptor.domain !== "team"
    )
      throw new E03RuntimeError(
        "unknown_control_body",
        `${envelope.command} contains unknown fields: ${unknown.join(", ")}`,
        { unknown },
      );
    return descriptor;
  }

  permissionBinding(envelope: E03ControlEnvelope): {
    permission: string;
    resource: string;
    mutation: CommandMutation;
    owner: string;
  } {
    const descriptor = this.validate(envelope);
    const taskId = String(
      envelope.body.task_id ??
        envelope.body.parent_task_id ??
        envelope.body.recipient_task_id ??
        envelope.parent_task_id,
    );
    return {
      permission: descriptor.permissionBinding,
      resource: `${envelope.run_id}/${envelope.session_id}/${taskId}`,
      mutation: descriptor.mutation,
      owner: descriptor.canonicalOwner,
    };
  }

  snapshot(): ControlCommandDescriptor[] {
    return [...this.descriptors.values()]
      .sort((left, right) => left.command.localeCompare(right.command))
      .map((value) => structuredClone(value));
  }

  byDomain(domain: CommandDomain): ControlCommandDescriptor[] {
    return this.snapshot().filter((descriptor) => descriptor.domain === domain);
  }

  private assertDescriptor(value: ControlCommandDescriptor): void {
    const { digest: checksum, ...payload } = value;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "control_descriptor_digest",
        `descriptor ${value.command} digest is invalid`,
      );
    if (
      !value.command ||
      !value.domain ||
      !value.mutation ||
      !value.permissionBinding ||
      !value.canonicalOwner
    )
      throw new E03RuntimeError(
        "invalid_control_descriptor",
        "control descriptor identity is incomplete",
      );
    if (
      new Set(value.requiredBody).size !== value.requiredBody.length ||
      new Set(value.optionalBody).size !== value.optionalBody.length
    )
      throw new E03RuntimeError(
        "duplicate_control_field",
        `descriptor ${value.command} repeats fields`,
      );
    if (value.requiredBody.some((key) => value.optionalBody.includes(key)))
      throw new E03RuntimeError(
        "ambiguous_control_field",
        `descriptor ${value.command} marks a field required and optional`,
      );
    if (value.mutation === "read" && value.requiresPhysicalEffect)
      throw new E03RuntimeError(
        "invalid_read_effect",
        `read command ${value.command} cannot require a physical effect`,
      );
  }
}

function descriptor(
  command: ControlCommand,
  domain: CommandDomain,
  mutation: CommandMutation,
  requiredBody: string[],
  optionalBody: string[],
  requiresTask: boolean,
  requiresExpectedRevision: boolean,
  requiresPhysicalEffect: boolean,
  permissionBinding: string,
  canonicalOwner = "typescript.E03AgentControlCoordinator",
): Omit<ControlCommandDescriptor, "digest"> {
  return {
    command,
    domain,
    mutation,
    requiredBody,
    optionalBody,
    requiresTask,
    requiresExpectedRevision,
    requiresPhysicalEffect,
    idempotent: true,
    permissionBinding,
    canonicalOwner,
  };
}

function sealDescriptor(
  value: Omit<ControlCommandDescriptor, "digest"> | ControlCommandDescriptor,
): ControlCommandDescriptor {
  const { digest: _digest, ...payload } = value as ControlCommandDescriptor;
  return { ...payload, digest: digest(payload) };
}

function present(value: unknown): boolean {
  return (
    value !== undefined &&
    value !== null &&
    (typeof value !== "string" || Boolean(value.trim()))
  );
}

export type ControlFieldType =
  | "string"
  | "integer"
  | "number"
  | "boolean"
  | "object"
  | "array"
  | "enum"
  | "timestamp"
  | "digest";

export interface ControlFieldConstraint {
  field: string;
  type: ControlFieldType;
  required: boolean;
  nullable: boolean;
  minimum: number | null;
  maximum: number | null;
  minimumLength: number | null;
  maximumLength: number | null;
  pattern: string | null;
  enumValues: string[];
  itemType: ControlFieldType | null;
  uniqueItems: boolean;
  sensitive: boolean;
  mutable: boolean;
  defaultValue: unknown;
  description: string;
  digest: string;
}

export interface ControlBodySchema {
  schemaId: string;
  command: ControlCommand;
  version: string;
  fields: ControlFieldConstraint[];
  allowUnknown: boolean;
  maximumProperties: number;
  maximumDepth: number;
  mutation: CommandMutation;
  owner: string;
  createdAt: string;
  digest: string;
}

export interface ControlValidationIssue {
  path: string;
  code: string;
  message: string;
  expected: string;
  actual: string;
  sensitive: boolean;
  digest: string;
}

export interface ControlValidationReport {
  reportId: string;
  command: ControlCommand;
  schemaId: string;
  accepted: boolean;
  issues: ControlValidationIssue[];
  normalizedBody: Record<string, unknown>;
  redactedBody: Record<string, unknown>;
  unknownFields: string[];
  defaultedFields: string[];
  validatedAt: string;
  digest: string;
}

function sealFieldConstraint(
  value: Omit<ControlFieldConstraint, "digest">,
): ControlFieldConstraint {
  return { ...value, digest: digest(value) };
}

function assertFieldConstraint(field: ControlFieldConstraint): void {
  const { digest: checksum, ...payload } = field;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_field_checksum",
      `control field ${field.field} checksum mismatch`,
    );
  if (!field.field || !field.description)
    throw new E03RuntimeError(
      "control_field_identity",
      "control field name and description are required",
    );
  if (
    field.minimum !== null &&
    field.maximum !== null &&
    field.maximum < field.minimum
  )
    throw new E03RuntimeError(
      "control_field_numeric_bounds",
      `control field ${field.field} numeric bounds are invalid`,
    );
  if (
    field.minimumLength !== null &&
    field.maximumLength !== null &&
    field.maximumLength < field.minimumLength
  )
    throw new E03RuntimeError(
      "control_field_length_bounds",
      `control field ${field.field} length bounds are invalid`,
    );
  if (field.type === "enum" && !field.enumValues.length)
    throw new E03RuntimeError(
      "control_field_enum_empty",
      `control enum field ${field.field} has no values`,
    );
}

function assertBodySchema(schema: ControlBodySchema): void {
  const { digest: checksum, ...payload } = schema;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_body_schema_checksum",
      `control body schema ${schema.schemaId} checksum mismatch`,
    );
  if (
    !schema.schemaId ||
    !schema.command ||
    !schema.version ||
    !schema.owner ||
    schema.maximumProperties < 1 ||
    schema.maximumDepth < 1
  )
    throw new E03RuntimeError(
      "control_body_schema_invalid",
      `control body schema ${schema.schemaId} is invalid`,
    );
  const names = new Set<string>();
  for (const field of schema.fields) {
    assertFieldConstraint(field);
    if (names.has(field.field))
      throw new E03RuntimeError(
        "duplicate_control_body_field",
        `control body field ${field.field} repeats`,
      );
    names.add(field.field);
  }
}

function valueType(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (Number.isInteger(value)) return "integer";
  return typeof value;
}

function matchesFieldType(value: unknown, type: ControlFieldType): boolean {
  if (
    type === "string" ||
    type === "enum" ||
    type === "timestamp" ||
    type === "digest"
  )
    return typeof value === "string";
  if (type === "integer") return Number.isSafeInteger(value);
  if (type === "number")
    return typeof value === "number" && Number.isFinite(value);
  if (type === "boolean") return typeof value === "boolean";
  if (type === "array") return Array.isArray(value);
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function objectDepth(value: unknown, depth = 0): number {
  if (!value || typeof value !== "object") return depth;
  const children = Array.isArray(value)
    ? value
    : Object.values(value as Record<string, unknown>);
  if (!children.length) return depth + 1;
  return Math.max(...children.map((child) => objectDepth(child, depth + 1)));
}

export class ControlBodySchemaRegistry {
  private schemas = new Map<ControlCommand, ControlBodySchema>();

  constructor(catalog = new ControlCommandCatalog()) {
    for (const descriptor of catalog.snapshot())
      this.register(this.fromDescriptor(descriptor));
  }

  register(
    input: Omit<ControlBodySchema, "schemaId" | "createdAt" | "digest"> & {
      schemaId?: string;
      createdAt?: string;
    },
  ): ControlBodySchema {
    const fields = input.fields.map((field) => {
      assertFieldConstraint(field);
      return structuredClone(field);
    });
    const payload = {
      ...structuredClone(input),
      schemaId:
        input.schemaId?.trim() ||
        `control-body-schema-${digest({ command: input.command, version: input.version }).slice(0, 32)}`,
      fields,
      createdAt: input.createdAt ?? new Date().toISOString(),
    };
    const schema = { ...payload, digest: digest(payload) };
    assertBodySchema(schema);
    const existing = this.schemas.get(schema.command);
    if (
      existing &&
      existing.version === schema.version &&
      existing.digest !== schema.digest
    )
      throw new E03RuntimeError(
        "control_body_schema_version_conflict",
        `control body schema ${schema.command}@${schema.version} changed`,
      );
    this.schemas.set(schema.command, schema);
    return structuredClone(schema);
  }

  validate(command: ControlCommand, body: unknown): ControlValidationReport {
    const schema = this.require(command);
    const object = requireObject(body, `control.${command}.body`);
    const issues: ControlValidationIssue[] = [];
    const normalizedBody: Record<string, unknown> = {};
    const redactedBody: Record<string, unknown> = {};
    const unknownFields = Object.keys(object)
      .filter((key) => !schema.fields.some((field) => field.field === key))
      .sort();
    const defaultedFields: string[] = [];
    const issue = (
      path: string,
      code: string,
      message: string,
      expected: string,
      actual: string,
      sensitive = false,
    ): void => {
      const payload = { path, code, message, expected, actual, sensitive };
      issues.push({ ...payload, digest: digest(payload) });
    };
    if (!schema.allowUnknown)
      for (const key of unknownFields)
        issue(
          `body.${key}`,
          "control_unknown_field",
          `field ${key} is not allowed`,
          "absent",
          valueType(object[key]),
        );
    if (Object.keys(object).length > schema.maximumProperties)
      issue(
        "body",
        "control_property_limit",
        "body contains too many properties",
        `<=${schema.maximumProperties}`,
        String(Object.keys(object).length),
      );
    if (objectDepth(object) > schema.maximumDepth)
      issue(
        "body",
        "control_body_depth",
        "body exceeds maximum nesting depth",
        `<=${schema.maximumDepth}`,
        String(objectDepth(object)),
      );
    for (const field of schema.fields) {
      let value: unknown = object[field.field];
      if (value === undefined && field.defaultValue !== undefined) {
        value = structuredClone(field.defaultValue);
        defaultedFields.push(field.field);
      }
      if (value === undefined) {
        if (field.required)
          issue(
            `body.${field.field}`,
            "control_required_field",
            `field ${field.field} is required`,
            field.type,
            "undefined",
            field.sensitive,
          );
        continue;
      }
      if (value === null && field.nullable) {
        normalizedBody[field.field] = null;
        redactedBody[field.field] = null;
        continue;
      }
      if (!matchesFieldType(value, field.type)) {
        issue(
          `body.${field.field}`,
          "control_field_type",
          `field ${field.field} has an invalid type`,
          field.type,
          valueType(value),
          field.sensitive,
        );
        continue;
      }
      if (typeof value === "number") {
        if (field.minimum !== null && value < field.minimum)
          issue(
            `body.${field.field}`,
            "control_field_minimum",
            `field ${field.field} is below minimum`,
            String(field.minimum),
            String(value),
            field.sensitive,
          );
        if (field.maximum !== null && value > field.maximum)
          issue(
            `body.${field.field}`,
            "control_field_maximum",
            `field ${field.field} exceeds maximum`,
            String(field.maximum),
            String(value),
            field.sensitive,
          );
      }
      if (typeof value === "string" || Array.isArray(value)) {
        if (field.minimumLength !== null && value.length < field.minimumLength)
          issue(
            `body.${field.field}`,
            "control_field_minimum_length",
            `field ${field.field} is too short`,
            String(field.minimumLength),
            String(value.length),
            field.sensitive,
          );
        if (field.maximumLength !== null && value.length > field.maximumLength)
          issue(
            `body.${field.field}`,
            "control_field_maximum_length",
            `field ${field.field} is too long`,
            String(field.maximumLength),
            String(value.length),
            field.sensitive,
          );
      }
      if (typeof value === "string" && field.pattern) {
        let pattern: RegExp;
        try {
          pattern = new RegExp(field.pattern);
        } catch {
          throw new E03RuntimeError(
            "control_field_pattern_invalid",
            `control schema field ${field.field} pattern is invalid`,
          );
        }
        if (!pattern.test(value))
          issue(
            `body.${field.field}`,
            "control_field_pattern",
            `field ${field.field} does not match pattern`,
            field.pattern,
            field.sensitive ? "[redacted]" : value,
            field.sensitive,
          );
      }
      if (field.type === "enum" && !field.enumValues.includes(String(value)))
        issue(
          `body.${field.field}`,
          "control_field_enum",
          `field ${field.field} is not an allowed value`,
          field.enumValues.join("|"),
          field.sensitive ? "[redacted]" : String(value),
          field.sensitive,
        );
      if (
        field.type === "timestamp" &&
        !Number.isFinite(Date.parse(String(value)))
      )
        issue(
          `body.${field.field}`,
          "control_field_timestamp",
          `field ${field.field} is not a timestamp`,
          "ISO-8601",
          field.sensitive ? "[redacted]" : String(value),
          field.sensitive,
        );
      if (field.type === "digest" && !/^[a-f0-9]{64}$/i.test(String(value)))
        issue(
          `body.${field.field}`,
          "control_field_digest",
          `field ${field.field} is not a SHA-256 digest`,
          "64 hexadecimal characters",
          field.sensitive ? "[redacted]" : String(value),
          field.sensitive,
        );
      if (
        Array.isArray(value) &&
        field.uniqueItems &&
        new Set(value.map(String)).size !== value.length
      )
        issue(
          `body.${field.field}`,
          "control_field_unique_items",
          `field ${field.field} contains duplicate items`,
          "unique values",
          String(value.length),
          field.sensitive,
        );
      normalizedBody[field.field] = structuredClone(value);
      redactedBody[field.field] = field.sensitive
        ? "[redacted]"
        : structuredClone(value);
    }
    if (schema.allowUnknown)
      for (const key of unknownFields) {
        normalizedBody[key] = structuredClone(object[key]);
        redactedBody[key] = structuredClone(object[key]);
      }
    const payload = {
      reportId: `control-validation-${digest({
        command,
        schemaId: schema.schemaId,
        body,
        issues: issues.map((value) => value.digest),
      }).slice(0, 32)}`,
      command,
      schemaId: schema.schemaId,
      accepted: issues.length === 0,
      issues,
      normalizedBody,
      redactedBody,
      unknownFields,
      defaultedFields,
      validatedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(command: ControlCommand, body: unknown): ControlValidationReport {
    const report = this.validate(command, body);
    if (!report.accepted)
      throw new E03RuntimeError(
        "control_body_invalid",
        `control body for ${command} failed validation`,
        {
          issues: report.issues.map((value) => ({
            path: value.path,
            code: value.code,
          })),
        },
      );
    return report;
  }

  restore(schemas: readonly ControlBodySchema[]): void {
    const next = new Map<ControlCommand, ControlBodySchema>();
    for (const raw of schemas) {
      const schema = structuredClone(raw);
      assertBodySchema(schema);
      if (next.has(schema.command))
        throw new E03RuntimeError(
          "duplicate_control_body_schema",
          `control body schema ${schema.command} repeats`,
        );
      next.set(schema.command, schema);
    }
    this.schemas = next;
  }

  snapshot(): ControlBodySchema[] {
    return [...this.schemas.values()]
      .sort((left, right) => left.command.localeCompare(right.command))
      .map((schema) => structuredClone(schema));
  }

  private require(command: ControlCommand): ControlBodySchema {
    const schema = this.schemas.get(command);
    if (!schema)
      throw new E03RuntimeError(
        "control_body_schema_missing",
        `control body schema for ${command} is missing`,
      );
    assertBodySchema(schema);
    return schema;
  }

  private fromDescriptor(
    value: ControlCommandDescriptor,
  ): Omit<ControlBodySchema, "schemaId" | "createdAt" | "digest"> {
    const required = new Set(value.requiredBody);
    const fields = [...new Set([...value.requiredBody, ...value.optionalBody])]
      .sort()
      .map((field) =>
        sealFieldConstraint({
          field,
          type:
            field.includes("revision") || field.includes("timeout")
              ? "integer"
              : field.startsWith("allow_") || field.startsWith("force")
                ? "boolean"
                : "string",
          required: required.has(field),
          nullable: false,
          minimum:
            field.includes("revision") || field.includes("timeout") ? 0 : null,
          maximum: field.includes("timeout") ? 86_400_000 : null,
          minimumLength:
            field.includes("revision") || field.includes("timeout") ? null : 1,
          maximumLength:
            field === "prompt" || field === "message" ? 1_000_000 : 4096,
          pattern: field.endsWith("_id") ? "^[^\\r\\n\\0]+$" : null,
          enumValues: [],
          itemType: null,
          uniqueItems: false,
          sensitive:
            field.includes("token") ||
            field.includes("credential") ||
            field.includes("secret"),
          mutable: value.mutation !== "read",
          defaultValue: undefined,
          description: `${value.command} ${field} field`,
        }),
      );
    return {
      command: value.command,
      version: "3.0",
      fields,
      allowUnknown: false,
      maximumProperties: Math.max(8, fields.length + 2),
      maximumDepth: 8,
      mutation: value.mutation,
      owner: value.canonicalOwner,
    };
  }
}

export interface ControlProtocolCapability {
  capability: string;
  minimumVersion: string;
  maximumVersion: string;
  required: boolean;
  digest: string;
}

export interface ControlProtocolPeer {
  peerId: string;
  runtime: string;
  runtimeVersion: string;
  protocolVersions: string[];
  commands: ControlCommand[];
  capabilities: ControlProtocolCapability[];
  maximumFrameBytes: number;
  supportsCompression: boolean;
  supportsStreaming: boolean;
  supportsLostAckRecovery: boolean;
  supportsRevisionFence: boolean;
  supportsPhysicalReceipt: boolean;
  registeredAt: string;
  expiresAt: string;
  revision: number;
  digest: string;
}

export interface ControlProtocolNegotiation {
  negotiationId: string;
  localPeerId: string;
  remotePeerId: string;
  accepted: boolean;
  code: string;
  protocolVersion: string | null;
  commands: ControlCommand[];
  capabilities: string[];
  missingLocalCapabilities: string[];
  missingRemoteCapabilities: string[];
  maximumFrameBytes: number;
  compression: boolean;
  streaming: boolean;
  lostAckRecovery: boolean;
  revisionFence: boolean;
  physicalReceipt: boolean;
  negotiatedAt: string;
  digest: string;
}

function versionParts(version: string): number[] {
  if (!/^\d+(?:\.\d+){0,3}$/.test(version))
    throw new E03RuntimeError(
      "control_protocol_version_invalid",
      `control protocol version ${version} is invalid`,
    );
  return version.split(".").map((part) => Number.parseInt(part, 10));
}

function compareVersions(left: string, right: string): number {
  const a = versionParts(left);
  const b = versionParts(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference) return difference;
  }
  return 0;
}

function versionWithin(
  version: string,
  minimum: string,
  maximum: string,
): boolean {
  return (
    compareVersions(version, minimum) >= 0 &&
    compareVersions(version, maximum) <= 0
  );
}

function assertProtocolCapability(capability: ControlProtocolCapability): void {
  const { digest: checksum, ...payload } = capability;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_capability_checksum",
      `control capability ${capability.capability} checksum mismatch`,
    );
  if (
    !capability.capability ||
    compareVersions(capability.minimumVersion, capability.maximumVersion) > 0
  )
    throw new E03RuntimeError(
      "control_capability_invalid",
      `control capability ${capability.capability} is invalid`,
    );
}

function assertProtocolPeer(peer: ControlProtocolPeer): void {
  const { digest: checksum, ...payload } = peer;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_peer_checksum",
      `control peer ${peer.peerId} checksum mismatch`,
    );
  if (
    !peer.peerId ||
    !peer.runtime ||
    !peer.runtimeVersion ||
    !peer.protocolVersions.length ||
    peer.maximumFrameBytes < 1024 ||
    peer.revision < 1
  )
    throw new E03RuntimeError(
      "control_peer_invalid",
      `control peer ${peer.peerId} is invalid`,
    );
  for (const version of peer.protocolVersions) versionParts(version);
  for (const capability of peer.capabilities)
    assertProtocolCapability(capability);
}

export class ControlProtocolNegotiator {
  private peers = new Map<string, ControlProtocolPeer>();

  register(
    input: Omit<
      ControlProtocolPeer,
      "registeredAt" | "expiresAt" | "revision" | "digest"
    > & {
      ttlMs: number;
      now?: string;
    },
  ): ControlProtocolPeer {
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "control_peer_ttl_invalid",
        "control peer TTL is invalid",
      );
    const existing = this.peers.get(input.peerId);
    const registeredAt = input.now ?? new Date().toISOString();
    const capabilities = input.capabilities
      .map((capability) => {
        assertProtocolCapability(capability);
        return structuredClone(capability);
      })
      .sort((left, right) => left.capability.localeCompare(right.capability));
    const payload = {
      peerId: input.peerId.trim(),
      runtime: input.runtime.trim(),
      runtimeVersion: input.runtimeVersion.trim(),
      protocolVersions: [...new Set(input.protocolVersions)]
        .sort(compareVersions)
        .reverse(),
      commands: [...new Set(input.commands)].sort(),
      capabilities,
      maximumFrameBytes: input.maximumFrameBytes,
      supportsCompression: input.supportsCompression,
      supportsStreaming: input.supportsStreaming,
      supportsLostAckRecovery: input.supportsLostAckRecovery,
      supportsRevisionFence: input.supportsRevisionFence,
      supportsPhysicalReceipt: input.supportsPhysicalReceipt,
      registeredAt: existing?.registeredAt ?? registeredAt,
      expiresAt: new Date(Date.parse(registeredAt) + input.ttlMs).toISOString(),
      revision: (existing?.revision ?? 0) + 1,
    };
    const peer = { ...payload, digest: digest(payload) };
    assertProtocolPeer(peer);
    this.peers.set(peer.peerId, peer);
    return structuredClone(peer);
  }

  negotiate(
    localPeerId: string,
    remotePeerId: string,
    now = new Date().toISOString(),
  ): ControlProtocolNegotiation {
    const local = this.require(localPeerId, now);
    const remote = this.require(remotePeerId, now);
    const sharedVersions = local.protocolVersions
      .filter((version) => remote.protocolVersions.includes(version))
      .sort(compareVersions)
      .reverse();
    const protocolVersion = sharedVersions[0] ?? null;
    const localCapabilities = new Map(
      local.capabilities.map((capability) => [
        capability.capability,
        capability,
      ]),
    );
    const remoteCapabilities = new Map(
      remote.capabilities.map((capability) => [
        capability.capability,
        capability,
      ]),
    );
    const missingLocalCapabilities = remote.capabilities
      .filter(
        (capability) =>
          capability.required && !localCapabilities.has(capability.capability),
      )
      .map((capability) => capability.capability)
      .sort();
    const missingRemoteCapabilities = local.capabilities
      .filter(
        (capability) =>
          capability.required && !remoteCapabilities.has(capability.capability),
      )
      .map((capability) => capability.capability)
      .sort();
    const capabilities = [...localCapabilities.keys()]
      .filter((name) => {
        const left = localCapabilities.get(name)!;
        const right = remoteCapabilities.get(name);
        if (!right || !protocolVersion) return false;
        return (
          versionWithin(
            protocolVersion,
            left.minimumVersion,
            left.maximumVersion,
          ) &&
          versionWithin(
            protocolVersion,
            right.minimumVersion,
            right.maximumVersion,
          )
        );
      })
      .sort();
    let code = "control_protocol_accepted";
    if (!protocolVersion) code = "control_protocol_version_mismatch";
    else if (missingLocalCapabilities.length)
      code = "control_protocol_local_capability_missing";
    else if (missingRemoteCapabilities.length)
      code = "control_protocol_remote_capability_missing";
    else if (!local.supportsRevisionFence || !remote.supportsRevisionFence)
      code = "control_protocol_revision_fence_required";
    else if (!local.supportsPhysicalReceipt || !remote.supportsPhysicalReceipt)
      code = "control_protocol_physical_receipt_required";
    const payload = {
      negotiationId: `control-negotiation-${digest({
        localPeerId,
        remotePeerId,
        localRevision: local.revision,
        remoteRevision: remote.revision,
      }).slice(0, 32)}`,
      localPeerId,
      remotePeerId,
      accepted: code === "control_protocol_accepted",
      code,
      protocolVersion,
      commands: local.commands
        .filter((command) => remote.commands.includes(command))
        .sort(),
      capabilities,
      missingLocalCapabilities,
      missingRemoteCapabilities,
      maximumFrameBytes: Math.min(
        local.maximumFrameBytes,
        remote.maximumFrameBytes,
      ),
      compression: local.supportsCompression && remote.supportsCompression,
      streaming: local.supportsStreaming && remote.supportsStreaming,
      lostAckRecovery:
        local.supportsLostAckRecovery && remote.supportsLostAckRecovery,
      revisionFence:
        local.supportsRevisionFence && remote.supportsRevisionFence,
      physicalReceipt:
        local.supportsPhysicalReceipt && remote.supportsPhysicalReceipt,
      negotiatedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(
    localPeerId: string,
    remotePeerId: string,
    now?: string,
  ): ControlProtocolNegotiation {
    const negotiation = this.negotiate(localPeerId, remotePeerId, now);
    if (!negotiation.accepted)
      throw new E03RuntimeError(
        negotiation.code,
        `control protocol negotiation ${negotiation.negotiationId} failed`,
        {
          missingLocalCapabilities: negotiation.missingLocalCapabilities,
          missingRemoteCapabilities: negotiation.missingRemoteCapabilities,
        },
      );
    return negotiation;
  }

  restore(peers: readonly ControlProtocolPeer[]): void {
    const next = new Map<string, ControlProtocolPeer>();
    for (const raw of peers) {
      const peer = structuredClone(raw);
      assertProtocolPeer(peer);
      if (next.has(peer.peerId))
        throw new E03RuntimeError(
          "duplicate_control_peer",
          `control peer ${peer.peerId} repeats`,
        );
      next.set(peer.peerId, peer);
    }
    this.peers = next;
  }

  snapshot(): ControlProtocolPeer[] {
    return [...this.peers.values()]
      .sort((left, right) => left.peerId.localeCompare(right.peerId))
      .map((peer) => structuredClone(peer));
  }

  private require(peerId: string, now: string): ControlProtocolPeer {
    const peer = this.peers.get(peerId);
    if (!peer)
      throw new E03RuntimeError(
        "control_peer_missing",
        `control peer ${peerId} is missing`,
      );
    assertProtocolPeer(peer);
    if (Date.parse(now) >= Date.parse(peer.expiresAt))
      throw new E03RuntimeError(
        "control_peer_expired",
        `control peer ${peerId} registration expired`,
      );
    return peer;
  }
}

export type ControlResponsePhase =
  | "prepare"
  | "effect"
  | "receipt"
  | "commit"
  | "ack"
  | "rejected";

export interface ControlResponseContract {
  contractId: string;
  command: ControlCommand;
  phases: ControlResponsePhase[];
  terminalPhases: ControlResponsePhase[];
  requiredFields: Record<ControlResponsePhase, string[]>;
  forbiddenFields: Record<ControlResponsePhase, string[]>;
  requiresRevisionAdvance: boolean;
  requiresReceipt: boolean;
  permitsLostAckRecovery: boolean;
  owner: string;
  version: string;
  digest: string;
}

export interface ControlResponseValidation {
  validationId: string;
  command: ControlCommand;
  contractId: string;
  accepted: boolean;
  code: string;
  phase: ControlResponsePhase;
  missingFields: string[];
  forbiddenFields: string[];
  revisionValid: boolean;
  requestMatched: boolean;
  commandMatched: boolean;
  validatedAt: string;
  digest: string;
}

function assertResponseContract(contract: ControlResponseContract): void {
  const { digest: checksum, ...payload } = contract;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_response_contract_checksum",
      `control response contract ${contract.contractId} checksum mismatch`,
    );
  if (!contract.phases.length || !contract.terminalPhases.length)
    throw new E03RuntimeError(
      "control_response_contract_phases",
      `control response contract ${contract.contractId} has no phases`,
    );
  for (const phase of contract.terminalPhases)
    if (!contract.phases.includes(phase))
      throw new E03RuntimeError(
        "control_response_terminal_phase",
        `control response terminal phase ${phase} is not allowed`,
      );
}

export class ControlResponseContractRegistry {
  private contracts = new Map<ControlCommand, ControlResponseContract>();

  constructor(catalog = new ControlCommandCatalog()) {
    for (const descriptor of catalog.snapshot()) {
      const mutation = descriptor.mutation !== "read";
      const phases: ControlResponsePhase[] = mutation
        ? descriptor.requiresPhysicalEffect
          ? ["prepare", "effect", "receipt", "commit", "ack", "rejected"]
          : ["prepare", "commit", "ack", "rejected"]
        : ["ack", "rejected"];
      this.register({
        command: descriptor.command,
        phases,
        terminalPhases: ["ack", "rejected"],
        requiredFields: {
          prepare: ["request_id", "command", "revision", "state"],
          effect: ["request_id", "command", "revision", "state"],
          receipt: ["request_id", "command", "revision", "state"],
          commit: ["request_id", "command", "revision", "state"],
          ack: ["request_id", "command", "revision", "state", "ok"],
          rejected: ["request_id", "command", "revision", "error", "ok"],
        },
        forbiddenFields: {
          prepare: ["error"],
          effect: ["error"],
          receipt: ["error"],
          commit: ["error"],
          ack: ["error"],
          rejected: ["result"],
        },
        requiresRevisionAdvance: mutation,
        requiresReceipt: descriptor.requiresPhysicalEffect,
        permitsLostAckRecovery: mutation,
        owner: descriptor.canonicalOwner,
        version: "3.0",
      });
    }
  }

  register(
    input: Omit<ControlResponseContract, "contractId" | "digest"> & {
      contractId?: string;
    },
  ): ControlResponseContract {
    const payload = {
      ...structuredClone(input),
      contractId:
        input.contractId?.trim() ||
        `control-response-contract-${digest({ command: input.command, version: input.version }).slice(0, 32)}`,
    };
    const contract = { ...payload, digest: digest(payload) };
    assertResponseContract(contract);
    this.contracts.set(contract.command, contract);
    return structuredClone(contract);
  }

  validate(input: {
    envelope: E03ControlEnvelope;
    response: Record<string, unknown>;
    priorRevision: number;
  }): ControlResponseValidation {
    const contract = this.require(input.envelope.command);
    const phase = String(input.response.phase ?? "") as ControlResponsePhase;
    const required = contract.requiredFields[phase] ?? [];
    const forbidden = contract.forbiddenFields[phase] ?? [];
    const missingFields = required.filter(
      (field) => !present(input.response[field]),
    );
    const forbiddenFields = forbidden.filter((field) =>
      present(input.response[field]),
    );
    const revision = input.response.revision;
    const revisionValid =
      typeof revision === "number" &&
      Number.isSafeInteger(revision) &&
      revision >= 0 &&
      (!contract.requiresRevisionAdvance ||
        phase === "rejected" ||
        revision > input.priorRevision);
    const requestMatched =
      input.response.request_id === input.envelope.request_id;
    const commandMatched = input.response.command === input.envelope.command;
    let code = "control_response_accepted";
    if (!contract.phases.includes(phase))
      code = "control_response_phase_denied";
    else if (missingFields.length) code = "control_response_field_missing";
    else if (forbiddenFields.length) code = "control_response_field_forbidden";
    else if (!revisionValid) code = "control_response_revision_invalid";
    else if (!requestMatched) code = "control_response_request_mismatch";
    else if (!commandMatched) code = "control_response_command_mismatch";
    const payload = {
      validationId: `control-response-validation-${digest({
        requestId: input.envelope.request_id,
        command: input.envelope.command,
        response: input.response,
      }).slice(0, 32)}`,
      command: input.envelope.command,
      contractId: contract.contractId,
      accepted: code === "control_response_accepted",
      code,
      phase,
      missingFields,
      forbiddenFields,
      revisionValid,
      requestMatched,
      commandMatched,
      validatedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(input: {
    envelope: E03ControlEnvelope;
    response: Record<string, unknown>;
    priorRevision: number;
  }): ControlResponseValidation {
    const validation = this.validate(input);
    if (!validation.accepted)
      throw new E03RuntimeError(
        validation.code,
        `control response ${validation.validationId} failed contract`,
        {
          missingFields: validation.missingFields,
          forbiddenFields: validation.forbiddenFields,
        },
      );
    return validation;
  }

  snapshot(): ControlResponseContract[] {
    return [...this.contracts.values()]
      .sort((left, right) => left.command.localeCompare(right.command))
      .map((contract) => structuredClone(contract));
  }

  private require(command: ControlCommand): ControlResponseContract {
    const contract = this.contracts.get(command);
    if (!contract)
      throw new E03RuntimeError(
        "control_response_contract_missing",
        `control response contract for ${command} is missing`,
      );
    assertResponseContract(contract);
    return contract;
  }
}

export type ControlCredentialState = "active" | "retiring" | "revoked";

export interface ControlCredential {
  credentialId: string;
  principalId: string;
  algorithm: "hmac-sha256";
  secret: string;
  allowedCommands: ControlCommand[];
  allowedRunIds: string[];
  state: ControlCredentialState;
  validFrom: string;
  validUntil: string;
  createdAt: string;
  retiredAt: string | null;
  revokedAt: string | null;
  revision: number;
  digest: string;
}

export interface SignedControlEnvelope {
  envelope: E03ControlEnvelope;
  credentialId: string;
  principalId: string;
  nonce: string;
  issuedAt: string;
  expiresAt: string;
  signature: string;
  digest: string;
}

export interface ControlAuthenticationResult {
  authenticationId: string;
  credentialId: string;
  principalId: string;
  requestId: string;
  command: ControlCommand;
  decision: "authenticated" | "denied";
  reason: string;
  verifiedAt: string;
  credentialRevision: number;
  digest: string;
}

function canonicalControlEnvelope(envelope: E03ControlEnvelope): string {
  const normalize = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === "object")
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, item]) => [key, normalize(item)]),
      );
    return value;
  };
  return JSON.stringify(normalize(envelope));
}

function signControlPayload(
  secret: string,
  envelope: E03ControlEnvelope,
  nonce: string,
  issuedAt: string,
  expiresAt: string,
): string {
  return createHmac("sha256", secret)
    .update(canonicalControlEnvelope(envelope))
    .update("\n")
    .update(nonce)
    .update("\n")
    .update(issuedAt)
    .update("\n")
    .update(expiresAt)
    .digest("hex");
}

function assertControlCredential(credential: ControlCredential): void {
  const { digest: checksum, ...payload } = credential;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_credential_digest",
      `control credential ${credential.credentialId} digest is invalid`,
    );
  if (!credential.credentialId || !credential.principalId || !credential.secret)
    throw new E03RuntimeError(
      "control_credential_identity",
      "control credential identity is incomplete",
    );
  if (credential.secret.length < 32)
    throw new E03RuntimeError(
      "control_credential_secret",
      "control credential secret must contain at least 32 characters",
    );
  if (Date.parse(credential.validFrom) >= Date.parse(credential.validUntil))
    throw new E03RuntimeError(
      "control_credential_interval",
      "control credential validity interval is invalid",
    );
  if (!Number.isSafeInteger(credential.revision) || credential.revision < 1)
    throw new E03RuntimeError(
      "control_credential_revision",
      "control credential revision is invalid",
    );
  if (credential.state === "revoked" && credential.revokedAt === null)
    throw new E03RuntimeError(
      "control_credential_state",
      "revoked control credential requires revokedAt",
    );
}

function assertSignedControlEnvelope(signed: SignedControlEnvelope): void {
  const { digest: checksum, ...payload } = signed;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "signed_control_digest",
      `signed control request ${signed.envelope.request_id} digest is invalid`,
    );
  if (
    !signed.credentialId ||
    !signed.principalId ||
    !signed.nonce ||
    !signed.signature
  )
    throw new E03RuntimeError(
      "signed_control_identity",
      "signed control request identity is incomplete",
    );
  if (Date.parse(signed.issuedAt) >= Date.parse(signed.expiresAt))
    throw new E03RuntimeError(
      "signed_control_interval",
      "signed control request expiry must follow issuance",
    );
}

export class ControlEnvelopeAuthenticator {
  private credentials = new Map<string, ControlCredential>();
  private nonceExpiry = new Map<string, string>();
  private authentications = new Map<string, ControlAuthenticationResult>();

  register(input: {
    credentialId: string;
    principalId: string;
    secret: string;
    allowedCommands?: readonly ControlCommand[];
    allowedRunIds?: readonly string[];
    validFrom: string;
    validUntil: string;
    createdAt: string;
  }): ControlCredential {
    const payload = {
      credentialId: input.credentialId.trim(),
      principalId: input.principalId.trim(),
      algorithm: "hmac-sha256" as const,
      secret: input.secret,
      allowedCommands: [...new Set(input.allowedCommands ?? [])],
      allowedRunIds: [...new Set(input.allowedRunIds ?? [])],
      state: "active" as const,
      validFrom: input.validFrom,
      validUntil: input.validUntil,
      createdAt: input.createdAt,
      retiredAt: null,
      revokedAt: null,
      revision: 1,
    };
    const credential = { ...payload, digest: digest(payload) };
    assertControlCredential(credential);
    const existing = this.credentials.get(credential.credentialId);
    if (existing && existing.digest !== credential.digest)
      throw new E03RuntimeError(
        "control_credential_conflict",
        `control credential ${credential.credentialId} already exists`,
      );
    this.credentials.set(credential.credentialId, credential);
    return structuredClone(credential);
  }

  sign(input: {
    credentialId: string;
    envelope: E03ControlEnvelope;
    nonce: string;
    issuedAt: string;
    expiresAt: string;
  }): SignedControlEnvelope {
    const credential = this.requireCredential(input.credentialId);
    if (credential.state === "revoked")
      throw new E03RuntimeError(
        "control_credential_revoked",
        `control credential ${credential.credentialId} is revoked`,
      );
    const signature = signControlPayload(
      credential.secret,
      input.envelope,
      input.nonce,
      input.issuedAt,
      input.expiresAt,
    );
    const payload = {
      envelope: structuredClone(input.envelope),
      credentialId: credential.credentialId,
      principalId: credential.principalId,
      nonce: input.nonce.trim(),
      issuedAt: input.issuedAt,
      expiresAt: input.expiresAt,
      signature,
    };
    const signed = { ...payload, digest: digest(payload) };
    assertSignedControlEnvelope(signed);
    return structuredClone(signed);
  }

  authenticate(
    signed: SignedControlEnvelope,
    verifiedAt: string,
  ): ControlAuthenticationResult {
    assertSignedControlEnvelope(signed);
    const credential = this.requireCredential(signed.credentialId);
    let decision: ControlAuthenticationResult["decision"] = "authenticated";
    let reason = "signature and authority verified";
    if (credential.principalId !== signed.principalId) {
      decision = "denied";
      reason = "principal does not match credential";
    } else if (credential.state === "revoked") {
      decision = "denied";
      reason = "credential is revoked";
    } else if (
      Date.parse(verifiedAt) < Date.parse(credential.validFrom) ||
      Date.parse(verifiedAt) >= Date.parse(credential.validUntil)
    ) {
      decision = "denied";
      reason = "credential is outside its validity interval";
    } else if (
      Date.parse(verifiedAt) < Date.parse(signed.issuedAt) ||
      Date.parse(verifiedAt) >= Date.parse(signed.expiresAt)
    ) {
      decision = "denied";
      reason = "signed request is outside its validity interval";
    } else if (
      credential.allowedCommands.length &&
      !credential.allowedCommands.includes(signed.envelope.command)
    ) {
      decision = "denied";
      reason = "command is outside credential authority";
    } else if (
      credential.allowedRunIds.length &&
      !credential.allowedRunIds.includes(signed.envelope.run_id)
    ) {
      decision = "denied";
      reason = "run is outside credential authority";
    } else if (
      this.nonceExpiry.has(`${credential.credentialId}:${signed.nonce}`)
    ) {
      decision = "denied";
      reason = "nonce was already consumed";
    } else {
      const expected = Buffer.from(
        signControlPayload(
          credential.secret,
          signed.envelope,
          signed.nonce,
          signed.issuedAt,
          signed.expiresAt,
        ),
        "hex",
      );
      const actual = Buffer.from(signed.signature, "hex");
      if (
        expected.length !== actual.length ||
        !timingSafeEqual(expected, actual)
      ) {
        decision = "denied";
        reason = "signature is invalid";
      }
    }
    if (decision === "authenticated")
      this.nonceExpiry.set(
        `${credential.credentialId}:${signed.nonce}`,
        signed.expiresAt,
      );
    const payload = {
      authenticationId: `control-auth-${digest({ request: signed.envelope.request_id, verifiedAt }).slice(0, 24)}`,
      credentialId: credential.credentialId,
      principalId: signed.principalId,
      requestId: signed.envelope.request_id,
      command: signed.envelope.command,
      decision,
      reason,
      verifiedAt,
      credentialRevision: credential.revision,
    };
    const result = { ...payload, digest: digest(payload) };
    this.authentications.set(result.authenticationId, result);
    return structuredClone(result);
  }

  retire(credentialId: string, retiredAt: string): ControlCredential {
    const credential = this.requireCredential(credentialId);
    if (credential.state !== "active")
      throw new E03RuntimeError(
        "control_credential_retire_state",
        `control credential ${credentialId} is ${credential.state}`,
      );
    return this.transitionCredential(credential, {
      state: "retiring",
      retiredAt,
    });
  }

  revoke(credentialId: string, revokedAt: string): ControlCredential {
    const credential = this.requireCredential(credentialId);
    if (credential.state === "revoked") return structuredClone(credential);
    return this.transitionCredential(credential, {
      state: "revoked",
      revokedAt,
    });
  }

  sweepNonces(now: string): number {
    let removed = 0;
    for (const [nonce, expiresAt] of this.nonceExpiry)
      if (Date.parse(now) >= Date.parse(expiresAt)) {
        this.nonceExpiry.delete(nonce);
        removed += 1;
      }
    return removed;
  }

  snapshot(): {
    credentials: ControlCredential[];
    nonces: Array<[string, string]>;
    authentications: ControlAuthenticationResult[];
  } {
    return {
      credentials: [...this.credentials.values()].map((value) =>
        structuredClone(value),
      ),
      nonces: [...this.nonceExpiry],
      authentications: [...this.authentications.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    credentials: readonly ControlCredential[];
    nonces: readonly (readonly [string, string])[];
    authentications: readonly ControlAuthenticationResult[];
  }): void {
    const credentials = new Map<string, ControlCredential>();
    const nonces = new Map<string, string>();
    const authentications = new Map<string, ControlAuthenticationResult>();
    for (const credential of input.credentials) {
      assertControlCredential(credential);
      if (credentials.has(credential.credentialId))
        throw new E03RuntimeError(
          "control_credential_restore_duplicate",
          `duplicate control credential ${credential.credentialId}`,
        );
      credentials.set(credential.credentialId, structuredClone(credential));
    }
    for (const [nonce, expiresAt] of input.nonces) {
      if (!nonce || !Number.isFinite(Date.parse(expiresAt)))
        throw new E03RuntimeError(
          "control_nonce_restore_invalid",
          "control nonce expiry row is invalid",
        );
      if (nonces.has(nonce))
        throw new E03RuntimeError(
          "control_nonce_restore_duplicate",
          `duplicate control nonce ${nonce}`,
        );
      nonces.set(nonce, expiresAt);
    }
    for (const result of input.authentications) {
      const { digest: checksum, ...payload } = result;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "control_authentication_restore_digest",
          `control authentication ${result.authenticationId} digest is invalid`,
        );
      if (!credentials.has(result.credentialId))
        throw new E03RuntimeError(
          "control_authentication_restore_credential",
          `control authentication ${result.authenticationId} has no credential`,
        );
      if (authentications.has(result.authenticationId))
        throw new E03RuntimeError(
          "control_authentication_restore_duplicate",
          `duplicate control authentication ${result.authenticationId}`,
        );
      authentications.set(result.authenticationId, structuredClone(result));
    }
    this.credentials = credentials;
    this.nonceExpiry = nonces;
    this.authentications = authentications;
  }

  private requireCredential(credentialId: string): ControlCredential {
    const credential = this.credentials.get(credentialId);
    if (!credential)
      throw new E03RuntimeError(
        "control_credential_missing",
        `control credential ${credentialId} does not exist`,
      );
    assertControlCredential(credential);
    return credential;
  }

  private transitionCredential(
    credential: ControlCredential,
    patch: Partial<
      Omit<ControlCredential, "credentialId" | "revision" | "digest">
    >,
  ): ControlCredential {
    const { digest: _, ...prior } = credential;
    const payload = {
      ...prior,
      ...patch,
      credentialId: credential.credentialId,
      revision: credential.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlCredential(next);
    this.credentials.set(next.credentialId, next);
    return structuredClone(next);
  }
}

export type ControlBatchMode = "atomic" | "best-effort" | "ordered";

export interface ControlBatchItem {
  itemId: string;
  ordinal: number;
  envelope: E03ControlEnvelope;
  dependencyItemIds: string[];
  optional: boolean;
  compensationCommand: ControlCommand | null;
  digest: string;
}

export interface ControlBatch {
  batchId: string;
  runId: string;
  sessionId: string;
  parentTaskId: string;
  mode: ControlBatchMode;
  items: ControlBatchItem[];
  maximumConcurrency: number;
  stopOnFailure: boolean;
  createdAt: string;
  deadlineAt: string;
  digest: string;
}

export interface ControlBatchValidation {
  batchId: string;
  valid: boolean;
  executionOrder: string[];
  parallelGroups: string[][];
  cyclePaths: string[][];
  duplicateRequestIds: string[];
  duplicateIdempotencyKeys: string[];
  ownershipBoundaries: Array<{
    owner: "E01" | "E02" | "E03";
    itemIds: string[];
  }>;
  errors: string[];
  digest: string;
}

function controlOwner(command: ControlCommand): "E01" | "E02" | "E03" {
  if (["context.compact", "context.clear", "session.model"].includes(command))
    return "E01";
  if (["permission.inspect", "mcp.inspect", "skills.inspect"].includes(command))
    return "E02";
  return "E03";
}

function assertControlBatchItem(item: ControlBatchItem): void {
  const { digest: checksum, ...payload } = item;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_batch_item_digest",
      `control batch item ${item.itemId} digest is invalid`,
    );
  if (!item.itemId || !Number.isSafeInteger(item.ordinal) || item.ordinal < 0)
    throw new E03RuntimeError(
      "control_batch_item_identity",
      "control batch item identity is invalid",
    );
  if (item.dependencyItemIds.includes(item.itemId))
    throw new E03RuntimeError(
      "control_batch_item_self_dependency",
      `control batch item ${item.itemId} depends on itself`,
    );
}

function assertControlBatch(batch: ControlBatch): void {
  const { digest: checksum, ...payload } = batch;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_batch_digest",
      `control batch ${batch.batchId} digest is invalid`,
    );
  if (!batch.batchId || !batch.runId || !batch.sessionId || !batch.parentTaskId)
    throw new E03RuntimeError(
      "control_batch_identity",
      "control batch identity is incomplete",
    );
  if (
    !Number.isSafeInteger(batch.maximumConcurrency) ||
    batch.maximumConcurrency < 1
  )
    throw new E03RuntimeError(
      "control_batch_concurrency",
      "control batch concurrency is invalid",
    );
  if (Date.parse(batch.createdAt) >= Date.parse(batch.deadlineAt))
    throw new E03RuntimeError(
      "control_batch_deadline",
      "control batch deadline is invalid",
    );
  const ordinals = new Set<number>();
  const itemIds = new Set<string>();
  for (const item of batch.items) {
    assertControlBatchItem(item);
    if (ordinals.has(item.ordinal) || itemIds.has(item.itemId))
      throw new E03RuntimeError(
        "control_batch_duplicate_item",
        "control batch contains duplicate item identity",
      );
    ordinals.add(item.ordinal);
    itemIds.add(item.itemId);
    if (
      item.envelope.run_id !== batch.runId ||
      item.envelope.session_id !== batch.sessionId ||
      item.envelope.parent_task_id !== batch.parentTaskId
    )
      throw new E03RuntimeError(
        "control_batch_item_custody",
        `control batch item ${item.itemId} belongs to another session`,
      );
  }
}

export class ControlBatchSchema {
  private readonly schema = new ControlSchema();

  parse(value: unknown): ControlBatch {
    const root = requireObject(value, "control_batch");
    const batchId = requireString(
      root.batch_id,
      "control_batch.batch_id",
      1,
      512,
    );
    const runId = requireString(root.run_id, "control_batch.run_id", 1, 512);
    const sessionId = requireString(
      root.session_id,
      "control_batch.session_id",
      1,
      512,
    );
    const parentTaskId = requireString(
      root.parent_task_id,
      "control_batch.parent_task_id",
      1,
      512,
    );
    const mode = String(root.mode ?? "ordered") as ControlBatchMode;
    if (!(["atomic", "best-effort", "ordered"] as string[]).includes(mode))
      throw new E03RuntimeError(
        "control_batch_mode",
        `control batch mode ${mode} is invalid`,
      );
    if (!Array.isArray(root.items) || !root.items.length)
      throw new E03RuntimeError(
        "control_batch_items",
        "control batch requires at least one item",
      );
    const items = root.items.map((raw, ordinal) => {
      const itemValue = requireObject(raw, `control_batch.items[${ordinal}]`);
      const envelope = this.schema.parse(itemValue.envelope);
      const dependencyItemIds = Array.isArray(itemValue.dependency_item_ids)
        ? itemValue.dependency_item_ids.map((value, index) =>
            requireString(
              value,
              `control_batch.items[${ordinal}].dependency_item_ids[${index}]`,
              1,
              512,
            ),
          )
        : [];
      const payload = {
        itemId: requireString(
          itemValue.item_id,
          `control_batch.items[${ordinal}].item_id`,
          1,
          512,
        ),
        ordinal,
        envelope,
        dependencyItemIds: [...new Set(dependencyItemIds)],
        optional: itemValue.optional === true,
        compensationCommand:
          itemValue.compensation_command === undefined ||
          itemValue.compensation_command === null
            ? null
            : (requireString(
                itemValue.compensation_command,
                `control_batch.items[${ordinal}].compensation_command`,
                1,
                128,
              ) as ControlCommand),
      };
      const item = { ...payload, digest: digest(payload) };
      assertControlBatchItem(item);
      return item;
    });
    const payload = {
      batchId,
      runId,
      sessionId,
      parentTaskId,
      mode,
      items,
      maximumConcurrency: requireInteger(
        root.maximum_concurrency ?? 1,
        "control_batch.maximum_concurrency",
        1,
      ),
      stopOnFailure: root.stop_on_failure !== false,
      createdAt: requireString(
        root.created_at,
        "control_batch.created_at",
        1,
        128,
      ),
      deadlineAt: requireString(
        root.deadline_at,
        "control_batch.deadline_at",
        1,
        128,
      ),
    };
    const batch = { ...payload, digest: digest(payload) };
    assertControlBatch(batch);
    return batch;
  }

  validate(batch: ControlBatch): ControlBatchValidation {
    assertControlBatch(batch);
    const errors: string[] = [];
    const byId = new Map(batch.items.map((item) => [item.itemId, item]));
    for (const item of batch.items)
      for (const dependencyId of item.dependencyItemIds)
        if (!byId.has(dependencyId))
          errors.push(
            `item ${item.itemId} depends on missing item ${dependencyId}`,
          );
    const duplicateRequestIds = duplicates(
      batch.items.map((item) => item.envelope.request_id),
    );
    const duplicateIdempotencyKeys = duplicates(
      batch.items.map((item) => item.envelope.idempotency_key),
    );
    if (duplicateRequestIds.length)
      errors.push(`duplicate request ids: ${duplicateRequestIds.join(", ")}`);
    if (duplicateIdempotencyKeys.length)
      errors.push(
        `duplicate idempotency keys: ${duplicateIdempotencyKeys.join(", ")}`,
      );
    if (batch.mode === "atomic") {
      const missingCompensation = batch.items
        .filter((item) => !item.optional && item.compensationCommand === null)
        .map((item) => item.itemId);
      if (missingCompensation.length)
        errors.push(
          `atomic items lack compensation: ${missingCompensation.join(", ")}`,
        );
    }
    const { order, groups, cycles } = orderControlBatchItems(batch.items);
    if (cycles.length) errors.push(`control batch contains dependency cycles`);
    const owners = new Map<"E01" | "E02" | "E03", string[]>();
    for (const item of batch.items) {
      const owner = controlOwner(item.envelope.command);
      owners.set(owner, [...(owners.get(owner) ?? []), item.itemId]);
    }
    const payload = {
      batchId: batch.batchId,
      valid: errors.length === 0,
      executionOrder: order,
      parallelGroups: groups,
      cyclePaths: cycles,
      duplicateRequestIds,
      duplicateIdempotencyKeys,
      ownershipBoundaries: [...owners].map(([owner, itemIds]) => ({
        owner,
        itemIds,
      })),
      errors,
    };
    return { ...payload, digest: digest(payload) };
  }
}

function duplicates(values: readonly string[]): string[] {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return [...counts]
    .filter(([, count]) => count > 1)
    .map(([value]) => value)
    .sort();
}

function orderControlBatchItems(items: readonly ControlBatchItem[]): {
  order: string[];
  groups: string[][];
  cycles: string[][];
} {
  const byId = new Map(items.map((item) => [item.itemId, item]));
  const remaining = new Map(
    items.map((item) => [
      item.itemId,
      new Set(
        item.dependencyItemIds.filter((dependencyId) => byId.has(dependencyId)),
      ),
    ]),
  );
  const order: string[] = [];
  const groups: string[][] = [];
  while (remaining.size) {
    const ready = [...remaining]
      .filter(([, dependencies]) => dependencies.size === 0)
      .map(([itemId]) => itemId)
      .sort(
        (left, right) =>
          byId.get(left)!.ordinal - byId.get(right)!.ordinal ||
          left.localeCompare(right),
      );
    if (!ready.length) break;
    groups.push(ready);
    order.push(...ready);
    for (const itemId of ready) remaining.delete(itemId);
    for (const dependencies of remaining.values())
      for (const itemId of ready) dependencies.delete(itemId);
  }
  const cycles = remaining.size
    ? traceBatchCycles([...remaining.keys()], byId)
    : [];
  return { order, groups, cycles };
}

function traceBatchCycles(
  pending: readonly string[],
  byId: ReadonlyMap<string, ControlBatchItem>,
): string[][] {
  const output = new Map<string, string[]>();
  const walk = (itemId: string, path: string[]): void => {
    const offset = path.indexOf(itemId);
    if (offset >= 0) {
      const cycle = [...path.slice(offset), itemId];
      const rotations = cycle
        .slice(0, -1)
        .map((_, index, body) => [
          ...body.slice(index),
          ...body.slice(0, index),
          body[index]!,
        ]);
      rotations.sort((left, right) =>
        left.join("->").localeCompare(right.join("->")),
      );
      output.set(rotations[0]!.join("->"), rotations[0]!);
      return;
    }
    const item = byId.get(itemId);
    if (!item) return;
    for (const dependencyId of item.dependencyItemIds)
      walk(dependencyId, [...path, itemId]);
  };
  for (const itemId of pending) walk(itemId, []);
  return [...output.values()];
}

export interface ControlAuthorizationPolicy {
  policyId: string;
  name: string;
  commandPatterns: string[];
  principals: string[];
  requiredClaims: Record<string, string[]>;
  effect: "allow" | "deny";
  priority: number;
  validFrom: string;
  validUntil: string | null;
  state: "active" | "disabled" | "revoked";
  revision: number;
  digest: string;
}
export interface ControlAuthorizationDecision {
  decisionId: string;
  requestId: string;
  principalId: string;
  command: ControlCommand;
  outcome: "allowed" | "denied";
  matchedPolicyIds: string[];
  missingClaims: string[];
  reason: string;
  decidedAt: string;
  sequence: number;
  previousDigest: string;
  digest: string;
}
function assertAuthorizationPolicy(value: ControlAuthorizationPolicy): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_authorization_policy_digest",
      `control authorization policy ${value.policyId} is corrupt`,
    );
  if (
    !value.policyId ||
    !value.name ||
    !value.commandPatterns.length ||
    !value.principals.length ||
    !Number.isSafeInteger(value.priority) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.validFrom)) ||
    (value.validUntil !== null && Number.isNaN(Date.parse(value.validUntil)))
  )
    throw new E03RuntimeError(
      "control_authorization_policy",
      `control authorization policy ${value.policyId} is invalid`,
    );
}
function assertAuthorizationDecision(
  value: ControlAuthorizationDecision,
): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_authorization_decision_digest",
      `control authorization decision ${value.decisionId} is corrupt`,
    );
  if (
    !value.decisionId ||
    !value.requestId ||
    !value.principalId ||
    !value.reason ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "control_authorization_decision",
      `control authorization decision ${value.decisionId} is invalid`,
    );
}
export class ControlCommandAuthorizationRuntime {
  private policies = new Map<string, ControlAuthorizationPolicy>();
  private decisions: ControlAuthorizationDecision[] = [];
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  register(
    input: Omit<ControlAuthorizationPolicy, "policyId" | "revision" | "digest">,
  ): ControlAuthorizationPolicy {
    if (
      [...this.policies.values()].some(
        (value) => value.name === input.name && value.state !== "revoked",
      )
    )
      throw new E03RuntimeError(
        "control_authorization_policy_duplicate",
        `control authorization policy ${input.name} already exists`,
      );
    const payload = {
      ...structuredClone(input),
      policyId: createId("control-authorization-policy"),
      commandPatterns: [...new Set(input.commandPatterns)].sort(),
      principals: [...new Set(input.principals)].sort(),
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertAuthorizationPolicy(policy);
    this.policies.set(policy.policyId, policy);
    return structuredClone(policy);
  }
  update(
    policyId: string,
    expectedRevision: number,
    patch: Partial<
      Pick<
        ControlAuthorizationPolicy,
        | "commandPatterns"
        | "principals"
        | "requiredClaims"
        | "effect"
        | "priority"
        | "validFrom"
        | "validUntil"
        | "state"
      >
    >,
  ): ControlAuthorizationPolicy {
    const policy = this.requirePolicy(policyId);
    this.assertPolicyRevision(policy, expectedRevision);
    if (policy.state === "revoked")
      throw new E03RuntimeError(
        "control_authorization_policy_update_state",
        `control authorization policy ${policyId} is revoked`,
      );
    const { digest: _, ...prior } = policy;
    const payload = {
      ...prior,
      ...structuredClone(patch),
      policyId: policy.policyId,
      commandPatterns: patch.commandPatterns
        ? [...new Set(patch.commandPatterns)].sort()
        : policy.commandPatterns,
      principals: patch.principals
        ? [...new Set(patch.principals)].sort()
        : policy.principals,
      revision: policy.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertAuthorizationPolicy(next);
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }
  revoke(
    policyId: string,
    expectedRevision: number,
  ): ControlAuthorizationPolicy {
    const policy = this.requirePolicy(policyId);
    this.assertPolicyRevision(policy, expectedRevision);
    if (policy.state === "revoked") return structuredClone(policy);
    return this.update(policyId, expectedRevision, { state: "revoked" });
  }
  authorize(input: {
    requestId: string;
    principalId: string;
    command: ControlCommand;
    claims?: Record<string, readonly string[]>;
    now?: string;
  }): ControlAuthorizationDecision {
    if (!input.requestId.trim() || !input.principalId.trim())
      throw new E03RuntimeError(
        "control_authorization_request",
        "control authorization request is invalid",
      );
    const duplicate = this.decisions.find(
      (value) => value.requestId === input.requestId,
    );
    if (duplicate) {
      if (
        duplicate.principalId !== input.principalId ||
        duplicate.command !== input.command
      )
        throw new E03RuntimeError(
          "control_authorization_request_conflict",
          `control authorization request ${input.requestId} was reused`,
        );
      return structuredClone(duplicate);
    }
    const now = input.now ?? this.clock.now();
    const timestamp = Date.parse(now);
    if (Number.isNaN(timestamp))
      throw new E03RuntimeError(
        "control_authorization_time",
        "control authorization time is invalid",
      );
    const matches = [...this.policies.values()]
      .filter(
        (policy) =>
          policy.state === "active" &&
          (policy.principals.includes(input.principalId) ||
            policy.principals.includes("*")) &&
          policy.commandPatterns.some((pattern) =>
            this.matches(input.command, pattern),
          ) &&
          Date.parse(policy.validFrom) <= timestamp &&
          (policy.validUntil === null ||
            Date.parse(policy.validUntil) > timestamp),
      )
      .sort(
        (left, right) =>
          right.priority - left.priority ||
          left.policyId.localeCompare(right.policyId),
      );
    const missingClaims: string[] = [];
    for (const policy of matches)
      for (const [claim, accepted] of Object.entries(policy.requiredClaims)) {
        const supplied = input.claims?.[claim] ?? [];
        if (!supplied.some((value) => accepted.includes(value)))
          missingClaims.push(`${policy.policyId}:${claim}`);
      }
    const highestPriority = matches[0]?.priority;
    const winning =
      highestPriority === undefined
        ? []
        : matches.filter((value) => value.priority === highestPriority);
    const denied =
      !winning.length ||
      winning.some((value) => value.effect === "deny") ||
      missingClaims.length > 0;
    const reason = !matches.length
      ? "no matching authorization policy"
      : missingClaims.length
        ? "required claims are missing"
        : denied
          ? "authorization policy denied command"
          : "authorization policy allowed command";
    const payload = {
      decisionId: createId("control-authorization-decision"),
      requestId: input.requestId.trim(),
      principalId: input.principalId.trim(),
      command: input.command,
      outcome: denied ? ("denied" as const) : ("allowed" as const),
      matchedPolicyIds: matches.map((value) => value.policyId),
      missingClaims: [...new Set(missingClaims)].sort(),
      reason,
      decidedAt: now,
      sequence: this.decisions.length + 1,
      previousDigest:
        this.decisions[this.decisions.length - 1]?.digest ?? "root",
    };
    const decision = { ...payload, digest: digest(payload) };
    assertAuthorizationDecision(decision);
    this.decisions.push(decision);
    return structuredClone(decision);
  }
  verify(): void {
    let previousDigest = "root";
    let sequence = 1;
    const requestIds = new Set<string>();
    for (const decision of this.decisions) {
      assertAuthorizationDecision(decision);
      if (
        decision.sequence !== sequence ||
        decision.previousDigest !== previousDigest ||
        requestIds.has(decision.requestId)
      )
        throw new E03RuntimeError(
          "control_authorization_decision_chain",
          `control authorization decision ${decision.decisionId} breaks chain`,
        );
      requestIds.add(decision.requestId);
      previousDigest = decision.digest;
      sequence += 1;
    }
  }
  snapshot(): {
    policies: ControlAuthorizationPolicy[];
    decisions: ControlAuthorizationDecision[];
  } {
    this.verify();
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      decisions: this.decisions.map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    policies: readonly ControlAuthorizationPolicy[];
    decisions: readonly ControlAuthorizationDecision[];
  }): void {
    const policies = new Map<string, ControlAuthorizationPolicy>();
    for (const value of snapshot.policies) {
      assertAuthorizationPolicy(value);
      if (policies.has(value.policyId))
        throw new E03RuntimeError(
          "control_authorization_policy_restore_duplicate",
          `duplicate control authorization policy ${value.policyId}`,
        );
      policies.set(value.policyId, structuredClone(value));
    }
    this.policies = policies;
    this.decisions = snapshot.decisions.map((value) => structuredClone(value));
    this.verify();
    for (const decision of this.decisions)
      if (decision.matchedPolicyIds.some((id) => !policies.has(id)))
        throw new E03RuntimeError(
          "control_authorization_decision_policy",
          `control authorization decision ${decision.decisionId} references missing policy`,
        );
  }
  private matches(command: string, pattern: string): boolean {
    if (pattern === "*") return true;
    if (pattern.endsWith(".*")) return command.startsWith(pattern.slice(0, -1));
    return command === pattern;
  }
  private requirePolicy(id: string): ControlAuthorizationPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_authorization_policy_missing",
        `control authorization policy ${id} does not exist`,
      );
    assertAuthorizationPolicy(value);
    return value;
  }
  private assertPolicyRevision(
    value: ControlAuthorizationPolicy,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_authorization_policy_stale_revision",
        `control authorization policy ${value.policyId} revision is stale`,
      );
  }
}

export interface ControlSchemaVersion {
  schemaId: string;
  version: string;
  state: "draft" | "active" | "deprecated" | "retired";
  commandSetDigest: string;
  minimumPeerVersion: string;
  maximumPeerVersion: string;
  activatedAt: string | null;
  deprecatedAt: string | null;
  retiredAt: string | null;
  revision: number;
  digest: string;
}
export interface ControlSchemaMigration {
  migrationId: string;
  fromSchemaId: string;
  toSchemaId: string;
  state: "registered" | "validated" | "active" | "disabled";
  reversible: boolean;
  transformedFields: string[];
  removedFields: string[];
  defaultedFields: string[];
  validationDigest: string | null;
  registeredAt: string;
  validatedAt: string | null;
  revision: number;
  digest: string;
}
function assertSchemaVersion(value: ControlSchemaVersion): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_schema_version_digest",
      `control schema version ${value.schemaId} is corrupt`,
    );
  if (
    !value.schemaId ||
    !/^\d+\.\d+$/.test(value.version) ||
    !value.commandSetDigest ||
    !/^\d+\.\d+$/.test(value.minimumPeerVersion) ||
    !/^\d+\.\d+$/.test(value.maximumPeerVersion) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_schema_version",
      `control schema version ${value.schemaId} is invalid`,
    );
}
function assertSchemaMigration(value: ControlSchemaMigration): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "control_schema_migration_digest",
      `control schema migration ${value.migrationId} is corrupt`,
    );
  if (
    !value.migrationId ||
    !value.fromSchemaId ||
    !value.toSchemaId ||
    value.fromSchemaId === value.toSchemaId ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1
  )
    throw new E03RuntimeError(
      "control_schema_migration",
      `control schema migration ${value.migrationId} is invalid`,
    );
}
export interface ControlErrorContract {
  contractId: string;
  errorCode: string;
  commandPatterns: string[];
  classification:
    | "validation"
    | "authorization"
    | "conflict"
    | "capacity"
    | "dependency"
    | "internal";
  retryable: boolean;
  maximumRetries: number;
  retryAfterMs: number;
  remediationActions: string[];
  safeToExpose: boolean;
  version: number;
  state: "draft" | "active" | "deprecated" | "retired";
  createdAt: string;
  activatedAt: string;
  revision: number;
  digest: string;
}

export interface ControlErrorOccurrence {
  occurrenceId: string;
  contractId: string;
  requestId: string;
  sessionId: string;
  command: string;
  errorCode: string;
  attempt: number;
  detailsDigest: string;
  state: "recorded" | "remediating" | "recovered" | "terminal";
  selectedAction: string;
  recordedAt: string;
  updatedAt: string;
  terminalAt: string;
  revision: number;
  digest: string;
}

export interface ControlRemediationReceipt {
  receiptId: string;
  occurrenceId: string;
  action: string;
  executorId: string;
  idempotencyKey: string;
  accepted: boolean;
  effectDigest: string;
  responseDigest: string;
  errorCode: string;
  executedAt: string;
  previousDigest: string;
  digest: string;
}

export interface ControlErrorCatalogSnapshot {
  contracts: ControlErrorContract[];
  occurrences: ControlErrorOccurrence[];
  receipts: ControlRemediationReceipt[];
  activeContractByErrorVersion: [string, string][];
  occurrenceByRequestAttempt: [string, string][];
  receiptByIdempotencyKey: [string, string][];
}

function assertControlErrorContract(value: ControlErrorContract): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.contractId ||
    !value.errorCode ||
    !value.commandPatterns.length ||
    new Set(value.commandPatterns).size !== value.commandPatterns.length ||
    value.maximumRetries < 0 ||
    value.retryAfterMs < 0 ||
    value.version < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_error_contract_corrupt",
      `control error contract ${value.contractId || "<empty>"} is corrupt`,
    );
}

function assertControlErrorOccurrence(value: ControlErrorOccurrence): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.occurrenceId ||
    !value.contractId ||
    !value.requestId ||
    !value.sessionId ||
    !value.command ||
    !value.errorCode ||
    value.attempt < 1 ||
    !value.detailsDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_error_occurrence_corrupt",
      `control error occurrence ${value.occurrenceId || "<empty>"} is corrupt`,
    );
}

function assertControlRemediationReceipt(
  value: ControlRemediationReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.occurrenceId ||
    !value.action ||
    !value.executorId ||
    !value.idempotencyKey ||
    !value.effectDigest ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "control_remediation_receipt_corrupt",
      `control remediation receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

export class ControlErrorCatalogRuntime {
  private contracts = new Map<string, ControlErrorContract>();
  private occurrences = new Map<string, ControlErrorOccurrence>();
  private receipts = new Map<string, ControlRemediationReceipt[]>();
  private activeContractByErrorVersion = new Map<string, string>();
  private occurrenceByRequestAttempt = new Map<string, string>();
  private receiptByIdempotencyKey = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  register(input: {
    contractId?: string;
    errorCode: string;
    commandPatterns: readonly string[];
    classification: ControlErrorContract["classification"];
    retryable: boolean;
    maximumRetries: number;
    retryAfterMs: number;
    remediationActions: readonly string[];
    safeToExpose: boolean;
    version: number;
  }): ControlErrorContract {
    const index = this.contractKey(input.errorCode, input.version);
    if (this.activeContractByErrorVersion.has(index))
      throw new E03RuntimeError(
        "control_error_contract_version_duplicate",
        `control error ${input.errorCode} version ${input.version} exists`,
      );
    if (
      !input.retryable &&
      (input.maximumRetries > 0 || input.retryAfterMs > 0)
    )
      throw new E03RuntimeError(
        "control_error_contract_retry_conflict",
        "non-retryable control error cannot declare retry policy",
      );
    const contractId = input.contractId ?? createId("control-error-contract");
    const payload = {
      contractId,
      errorCode: input.errorCode,
      commandPatterns: [...new Set(input.commandPatterns)].sort(),
      classification: input.classification,
      retryable: input.retryable,
      maximumRetries: input.maximumRetries,
      retryAfterMs: input.retryAfterMs,
      remediationActions: [...new Set(input.remediationActions)].sort(),
      safeToExpose: input.safeToExpose,
      version: input.version,
      state: "draft" as const,
      createdAt: this.clock.now(),
      activatedAt: "",
      revision: 1,
    };
    const contract = { ...payload, digest: digest(payload) };
    assertControlErrorContract(contract);
    this.contracts.set(contractId, contract);
    return structuredClone(contract);
  }

  activate(contractId: string, expectedRevision: number): ControlErrorContract {
    const contract = this.requireContract(contractId);
    this.assertContractRevision(contract, expectedRevision);
    if (contract.state !== "draft")
      throw new E03RuntimeError(
        "control_error_contract_activate_state",
        `control error contract ${contractId} is ${contract.state}`,
      );
    const key = this.contractKey(contract.errorCode, contract.version);
    if (this.activeContractByErrorVersion.has(key))
      throw new E03RuntimeError(
        "control_error_contract_active_duplicate",
        `control error contract ${key} already active`,
      );
    const next = this.transitionContract(contract, {
      state: "active",
      activatedAt: this.clock.now(),
    });
    this.activeContractByErrorVersion.set(key, contractId);
    return next;
  }

  resolve(errorCode: string, command: string): ControlErrorContract | null {
    const candidates = [...this.contracts.values()]
      .filter(
        (value) =>
          value.errorCode === errorCode &&
          value.state === "active" &&
          value.commandPatterns.some((pattern) =>
            this.matches(pattern, command),
          ),
      )
      .sort((a, b) => b.version - a.version);
    return candidates[0] ? structuredClone(candidates[0]) : null;
  }

  record(input: {
    occurrenceId?: string;
    requestId: string;
    sessionId: string;
    command: string;
    errorCode: string;
    attempt: number;
    detailsDigest: string;
  }): ControlErrorOccurrence {
    const index = this.occurrenceKey(input.requestId, input.attempt);
    const duplicateId = this.occurrenceByRequestAttempt.get(index);
    if (duplicateId)
      return structuredClone(this.requireOccurrence(duplicateId));
    const contract = this.resolve(input.errorCode, input.command);
    if (!contract)
      throw new E03RuntimeError(
        "control_error_contract_missing",
        `control error ${input.errorCode} has no contract`,
      );
    const occurrenceId =
      input.occurrenceId ?? createId("control-error-occurrence");
    const now = this.clock.now();
    const payload = {
      occurrenceId,
      contractId: contract.contractId,
      requestId: input.requestId,
      sessionId: input.sessionId,
      command: input.command,
      errorCode: input.errorCode,
      attempt: input.attempt,
      detailsDigest: input.detailsDigest,
      state: "recorded" as const,
      selectedAction: "",
      recordedAt: now,
      updatedAt: now,
      terminalAt: "",
      revision: 1,
    };
    const occurrence = { ...payload, digest: digest(payload) };
    assertControlErrorOccurrence(occurrence);
    this.occurrences.set(occurrenceId, occurrence);
    this.receipts.set(occurrenceId, []);
    this.occurrenceByRequestAttempt.set(index, occurrenceId);
    return structuredClone(occurrence);
  }

  chooseRemediation(
    occurrenceId: string,
    expectedRevision: number,
    action: string,
  ): ControlErrorOccurrence {
    const occurrence = this.requireOccurrence(occurrenceId);
    this.assertOccurrenceRevision(occurrence, expectedRevision);
    if (occurrence.state !== "recorded")
      throw new E03RuntimeError(
        "control_error_remediation_state",
        `control error occurrence ${occurrenceId} is ${occurrence.state}`,
      );
    const contract = this.requireContract(occurrence.contractId);
    if (!contract.remediationActions.includes(action))
      throw new E03RuntimeError(
        "control_error_remediation_action_denied",
        `control error remediation ${action} is denied`,
      );
    if (occurrence.attempt > contract.maximumRetries && action === "retry")
      throw new E03RuntimeError(
        "control_error_retry_exhausted",
        `control error occurrence ${occurrenceId} exhausted retries`,
      );
    return this.transitionOccurrence(occurrence, {
      state: "remediating",
      selectedAction: action,
    });
  }

  recordReceipt(input: {
    receiptId?: string;
    occurrenceId: string;
    expectedRevision: number;
    action: string;
    executorId: string;
    idempotencyKey: string;
    accepted: boolean;
    effectDigest: string;
    responseDigest: string;
    errorCode?: string;
  }): ControlRemediationReceipt {
    const duplicateId = this.receiptByIdempotencyKey.get(input.idempotencyKey);
    if (duplicateId) return structuredClone(this.requireReceipt(duplicateId));
    const occurrence = this.requireOccurrence(input.occurrenceId);
    this.assertOccurrenceRevision(occurrence, input.expectedRevision);
    if (
      occurrence.state !== "remediating" ||
      occurrence.selectedAction !== input.action
    )
      throw new E03RuntimeError(
        "control_remediation_receipt_state",
        `control error occurrence ${occurrence.occurrenceId} remediation mismatch`,
      );
    const entries = this.receiptEntries(occurrence.occurrenceId);
    const payload = {
      receiptId: input.receiptId ?? createId("control-remediation-receipt"),
      occurrenceId: occurrence.occurrenceId,
      action: input.action,
      executorId: input.executorId,
      idempotencyKey: input.idempotencyKey,
      accepted: input.accepted,
      effectDigest: input.effectDigest,
      responseDigest: input.responseDigest,
      errorCode: input.errorCode ?? "",
      executedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertControlRemediationReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(occurrence.occurrenceId, entries);
    this.receiptByIdempotencyKey.set(input.idempotencyKey, receipt.receiptId);
    this.transitionOccurrence(occurrence, {
      state: input.accepted ? "recovered" : "terminal",
      terminalAt: this.clock.now(),
    });
    return structuredClone(receipt);
  }

  deprecate(
    contractId: string,
    expectedRevision: number,
  ): ControlErrorContract {
    const contract = this.requireContract(contractId);
    this.assertContractRevision(contract, expectedRevision);
    if (contract.state !== "active")
      throw new E03RuntimeError(
        "control_error_contract_deprecate_state",
        `control error contract ${contractId} is ${contract.state}`,
      );
    const next = this.transitionContract(contract, { state: "deprecated" });
    this.activeContractByErrorVersion.delete(
      this.contractKey(contract.errorCode, contract.version),
    );
    return next;
  }

  snapshot(): ControlErrorCatalogSnapshot {
    return {
      contracts: [...this.contracts.values()].map((value) =>
        structuredClone(value),
      ),
      occurrences: [...this.occurrences.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeContractByErrorVersion: [
        ...this.activeContractByErrorVersion.entries(),
      ],
      occurrenceByRequestAttempt: [
        ...this.occurrenceByRequestAttempt.entries(),
      ],
      receiptByIdempotencyKey: [...this.receiptByIdempotencyKey.entries()],
    };
  }

  restore(snapshot: ControlErrorCatalogSnapshot): void {
    const contracts = new Map<string, ControlErrorContract>();
    const occurrences = new Map<string, ControlErrorOccurrence>();
    const receipts = new Map<string, ControlRemediationReceipt[]>();
    for (const value of snapshot.contracts) {
      assertControlErrorContract(value);
      if (contracts.has(value.contractId))
        throw new E03RuntimeError(
          "control_error_restore_contract_duplicate",
          `contract ${value.contractId} duplicate`,
        );
      contracts.set(value.contractId, structuredClone(value));
    }
    for (const value of snapshot.occurrences) {
      assertControlErrorOccurrence(value);
      if (
        !contracts.has(value.contractId) ||
        occurrences.has(value.occurrenceId)
      )
        throw new E03RuntimeError(
          "control_error_restore_occurrence",
          `occurrence ${value.occurrenceId} invalid`,
        );
      occurrences.set(value.occurrenceId, structuredClone(value));
      receipts.set(value.occurrenceId, []);
    }
    for (const value of snapshot.receipts) {
      assertControlRemediationReceipt(value);
      const entries = receipts.get(value.occurrenceId);
      if (!entries || value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "control_error_restore_receipt_chain",
          `receipt ${value.receiptId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const activeContractByErrorVersion = new Map(
      snapshot.activeContractByErrorVersion,
    );
    const occurrenceByRequestAttempt = new Map(
      snapshot.occurrenceByRequestAttempt,
    );
    const receiptByIdempotencyKey = new Map(snapshot.receiptByIdempotencyKey);
    if (
      activeContractByErrorVersion.size !==
        snapshot.activeContractByErrorVersion.length ||
      occurrenceByRequestAttempt.size !==
        snapshot.occurrenceByRequestAttempt.length ||
      receiptByIdempotencyKey.size !== snapshot.receiptByIdempotencyKey.length
    )
      throw new E03RuntimeError(
        "control_error_restore_index_duplicate",
        "control error indexes duplicate",
      );
    for (const [key, contractId] of activeContractByErrorVersion) {
      const value = contracts.get(contractId);
      if (
        !value ||
        key !== this.contractKey(value.errorCode, value.version) ||
        value.state !== "active"
      )
        throw new E03RuntimeError(
          "control_error_restore_contract_index",
          `contract index ${key} invalid`,
        );
    }
    for (const [key, occurrenceId] of occurrenceByRequestAttempt) {
      const value = occurrences.get(occurrenceId);
      if (!value || key !== this.occurrenceKey(value.requestId, value.attempt))
        throw new E03RuntimeError(
          "control_error_restore_occurrence_index",
          `occurrence index ${key} invalid`,
        );
    }
    for (const [key, receiptId] of receiptByIdempotencyKey) {
      const value = [...receipts.values()]
        .flat()
        .find((entry) => entry.receiptId === receiptId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "control_error_restore_receipt_index",
          `receipt index ${key} invalid`,
        );
    }
    this.contracts = contracts;
    this.occurrences = occurrences;
    this.receipts = receipts;
    this.activeContractByErrorVersion = activeContractByErrorVersion;
    this.occurrenceByRequestAttempt = occurrenceByRequestAttempt;
    this.receiptByIdempotencyKey = receiptByIdempotencyKey;
  }

  private matches(pattern: string, command: string): boolean {
    return (
      pattern === "*" ||
      pattern === command ||
      (pattern.endsWith(".*") && command.startsWith(pattern.slice(0, -1)))
    );
  }

  private contractKey(errorCode: string, version: number): string {
    return `${errorCode}\u0000${version}`;
  }

  private occurrenceKey(requestId: string, attempt: number): string {
    return `${requestId}\u0000${attempt}`;
  }

  private receiptEntries(occurrenceId: string): ControlRemediationReceipt[] {
    return this.receipts.get(occurrenceId) ?? [];
  }

  private requireContract(id: string): ControlErrorContract {
    const value = this.contracts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_error_contract_missing",
        `contract ${id} missing`,
      );
    assertControlErrorContract(value);
    return value;
  }

  private requireOccurrence(id: string): ControlErrorOccurrence {
    const value = this.occurrences.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_error_occurrence_missing",
        `occurrence ${id} missing`,
      );
    assertControlErrorOccurrence(value);
    return value;
  }

  private requireReceipt(id: string): ControlRemediationReceipt {
    const value = [...this.receipts.values()]
      .flat()
      .find((entry) => entry.receiptId === id);
    if (!value)
      throw new E03RuntimeError(
        "control_remediation_receipt_missing",
        `receipt ${id} missing`,
      );
    assertControlRemediationReceipt(value);
    return value;
  }

  private assertContractRevision(
    value: ControlErrorContract,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_error_contract_stale_revision",
        `contract ${value.contractId} stale`,
      );
  }

  private assertOccurrenceRevision(
    value: ControlErrorOccurrence,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_error_occurrence_stale_revision",
        `occurrence ${value.occurrenceId} stale`,
      );
  }

  private transitionContract(
    value: ControlErrorContract,
    patch: Partial<
      Omit<ControlErrorContract, "contractId" | "revision" | "digest">
    >,
  ): ControlErrorContract {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      contractId: value.contractId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlErrorContract(next);
    this.contracts.set(next.contractId, next);
    return structuredClone(next);
  }

  private transitionOccurrence(
    value: ControlErrorOccurrence,
    patch: Partial<
      Omit<ControlErrorOccurrence, "occurrenceId" | "revision" | "digest">
    >,
  ): ControlErrorOccurrence {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      occurrenceId: value.occurrenceId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertControlErrorOccurrence(next);
    this.occurrences.set(next.occurrenceId, next);
    return structuredClone(next);
  }
}

export class ControlSchemaEvolutionRuntime {
  private versions = new Map<string, ControlSchemaVersion>();
  private migrations = new Map<string, ControlSchemaMigration>();
  private activeSchemaId: string | null = null;
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  registerVersion(
    input: Omit<
      ControlSchemaVersion,
      | "schemaId"
      | "state"
      | "activatedAt"
      | "deprecatedAt"
      | "retiredAt"
      | "revision"
      | "digest"
    >,
  ): ControlSchemaVersion {
    if (
      [...this.versions.values()].some(
        (value) => value.version === input.version && value.state !== "retired",
      )
    )
      throw new E03RuntimeError(
        "control_schema_version_duplicate",
        `control schema version ${input.version} already exists`,
      );
    const payload = {
      ...input,
      schemaId: createId("control-schema-version"),
      state: "draft" as const,
      activatedAt: null,
      deprecatedAt: null,
      retiredAt: null,
      revision: 1,
    };
    const version = { ...payload, digest: digest(payload) };
    assertSchemaVersion(version);
    this.versions.set(version.schemaId, version);
    return structuredClone(version);
  }
  registerMigration(
    input: Omit<
      ControlSchemaMigration,
      | "migrationId"
      | "state"
      | "validationDigest"
      | "registeredAt"
      | "validatedAt"
      | "revision"
      | "digest"
    >,
  ): ControlSchemaMigration {
    this.requireVersion(input.fromSchemaId);
    this.requireVersion(input.toSchemaId);
    if (
      [...this.migrations.values()].some(
        (value) =>
          value.fromSchemaId === input.fromSchemaId &&
          value.toSchemaId === input.toSchemaId &&
          value.state !== "disabled",
      )
    )
      throw new E03RuntimeError(
        "control_schema_migration_duplicate",
        "control schema migration already exists",
      );
    const payload = {
      ...structuredClone(input),
      migrationId: createId("control-schema-migration"),
      transformedFields: [...new Set(input.transformedFields)].sort(),
      removedFields: [...new Set(input.removedFields)].sort(),
      defaultedFields: [...new Set(input.defaultedFields)].sort(),
      state: "registered" as const,
      validationDigest: null,
      registeredAt: this.clock.now(),
      validatedAt: null,
      revision: 1,
    };
    const migration = { ...payload, digest: digest(payload) };
    assertSchemaMigration(migration);
    this.migrations.set(migration.migrationId, migration);
    return structuredClone(migration);
  }
  validateMigration(
    migrationId: string,
    expectedRevision: number,
    fixtures: readonly { inputDigest: string; outputDigest: string }[],
  ): ControlSchemaMigration {
    const migration = this.requireMigration(migrationId);
    this.assertMigrationRevision(migration, expectedRevision);
    if (migration.state !== "registered")
      throw new E03RuntimeError(
        "control_schema_migration_validate_state",
        `control schema migration ${migrationId} is ${migration.state}`,
      );
    if (
      !fixtures.length ||
      fixtures.some((value) => !value.inputDigest || !value.outputDigest)
    )
      throw new E03RuntimeError(
        "control_schema_migration_fixtures",
        "control schema migration fixtures are required",
      );
    return this.transitionMigration(migration, {
      state: "validated",
      validationDigest: digest(fixtures),
      validatedAt: this.clock.now(),
    });
  }
  activate(schemaId: string, expectedRevision: number): ControlSchemaVersion {
    const version = this.requireVersion(schemaId);
    this.assertVersionRevision(version, expectedRevision);
    if (version.state !== "draft" && version.state !== "deprecated")
      throw new E03RuntimeError(
        "control_schema_activate_state",
        `control schema ${schemaId} is ${version.state}`,
      );
    if (this.activeSchemaId) {
      const prior = this.requireVersion(this.activeSchemaId);
      const migration = [...this.migrations.values()].find(
        (value) =>
          value.fromSchemaId === prior.schemaId &&
          value.toSchemaId === version.schemaId &&
          value.state === "validated",
      );
      if (!migration)
        throw new E03RuntimeError(
          "control_schema_activate_migration",
          `control schema ${version.version} lacks validated migration`,
        );
      this.transitionMigration(migration, { state: "active" });
      this.transitionVersion(prior, {
        state: "deprecated",
        deprecatedAt: this.clock.now(),
      });
    }
    const next = this.transitionVersion(version, {
      state: "active",
      activatedAt: this.clock.now(),
      deprecatedAt: null,
    });
    this.activeSchemaId = next.schemaId;
    return next;
  }
  retire(schemaId: string, expectedRevision: number): ControlSchemaVersion {
    const version = this.requireVersion(schemaId);
    this.assertVersionRevision(version, expectedRevision);
    if (version.state !== "deprecated")
      throw new E03RuntimeError(
        "control_schema_retire_state",
        `control schema ${schemaId} is ${version.state}`,
      );
    if (this.activeSchemaId === schemaId)
      throw new E03RuntimeError(
        "control_schema_retire_active",
        `active control schema ${schemaId} cannot retire`,
      );
    return this.transitionVersion(version, {
      state: "retired",
      retiredAt: this.clock.now(),
    });
  }
  migrationPath(
    fromVersion: string,
    toVersion: string,
  ): ControlSchemaMigration[] {
    const from = [...this.versions.values()].find(
      (value) => value.version === fromVersion,
    );
    const to = [...this.versions.values()].find(
      (value) => value.version === toVersion,
    );
    if (!from || !to)
      throw new E03RuntimeError(
        "control_schema_path_version",
        "control schema migration path endpoint is missing",
      );
    const queue: Array<{ schemaId: string; path: ControlSchemaMigration[] }> = [
      { schemaId: from.schemaId, path: [] },
    ];
    const seen = new Set<string>();
    while (queue.length) {
      const cursor = queue.shift()!;
      if (cursor.schemaId === to.schemaId)
        return cursor.path.map((value) => structuredClone(value));
      if (seen.has(cursor.schemaId)) continue;
      seen.add(cursor.schemaId);
      for (const migration of this.migrations.values())
        if (
          migration.fromSchemaId === cursor.schemaId &&
          (migration.state === "validated" || migration.state === "active")
        )
          queue.push({
            schemaId: migration.toSchemaId,
            path: [...cursor.path, migration],
          });
    }
    throw new E03RuntimeError(
      "control_schema_path_missing",
      `no control schema migration path from ${fromVersion} to ${toVersion}`,
    );
  }
  snapshot(): {
    versions: ControlSchemaVersion[];
    migrations: ControlSchemaMigration[];
    activeSchemaId: string | null;
  } {
    return {
      versions: [...this.versions.values()].map((value) =>
        structuredClone(value),
      ),
      migrations: [...this.migrations.values()].map((value) =>
        structuredClone(value),
      ),
      activeSchemaId: this.activeSchemaId,
    };
  }
  restore(snapshot: {
    versions: readonly ControlSchemaVersion[];
    migrations: readonly ControlSchemaMigration[];
    activeSchemaId: string | null;
  }): void {
    const versions = new Map<string, ControlSchemaVersion>();
    const migrations = new Map<string, ControlSchemaMigration>();
    for (const value of snapshot.versions) {
      assertSchemaVersion(value);
      if (versions.has(value.schemaId))
        throw new E03RuntimeError(
          "control_schema_version_restore_duplicate",
          `duplicate control schema version ${value.schemaId}`,
        );
      versions.set(value.schemaId, structuredClone(value));
    }
    for (const value of snapshot.migrations) {
      assertSchemaMigration(value);
      if (
        migrations.has(value.migrationId) ||
        !versions.has(value.fromSchemaId) ||
        !versions.has(value.toSchemaId)
      )
        throw new E03RuntimeError(
          "control_schema_migration_restore",
          `control schema migration ${value.migrationId} is invalid`,
        );
      migrations.set(value.migrationId, structuredClone(value));
    }
    if (snapshot.activeSchemaId !== null) {
      const active = versions.get(snapshot.activeSchemaId);
      if (!active || active.state !== "active")
        throw new E03RuntimeError(
          "control_schema_active_restore",
          "active control schema index is invalid",
        );
    }
    this.versions = versions;
    this.migrations = migrations;
    this.activeSchemaId = snapshot.activeSchemaId;
  }
  private requireVersion(id: string): ControlSchemaVersion {
    const value = this.versions.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_schema_version_missing",
        `control schema version ${id} does not exist`,
      );
    assertSchemaVersion(value);
    return value;
  }
  private requireMigration(id: string): ControlSchemaMigration {
    const value = this.migrations.get(id);
    if (!value)
      throw new E03RuntimeError(
        "control_schema_migration_missing",
        `control schema migration ${id} does not exist`,
      );
    assertSchemaMigration(value);
    return value;
  }
  private assertVersionRevision(
    value: ControlSchemaVersion,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_schema_version_stale_revision",
        `control schema version ${value.schemaId} revision is stale`,
      );
  }
  private assertMigrationRevision(
    value: ControlSchemaMigration,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "control_schema_migration_stale_revision",
        `control schema migration ${value.migrationId} revision is stale`,
      );
  }
  private transitionVersion(
    value: ControlSchemaVersion,
    patch: Partial<
      Omit<ControlSchemaVersion, "schemaId" | "revision" | "digest">
    >,
  ): ControlSchemaVersion {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      schemaId: value.schemaId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertSchemaVersion(next);
    this.versions.set(next.schemaId, next);
    return structuredClone(next);
  }
  private transitionMigration(
    value: ControlSchemaMigration,
    patch: Partial<
      Omit<ControlSchemaMigration, "migrationId" | "revision" | "digest">
    >,
  ): ControlSchemaMigration {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      migrationId: value.migrationId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertSchemaMigration(next);
    this.migrations.set(next.migrationId, next);
    return structuredClone(next);
  }
}
