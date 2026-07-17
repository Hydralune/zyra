import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export interface McpSchemaIssue {
  path: string;
  schemaPath: string;
  keyword: string;
  message: string;
  expected: JsonValue | null;
  actual: JsonValue | null;
}

export interface McpSchemaValidation {
  valid: boolean;
  value: JsonValue;
  issues: McpSchemaIssue[];
  schemaDigest: string;
  valueDigest: string;
  defaultsApplied: string[];
  propertiesRemoved: string[];
}

export interface McpSchemaRuntimeOptions {
  maximumDepth?: number;
  maximumIssues?: number;
  applyDefaults?: boolean;
  removeAdditional?: boolean;
  coerceScalars?: boolean;
}

interface ValidationState {
  issues: McpSchemaIssue[];
  defaultsApplied: string[];
  propertiesRemoved: string[];
  seen: Set<string>;
}

export class McpSchemaRuntime {
  private readonly maximumDepth: number;
  private readonly maximumIssues: number;
  private readonly applyDefaults: boolean;
  private readonly removeAdditional: boolean;
  private readonly coerceScalars: boolean;

  constructor(options: McpSchemaRuntimeOptions = {}) {
    this.maximumDepth = options.maximumDepth ?? 128;
    this.maximumIssues = options.maximumIssues ?? 1_000;
    this.applyDefaults = options.applyDefaults ?? false;
    this.removeAdditional = options.removeAdditional ?? false;
    this.coerceScalars = options.coerceScalars ?? false;
  }

  validate(schemaValue: JsonObject, value: JsonValue): McpSchemaValidation {
    const schema = cloneJson(schemaValue);
    const input = canonicalJson(value);
    const state: ValidationState = {
      issues: [],
      defaultsApplied: [],
      propertiesRemoved: [],
      seen: new Set(),
    };
    const output = this.walk(schema, input, "$", "$", 0, schema, state);
    return {
      valid: state.issues.length === 0,
      value: output,
      issues: state.issues,
      schemaDigest: sha256(schema),
      valueDigest: sha256(output),
      defaultsApplied: state.defaultsApplied,
      propertiesRemoved: state.propertiesRemoved,
    };
  }

  require(schema: JsonObject, value: JsonValue, label = "MCP value"): JsonValue {
    const result = this.validate(schema, value);
    if (result.valid) return result.value;
    throw new McpRuntimeError({
      failureId: deterministicMcpId("mcp-schema", {
        schema_digest: result.schemaDigest,
        value_digest: result.valueDigest,
        issues: result.issues,
      }),
      category: "protocol",
      code: "schema_validation_failed",
      message: `${label} failed schema validation: ${result.issues[0]?.message ?? "unknown issue"}`,
      retryable: false,
      disposition: "terminal",
      details: {
        issues: canonicalJson(result.issues),
        schema_digest: result.schemaDigest,
        value_digest: result.valueDigest,
      },
    });
  }

  private walk(
    schema: JsonObject,
    value: JsonValue,
    path: string,
    schemaPath: string,
    depth: number,
    root: JsonObject,
    state: ValidationState,
  ): JsonValue {
    if (depth > this.maximumDepth) {
      this.issue(state, path, schemaPath, "depth", `schema validation exceeds depth ${this.maximumDepth}`, this.maximumDepth, depth);
      return value;
    }
    if (typeof schema.$ref === "string") {
      const resolved = resolveReference(root, schema.$ref);
      if (!resolved) {
        this.issue(state, path, `${schemaPath}/$ref`, "$ref", `unresolved schema reference ${schema.$ref}`, schema.$ref, null);
        return value;
      }
      const key = `${schema.$ref}\0${path}`;
      if (state.seen.has(key)) return value;
      state.seen.add(key);
      const output = this.walk(resolved, value, path, `${schemaPath}/$ref`, depth + 1, root, state);
      state.seen.delete(key);
      return output;
    }
    if (Array.isArray(schema.allOf)) {
      let output = value;
      for (const [index, child] of schema.allOf.entries()) {
        if (isObject(child)) output = this.walk(child, output, path, `${schemaPath}/allOf/${index}`, depth + 1, root, state);
      }
      value = output;
    }
    if (Array.isArray(schema.anyOf)) {
      const matches = this.branchMatches(schema.anyOf, value, path, `${schemaPath}/anyOf`, depth, root);
      if (!matches.length) this.issue(state, path, `${schemaPath}/anyOf`, "anyOf", "value matches none of anyOf schemas", null, value);
      else value = matches[0].value;
    }
    if (Array.isArray(schema.oneOf)) {
      const matches = this.branchMatches(schema.oneOf, value, path, `${schemaPath}/oneOf`, depth, root);
      if (matches.length !== 1) this.issue(state, path, `${schemaPath}/oneOf`, "oneOf", `value matches ${matches.length} oneOf schemas`, 1, matches.length);
      else value = matches[0].value;
    }
    if (isObject(schema.not)) {
      const branch = this.branch(schema.not, value, path, `${schemaPath}/not`, depth, root);
      if (branch.valid) this.issue(state, path, `${schemaPath}/not`, "not", "value matches forbidden schema", null, value);
    }
    if (isObject(schema.if)) {
      const condition = this.branch(schema.if, value, path, `${schemaPath}/if`, depth, root);
      if (condition.valid && isObject(schema.then)) value = this.walk(schema.then, value, path, `${schemaPath}/then`, depth + 1, root, state);
      if (!condition.valid && isObject(schema.else)) value = this.walk(schema.else, value, path, `${schemaPath}/else`, depth + 1, root, state);
    }
    const types = normalizeTypes(schema.type);
    if (types.length && !types.some((type) => typeMatches(type, value))) {
      const coerced = this.coerceScalars ? coerceValue(types, value) : null;
      if (coerced?.matched) value = coerced.value;
      else {
        this.issue(state, path, `${schemaPath}/type`, "type", `expected ${types.join(" or ")}, received ${typeName(value)}`, canonicalJson(types), typeName(value));
        return value;
      }
    }
    if (schema.const !== undefined && sha256(schema.const) !== sha256(value)) {
      this.issue(state, path, `${schemaPath}/const`, "const", "value differs from const", canonicalJson(schema.const), value);
    }
    if (Array.isArray(schema.enum) && !schema.enum.some((candidate) => sha256(candidate) === sha256(value))) {
      this.issue(state, path, `${schemaPath}/enum`, "enum", "value is not in enum", canonicalJson(schema.enum), value);
    }
    if (typeof value === "string") return this.validateString(schema, value, path, schemaPath, state);
    if (typeof value === "number") return this.validateNumber(schema, value, path, schemaPath, state);
    if (Array.isArray(value)) return this.validateArray(schema, value, path, schemaPath, depth, root, state);
    if (isObject(value)) return this.validateObject(schema, value, path, schemaPath, depth, root, state);
    return value;
  }

  private validateString(schema: JsonObject, value: string, path: string, schemaPath: string, state: ValidationState): string {
    if (typeof schema.minLength === "number" && [...value].length < schema.minLength) this.issue(state, path, `${schemaPath}/minLength`, "minLength", `string length is below ${schema.minLength}`, schema.minLength, [...value].length);
    if (typeof schema.maxLength === "number" && [...value].length > schema.maxLength) this.issue(state, path, `${schemaPath}/maxLength`, "maxLength", `string length exceeds ${schema.maxLength}`, schema.maxLength, [...value].length);
    if (typeof schema.pattern === "string") {
      let pattern: RegExp;
      try { pattern = new RegExp(schema.pattern, "u"); } catch { this.issue(state, path, `${schemaPath}/pattern`, "pattern", "schema pattern is invalid", schema.pattern, null); return value; }
      if (!pattern.test(value)) this.issue(state, path, `${schemaPath}/pattern`, "pattern", `string does not match ${schema.pattern}`, schema.pattern, value);
    }
    if (typeof schema.format === "string" && !formatMatches(schema.format, value)) this.issue(state, path, `${schemaPath}/format`, "format", `string is not valid ${schema.format}`, schema.format, value);
    return value;
  }

  private validateNumber(schema: JsonObject, value: number, path: string, schemaPath: string, state: ValidationState): number {
    if (typeof schema.minimum === "number" && value < schema.minimum) this.issue(state, path, `${schemaPath}/minimum`, "minimum", `number is below ${schema.minimum}`, schema.minimum, value);
    if (typeof schema.maximum === "number" && value > schema.maximum) this.issue(state, path, `${schemaPath}/maximum`, "maximum", `number exceeds ${schema.maximum}`, schema.maximum, value);
    if (typeof schema.exclusiveMinimum === "number" && value <= schema.exclusiveMinimum) this.issue(state, path, `${schemaPath}/exclusiveMinimum`, "exclusiveMinimum", `number must exceed ${schema.exclusiveMinimum}`, schema.exclusiveMinimum, value);
    if (typeof schema.exclusiveMaximum === "number" && value >= schema.exclusiveMaximum) this.issue(state, path, `${schemaPath}/exclusiveMaximum`, "exclusiveMaximum", `number must be below ${schema.exclusiveMaximum}`, schema.exclusiveMaximum, value);
    if (typeof schema.multipleOf === "number" && schema.multipleOf > 0) {
      const quotient = value / schema.multipleOf;
      if (Math.abs(quotient - Math.round(quotient)) > Number.EPSILON * 10) this.issue(state, path, `${schemaPath}/multipleOf`, "multipleOf", `number is not a multiple of ${schema.multipleOf}`, schema.multipleOf, value);
    }
    return value;
  }

  private validateArray(
    schema: JsonObject,
    value: JsonValue[],
    path: string,
    schemaPath: string,
    depth: number,
    root: JsonObject,
    state: ValidationState,
  ): JsonValue[] {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) this.issue(state, path, `${schemaPath}/minItems`, "minItems", `array has fewer than ${schema.minItems} items`, schema.minItems, value.length);
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) this.issue(state, path, `${schemaPath}/maxItems`, "maxItems", `array has more than ${schema.maxItems} items`, schema.maxItems, value.length);
    if (schema.uniqueItems === true) {
      const seen = new Set<string>();
      for (const item of value) {
        const key = sha256(item);
        if (seen.has(key)) { this.issue(state, path, `${schemaPath}/uniqueItems`, "uniqueItems", "array contains duplicate items", true, value); break; }
        seen.add(key);
      }
    }
    const output = [...value];
    if (Array.isArray(schema.prefixItems)) {
      for (let index = 0; index < Math.min(output.length, schema.prefixItems.length); index += 1) {
        const child = schema.prefixItems[index];
        if (isObject(child)) output[index] = this.walk(child, output[index], `${path}[${index}]`, `${schemaPath}/prefixItems/${index}`, depth + 1, root, state);
      }
    }
    if (isObject(schema.items)) {
      const start = Array.isArray(schema.prefixItems) ? schema.prefixItems.length : 0;
      for (let index = start; index < output.length; index += 1) output[index] = this.walk(schema.items, output[index], `${path}[${index}]`, `${schemaPath}/items`, depth + 1, root, state);
    } else if (schema.items === false && output.length > (Array.isArray(schema.prefixItems) ? schema.prefixItems.length : 0)) {
      this.issue(state, path, `${schemaPath}/items`, "items", "additional array items are forbidden", false, output.length);
    }
    if (isObject(schema.contains)) {
      let count = 0;
      for (let index = 0; index < output.length; index += 1) if (this.branch(schema.contains, output[index], `${path}[${index}]`, `${schemaPath}/contains`, depth, root).valid) count += 1;
      const minimum = typeof schema.minContains === "number" ? schema.minContains : 1;
      const maximum = typeof schema.maxContains === "number" ? schema.maxContains : Number.MAX_SAFE_INTEGER;
      if (count < minimum || count > maximum) this.issue(state, path, `${schemaPath}/contains`, "contains", `array contains ${count} matching items, expected [${minimum}, ${maximum}]`, canonicalJson([minimum, maximum]), count);
    }
    return output;
  }

  private validateObject(
    schema: JsonObject,
    value: JsonObject,
    path: string,
    schemaPath: string,
    depth: number,
    root: JsonObject,
    state: ValidationState,
  ): JsonObject {
    const output = cloneJson(value);
    const properties = isObject(schema.properties) ? schema.properties : {};
    const required = Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === "string") : [];
    for (const name of required) {
      if (!Object.hasOwn(output, name)) {
        const property = isObject(properties[name]) ? properties[name] : null;
        if (this.applyDefaults && property && property.default !== undefined) {
          output[name] = canonicalJson(property.default);
          state.defaultsApplied.push(`${path}.${name}`);
        } else {
          this.issue(state, `${path}.${name}`, `${schemaPath}/required`, "required", `required property ${name} is missing`, name, null);
        }
      }
    }
    const patternProperties = isObject(schema.patternProperties) ? schema.patternProperties : {};
    const matched = new Set<string>();
    for (const [name, childSchema] of Object.entries(properties)) {
      if (!isObject(childSchema)) continue;
      if (!Object.hasOwn(output, name) && this.applyDefaults && childSchema.default !== undefined) {
        output[name] = canonicalJson(childSchema.default);
        state.defaultsApplied.push(`${path}.${name}`);
      }
      if (Object.hasOwn(output, name)) {
        output[name] = this.walk(childSchema, output[name], `${path}.${name}`, `${schemaPath}/properties/${escapePointer(name)}`, depth + 1, root, state);
        matched.add(name);
      }
    }
    for (const [patternText, childSchema] of Object.entries(patternProperties)) {
      if (!isObject(childSchema)) continue;
      let pattern: RegExp;
      try { pattern = new RegExp(patternText, "u"); } catch { this.issue(state, path, `${schemaPath}/patternProperties/${escapePointer(patternText)}`, "patternProperties", `invalid property pattern ${patternText}`, patternText, null); continue; }
      for (const name of Object.keys(output)) {
        if (!pattern.test(name)) continue;
        output[name] = this.walk(childSchema, output[name], `${path}.${name}`, `${schemaPath}/patternProperties/${escapePointer(patternText)}`, depth + 1, root, state);
        matched.add(name);
      }
    }
    const additional = schema.additionalProperties;
    for (const name of Object.keys(output)) {
      if (matched.has(name)) continue;
      if (additional === false) {
        if (this.removeAdditional) {
          delete output[name];
          state.propertiesRemoved.push(`${path}.${name}`);
        } else {
          this.issue(state, `${path}.${name}`, `${schemaPath}/additionalProperties`, "additionalProperties", `additional property ${name} is forbidden`, false, output[name]);
        }
      } else if (isObject(additional)) {
        output[name] = this.walk(additional, output[name], `${path}.${name}`, `${schemaPath}/additionalProperties`, depth + 1, root, state);
      }
    }
    const propertyCount = Object.keys(output).length;
    if (typeof schema.minProperties === "number" && propertyCount < schema.minProperties) this.issue(state, path, `${schemaPath}/minProperties`, "minProperties", `object has fewer than ${schema.minProperties} properties`, schema.minProperties, propertyCount);
    if (typeof schema.maxProperties === "number" && propertyCount > schema.maxProperties) this.issue(state, path, `${schemaPath}/maxProperties`, "maxProperties", `object has more than ${schema.maxProperties} properties`, schema.maxProperties, propertyCount);
    if (isObject(schema.dependentRequired)) {
      for (const [name, dependencies] of Object.entries(schema.dependentRequired)) {
        if (!Object.hasOwn(output, name) || !Array.isArray(dependencies)) continue;
        for (const dependency of dependencies) if (typeof dependency === "string" && !Object.hasOwn(output, dependency)) this.issue(state, `${path}.${dependency}`, `${schemaPath}/dependentRequired/${escapePointer(name)}`, "dependentRequired", `${dependency} is required when ${name} exists`, dependency, null);
      }
    }
    return output;
  }

  private branchMatches(
    schemas: JsonValue[],
    value: JsonValue,
    path: string,
    schemaPath: string,
    depth: number,
    root: JsonObject,
  ): { value: JsonValue; valid: boolean }[] {
    return schemas
      .map((schema, index) => isObject(schema) ? this.branch(schema, value, path, `${schemaPath}/${index}`, depth, root) : { value, valid: false })
      .filter((branch) => branch.valid);
  }

  private branch(schema: JsonObject, value: JsonValue, path: string, schemaPath: string, depth: number, root: JsonObject) {
    const branchState: ValidationState = { issues: [], defaultsApplied: [], propertiesRemoved: [], seen: new Set() };
    const output = this.walk(schema, cloneJson(value), path, schemaPath, depth + 1, root, branchState);
    return { value: output, valid: branchState.issues.length === 0 };
  }

  private issue(
    state: ValidationState,
    path: string,
    schemaPath: string,
    keyword: string,
    message: string,
    expected: unknown,
    actual: unknown,
  ): void {
    if (state.issues.length >= this.maximumIssues) return;
    state.issues.push({
      path,
      schemaPath,
      keyword,
      message,
      expected: expected === undefined ? null : canonicalJson(expected),
      actual: actual === undefined ? null : canonicalJson(actual),
    });
  }
}

function normalizeTypes(value: JsonValue | undefined): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  return [];
}

function typeMatches(type: string, value: JsonValue): boolean {
  if (type === "null") return value === null;
  if (type === "array") return Array.isArray(value);
  if (type === "object") return isObject(value);
  if (type === "integer") return typeof value === "number" && Number.isSafeInteger(value);
  if (type === "number") return typeof value === "number";
  if (type === "string") return typeof value === "string";
  if (type === "boolean") return typeof value === "boolean";
  return true;
}

function coerceValue(types: string[], value: JsonValue): { matched: boolean; value: JsonValue } {
  if (typeof value === "string") {
    if (types.includes("boolean") && (value === "true" || value === "false")) return { matched: true, value: value === "true" };
    if (types.includes("integer") && /^-?\d+$/.test(value)) return { matched: true, value: Number(value) };
    if (types.includes("number") && /^-?\d+(?:\.\d+)?$/.test(value)) return { matched: true, value: Number(value) };
    if (types.includes("null") && value === "null") return { matched: true, value: null };
  }
  if (types.includes("string") && (typeof value === "number" || typeof value === "boolean")) return { matched: true, value: String(value) };
  return { matched: false, value };
}

function formatMatches(format: string, value: string): boolean {
  if (format === "date-time") return !Number.isNaN(Date.parse(value)) && /T/.test(value);
  if (format === "date") return /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
  if (format === "time") return /^\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  if (format === "email") return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value);
  if (format === "hostname") return value.length <= 253 && value.split(".").every((label) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label));
  if (format === "ipv4") return value.split(".").length === 4 && value.split(".").every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
  if (format === "ipv6") return value.includes(":") && /^[0-9A-Fa-f:]+$/.test(value);
  if (format === "uri" || format === "uri-reference") { try { format === "uri" ? new URL(value) : new URL(value, "https://example.invalid"); return true; } catch { return false; } }
  if (format === "uuid") return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
  return true;
}

function resolveReference(root: JsonObject, reference: string): JsonObject | null {
  if (!reference.startsWith("#/")) return null;
  let value: JsonValue = root;
  for (const part of reference.slice(2).split("/").map((item) => item.replace(/~1/g, "/").replace(/~0/g, "~"))) {
    if (!isObject(value) || value[part] === undefined) return null;
    value = value[part];
  }
  return isObject(value) ? value : null;
}

function typeName(value: JsonValue): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number" && Number.isSafeInteger(value)) return "integer";
  return typeof value;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function escapePointer(value: string): string {
  return value.replace(/~/g, "~0").replace(/\//g, "~1");
}
