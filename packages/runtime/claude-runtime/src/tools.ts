import {
  asObject,
  asString,
  runtimeId,
  type JsonObject,
  type JsonValue,
  type ToolBatch,
  type ToolSpecContract,
  type ToolStep,
} from "./contracts.ts";

export interface ToolSchemaFailure {
  path: string;
  code: string;
  message: string;
}

export class RuntimeToolRegistry {
  private readonly tools = new Map<string, ToolSpecContract>();

  constructor(specs: ToolSpecContract[]) {
    for (const candidate of specs) {
      const name = asString(candidate.name).trim();
      if (!name) {
        throw new Error("tool registry contains an empty tool name");
      }
      if (this.tools.has(name)) {
        throw new Error("duplicate tool registration: " + name);
      }
      this.tools.set(name, structuredClone(candidate));
    }
  }

  get(name: string): ToolSpecContract | undefined {
    const selected = this.tools.get(name);
    return selected ? structuredClone(selected) : undefined;
  }

  list(): ToolSpecContract[] {
    return [...this.tools.values()].map((item) => structuredClone(item));
  }

  validate(step: ToolStep): ToolSchemaFailure[] {
    const spec = this.tools.get(step.tool_name);
    if (!spec) {
      return [{
        path: "$.tool_name",
        code: "unknown_tool",
        message: "tool is not registered: " + step.tool_name,
      }];
    }
    return validateSchema(spec.input_schema, step.arguments, "$");
  }

  readOnly(name: string): boolean {
    const metadata = this.tools.get(name)?.metadata ?? {};
    return metadata.read_only === "true" && metadata.concurrency_safe === "true";
  }
}

export function normalizeTurns(rawTurns: JsonValue[]): ToolStep[][] {
  const turns: ToolStep[][] = [];
  for (const rawTurn of rawTurns) {
    const candidates = Array.isArray(rawTurn)
      ? rawTurn
      : Array.isArray(asObject(rawTurn).steps)
        ? asObject(rawTurn).steps as JsonValue[]
        : [];
    const steps: ToolStep[] = [];
    for (const candidate of candidates) {
      const item = asObject(candidate);
      const metadata = asObject(item.metadata);
      const toolName = asString(item.tool_name || item.toolName).trim();
      if (!toolName) {
        continue;
      }
      steps.push({
        tool_name: toolName,
        arguments: asObject(item.arguments),
        step_id: asString(
          item.step_id
          || item.stepId
          || item.tool_use_id
          || item.toolUseId
          || item.tool_call_id
          || item.toolCallId
          || item.call_id
          || item.callID
          || item.id
          || metadata.assistant_tool_use_id
          || metadata.tool_use_id
          || metadata.tool_call_id
          || metadata.call_id,
        ) || undefined,
        prompt: asString(item.prompt) || undefined,
        metadata,
      });
    }
    turns.push(steps);
  }
  return turns;
}

export function scheduleToolBatches(
  registry: RuntimeToolRegistry,
  steps: ToolStep[],
  turnIndex: number,
  maxReadOnlyConcurrency: number,
): ToolBatch[] {
  const batches: ToolBatch[] = [];
  let cursor = 0;
  while (cursor < steps.length) {
    const current = steps[cursor];
    if (!registry.readOnly(current.tool_name)) {
      batches.push({
        batchId: runtimeId("toolbatch"),
        turnIndex,
        executionMode: "serial_non_read_only",
        steps: [current],
      });
      cursor += 1;
      continue;
    }
    const selected: ToolStep[] = [];
    while (
      cursor < steps.length
      && registry.readOnly(steps[cursor].tool_name)
      && selected.length < maxReadOnlyConcurrency
    ) {
      selected.push(steps[cursor]);
      cursor += 1;
    }
    batches.push({
      batchId: runtimeId("toolbatch"),
      turnIndex,
      executionMode: "concurrent_read_only",
      steps: selected,
    });
  }
  return batches;
}

function validateSchema(schemaValue: JsonObject, value: JsonValue, path: string): ToolSchemaFailure[] {
  const schema = asObject(schemaValue);
  const expectedType = asString(schema.type);
  const failures: ToolSchemaFailure[] = [];
  if (expectedType && !matchesType(expectedType, value)) {
    failures.push({
      path,
      code: "type_mismatch",
      message: "expected " + expectedType,
    });
    return failures;
  }
  if (expectedType === "object") {
    const record = asObject(value);
    const required = Array.isArray(schema.required) ? schema.required : [];
    for (const keyValue of required) {
      const key = asString(keyValue);
      if (key && !(key in record)) {
        failures.push({
          path: path + "." + key,
          code: "required",
          message: "required property is missing",
        });
      }
    }
    const properties = asObject(schema.properties);
    for (const [key, childSchema] of Object.entries(properties)) {
      if (key in record) {
        failures.push(...validateSchema(asObject(childSchema), record[key], path + "." + key));
      }
    }
  }
  if (expectedType === "array" && Array.isArray(value) && schema.items) {
    value.forEach((item, index) => {
      failures.push(...validateSchema(asObject(schema.items), item, path + "[" + String(index) + "]"));
    });
  }
  return failures;
}

function matchesType(expected: string, value: JsonValue): boolean {
  if (expected === "object") {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  if (expected === "array") {
    return Array.isArray(value);
  }
  if (expected === "integer") {
    return typeof value === "number" && Number.isInteger(value);
  }
  if (expected === "number") {
    return typeof value === "number" && Number.isFinite(value);
  }
  if (expected === "string") {
    return typeof value === "string";
  }
  if (expected === "boolean") {
    return typeof value === "boolean";
  }
  if (expected === "null") {
    return value === null;
  }
  return true;
}
